"""Second round of review findings on the watch cycle: pending delivery, per-service maintenance."""

from __future__ import annotations

from pathlib import Path

from ops_agent.watch import concerns

from test_watch import Analyzer, Notifier, T0, cycle, snap, store  # noqa: F401

MAINTENANCE_MC = [{"state": "in_progress", "affected_service_ids": ["minecraft-network"]}]


# --- an undelivered message is never lost -----------------------------------------------------


def test_a_pending_message_is_delivered_even_if_the_service_has_recovered_meanwhile(store) -> None:
    notifier, analyzer = Notifier(fail_times=1), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300).action == "notify_failed"
    assert store.load()["pending"] and analyzer.calls == 1

    # The service is back before the retry: the paid analysis must still reach the approver.
    result = cycle(store, notifier, analyzer, snap(), T0 + 400)
    assert result.action == "recovered"
    assert len(notifier.sent) == 2
    assert "AIによる分析" in notifier.sent[0] and "回復" in notifier.sent[1]
    assert analyzer.calls == 1 and "pending" not in store.load() and "fingerprint" not in store.load()


def test_a_pending_message_is_delivered_even_if_the_set_of_failing_services_changed(store) -> None:
    notifier, analyzer = Notifier(fail_times=1), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    both = snap(herta="outage", minecraft="degraded")
    assert cycle(store, notifier, analyzer, both, T0 + 400).action == "resent"
    assert len(notifier.sent) == 1 and "AIによる分析" in notifier.sent[0]
    assert analyzer.calls == 1 and "pending" not in store.load()
    # The new combination is its own episode: it has to last before it is analyzed.
    assert store.load()["fingerprint"] == "herta-discord-bot|minecraft-network" and not store.load().get("notified_at")
    assert cycle(store, notifier, analyzer, both, T0 + 500).action == "waiting"
    assert cycle(store, notifier, analyzer, both, T0 + 700).action == "notified" and analyzer.calls == 2


def test_a_pending_message_survives_repeated_delivery_failures(store) -> None:
    notifier, analyzer = Notifier(fail_times=3), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert cycle(store, notifier, analyzer, snap(), T0 + 400).action == "notify_failed"
    assert cycle(store, notifier, analyzer, snap(), T0 + 500).action == "notify_failed"
    assert store.load()["pending"]
    assert cycle(store, notifier, analyzer, snap(), T0 + 600).action == "recovered"
    assert len(notifier.sent) == 2 and analyzer.calls == 1


def test_silence_still_holds_a_pending_message_back(store) -> None:
    from ops_agent.watch import silence

    notifier, analyzer = Notifier(fail_times=1), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    silence(store, 30, T0 + 310)
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 400).action == "silenced"
    assert store.load()["pending"]
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 310 + 1801).action == "resent"


# --- maintenance is judged per service ----------------------------------------------------------


def test_a_recovered_service_is_announced_even_while_another_one_is_under_maintenance(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    # Herta recovers; Minecraft is down but covered by a maintenance. Herta's recovery is real.
    mixed = snap(minecraft="outage", maintenance=MAINTENANCE_MC)
    result = cycle(store, notifier, analyzer, mixed, T0 + 400)
    assert result.action == "recovered"
    assert "回復" in notifier.sent[-1] and "Herta" in notifier.sent[-1] and "Minecraft" not in notifier.sent[-1]


def test_a_service_that_moved_into_maintenance_is_not_announced_as_recovered(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0)
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 300)
    covered = snap(minecraft="outage", maintenance=MAINTENANCE_MC)
    assert cycle(store, notifier, analyzer, covered, T0 + 400).action == "suppressed_by_maintenance"
    assert len(notifier.sent) == 1 and all("回復" not in message for message in notifier.sent)


def test_a_mixed_episode_reports_only_what_actually_recovered(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    both = snap(herta="outage", minecraft="outage")
    cycle(store, notifier, analyzer, both, T0)
    cycle(store, notifier, analyzer, both, T0 + 300)
    # Herta is back; Minecraft is still down but now covered by a maintenance.
    now_covered = snap(minecraft="outage", maintenance=MAINTENANCE_MC)
    result = cycle(store, notifier, analyzer, now_covered, T0 + 400)
    assert result.action == "recovered"
    assert "Herta" in notifier.sent[-1] and "Minecraft" not in notifier.sent[-1]


def test_concerns_reports_which_services_are_covered_and_the_name_behind_each_id() -> None:
    found = concerns(snap(minecraft="outage", herta="outage", maintenance=MAINTENANCE_MC)["status"])
    assert found.suppressed_ids == frozenset({"minecraft-network"})
    assert found.names_by_id == {"herta-discord-bot": "Herta"}


# --- the documented first run uses the real unit --------------------------------------------------


def test_the_first_run_is_the_real_unit_not_a_hand_built_environment() -> None:
    deploy = (Path(__file__).resolve().parents[1] / "DEPLOY.md").read_text(encoding="utf-8")
    assert "systemctl start ivrm-ops-agent.service" in deploy and "journalctl -u ivrm-ops-agent.service" in deploy
    assert "xargs" not in deploy  # the env file has comment lines that would be passed to env as commands
