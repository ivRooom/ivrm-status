"""Regression tests for the review findings on the watch cycle."""

from __future__ import annotations

import pytest

from ops_agent.notify import NotifySettings
from ops_agent.watch import concerns, watch_once

from test_watch import Analyzer, Notifier, T0, URL, cycle, snap, store  # noqa: F401


def with_incident(snapshot, *, status="investigating", public_id="INC-0123456789AB", title="Herta 応答遅延"):
    snapshot["status"]["incidents"] = [{"public_id": public_id, "title": title, "status": status}]
    return snapshot


# --- a flapping service is one continuing problem --------------------------------------------


def test_a_service_flipping_between_unhealthy_states_still_gets_reported(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    states = ["outage", "degraded", "unknown", "outage", "degraded", "unknown"]
    results = [
        cycle(store, notifier, analyzer, snap(herta=state), T0 + index * 100).action
        for index, state in enumerate(states)
    ]
    # 0, 100, 200, 300, 400, 500 seconds: the debounce (300s) is reached at the 4th cycle even
    # though the status changed at every single one.
    assert results[:3] == ["waiting"] * 3
    assert results[3] == "notified" and analyzer.calls == 1


def test_a_status_change_after_the_notification_is_a_reminder_not_a_new_analysis(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert cycle(store, notifier, analyzer, snap(herta="degraded"), T0 + 400).action == "already_notified"
    assert cycle(store, notifier, analyzer, snap(herta="unknown"), T0 + 500).action == "already_notified"
    assert analyzer.calls == 1 and len(notifier.sent) == 1


def test_the_fingerprint_does_not_depend_on_the_status() -> None:
    assert concerns(snap(herta="outage")["status"]).fingerprint == concerns(snap(herta="degraded")["status"]).fingerprint


# --- partial Discord configuration is an error ------------------------------------------------


@pytest.mark.parametrize(
    ("user", "token"),
    [("123456789012345678", ""), ("", "A" * 24 + "." + "b" * 24)],
)
def test_one_discord_setting_without_the_other_is_refused(monkeypatch, user: str, token: str) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", user)
    monkeypatch.setenv("OPS_AGENT_DISCORD_BOT_TOKEN", token)
    with pytest.raises(ValueError, match="both"):
        NotifySettings.from_env()


def test_both_or_neither_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", "123456789012345678")
    monkeypatch.setenv("OPS_AGENT_DISCORD_BOT_TOKEN", "A" * 24 + "." + "b" * 24)
    assert NotifySettings.from_env().configured
    monkeypatch.delenv("OPS_AGENT_DISCORD_USER_ID")
    monkeypatch.delenv("OPS_AGENT_DISCORD_BOT_TOKEN")
    assert not NotifySettings.from_env().configured


def test_a_half_configured_watch_stops_with_the_config_error_instead_of_going_quiet(monkeypatch, tmp_path, capsys) -> None:
    from ops_agent import __main__ as cli

    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", "123456789012345678")
    monkeypatch.delenv("OPS_AGENT_DISCORD_BOT_TOKEN", raising=False)
    assert cli.main(["--watch"]) == 78
    assert "notify_misconfigured" in capsys.readouterr().err


# --- unresolved incidents count (same rule as needs_analysis) -----------------------------------


@pytest.mark.parametrize("status", ["investigating", "identified", "monitoring"])
def test_an_unresolved_incident_is_a_concern_even_when_every_service_is_up(status: str) -> None:
    found = concerns(with_incident(snap(), status=status)["status"])
    assert found.services == ["Incident: Herta 応答遅延"] and found.fingerprint == "incident:INC-0123456789AB"


def test_a_resolved_incident_is_not(store) -> None:
    assert concerns(with_incident(snap(), status="resolved")["status"]).services == []


def test_an_open_incident_prevents_a_false_recovery_and_is_reported_when_it_lasts(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    # The service is back up, but the incident is still open: not a recovery.
    still_open = with_incident(snap(), status="monitoring")
    assert cycle(store, notifier, analyzer, still_open, T0 + 400).action == "waiting"
    # Herta really did recover, and that is announced; the incident is NOT claimed as recovered.
    recoveries = [message for message in notifier.sent if "回復" in message]
    assert len(recoveries) == 1 and "Herta" in recoveries[0] and "Incident" not in recoveries[0]
    # The open incident is a new episode: reported once it has lasted, no second "recovery" yet.
    assert cycle(store, notifier, analyzer, still_open, T0 + 700).action == "notified"
    assert "Incident" in notifier.sent[-1] and len([m for m in notifier.sent if "回復" in m]) == 1
    # Once the incident is resolved and everything is up, the recovery is announced.
    assert cycle(store, notifier, analyzer, with_incident(snap(), status="resolved"), T0 + 900).action == "recovered"


def test_an_incident_on_a_healthy_system_is_analyzed_after_it_lasts(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    open_incident = with_incident(snap())
    cycle(store, notifier, analyzer, open_incident, T0)
    assert cycle(store, notifier, analyzer, open_incident, T0 + 300).action == "notified"
    assert "Incident" in notifier.sent[0]


# --- maintenance is not a recovery --------------------------------------------------------------


def test_starting_a_maintenance_on_a_failing_service_is_not_announced_as_a_recovery(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0)
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 300)
    assert len(notifier.sent) == 1
    covered = snap(minecraft="outage", maintenance=[{"state": "in_progress", "affected_service_ids": ["minecraft-network"]}])
    assert cycle(store, notifier, analyzer, covered, T0 + 400).action == "suppressed_by_maintenance"
    assert len(notifier.sent) == 1 and all("回復" not in message for message in notifier.sent)
    assert "fingerprint" not in store.load()


def test_a_problem_that_outlives_the_maintenance_is_counted_again_from_the_start(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    covered = snap(minecraft="outage", maintenance=[{"state": "in_progress", "affected_service_ids": ["minecraft-network"]}])
    cycle(store, notifier, analyzer, covered, T0)
    assert cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 1000).action == "waiting"
    assert cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 1300).action == "notified"


def test_a_real_recovery_is_still_a_recovery(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert cycle(store, notifier, analyzer, snap(), T0 + 600).action == "recovered"


# --- the documented silence command reaches the deployed state ---------------------------------


def test_the_documented_silence_command_names_the_deployed_state_file() -> None:
    from pathlib import Path

    deploy = (Path(__file__).resolve().parents[1] / "DEPLOY.md").read_text(encoding="utf-8")
    service = (Path(__file__).resolve().parents[1] / "deploy" / "ivrm-ops-agent.service").read_text(encoding="utf-8")
    state_path = "/var/lib/ivrm-ops-agent/state.json"
    assert f"OPS_AGENT_STATE_PATH={state_path}" in service  # what the timer reads
    silence_block = deploy[deploy.index("--silence 60") - 400 : deploy.index("--silence 60") + 20]
    assert f"OPS_AGENT_STATE_PATH={state_path}" in silence_block and "sudo -u ivrm-ops-agent" in silence_block
