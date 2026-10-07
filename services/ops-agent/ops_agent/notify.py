"""Notify the approver on Discord (direct message). Stdlib only.

The bot token is a secret: it is never logged, never put in an exception message and
kept out of repr(). Text that came from the model or from public status records is
untrusted, so mentions are disabled and control characters are removed.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

MAX_MESSAGE_CHARS = 1900  # Discord's limit is 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_DISCORD_ID = re.compile(r"\d{17,20}")
_TOKEN = re.compile(r"[A-Za-z0-9._-]{20,200}")
API_BASE = "https://discord.com/api/v10"


class NotifyError(RuntimeError):
    """Delivery failed. The message never contains the token or the request body."""


@dataclass(frozen=True)
class NotifySettings:
    user_id: str = ""
    bot_token: str = field(default="", repr=False)
    api_base: str = API_BASE
    timeout_seconds: float = 8.0

    @property
    def configured(self) -> bool:
        return bool(self.user_id and self.bot_token)

    @classmethod
    def from_env(cls) -> "NotifySettings":
        user_id = os.getenv("OPS_AGENT_DISCORD_USER_ID", "").strip()
        token = os.getenv("OPS_AGENT_DISCORD_BOT_TOKEN", "").strip()
        base = os.getenv("OPS_AGENT_DISCORD_API_BASE", API_BASE).rstrip("/")
        if bool(user_id) != bool(token):
            # One without the other is a typo or an omission, not "notifications off": fail loudly
            # instead of quietly dropping every alert.
            raise ValueError("set both OPS_AGENT_DISCORD_USER_ID and OPS_AGENT_DISCORD_BOT_TOKEN, or neither")
        if user_id and not _DISCORD_ID.fullmatch(user_id):
            raise ValueError("OPS_AGENT_DISCORD_USER_ID must be a Discord user id (17-20 digits)")
        if token and not _TOKEN.fullmatch(token):
            raise ValueError("OPS_AGENT_DISCORD_BOT_TOKEN does not look like a bot token")
        return cls(user_id=user_id, bot_token=token, api_base=base)


def clean(text: object, limit: int) -> str:
    value = _CONTROL.sub("", str(text if text is not None else "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _truncate(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_CHARS else message[: MAX_MESSAGE_CHARS - 1] + "…"


SEVERITY_LABEL = {"none": "情報なし", "info": "情報", "warning": "注意", "critical": "重大"}


def format_analysis(analysis: dict[str, Any], services: list[str], since: str, status_url: str) -> str:
    lines = [
        f"**[ivRooom Status] {SEVERITY_LABEL.get(analysis.get('severity'), '不明')}**: {', '.join(services)}",
        f"継続: {since} から",
        "",
        clean(analysis.get("summary"), 400),
    ]
    causes = analysis.get("suspected_causes") or []
    steps = analysis.get("next_steps") or []
    if causes:
        lines += ["", "**考えられる原因（推測）**"] + [f"- {clean(item, 200)}" for item in causes[:5]]
    if steps:
        lines += ["", "**次の確認**"] + [f"- {clean(item, 200)}" for item in steps[:5]]
    if analysis.get("announcement_recommended"):
        lines += ["", "お知らせの公開を検討してください（公開は管理画面で行います。自動では公開されません）。"]
    lines += ["", f"<{status_url}>", "*AIによる分析です。事実は、ステータスページで確認してください。*"]
    return _truncate("\n".join(lines))


def format_fallback(services: list[str], since: str, reason: str, status_url: str) -> str:
    return _truncate(
        "\n".join(
            [
                f"**[ivRooom Status] 異常を検知**: {', '.join(services)}",
                f"継続: {since} から",
                f"（AIによる分析は行っていません: {clean(reason, 120)}）",
                "",
                f"<{status_url}>",
            ]
        )
    )


def format_reminder(services: list[str], since: str, status_url: str) -> str:
    return _truncate(f"**[ivRooom Status] 継続中**: {', '.join(services)}（{since} から）\n<{status_url}>")


def format_recovered(services: list[str], status_url: str) -> str:
    return _truncate(f"**[ivRooom Status] 回復**: {', '.join(services)}\n<{status_url}>")


Opener = Callable[[urllib.request.Request, float], Any]


def _default_open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https base is enforced below


class DiscordDM:
    def __init__(self, settings: NotifySettings, opener: Optional[Opener] = None) -> None:
        if not settings.configured:
            raise ValueError("discord is not configured")
        local = settings.api_base.startswith(("http://127.0.0.1", "http://localhost"))
        if not settings.api_base.startswith("https://") and not local:
            raise ValueError("discord api base must be https")
        self.settings = settings
        self._open = opener or _default_open

    def _call(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.api_base}{path}",
            data=json.dumps(body).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bot {self.settings.bot_token}",
                "Content-Type": "application/json",
                "User-Agent": "ivrm-ops-agent (https://status.ivrm.jp, 1.0)",
            },
        )
        try:
            with self._open(request, self.settings.timeout_seconds) as response:
                raw = response.read(200_000)
        except urllib.error.HTTPError as exc:  # status only: the body may echo the request
            raise NotifyError(f"discord returned HTTP {exc.code} for {method} {path.split('/')[1]}") from None
        except (OSError, ValueError) as exc:
            raise NotifyError(f"discord request failed: {type(exc).__name__}") from None
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise NotifyError("discord returned a non-json response") from None

    def open_channel(self) -> str:
        channel = self._call("POST", "/users/@me/channels", {"recipient_id": self.settings.user_id}).get("id")
        if not isinstance(channel, str) or not _DISCORD_ID.fullmatch(channel):
            raise NotifyError("discord did not return a channel id")
        return channel

    def send(self, message: str, channel_id: Optional[str] = None) -> str:
        """Send the message and return the DM channel id (cache it to skip the lookup next time)."""
        channel = channel_id or self.open_channel()
        self._call(
            "POST",
            f"/channels/{channel}/messages",
            {"content": _truncate(message), "allowed_mentions": {"parse": []}},
        )
        return channel
