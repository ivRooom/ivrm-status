from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .impact import bucket_statuses
from .minecraft_probe import MinecraftProbeResult, MinecraftStatusProbe
from .models import PublicService, PublicStatus


@dataclass(frozen=True, slots=True)
class MinecraftSource:
    current_path: Path
    history_path: Path
    stale_after_seconds: int
    probe: MinecraftStatusProbe | None = None
    min_impact_seconds: int = 0

    def public_service(self, now: datetime) -> PublicService:
        current = self._read_json(self.current_path)
        history = self._read_json(self.history_path)
        probe_result = self.probe.check() if self.probe else None

        if not isinstance(current, dict):
            return self._service_without_snapshot(probe_result)

        collected_at = self._parse_datetime(current.get("collected_at"))
        raw_status = self._normalize_status(current.get("status"))
        is_stale = (
            collected_at is None
            or (now - collected_at).total_seconds() > self.stale_after_seconds
        )
        status = self._resolve_status(raw_status, is_stale, probe_result)

        server = current.get("server") if isinstance(current.get("server"), dict) else {}
        players = current.get("players") if isinstance(current.get("players"), dict) else {}
        settings = current.get("settings") if isinstance(current.get("settings"), dict) else {}
        history_items = (
            history
            if isinstance(history, list)
            else history.get("history", [])
            if isinstance(history, dict)
            else []
        )
        timeline = self._timeline(history_items, now)
        timeline[-1] = status

        players_online = self._safe_int(players.get("online"), 0)
        players_max = self._safe_int(
            players.get("max"),
            self._safe_int(settings.get("max-players"), 0),
        )
        if probe_result is not None and probe_result.reachable is True:
            if probe_result.players_online is not None:
                players_online = probe_result.players_online
            if probe_result.players_max is not None:
                players_max = probe_result.players_max

        meta: dict[str, Any] = {
            "type": "minecraft",
            "connection": str(server.get("connection") or "mc.ivrm.jp"),
            "playersOnline": players_online,
            "playersMax": players_max,
            "mode": str(server.get("mode") or "Minecraft Server"),
            "probeStatus": self._probe_status(probe_result),
        }
        if probe_result is not None and probe_result.latency_ms is not None:
            meta["latencyMs"] = probe_result.latency_ms
        if probe_result is not None and probe_result.version_name:
            meta["serverVersion"] = probe_result.version_name

        checked_at = (
            probe_result.checked_at
            if probe_result is not None and probe_result.reachable is not None
            else collected_at
        )
        return PublicService(
            id="minecraft-network",
            group="ゲームサービス",
            name="Minecraft Network",
            description=str(server.get("name") or "Minecraftサーバー"),
            status=status,
            checked_at=checked_at,
            last_received_at=collected_at,
            timeline=timeline,
            meta=meta,
        )

    def history_samples(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, PublicStatus]]:
        raw_history = self._read_json(self.history_path)
        items = (
            raw_history
            if isinstance(raw_history, list)
            else raw_history.get("history", [])
            if isinstance(raw_history, dict)
            else []
        )
        samples: list[tuple[datetime, PublicStatus]] = []
        seen: set[str] = set()

        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                collected_at = self._parse_datetime(item.get("collected_at"))
                if collected_at is None or collected_at < start or collected_at > end:
                    continue
                key = collected_at.isoformat()
                seen.add(key)
                samples.append((collected_at, self._normalize_status(item.get("status"))))

        current = self._read_json(self.current_path)
        current_collected_at: datetime | None = None
        current_raw_status = PublicStatus.UNKNOWN
        if isinstance(current, dict):
            current_collected_at = self._parse_datetime(current.get("collected_at"))
            current_raw_status = self._normalize_status(current.get("status"))
            if current_collected_at is not None and start <= current_collected_at <= end:
                key = current_collected_at.isoformat()
                if key not in seen:
                    samples.append((current_collected_at, current_raw_status))

        if self.probe:
            probe_result = self.probe.check()
            if probe_result.reachable is not None and start <= probe_result.checked_at <= end:
                is_stale = (
                    current_collected_at is None
                    or (probe_result.checked_at - current_collected_at).total_seconds()
                    > self.stale_after_seconds
                )
                samples.append(
                    (
                        probe_result.checked_at,
                        self._resolve_status(current_raw_status, is_stale, probe_result),
                    )
                )

        return sorted(samples, key=lambda sample: sample[0])

    def _service_without_snapshot(
        self,
        probe_result: MinecraftProbeResult | None,
    ) -> PublicService:
        status = self._resolve_status(PublicStatus.UNKNOWN, True, probe_result)
        timeline = [PublicStatus.UNKNOWN] * 24
        timeline[-1] = status
        meta: dict[str, Any] = {
            "type": "minecraft",
            "connection": "mc.ivrm.jp",
            "playersOnline": probe_result.players_online
            if probe_result and probe_result.players_online is not None
            else 0,
            "playersMax": probe_result.players_max
            if probe_result and probe_result.players_max is not None
            else 0,
            "mode": "Minecraft Server",
            "probeStatus": self._probe_status(probe_result),
        }
        if probe_result and probe_result.latency_ms is not None:
            meta["latencyMs"] = probe_result.latency_ms
        if probe_result and probe_result.version_name:
            meta["serverVersion"] = probe_result.version_name

        return PublicService(
            id="minecraft-network",
            group="ゲームサービス",
            name="Minecraft Network",
            description="Minecraftサーバー",
            status=status,
            checked_at=probe_result.checked_at
            if probe_result and probe_result.reachable is not None
            else None,
            timeline=timeline,
            meta=meta,
        )

    @staticmethod
    def _resolve_status(
        raw_status: PublicStatus,
        is_stale: bool,
        probe_result: MinecraftProbeResult | None,
    ) -> PublicStatus:
        if raw_status == PublicStatus.MAINTENANCE:
            return PublicStatus.MAINTENANCE
        if probe_result is not None:
            if probe_result.reachable is False:
                return PublicStatus.OUTAGE
            if probe_result.reachable is True:
                return (
                    PublicStatus.DEGRADED
                    if raw_status == PublicStatus.DEGRADED
                    else PublicStatus.OPERATIONAL
                )
        return PublicStatus.UNKNOWN if is_stale else raw_status

    @staticmethod
    def _probe_status(probe_result: MinecraftProbeResult | None) -> str:
        if probe_result is None:
            return "disabled"
        if probe_result.reachable is True:
            return "reachable"
        if probe_result.reachable is False:
            return "unreachable"
        return "indeterminate"

    def _timeline(self, history: Any, now: datetime) -> list[PublicStatus]:
        if not isinstance(history, list):
            return [PublicStatus.UNKNOWN] * 24

        start = now - timedelta(hours=24)
        samples: list[tuple[datetime, PublicStatus]] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            collected_at = self._parse_datetime(item.get("collected_at"))
            if collected_at is None or collected_at < start or collected_at > now:
                continue
            samples.append((collected_at, self._normalize_status(item.get("status"))))

        starts = [start + timedelta(hours=index) for index in range(24)]
        return bucket_statuses(samples, starts, timedelta(hours=1), self.min_impact_seconds, now)

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            if path.stat().st_size > 5_000_000:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _normalize_status(value: Any) -> PublicStatus:
        normalized = str(value or "unknown").lower()
        aliases = {
            "online": PublicStatus.OPERATIONAL,
            "ok": PublicStatus.OPERATIONAL,
            "healthy": PublicStatus.OPERATIONAL,
            "up": PublicStatus.OPERATIONAL,
            "warning": PublicStatus.DEGRADED,
            "partial": PublicStatus.DEGRADED,
            "offline": PublicStatus.OUTAGE,
            "down": PublicStatus.OUTAGE,
            "critical": PublicStatus.OUTAGE,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return PublicStatus(normalized)
        except ValueError:
            return PublicStatus.UNKNOWN

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
