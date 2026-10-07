from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ops_agent.agent import AnalysisFailed, Outcome
from ops_agent.analysis import validate_analysis
from ops_agent.budget import BudgetExceeded
from ops_agent.notify import (
    DiscordDM,
    NotifyError,
    NotifySettings,
    clean,
    format_analysis,
    format_fallback,
)
from ops_agent.watch import StateStore, concerns, silence, watch_once

T0 = 1_800_000_000.0
URL = "https://status.ivrm.jp/"
ANALYSIS = validate_analysis(
    {
        "severity": "warning",
        "summary": "Hertaが応答していません。",
        "suspected_causes": ["再起動の可能性"],
        "next_steps": ["ログを確認する"],
        "announcement_recommended": True,
    }
)


def snap(*, herta="operational", minecraft="operational", maintenance=None):
    return {
        "status": {
            "services": [
                {"id": "minecraft-network", "name": "Minecraft Network", "status": minecraft},
                {"id": "herta-discord-bot", "name": "Herta", "status": herta},
            ],
            "maintenance": maintenance or [],
        },
        "history": {},
    }


class Notifier:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[str] = []
        self.channels: list[object] = []
        self.fail_times = fail_times

    def send(self, message: str, channel_id=None) -> str:
        self.channels.append(channel_id)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise NotifyError("discord returned HTTP 500")
        self.sent.append(message)
        return "111111111111111111"


class Analyzer:
    def __init__(self, raises: Exception | None = None) -> None:
        self.calls = 0
        self.raises = raises

    def __call__(self, _snapshot):
        self.calls += 1
        if self.raises:
            raise self.raises
        return Outcome(skipped=False, reason="analyzed", analysis=ANALYSIS, cost_jpy=0.5)


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.json")


def cycle(store, notifier, analyzer, snapshot, at, **kwargs):
    return watch_once(
        snapshot=snapshot, analyze_fn=analyzer, notifier=notifier, store=store, now=at, status_url=URL, **kwargs
    )


def test_a_healthy_system_does_nothing_and_costs_nothing(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    assert cycle(store, notifier, analyzer, snap(), T0).action == "healthy"
    assert notifier.sent == [] and analyzer.calls == 0
    assert not store.path.exists()


def test_a_problem_must_last_before_anything_happens(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0).action == "waiting"
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 240).action == "waiting"
    assert notifier.sent == [] and analyzer.calls == 0


def test_a_brief_blip_that_recovers_never_notifies(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    assert cycle(store, notifier, analyzer, snap(), T0 + 120).action == "blip_ended"
    assert notifier.sent == [] and analyzer.calls == 0
    assert "fingerprint" not in store.load()
    # and a new problem starts a fresh timer rather than inheriting the old one
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 400).action == "waiting"


def test_a_sustained_problem_is_analyzed_once_and_the_approver_is_told(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    result = cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert result.action == "notified" and analyzer.calls == 1 and len(notifier.sent) == 1
    message = notifier.sent[0]
    assert "Herta" in message and "AIによる分析" in message and "自動では公開されません" in message
    # later cycles within the repeat window: silence, and no second model call
    for minutes in (5, 10, 30, 55):
        assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300 + minutes * 60).action == "already_notified"
    assert analyzer.calls == 1 and len(notifier.sent) == 1


def test_a_long_outage_gets_a_reminder_without_another_model_call(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    result = cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300 + 3600)
    assert result.action == "reminded" and analyzer.calls == 1 and len(notifier.sent) == 2
    assert "継続中" in notifier.sent[1]


def test_recovery_is_announced_once_and_resets_everything(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert cycle(store, notifier, analyzer, snap(), T0 + 900).action == "recovered"
    assert "回復" in notifier.sent[-1] and len(notifier.sent) == 2
    assert cycle(store, notifier, analyzer, snap(), T0 + 1200).action == "healthy"
    assert len(notifier.sent) == 2


def test_a_different_set_of_failing_services_is_a_new_episode(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    result = cycle(store, notifier, analyzer, snap(herta="outage", minecraft="degraded"), T0 + 400)
    assert result.action == "waiting"  # conservative: the new combination must last as well


def test_maintenance_silences_only_the_services_it_covers() -> None:
    maintenance = [{"state": "in_progress", "affected_service_ids": ["minecraft-network"]}]
    covered = concerns(snap(minecraft="outage", maintenance=maintenance)["status"])
    assert covered.services == []
    mixed = concerns(snap(minecraft="outage", herta="outage", maintenance=maintenance)["status"])
    assert mixed.services == ["Herta"]
    scheduled = [{"state": "scheduled", "affected_service_ids": ["minecraft-network"]}]
    assert concerns(snap(minecraft="outage", maintenance=scheduled)["status"]).services == ["Minecraft Network"]


def test_a_service_reporting_maintenance_is_not_a_concern() -> None:
    assert concerns(snap(minecraft="maintenance")["status"]).services == []


@pytest.mark.parametrize("status", ["degraded", "outage", "unknown"])
def test_every_unhealthy_status_counts(status: str) -> None:
    assert concerns(snap(herta=status)["status"]).services == ["Herta"]


def test_an_unreadable_status_is_treated_as_a_concern() -> None:
    assert concerns({"unexpected": True}).services == ["status"]
    assert concerns(None).services == ["status"]


def test_silence_suppresses_everything_until_it_expires(store: StateStore) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    silence(store, 60, T0)
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 100).action == "silenced"
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 3500).action == "silenced"
    assert notifier.sent == [] and analyzer.calls == 0
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 3700).action == "waiting"


@pytest.mark.parametrize("minutes", [0, -5, 1441])
def test_silence_is_bounded(store: StateStore, minutes: int) -> None:
    with pytest.raises(ValueError):
        silence(store, minutes, T0)


def run_to_notification(store, notifier, analyzer):
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    return cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)


def test_when_the_budget_is_spent_the_approver_still_hears_about_it(store: StateStore) -> None:
    notifier = Notifier()
    result = run_to_notification(store, notifier, Analyzer(raises=BudgetExceeded("limit")))
    assert result.action == "notified_fallback_budget"
    assert "予算" in notifier.sent[0] and "AIによる分析は行っていません" in notifier.sent[0]
    assert "公開" not in notifier.sent[0]


@pytest.mark.parametrize(
    ("error", "action"),
    [
        (AnalysisFailed("bad output"), "notified_fallback_failed"),
        (RuntimeError("Unable to locate credentials AKIASECRET"), "notified_fallback_unavailable"),
    ],
)
def test_when_the_model_fails_a_plain_message_is_sent_without_leaking_details(store: StateStore, error, action) -> None:
    notifier = Notifier()
    assert run_to_notification(store, notifier, Analyzer(raises=error)).action == action
    assert "AKIASECRET" not in notifier.sent[0] and "bad output" not in notifier.sent[0]


def test_a_failed_delivery_is_retried_without_paying_for_the_analysis_again(store: StateStore) -> None:
    notifier, analyzer = Notifier(fail_times=1), Analyzer()
    assert run_to_notification(store, notifier, analyzer).action == "notify_failed"
    assert store.load()["pending"] and analyzer.calls == 1
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 600).action == "resent"
    assert analyzer.calls == 1 and len(notifier.sent) == 1
    assert "pending" not in store.load()
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 900).action == "already_notified"


def test_the_dm_channel_is_remembered_between_cycles(store: StateStore) -> None:
    notifier = Notifier()
    run_to_notification(store, notifier, Analyzer())
    assert notifier.channels == [None]
    assert store.load()["dm_channel_id"] == "111111111111111111"
    cycle(store, notifier, Analyzer(), snap(), T0 + 900)  # recovery message reuses it
    assert notifier.channels[-1] == "111111111111111111"


def test_a_corrupt_state_file_starts_clean_instead_of_crashing(store: StateStore) -> None:
    store.path.write_text("{not json", encoding="utf-8")
    assert cycle(store, Notifier(), Analyzer(), snap(), T0).action == "healthy"
    store.path.write_text("[1, 2]", encoding="utf-8")
    assert cycle(store, Notifier(), Analyzer(), snap(herta="outage"), T0).action == "waiting"


# --- message safety ---------------------------------------------------------------------


def test_text_from_the_model_cannot_ping_anyone_or_exceed_the_limit() -> None:
    hostile = validate_analysis(
        {
            "severity": "critical",
            "summary": "@everyone <@123456789012345678> " + "あ" * 300,
            "suspected_causes": ["x" * 190] * 5,
            "next_steps": ["y" * 190] * 5,
            "announcement_recommended": False,
        }
    )
    message = format_analysis(hostile.to_dict(), ["Herta"], "10/07 09:00 JST", URL)
    assert len(message) <= 1900
    fallback = format_fallback(["Herta"], "10/07 09:00 JST", "z" * 5000, URL)
    assert len(fallback) <= 1900


def test_control_characters_are_removed() -> None:
    assert clean("a\x1b[31mb\x00c", 50) == "a[31mbc"


# --- Discord transport (against a local fake API; nothing real is contacted) --------------


class FakeDiscord(BaseHTTPRequestHandler):
    requests: list[dict] = []
    fail_with: int | None = None

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if type(self).fail_with:
            self.send_response(type(self).fail_with)
            self.end_headers()
            self.wfile.write(json.dumps({"echo": body, "auth": self.headers.get("Authorization")}).encode())
            return
        payload = {"id": "222222222222222222"} if self.path.endswith("/channels") else {"id": "1"}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture()
def discord():
    FakeDiscord.requests, FakeDiscord.fail_with = [], None
    server = HTTPServer(("127.0.0.1", 0), FakeDiscord)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = "A" * 24 + "." + "b" * 24
    settings = NotifySettings(user_id="123456789012345678", bot_token=token, api_base=f"http://127.0.0.1:{server.server_port}")
    yield DiscordDM(settings), token
    server.shutdown()


def test_a_dm_opens_a_channel_then_posts_with_mentions_disabled(discord) -> None:
    dm, token = discord
    channel = dm.send("hello <@123456789012345678> @everyone")
    assert channel == "222222222222222222"
    open_call, post_call = FakeDiscord.requests
    assert open_call["path"].endswith("/users/@me/channels") and open_call["body"] == {"recipient_id": "123456789012345678"}
    assert post_call["path"].endswith("/channels/222222222222222222/messages")
    assert post_call["body"]["allowed_mentions"] == {"parse": []}
    assert post_call["auth"] == f"Bot {token}"


def test_a_known_channel_skips_the_lookup(discord) -> None:
    dm, _ = discord
    dm.send("hi", "222222222222222222")
    assert [r["path"].split("/")[-1] for r in FakeDiscord.requests] == ["messages"]


def test_delivery_errors_never_contain_the_token_or_the_message(discord) -> None:
    dm, token = discord
    FakeDiscord.fail_with = 401
    with pytest.raises(NotifyError) as raised:
        dm.send("a secret message body")
    text = str(raised.value) + repr(raised.value)
    assert token not in text and "secret message body" not in text and "401" in text


def test_the_token_is_not_in_the_settings_repr(discord) -> None:
    dm, token = discord
    assert token not in repr(dm.settings)


def test_an_unreachable_discord_is_a_notify_error_not_a_crash() -> None:
    settings = NotifySettings(user_id="123456789012345678", bot_token="A" * 24 + "." + "b" * 24, api_base="http://127.0.0.1:1")
    with pytest.raises(NotifyError):
        DiscordDM(settings).send("x")


@pytest.mark.parametrize("base", ["http://discord.com/api/v10", "ftp://x", "file:///etc"])
def test_a_non_https_discord_base_is_refused(base: str) -> None:
    settings = NotifySettings(user_id="123456789012345678", bot_token="A" * 24 + "." + "b" * 24, api_base=base)
    with pytest.raises(ValueError):
        DiscordDM(settings)


@pytest.mark.parametrize(
    ("user", "token"),
    [("123", "A" * 24), ("12345678901234567x", "A" * 24), ("123456789012345678", "short"), ("123456789012345678", "has space " * 4)],
)
def test_bad_notification_settings_are_rejected(monkeypatch, user: str, token: str) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", user)
    monkeypatch.setenv("OPS_AGENT_DISCORD_BOT_TOKEN", token)
    with pytest.raises(ValueError):
        NotifySettings.from_env()


def test_unconfigured_notifications_fall_back_to_the_journal(monkeypatch) -> None:
    monkeypatch.delenv("OPS_AGENT_DISCORD_USER_ID", raising=False)
    monkeypatch.delenv("OPS_AGENT_DISCORD_BOT_TOKEN", raising=False)
    assert not NotifySettings.from_env().configured


# --- CLI -------------------------------------------------------------------------------


def test_the_watch_command_on_a_healthy_system_needs_no_aws_and_no_discord(monkeypatch, tmp_path: Path, capsys) -> None:
    import boto3

    from ops_agent import __main__ as cli

    class Client:
        def __init__(self, *_a, **_k) -> None: ...

        def snapshot(self):
            return snap()

    def forbidden(*_a, **_k):
        raise AssertionError("boto3 must not be used on a healthy cycle")

    monkeypatch.setattr(cli, "StatusClient", Client)
    monkeypatch.setattr(boto3, "client", forbidden)
    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPS_AGENT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    assert cli.main(["--watch"]) == 0
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["action"] == "healthy"


def test_the_silence_command_writes_the_state(monkeypatch, tmp_path: Path, capsys) -> None:
    from ops_agent import __main__ as cli

    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    assert cli.main(["--silence", "30"]) == 0
    assert json.loads((tmp_path / "state.json").read_text())["silence_until"] > 0
    assert cli.main(["--silence", "0"]) == 64


def test_a_bad_discord_setting_stops_the_watch_with_a_config_error(monkeypatch, tmp_path: Path, capsys) -> None:
    from ops_agent import __main__ as cli

    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", "nope")
    monkeypatch.setenv("OPS_AGENT_DISCORD_BOT_TOKEN", "A" * 24)
    assert cli.main(["--watch"]) == 78
    assert "notify_misconfigured" in capsys.readouterr().err


def test_a_crash_between_the_analysis_and_the_send_does_not_pay_for_the_analysis_twice(store: StateStore) -> None:
    class Crashing(Notifier):
        def send(self, message, channel_id=None):
            raise RuntimeError("process killed")  # not a NotifyError: nothing catches this

    analyzer = Analyzer()
    cycle(store, Crashing(), analyzer, snap(herta="outage"), T0)
    with pytest.raises(RuntimeError):
        cycle(store, Crashing(), analyzer, snap(herta="outage"), T0 + 300)
    assert analyzer.calls == 1 and store.load()["pending"]  # saved before the send was attempted

    notifier = Notifier()
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 600).action == "resent"
    assert analyzer.calls == 1 and len(notifier.sent) == 1
