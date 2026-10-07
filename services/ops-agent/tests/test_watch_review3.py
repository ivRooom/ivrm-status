"""Third round of review findings: history is optional, pending goes out first, partial recoveries,
and the silence never races with the watch state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_agent.agent import Outcome
from ops_agent.status_client import StatusFetchError
from ops_agent.watch import StateStore, flush_pending, silence

from test_watch import ANALYSIS, Analyzer, Notifier, T0, cycle, snap, store  # noqa: F401


# --- the history is for the model, not for deciding anything ------------------------------------


class Api:
    """A status API whose history endpoint is broken."""

    def __init__(self, status, history_fails=True) -> None:
        self._status = status
        self.history_fails = history_fails
        self.history_calls = 0

    def __call__(self, *_a, **_k):
        return self

    def status(self):
        return self._status

    def history(self):
        self.history_calls += 1
        if self.history_fails:
            raise StatusFetchError("request failed: HTTP 500")
        return {"services": []}


class Discord:
    sent: list = []

    def __init__(self, _settings) -> None: ...

    def send(self, message, channel_id=None):
        Discord.sent.append(message)
        return "111111111111111111"


def run_cli(monkeypatch, tmp_path: Path, api, at: float, *, state: Path | None = None):
    import boto3

    from ops_agent import __main__ as cli

    analyses = []

    def fake_analyze(_settings, _bedrock, snapshot, _ledger, force=False):
        analyses.append(dict(snapshot))
        return Outcome(skipped=False, reason="analyzed", analysis=ANALYSIS, cost_jpy=0.1)

    monkeypatch.setattr(cli, "StatusClient", api)
    monkeypatch.setattr(cli, "analyze", fake_analyze)
    monkeypatch.setattr(cli, "DiscordDM", Discord)
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: object())
    monkeypatch.setattr(cli.time, "time", lambda: at)
    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(state or tmp_path / "state.json"))
    monkeypatch.setenv("OPS_AGENT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", "123456789012345678")
    monkeypatch.setenv("OPS_AGENT_DISCORD_BOT_TOKEN", "A" * 24 + "." + "b" * 24)
    return cli.main(["--watch"]), analyses


def test_a_broken_history_endpoint_does_not_stop_an_outage_from_being_reported(monkeypatch, tmp_path: Path) -> None:
    Discord.sent = []
    api = Api(snap(herta="outage")["status"])
    assert run_cli(monkeypatch, tmp_path, api, T0)[0] == 0
    code, analyses = run_cli(monkeypatch, tmp_path, api, T0 + 300)
    assert code == 0 and len(Discord.sent) == 1 and "Herta" in Discord.sent[0]
    assert analyses[0]["history"] == {}  # analyzed without the history rather than not at all


def test_the_history_is_fetched_only_when_an_analysis_is_due(monkeypatch, tmp_path: Path) -> None:
    Discord.sent = []
    api = Api(snap(herta="outage")["status"], history_fails=False)
    run_cli(monkeypatch, tmp_path, api, T0)  # waiting: nothing is analyzed
    assert api.history_calls == 0
    run_cli(monkeypatch, tmp_path, api, T0 + 300)  # now due
    assert api.history_calls == 1
    run_cli(monkeypatch, tmp_path, api, T0 + 400)  # already notified
    assert api.history_calls == 1


def test_a_healthy_cycle_never_asks_for_the_history(monkeypatch, tmp_path: Path) -> None:
    api = Api(snap()["status"], history_fails=False)
    assert run_cli(monkeypatch, tmp_path, api, T0)[0] == 0
    assert api.history_calls == 0


# --- an unreachable status API cannot strand a paid-for notice ------------------------------------


def test_a_pending_notice_is_delivered_even_when_the_status_api_is_down(monkeypatch, tmp_path: Path) -> None:
    Discord.sent = []
    state = tmp_path / "state.json"
    StateStore(state).save({"fingerprint": "herta-discord-bot", "first_seen": T0, "services": ["Herta"], "pending": "PAID-FOR NOTICE"})

    class Down(Api):
        def status(self):
            raise StatusFetchError("request failed: timed out")

    code, _ = run_cli(monkeypatch, tmp_path, Down({}), T0 + 600, state=state)
    assert code == 3  # the status really is unavailable...
    assert Discord.sent == ["PAID-FOR NOTICE"]  # ...but the notice was not held hostage by it
    assert "pending" not in StateStore(state).load()


def test_flush_pending_does_nothing_without_a_pending_notice(store) -> None:
    notifier = Notifier()
    assert flush_pending(notifier, store, T0) is None and notifier.sent == []


def test_flush_pending_reports_a_delivery_failure_and_keeps_the_notice(store) -> None:
    store.save({"pending": "x", "fingerprint": "a"})
    notifier = Notifier(fail_times=1)
    assert flush_pending(notifier, store, T0).action == "notify_failed"
    assert store.load()["pending"] == "x"
    assert flush_pending(notifier, store, T0 + 60).action == "resent"


def test_flush_pending_respects_a_silence(store) -> None:
    store.save({"pending": "x", "fingerprint": "a"})
    silence(store, 30, T0)
    notifier = Notifier()
    assert flush_pending(notifier, store, T0 + 60) is None and notifier.sent == []


# --- a recovery is announced before a replaced episode is forgotten -------------------------------


def test_a_recovered_service_is_announced_when_another_one_starts_failing(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert len(notifier.sent) == 1
    # Herta is back, Minecraft is down: the approver is still holding Herta's alert.
    result = cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 400)
    assert result.action == "waiting"
    assert len(notifier.sent) == 2
    assert "回復" in notifier.sent[1] and "Herta" in notifier.sent[1] and "Minecraft" not in notifier.sent[1]
    # and the new problem is its own episode
    assert store.load()["fingerprint"] == "minecraft-network" and not store.load().get("notified_at")


def test_a_service_that_is_still_failing_is_not_announced_as_recovered(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    cycle(store, notifier, analyzer, snap(herta="outage", minecraft="outage"), T0 + 400)
    assert len(notifier.sent) == 1  # Herta is still down: no recovery for it


def test_a_service_that_moved_into_maintenance_is_not_announced_when_the_episode_is_replaced(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0)
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 300)
    covered = [{"state": "in_progress", "affected_service_ids": ["minecraft-network"]}]
    cycle(store, notifier, analyzer, snap(minecraft="outage", herta="degraded", maintenance=covered), T0 + 400)
    assert len(notifier.sent) == 1 and all("回復" not in message for message in notifier.sent)


def test_nothing_is_announced_for_an_episode_that_was_never_notified(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 100)
    assert notifier.sent == []


# --- the silence and the watch state never overwrite each other -------------------------------------


def test_a_silence_set_in_the_middle_of_a_cycle_survives_the_cycle(store) -> None:
    notifier = Notifier()

    def analyzer(_snapshot):
        silence(store, 30, T0 + 300)  # the operator runs --silence while the cycle is analyzing
        return Outcome(skipped=False, reason="analyzed", analysis=ANALYSIS, cost_jpy=0.1)

    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)  # the cycle saves its state after
    assert store.load_silence() > T0 + 300  # still there
    assert cycle(store, Notifier(), Analyzer(), snap(herta="outage"), T0 + 900).action == "silenced"


def test_the_silence_command_never_touches_the_watch_state(store) -> None:
    store.save({"fingerprint": "herta-discord-bot", "first_seen": T0, "pending": "x"})
    before = store.path.read_text(encoding="utf-8")
    silence(store, 60, T0)
    assert store.path.read_text(encoding="utf-8") == before
    assert json.loads(store.silence_path.read_text(encoding="utf-8"))["until"] == T0 + 3600


def test_an_unreadable_silence_file_means_no_silence(store) -> None:
    store.silence_path.parent.mkdir(parents=True, exist_ok=True)
    for content in ("{not json", "[]", '{"until": "soon"}', ""):
        store.silence_path.write_text(content, encoding="utf-8")
        assert store.load_silence() == 0.0
