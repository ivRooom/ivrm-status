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
)

logger = logging.getLogger("ops_agent")
JST = timezone(timedelta(hours=9), "JST")
HEALTHY = {"operational", "maintenance"}


class Notifier(Protocol):
    def send(self, message: str, channel_id: Optional[str] = None) -> str: ...


class StdoutNotifier:
    """Used when Discord is not configured: the journal keeps what would have been sent."""

    def send(self, message: str, channel_id: Optional[str] = None) -> str:
        print(message)
        return channel_id or ""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
            os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file
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
        if service.get("id") in covered:
            suppressed_ids.add(str(service.get("id")))
            continue
        name = str(service.get("name") or service.get("id") or "不明")[:60]
        names.append(name)
        parts.append(str(service.get("id")))
        names_by_id[str(service.get("id"))] = name
    for incident in status.get("incidents") or []:
        if isinstance(incident, dict) and str(incident.get("status", "")).lower() != "resolved":
            name = "Incident: " + str(incident.get("title") or incident.get("public_id") or "不明")[:50]
            incident_id = f"incident:{incident.get('public_id')}"
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
    state = store.load()
    state["silence_until"] = now + minutes * 60
    store.save(state)
    return state["silence_until"]


def _deliver(notifier: Notifier, store: StateStore, state: dict[str, Any], message: str) -> None:
    """Send, remembering the DM channel. The caller has already persisted `pending`."""
    channel = notifier.send(message, state.get("dm_channel_id") or None)
    if channel and channel != state.get("dm_channel_id"):
        state["dm_channel_id"] = channel


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
    state = store.load()
    if float(state.get("silence_until") or 0) > now:
        return WatchResult("silenced")

    # A message that could not be delivered earlier goes out FIRST, whatever the health is now:
    # it was already paid for, and if the service has since recovered, dropping it would leave the
    # approver never knowing about an outage that was analyzed. The cycle then carries on and may
    # follow it with the recovery notice.
    if state.get("pending"):
        try:
            _deliver(notifier, store, state, str(state["pending"]))
        except NotifyError as exc:
            store.save(state)
            return WatchResult("notify_failed", str(exc))
        state.pop("pending", None)
        state["notified_at"] = now
        store.save(state)
        resent = True
    else:
        resent = False

    current = concerns(snapshot.get("status", {}))
    keep = {k: state[k] for k in ("dm_channel_id", "silence_until") if k in state}

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
                _deliver(notifier, store, keep, format_recovered(names, status_url))
            except NotifyError as exc:  # best effort: a recovery notice is not worth a retry loop
                logger.warning("ops_agent_recovery_notice_failed %s", exc)
        store.save(keep)
        return WatchResult("recovered" if notified else "blip_ended", ", ".join(names))

    if current.fingerprint != state.get("fingerprint"):
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
        _deliver(notifier, store, state, message)
    except NotifyError as exc:
        store.save(state)
        return WatchResult("notify_failed", str(exc))
    state.pop("pending", None)
    state["notified_at"] = now
    store.save(state)
    return WatchResult(action)
