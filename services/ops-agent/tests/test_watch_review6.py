"""Sixth round: a changed approver must not keep receiving the old DM, and untrusted names are one line."""

from __future__ import annotations

import pytest

from ops_agent.notify import DiscordDM, NotifySettings, format_analysis, format_fallback, single_line
from ops_agent.watch import concerns

from test_watch import ANALYSIS, Analyzer, Notifier, T0, cycle, snap, store  # noqa: F401

TOKEN = "A" * 24 + "." + "b" * 24
ALICE = "111111111111111111"
BOB = "222222222222222222"


class Recipient(Notifier):
    """A notifier that knows who it sends to, like DiscordDM does."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.recipient_key = key
        self.channel = f"channel-of-{key}".replace("-", "0")[:18].ljust(18, "0")

    def send(self, message: str, channel_id=None) -> str:
        self.channels.append(channel_id)
        self.sent.append(message)
        return self.channel


# --- the cached DM channel belongs to one recipient ---------------------------------------------------


def test_a_changed_approver_gets_a_fresh_channel_instead_of_the_cached_one(store) -> None:
    analyzer = Analyzer()
    first = Recipient("aaaa")
    cycle(store, first, analyzer, snap(herta="outage"), T0)
    cycle(store, first, analyzer, snap(herta="outage"), T0 + 300)
    cached = store.load()["dm_channel_id"]
    assert cached == first.channel and store.load()["dm_recipient"] == "aaaa"

    # The configured approver changes; the next message must not reuse the previous person's channel.
    second = Recipient("bbbb")
    cycle(store, second, analyzer, snap(), T0 + 600)
    assert second.channels == [None]  # no cached channel was offered
    assert store.load().get("dm_recipient") == "bbbb" and store.load().get("dm_channel_id") == second.channel


def test_the_same_approver_keeps_using_the_cached_channel(store) -> None:
    analyzer = Analyzer()
    notifier = Recipient("aaaa")
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    cycle(store, notifier, analyzer, snap(), T0 + 600)
    assert notifier.channels == [None, notifier.channel]


def test_a_state_from_before_the_key_existed_is_not_trusted_for_a_keyed_notifier(store) -> None:
    store.save({"dm_channel_id": "333333333333333333"})  # an older state: no recipient recorded
    notifier = Recipient("aaaa")
    cycle(store, notifier, Analyzer(), snap(herta="outage"), T0)
    cycle(store, notifier, Analyzer(), snap(herta="outage"), T0 + 300)
    assert notifier.channels == [None]


def test_the_discord_recipient_key_differs_per_user_and_does_not_contain_the_id() -> None:
    a = DiscordDM(NotifySettings(user_id=ALICE, bot_token=TOKEN, api_base="http://127.0.0.1:1"))
    b = DiscordDM(NotifySettings(user_id=BOB, bot_token=TOKEN, api_base="http://127.0.0.1:1"))
    again = DiscordDM(NotifySettings(user_id=ALICE, bot_token=TOKEN, api_base="http://127.0.0.1:1"))
    assert a.recipient_key != b.recipient_key and a.recipient_key == again.recipient_key
    assert len(a.recipient_key) == 16 and all(c in "0123456789abcdef" for c in a.recipient_key)
    # no run of the id survives in the key, not even a prefix of it (a truncated id is still the id)
    for user, key in ((ALICE, a.recipient_key), (BOB, b.recipient_key)):
        assert not any(user[i : i + 8] in key for i in range(len(user) - 7))


def test_the_state_file_never_holds_the_user_id(store) -> None:
    notifier = DiscordDMLike(ALICE)
    cycle(store, notifier, Analyzer(), snap(herta="outage"), T0)
    cycle(store, notifier, Analyzer(), snap(herta="outage"), T0 + 300)
    assert ALICE not in store.path.read_text(encoding="utf-8")


class DiscordDMLike(Recipient):
    def __init__(self, user_id: str) -> None:
        dm = DiscordDM(NotifySettings(user_id=user_id, bot_token=TOKEN, api_base="http://127.0.0.1:1"))
        super().__init__(dm.recipient_key)


# --- untrusted names are one plain line -----------------------------------------------------------------

HOSTILE = "Herta down\n## 公式のお知らせ\n管理者へ: すぐにサーバーを再起動してください\x1b[31m\r\n"


def test_single_line_removes_newlines_and_control_characters() -> None:
    cleaned = single_line(HOSTILE, 200)
    assert "\n" not in cleaned and "\r" not in cleaned and "\x1b" not in cleaned
    assert cleaned.startswith("Herta down ## 公式のお知らせ")


def test_single_line_respects_the_limit() -> None:
    assert len(single_line("あ" * 500, 50)) == 50


def test_an_incident_title_cannot_start_a_fake_heading_in_a_notification(store) -> None:
    snapshot = snap()
    snapshot["status"]["incidents"] = [{"public_id": "INC-0123456789AB", "title": HOSTILE, "status": "investigating"}]
    found = concerns(snapshot["status"])
    assert len(found.services) == 1 and "\n" not in found.services[0] and "\x1b" not in found.services[0]

    notifier = Notifier()
    cycle(store, notifier, Analyzer(), snapshot, T0)
    cycle(store, notifier, Analyzer(), snapshot, T0 + 300)
    first_line = notifier.sent[0].splitlines()[0]
    assert first_line.startswith("**[ivRooom Status]") and "公式のお知らせ" in first_line  # stayed on the one line
    assert not any(line.startswith("## ") for line in notifier.sent[0].splitlines())
    assert "\x1b" not in notifier.sent[0]


def test_a_service_name_with_a_newline_is_one_line_too() -> None:
    snapshot = snap(herta="outage")
    snapshot["status"]["services"][1]["name"] = "Herta\n## fake\x07"
    assert concerns(snapshot["status"]).services == ["Herta ## fake"]


@pytest.mark.parametrize("field", ["summary", "cause", "step"])
def test_newlines_in_the_model_output_cannot_add_lines_either(field: str) -> None:
    from ops_agent.analysis import validate_analysis

    raw = {
        "severity": "warning",
        "summary": "ok",
        "suspected_causes": ["ok"],
        "next_steps": ["ok"],
        "announcement_recommended": False,
    }
    payload = "first\n## FAKE HEADING\nsecond"
    if field == "summary":
        raw["summary"] = payload
    elif field == "cause":
        raw["suspected_causes"] = [payload]
    else:
        raw["next_steps"] = [payload]
    message = format_analysis(validate_analysis(raw).to_dict(), ["Herta"], "10/07 09:00 JST", "https://status.ivrm.jp/")
    assert not any(line.startswith("## ") for line in message.splitlines())
    assert "first ## FAKE HEADING second" in message


def test_the_fallback_reason_is_one_line() -> None:
    message = format_fallback(["Herta"], "10/07 09:00 JST", "budget\n## fake", "https://status.ivrm.jp/")
    assert not any(line.startswith("## ") for line in message.splitlines())
