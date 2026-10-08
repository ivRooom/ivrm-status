"""The webhook destination: least privilege, and the URL (a secret) never leaks."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ops_agent import __main__ as cli
from ops_agent.notify import (
    DiscordDM,
    DiscordWebhook,
    NotifyError,
    NotifySettings,
    build_notifier,
)

WEBHOOK_ID = "123456789012345678"
SECRET = "abcDEF-ghiJKL_mnoPQR.stuVWX0123456789"
URL = f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{SECRET}"
BOT_TOKEN = "A" * 24 + "." + "b" * 24
USER = "111111111111111111"


def settings(url: str = URL, **extra) -> NotifySettings:
    return NotifySettings(webhook_url=url, **extra)


# --- the URL is validated strictly --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"https://discordapp.com/api/webhooks/{WEBHOOK_ID}/{SECRET}",
        f"https://discord.com/api/v10/webhooks/{WEBHOOK_ID}/{SECRET}",
        f"http://127.0.0.1:8123/api/webhooks/{WEBHOOK_ID}/{SECRET}",
        f"http://localhost:8123/api/webhooks/{WEBHOOK_ID}/{SECRET}",
    ],
)
def test_real_discord_and_loopback_urls_are_accepted(monkeypatch, url: str) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_WEBHOOK_URL", url)
    assert NotifySettings.from_env().webhook_url == url


@pytest.mark.parametrize(
    "url",
    [
        f"http://discord.com/api/webhooks/{WEBHOOK_ID}/{SECRET}",  # not https
        f"https://discord.com.evil.example/api/webhooks/{WEBHOOK_ID}/{SECRET}",  # look-alike host
        f"https://discord.com@evil.example/api/webhooks/{WEBHOOK_ID}/{SECRET}",  # userinfo trick
        f"https://evil.example/api/webhooks/{WEBHOOK_ID}/{SECRET}",
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{SECRET}?thread_id=1",  # query string
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{SECRET}#frag",
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}/short",  # no real secret
        f"https://discord.com/api/webhooks/12/{SECRET}",
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}",
        "https://discord.com/api/webhooks/",
        "file:///etc/passwd",
        "ftp://discord.com/api/webhooks/x/y",
        f"http://127.0.0.1.evil.example/api/webhooks/{WEBHOOK_ID}/{SECRET}",
    ],
)
def test_anything_else_is_refused_before_it_can_be_used(monkeypatch, url: str) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_WEBHOOK_URL", url)
    with pytest.raises(ValueError, match="webhook"):
        NotifySettings.from_env()
    with pytest.raises(ValueError):
        DiscordWebhook(settings(url))  # and the class itself does not trust the settings either


def test_surrounding_whitespace_in_the_env_file_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_WEBHOOK_URL", f"  {URL}\n")
    assert NotifySettings.from_env().webhook_url == URL


def test_the_class_itself_is_strict_about_whitespace_and_never_sends_to_a_padded_url() -> None:
    for padded in (URL + "\n", " " + URL, URL + " ", URL + "\r\n"):
        with pytest.raises(ValueError):
            DiscordWebhook(settings(padded))


def test_the_webhook_and_the_bot_settings_cannot_both_be_set(monkeypatch) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_WEBHOOK_URL", URL)
    monkeypatch.setenv("OPS_AGENT_DISCORD_USER_ID", USER)
    monkeypatch.setenv("OPS_AGENT_DISCORD_BOT_TOKEN", BOT_TOKEN)
    with pytest.raises(ValueError, match="not both"):
        NotifySettings.from_env()


@pytest.mark.parametrize("variable", ["OPS_AGENT_DISCORD_USER_ID", "OPS_AGENT_DISCORD_BOT_TOKEN"])
def test_a_webhook_with_half_of_the_bot_settings_is_refused_too(monkeypatch, variable: str) -> None:
    monkeypatch.setenv("OPS_AGENT_DISCORD_WEBHOOK_URL", URL)
    monkeypatch.setenv(variable, USER if variable.endswith("ID") else BOT_TOKEN)
    with pytest.raises(ValueError):
        NotifySettings.from_env()


# --- choosing the destination --------------------------------------------------------------------------


def test_the_factory_picks_the_configured_destination() -> None:
    assert isinstance(build_notifier(settings()), DiscordWebhook)
    assert isinstance(build_notifier(NotifySettings(user_id=USER, bot_token=BOT_TOKEN)), DiscordDM)
    assert build_notifier(NotifySettings()) is None


def test_the_settings_are_configured_by_either_destination() -> None:
    assert settings().configured and NotifySettings(user_id=USER, bot_token=BOT_TOKEN).configured
    assert not NotifySettings().configured


# --- sending ----------------------------------------------------------------------------------------------


class Hook(BaseHTTPRequestHandler):
    requests: list = []
    status = 204

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        self.send_response(type(self).status)
        self.end_headers()
        if type(self).status >= 400:  # an error body that echoes everything it was given
            self.wfile.write(json.dumps({"message": "echo", "path": self.path, "body": body}).encode())

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture()
def hook():
    Hook.requests, Hook.status = [], 204
    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/api/webhooks/{WEBHOOK_ID}/{SECRET}"
    yield DiscordWebhook(settings(url)), url
    server.shutdown()


def test_a_message_is_posted_with_mentions_disabled_and_no_credentials(hook) -> None:
    notifier, _ = hook
    assert notifier.send("hello <@123456789012345678> @everyone") == ""
    request = Hook.requests[0]
    assert request["path"] == f"/api/webhooks/{WEBHOOK_ID}/{SECRET}"
    assert request["body"]["allowed_mentions"] == {"parse": []}
    assert request["auth"] is None  # a webhook needs no account credential at all


def test_a_long_message_is_truncated_to_the_discord_limit(hook) -> None:
    notifier, _ = hook
    notifier.send("あ" * 5000)
    assert len(Hook.requests[0]["body"]["content"]) <= 1900


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_errors_report_the_status_and_never_the_url_or_the_body(hook, status: int) -> None:
    notifier, url = hook
    Hook.status = status
    with pytest.raises(NotifyError) as raised:
        notifier.send("a private message body")
    text = str(raised.value) + repr(raised.value)
    assert str(status) in text and raised.value.status_code == status
    for secret in (SECRET, url, WEBHOOK_ID, "a private message body"):
        assert secret not in text


def test_an_unreachable_destination_is_a_notify_error_without_the_url() -> None:
    url = f"http://127.0.0.1:1/api/webhooks/{WEBHOOK_ID}/{SECRET}"
    with pytest.raises(NotifyError) as raised:
        DiscordWebhook(settings(url)).send("x")
    assert SECRET not in str(raised.value)


def test_the_url_never_appears_in_the_settings_repr_or_the_notifier_repr() -> None:
    notifier = DiscordWebhook(settings())
    assert SECRET not in repr(notifier.settings) and SECRET not in repr(settings())
    assert SECRET not in notifier.recipient_key and WEBHOOK_ID not in notifier.recipient_key


def test_the_recipient_key_tells_two_webhooks_apart_without_holding_the_url() -> None:
    other = f"https://discord.com/api/webhooks/987654321098765432/{SECRET}"
    assert DiscordWebhook(settings()).recipient_key != DiscordWebhook(settings(other)).recipient_key
    assert DiscordWebhook(settings()).recipient_key == DiscordWebhook(settings()).recipient_key


def test_an_http_error_from_the_opener_is_reported_by_status_only() -> None:
    def opener(request, _timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"unknown webhook"))

    with pytest.raises(NotifyError) as raised:
        DiscordWebhook(settings(), opener=opener).send("x")
    assert raised.value.status_code == 404 and SECRET not in str(raised.value)


# --- the watch cycle and the test command use it -----------------------------------------------------------


class Fake:
    sent: list = []

    def __init__(self, *_a, **_k) -> None:
        self.recipient_key = "wh-test"

    def send(self, message, channel_id=None):
        Fake.sent.append(message)
        return ""


def env(monkeypatch, tmp_path: Path, **values: str) -> None:
    for name in ("WEBHOOK_URL", "USER_ID", "BOT_TOKEN"):
        monkeypatch.delenv(f"OPS_AGENT_DISCORD_{name}", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(f"OPS_AGENT_DISCORD_{name}", value)
    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPS_AGENT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))


def test_the_test_command_sends_one_short_message_and_reports_the_destination(monkeypatch, tmp_path: Path, capsys) -> None:
    Fake.sent = []
    env(monkeypatch, tmp_path, WEBHOOK_URL=URL)
    monkeypatch.setattr(cli, "build_notifier", lambda _s: Fake())
    assert cli.main(["--test-notify"]) == 0
    assert json.loads(capsys.readouterr().out) == {"test_notify": "sent", "destination": "webhook"}
    assert len(Fake.sent) == 1 and "通知の試験" in Fake.sent[0] and "障害ではありません" in Fake.sent[0]
    assert not (tmp_path / "state.json").exists() and not (tmp_path / "ledger.jsonl").exists()  # no state, no spend


def test_the_test_command_reports_the_bot_destination(monkeypatch, tmp_path: Path, capsys) -> None:
    Fake.sent = []
    env(monkeypatch, tmp_path, USER_ID=USER, BOT_TOKEN=BOT_TOKEN)
    monkeypatch.setattr(cli, "build_notifier", lambda _s: Fake())
    assert cli.main(["--test-notify"]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == "dm"


def test_the_test_command_refuses_when_nothing_is_configured(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path)
    assert cli.main(["--test-notify"]) == 78
    assert "notify_not_configured" in capsys.readouterr().err


def test_the_test_command_reports_a_misconfiguration(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path, WEBHOOK_URL="https://evil.example/hook")
    assert cli.main(["--test-notify"]) == 78
    err = capsys.readouterr().err
    assert "notify_misconfigured" in err and "evil.example" not in err


def test_the_test_command_reports_a_delivery_failure_without_leaking(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path, WEBHOOK_URL=URL)

    class Failing(Fake):
        def send(self, message, channel_id=None):
            raise NotifyError("discord webhook returned HTTP 404", 404)

    monkeypatch.setattr(cli, "build_notifier", lambda _s: Failing())
    assert cli.main(["--test-notify"]) == 4
    err = capsys.readouterr().err
    assert "notify_failed" in err and "404" in err and SECRET not in err


@pytest.mark.parametrize("extra", [["--dry-run"], ["--force"], ["--watch"], ["--check"], ["--silence", "5"]])
def test_the_test_command_cannot_be_combined_with_other_modes(monkeypatch, tmp_path: Path, capsys, extra) -> None:
    env(monkeypatch, tmp_path, WEBHOOK_URL=URL)
    monkeypatch.setattr(cli, "build_notifier", lambda _s: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.main(["--test-notify", *extra]) == 64
    assert "usage" in capsys.readouterr().err


def test_the_watch_cycle_delivers_through_the_configured_destination(monkeypatch, tmp_path: Path) -> None:
    import boto3

    from ops_agent.agent import Outcome
    from ops_agent.analysis import validate_analysis

    Fake.sent = []
    env(monkeypatch, tmp_path, WEBHOOK_URL=URL)
    analysis = validate_analysis(
        {"severity": "warning", "summary": "s", "suspected_causes": [], "next_steps": [], "announcement_recommended": False}
    )

    class Client:
        def __init__(self, *_a, **_k) -> None: ...

        def status(self):
            return {"services": [{"id": "herta-discord-bot", "name": "Herta", "status": "outage"}], "maintenance": []}

        def history(self):
            return {}

    monkeypatch.setattr(cli, "StatusClient", Client)
    monkeypatch.setattr(cli, "build_notifier", lambda _s: Fake())
    monkeypatch.setattr(cli, "analyze", lambda *_a, **_k: Outcome(False, "ok", analysis, 0.1))
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: object())
    for at in (1_800_000_000.0, 1_800_000_300.0):
        monkeypatch.setattr(cli.time, "time", lambda at=at: at)
        assert cli.main(["--watch"]) == 0
    assert len(Fake.sent) == 1 and "Herta" in Fake.sent[0]


# --- the guide and the example agree with the code -----------------------------------------------------------


def test_the_env_example_offers_the_webhook_first_and_holds_no_secret() -> None:
    text = (Path(__file__).resolve().parents[1] / "deploy" / "env.example").read_text(encoding="utf-8")
    assert text.index("OPS_AGENT_DISCORD_WEBHOOK_URL=") < text.index("OPS_AGENT_DISCORD_BOT_TOKEN=")
    for name in ("WEBHOOK_URL", "BOT_TOKEN", "USER_ID"):
        assert f"OPS_AGENT_DISCORD_{name}=\n" in text  # the value is empty


def test_the_guide_documents_the_webhook_and_the_test_command() -> None:
    guide = (Path(__file__).resolve().parents[1] / "DEPLOY.md").read_text(encoding="utf-8")
    assert "OPS_AGENT_DISCORD_WEBHOOK_URL" in guide and "--test-notify" in guide
    assert "Hertaの内部API" in guide  # why it is not used
    assert "両方あると" in guide  # the either/or rule


def test_the_readme_summary_describes_both_destinations_and_the_setup_checks() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    summary = readme[readme.index("## 定期実行") : readme.index("## 設定（環境変数）")]
    assert "Webhook" in summary and "DM" in summary  # both ways to be notified
    assert "--check" in summary and "--test-notify" in summary
    assert "DMで通知します" not in summary  # the old DM-only wording
