"""Eighth round: a shrinking episode keeps its state, and the DM channel follows the bot as well."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from ops_agent.notify import DiscordDM, NotifyError, NotifySettings

from test_watch import Analyzer, Notifier, T0, cycle, snap, store  # noqa: F401

ALICE = "111111111111111111"


def both(herta="outage", minecraft="outage"):
    return snap(herta=herta, minecraft=minecraft)


# --- an episode that only shrinks carries on --------------------------------------------------------------


def test_a_survivor_of_a_shrinking_episode_is_not_analyzed_or_alerted_again(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, both(), T0)
    cycle(store, notifier, analyzer, both(), T0 + 300)
    assert analyzer.calls == 1 and len(notifier.sent) == 1 and "Herta" in notifier.sent[0]

    # Minecraft recovers; Herta never did.
    result = cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 400)
    assert result.action == "already_notified"
    assert len(notifier.sent) == 2 and "回復" in notifier.sent[1] and "Minecraft" in notifier.sent[1]
    assert "Herta" not in notifier.sent[1]

    # Five minutes later (the old behaviour re-analyzed and re-alerted here): nothing new.
    for at in (T0 + 700, T0 + 1000, T0 + 1500):
        assert cycle(store, notifier, analyzer, snap(herta="outage"), at).action == "already_notified"
    assert analyzer.calls == 1 and len(notifier.sent) == 2


def test_the_survivor_still_gets_its_hourly_reminder(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, both(), T0)
    cycle(store, notifier, analyzer, both(), T0 + 300)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 400)
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300 + 3600).action == "reminded"
    assert "Herta" in notifier.sent[-1] and "継続中" in notifier.sent[-1]
    assert analyzer.calls == 1


def test_the_debounce_of_an_unnotified_episode_is_not_restarted_when_it_shrinks(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, both(), T0)
    # Minecraft recovers before anything was sent; Herta has been failing since T0.
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 200).action == "waiting"
    assert cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300).action == "notified"
    assert analyzer.calls == 1 and "Herta" in notifier.sent[0]


def test_a_changed_or_grown_set_is_still_a_new_episode(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    # A second service fails: the set grew, so the new combination has to last on its own.
    assert cycle(store, notifier, analyzer, both(), T0 + 400).action == "waiting"
    assert cycle(store, notifier, analyzer, both(), T0 + 700).action == "notified" and analyzer.calls == 2


def test_a_swap_is_a_new_episode_and_announces_what_recovered(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    cycle(store, notifier, analyzer, snap(herta="outage"), T0)
    cycle(store, notifier, analyzer, snap(herta="outage"), T0 + 300)
    assert cycle(store, notifier, analyzer, snap(minecraft="outage"), T0 + 400).action == "waiting"
    assert "回復" in notifier.sent[1] and "Herta" in notifier.sent[1]


# --- the DM channel follows the bot as well as the approver ------------------------------------------------


def bot_token(bot_id: str) -> str:
    return f"{bot_id}.{'b' * 6}.{'c' * 27}"


def key(user: str, token: str) -> str:
    return DiscordDM(NotifySettings(user_id=user, bot_token=token, api_base="http://127.0.0.1:1")).recipient_key


def test_replacing_the_bot_changes_the_key_and_the_key_never_holds_the_token() -> None:
    old, new = bot_token("MTIzNDU2Nzg5MDEyMzQ1Njc4"), bot_token("OTg3NjU0MzIxMDk4NzY1NDMy")
    assert key(ALICE, old) != key(ALICE, new)
    assert key(ALICE, old) == key(ALICE, old)
    assert old not in key(ALICE, old) and "c" * 27 not in key(ALICE, old)


def test_rotating_only_the_secret_part_of_the_token_keeps_the_same_key() -> None:
    # Regenerating a bot's token keeps its id, and the DM channel stays valid for it.
    first = f"MTIzNDU2Nzg5MDEyMzQ1Njc4.{'b' * 6}.{'c' * 27}"
    rotated = f"MTIzNDU2Nzg5MDEyMzQ1Njc4.{'x' * 6}.{'y' * 27}"
    assert key(ALICE, first) == key(ALICE, rotated)


class Response:
    def __init__(self, payload) -> None:
        self.payload = json.dumps(payload).encode()

    def read(self, _n=-1):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def discord(script):
    """A DiscordDM whose HTTP layer follows `script`: (method, path) -> payload or an HTTP status."""
    calls: list[str] = []

    def opener(request, _timeout):
        path = request.full_url.split("/api/v10", 1)[-1] if "/api/v10" in request.full_url else request.full_url.split("127.0.0.1:1", 1)[-1]
        calls.append(path)
        outcome = script(path)
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(request.full_url, outcome, "refused", {}, io.BytesIO(b'{"message": "secret body"}'))
        return Response(outcome)

    settings = NotifySettings(user_id=ALICE, bot_token=bot_token("MTIzNDU2Nzg5MDEyMzQ1Njc4"), api_base="https://discord.example/api/v10")
    return DiscordDM(settings, opener=opener), calls


@pytest.mark.parametrize("status", [403, 404])
def test_a_refused_cached_channel_is_reopened_once_and_the_message_still_goes_out(status: int) -> None:
    def script(path):
        if path == "/channels/333333333333333333/messages":
            return status
        if path == "/users/@me/channels":
            return {"id": "444444444444444444"}
        return {"id": "1"}

    dm, calls = discord(script)
    assert dm.send("hello", "333333333333333333") == "444444444444444444"
    assert calls == ["/channels/333333333333333333/messages", "/users/@me/channels", "/channels/444444444444444444/messages"]


def test_a_refused_fresh_channel_is_not_retried_in_a_loop() -> None:
    dm, calls = discord(lambda path: {"id": "444444444444444444"} if path.endswith("/channels") else 403)
    with pytest.raises(NotifyError) as raised:
        dm.send("hello")  # no cached channel: nothing to heal
    assert raised.value.status_code == 403
    assert calls == ["/users/@me/channels", "/channels/444444444444444444/messages"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_other_errors_do_not_trigger_a_reopen(status: int) -> None:
    dm, calls = discord(lambda path: status)
    with pytest.raises(NotifyError):
        dm.send("hello", "333333333333333333")
    assert calls == ["/channels/333333333333333333/messages"]


def test_a_second_refusal_after_reopening_is_reported_not_swallowed() -> None:
    def script(path):
        return {"id": "444444444444444444"} if path.endswith("/users/@me/channels") else 403

    dm, calls = discord(script)
    with pytest.raises(NotifyError) as raised:
        dm.send("hello", "333333333333333333")
    assert "secret body" not in str(raised.value) and raised.value.status_code == 403
    assert len(calls) == 3  # old channel, reopen, new channel: no more
