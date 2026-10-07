"""Fourth round of review findings: a service reporting 'maintenance' itself, and a silence that
starts while a cycle is already running."""

from __future__ import annotations

from ops_agent.agent import Outcome
from ops_agent.watch import concerns, silence

from test_watch import ANALYSIS, Analyzer, Notifier, T0, cycle, snap, store  # noqa: F401


# --- a service that says "maintenance" is not recovered ---------------------------------------------


def test_a_service_reporting_maintenance_is_recorded_as_suppressed() -> None:
    found = concerns(snap(minecraft="maintenance")["status"])
    assert found.services == [] and found.suppressed and found.suppressed_ids == frozenset({"minecraft-network"})


def test_a_notified_service_that_switches_to_maintenance_is_not_announced_as_recovered(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0)
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 300)
    assert len(notifier.sent) == 1
    # No maintenance record at all: the service itself now reports "maintenance".
    result = cycle(store, notifier, analyzer, snap(minecraft="maintenance"), T0 + 400)
    assert result.action == "suppressed_by_maintenance"
    assert len(notifier.sent) == 1 and all("回復" not in message for message in notifier.sent)


def test_operational_is_still_a_recovery_and_maintenance_alone_is_not_a_problem(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    assert cycle(store, notifier, analyzer, snap(minecraft="maintenance"), T0).action == "healthy"  # no episode to end
    assert notifier.sent == [] and analyzer.calls == 0
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 10)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 400)
    assert cycle(store, notifier, analyzer, snap(), T0 + 800).action == "recovered"


# --- a silence that starts mid-cycle is honored right before delivery -----------------------------------


def silencing_analyzer(store, at):
    def analyze(_snapshot):
        silence(store, 30, at)  # the operator runs --silence while the model is thinking
        return Outcome(skipped=False, reason="analyzed", analysis=ANALYSIS, cost_jpy=0.1)

    return analyze


def test_a_notice_is_held_when_a_silence_starts_during_the_analysis(store) -> None:
    notifier = Notifier()
    analyzer = silencing_analyzer(store, T0 + 300)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    result = cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert result.action == "silenced"
    assert notifier.sent == []  # nothing went out although the cycle had started before the silence
    assert store.load()["pending"]  # the analysis was paid for: it is kept, not lost


def test_the_held_notice_goes_out_once_the_silence_has_ended(store) -> None:
    notifier = Notifier()
    analyzer = silencing_analyzer(store, T0 + 300)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert cycle(store, notifier, Analyzer(), snap(herta="outage"), T0 + 900).action == "silenced"
    resumed = Analyzer()
    assert cycle(store, notifier, resumed, snap(herta="outage"), T0 + 300 + 1801).action == "resent"
    assert len(notifier.sent) == 1 and "AIによる分析" in notifier.sent[0] and resumed.calls == 0


def test_a_recovery_notice_is_not_sent_during_a_silence_that_started_mid_cycle(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    silence(store, 30, T0 + 400)
    cycle(store, notifier, analyzer, snap(), T0 + 410)  # the cycle sees the silence at the start
    assert len(notifier.sent) == 1 and all("回復" not in message for message in notifier.sent)


def test_a_reminder_is_not_sent_when_a_silence_starts_before_it(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    silence(store, 30, T0 + 3500)
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300 + 3600).action == "silenced"
    assert len(notifier.sent) == 1
