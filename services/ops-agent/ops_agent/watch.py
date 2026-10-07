"""One watch cycle: decide whether something needs attention, analyze it once, tell the approver.

Designed to run from a timer every few minutes. Everything that costs money or makes noise is
gated here:

* a problem must last min_duration before anything happens (a restart during development is a
  blip, not an incident);
* services under an in-progress maintenance are ignored, and only those services;
* the same problem is analyzed once; later cycles only send a reminder (no model call);
* a manual silence suppresses everything until it expires;
* if the model is unavailable or the budget is spent, the approver still gets a plain message.

Nothing here publishes anything or touches a server.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .agent import AnalysisFailed, Outcome
from .budget import BudgetExceeded
from .notify import (
    NotifyError,
    format_analysis,
    format_fallback,
    format_recovered,
    format_reminder,
    single_line,
)

logger = logging.getLogger("ops_agent")
JST = timezone(timedelta(hours=9), "JST")
HEALTHY = {"operational"}
MAINTENANCE = "maintenance"


class Notifier(Protocol):
    def send(self, message: str, channel_id: Optional[str] = None) -> str: ...


class StdoutNotifier:
    """Used when Discord is not configured: the journal keeps what would have been sent."""

    def send(self, message: str, channel_id: Optional[str] = None) -> str:
        print(message)
        return channel_id or ""


class StateStore:
    """The episode state has ONE writer: the timer's watch cycle (a oneshot unit never overlaps
    itself). The silence lives in a separate file written only by the --silence command and only
    read by the watch cycle, so the two never overwrite each other and no lock is needed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.silence_path = path.with_name(path.name + ".silence")

    def load_silence(self) -> float:
        try:
            data = json.loads(self.silence_path.read_text(encoding="utf-8"))
            return float(data.get("until") or 0)
        except (OSError, ValueError, AttributeError):
            return 0.0

    def save_silence(self, until: float) -> None:
        self._write(self.silence_path, {"until": until})

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        self._write(self.path, state)

    @staticmethod
    def _write(target: Path, data: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".state-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, target)  # atomic: a crash never leaves a half-written file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


@dataclass(frozen=True)
class Concern:
    services: list[str]
    fingerprint: str
    # Something is unhealthy but covered by an in-progress maintenance: nothing to report, and
    # not a recovery either.
    suppressed: bool = False
    # Which services are covered (and still unhealthy), and the id behind each reported name, so
    # a later cycle can tell "this service recovered" from "this service went into maintenance".
    suppressed_ids: frozenset = frozenset()
    names_by_id: Any = None


def concerns(status: Any) -> Concern:
    """What needs attention: unhealthy services not covered by an in-progress maintenance, and
    unresolved incidents (the same rule as needs_analysis).

    The fingerprint identifies WHICH services/incidents are affected, not their status. A service
    that flips between degraded, outage and unknown is one continuing problem; keying on the
    status would restart the debounce at every flip and a flapping service would never be
    reported.
    """
    services = status.get("services") if isinstance(status, dict) else None
    if not isinstance(services, list):
        return Concern(["status"], "status:invalid")  # cannot tell: a person should look
    covered: set[str] = set()
    for item in status.get("maintenance") or []:
        if isinstance(item, dict) and str(item.get("state", "")).lower() == "in_progress":
            ids = item.get("affected_service_ids")
            if isinstance(ids, list):
                covered.update(str(i) for i in ids)
    names: list[str] = []
    parts: list[str] = []
    names_by_id: dict[str, str] = {}
    suppressed_ids: set[str] = set()
    for service in services:
        if not isinstance(service, dict) or service.get("status") in HEALTHY:
            continue
        if service.get("status") == MAINTENANCE or service.get("id") in covered:
            # Under maintenance, whether the service says so itself or an in-progress maintenance
            # record covers it. Not a concern, and not a recovery either, so it must be recorded
            # as suppressed rather than skipped like a healthy service.
            suppressed_ids.add(str(service.get("id")))
            continue
        name = single_line(service.get("name") or service.get("id") or "不明", 60)
        names.append(name)
        parts.append(str(service.get("id")))
        names_by_id[str(service.get("id"))] = name
    for incident in status.get("incidents") or []:
        if isinstance(incident, dict) and str(incident.get("status", "")).lower() != "resolved":
            incident_id = f"incident:{incident.get('public_id')}"
            affected = incident.get("affected_service_ids")
            if isinstance(affected, list) and affected and all(str(i) in covered for i in affected):
                # Everything this incident touches is under a planned maintenance: same rule as for
                # the services themselves, so it neither starts a debounce nor counts as recovered.
                suppressed_ids.add(incident_id)
                continue
            # The title is public, attacker-influenced text: one clean line, never raw.
            name = "Incident: " + single_line(incident.get("title") or incident.get("public_id") or "不明", 50)
            names.append(name)
            parts.append(incident_id)
            names_by_id[incident_id] = name
    return Concern(
        sorted(names), "|".join(sorted(parts)), bool(suppressed_ids), frozenset(suppressed_ids), names_by_id
    )


@dataclass(frozen=True)
class WatchResult:
    action: str
    detail: str = ""


def _since(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, JST).strftime("%m/%d %H:%M JST")


def silence(store: StateStore, minutes: int, now: float) -> float:
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("silence must be between 1 minute and 24 hours")
    until = now + minutes * 60
    store.save_silence(until)  # its own file: never read-modify-write the watch state
    return until


class Silenced(Exception):
    """A silence started while the cycle was running; nothing may be sent now."""


def _deliver(notifier: Notifier, store: StateStore, state: dict[str, Any], message: str, now: float) -> None:
    """Send, remembering the DM channel. The caller has already persisted `pending`.

    The silence is read again right here: --silence can be run at any moment, including while
    the history is being fetched or the model is thinking, long after the cycle first looked.
    """
    if store.load_silence() > now:
        raise Silenced()
    # The cached DM channel belongs to one recipient. If the configured approver changed, the old
    # channel must not be used: it would keep delivering to the previous person.
    recipient = getattr(notifier, "recipient_key", "")
    cached = state.get("dm_channel_id") if state.get("dm_recipient", "") == recipient else None
    channel = notifier.send(message, cached or None)
    if channel:
        state["dm_channel_id"] = channel
        state["dm_recipient"] = recipient


def flush_pending(notifier: Notifier, store: StateStore, now: float) -> Optional[WatchResult]:
    """Deliver a notice that failed to send earlier. Needs no status, so it can run before the
    status is even fetched: an unreachable status API must not strand a notice already paid for.

    Returns None when there is nothing to send (or a silence is active).
    """
    if store.load_silence() > now:
        return None
    state = store.load()
    if not state.get("pending"):
        return None
    try:
        _deliver(notifier, store, state, str(state["pending"]), now)
    except Silenced:
        return None  # keeps the notice; it goes out after the silence ends
    except NotifyError as exc:
        store.save(state)
        return WatchResult("notify_failed", str(exc))
    state.pop("pending", None)
    state["notified_at"] = now
    store.save(state)
    return WatchResult("resent")


def watch_once(
    *,
    snapshot: dict[str, Any],
    analyze_fn: Callable[[dict[str, Any]], Outcome],
    notifier: Notifier,
    store: StateStore,
    now: float,
    status_url: str,
    min_duration_seconds: int = 300,
    repeat_after_seconds: int = 3600,
) -> WatchResult:
    if store.load_silence() > now:
        return WatchResult("silenced")

    # A message that could not be delivered earlier goes out FIRST, whatever the health is now:
    # it was already paid for, and if the service has since recovered, dropping it would leave the
    # approver never knowing about an outage that was analyzed. The cycle then carries on and may
    # follow it with the recovery notice.
    flushed = flush_pending(notifier, store, now)
    if flushed is not None and flushed.action == "notify_failed":
        return flushed
    resent = flushed is not None
    state = store.load()

    current = concerns(snapshot.get("status", {}))
    keep = {k: state[k] for k in ("dm_channel_id", "dm_recipient") if k in state}

    if not current.services:
        if not state.get("fingerprint"):
            return WatchResult("healthy")
        episode_ids = [part for part in str(state["fingerprint"]).split("|") if part]
        moved_to_maintenance = [i for i in episode_ids if i in current.suppressed_ids]
        recovered_ids = [i for i in episode_ids if i not in current.suppressed_ids]
        notified = bool(state.get("notified_at"))
        stored_names = state.get("names_by_id") or {}
        names = [stored_names.get(i, i) for i in recovered_ids] or list(state.get("services") or [])

        if moved_to_maintenance and not recovered_ids:
            # Everything in the episode is now covered by a maintenance: not a recovery. Forget
            # the episode quietly; if the problem outlives the maintenance it is counted again.
            store.save(keep)
            return WatchResult("suppressed_by_maintenance", ", ".join(moved_to_maintenance))

        if notified:
            try:
                _deliver(notifier, store, keep, format_recovered(names, status_url), now)
            except Silenced:
                pass  # the operator asked for quiet; a recovery notice is not worth holding back
            except NotifyError as exc:  # best effort: a recovery notice is not worth a retry loop
                logger.warning("ops_agent_recovery_notice_failed %s", exc)
        store.save(keep)
        return WatchResult("recovered" if notified else "blip_ended", ", ".join(names))

    if current.fingerprint != state.get("fingerprint"):
        # A notified episode is being replaced. Whatever disappeared from it recovered (it did not
        # go into maintenance: that is not a recovery), and the approver is still holding its
        # alert, so announce that before the new episode starts.
        previous = [part for part in str(state.get("fingerprint") or "").split("|") if part]
        now_ids = {part for part in current.fingerprint.split("|") if part}
        gone = [i for i in previous if i not in now_ids and i not in current.suppressed_ids]
        if gone and state.get("notified_at"):
            stored = state.get("names_by_id") or {}
            try:
                _deliver(notifier, store, keep, format_recovered([stored.get(i, i) for i in gone], status_url), now)
            except Silenced:
                pass
            except NotifyError as exc:  # best effort, like every recovery notice
                logger.warning("ops_agent_recovery_notice_failed %s", exc)
        if now_ids and now_ids < set(previous):
            # The episode only SHRANK: what is left has been failing since before, so the debounce
            # and the notification carry on. Restarting them would bill a second analysis and send
            # a duplicate alert for a service that never recovered.
            state = {
                **keep,
                "fingerprint": current.fingerprint,
                "first_seen": state["first_seen"],
                "services": current.services,
                "names_by_id": current.names_by_id or {},
                **({"notified_at": state["notified_at"]} if state.get("notified_at") else {}),
            }
        else:
            state = {
                **keep,
                "fingerprint": current.fingerprint,
                "first_seen": now,
                "services": current.services,
                "names_by_id": current.names_by_id or {},
            }
        store.save(state)

    first_seen = float(state["first_seen"])

    if resent:
        return WatchResult("resent")

    if now - first_seen < min_duration_seconds:
        return WatchResult("waiting", f"{int(now - first_seen)}s of {min_duration_seconds}s")

    if state.get("notified_at"):
        if now - float(state["notified_at"]) < repeat_after_seconds:
            return WatchResult("already_notified")
        reminder = format_reminder(current.services, _since(first_seen), status_url)
        return _send(notifier, store, state, reminder, now, "reminded")

    try:
        outcome = analyze_fn(snapshot)
        if outcome.analysis is None:
            raise AnalysisFailed("no analysis returned")
        message = format_analysis(outcome.analysis.to_dict(), current.services, _since(first_seen), status_url)
        action = "notified"
    except BudgetExceeded:
        message = format_fallback(current.services, _since(first_seen), "月額の予算に達したため", status_url)
        action = "notified_fallback_budget"
    except AnalysisFailed:
        message = format_fallback(current.services, _since(first_seen), "分析に失敗したため", status_url)
        action = "notified_fallback_failed"
    except Exception as exc:  # noqa: BLE001 - credentials, network, SDK: report the type only
        logger.warning("ops_agent_analysis_unavailable %s", type(exc).__name__)
        message = format_fallback(current.services, _since(first_seen), "分析を実行できなかったため", status_url)
        action = "notified_fallback_unavailable"

    state["pending"] = message  # persisted before sending: a crash must not pay for the analysis twice
    store.save(state)
    return _send(notifier, store, state, message, now, action)


def _send(notifier: Notifier, store: StateStore, state: dict[str, Any], message: str, now: float, action: str) -> WatchResult:
    try:
        _deliver(notifier, store, state, message, now)
    except Silenced:
        # An analysis was already saved as pending before this call and goes out after the silence.
        # A reminder is simply asked again next cycle.
        store.save(state)
        return WatchResult("silenced")
    except NotifyError as exc:
        store.save(state)
        return WatchResult("notify_failed", str(exc))
    state.pop("pending", None)
    state["notified_at"] = now
    store.save(state)
    return WatchResult(action)
