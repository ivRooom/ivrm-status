from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    herta_ingest_secret: str
    max_body_bytes: int = 16_384
    max_clock_skew_seconds: int = 120
    replay_ttl_seconds: int = 600
    rate_limit_per_minute: int = 10
    history_retention_days: int = 30
    herta_stale_after_seconds: int = 120
    minecraft_stale_after_seconds: int = 300
    minecraft_current_path: Path = Path("/data/minecraft/current.json")
    minecraft_history_path: Path = Path("/data/minecraft/history.json")
    minecraft_probe_connect_host: str | None = None
    minecraft_probe_server_address: str = "mc.ivrm.jp"
    minecraft_probe_port: int = 25_565
    minecraft_probe_timeout_seconds: float = 2.0
    minecraft_probe_cache_seconds: int = 15
    public_content_feed_url: str | None = "https://console.ivrm.jp/api/public/status-feed"
    public_content_cache_path: Path = Path("/data/public-status-content.json")
    public_content_timeout_seconds: float = 1.5
    public_content_refresh_seconds: int = 30
    public_content_stale_seconds: int = 600
    enable_docs: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        probe_host = os.getenv(
            "MINECRAFT_PROBE_CONNECT_HOST",
            "host.docker.internal",
        ).strip()
        content_feed_url = os.getenv(
            "STATUS_PUBLIC_CONTENT_FEED_URL",
            "https://console.ivrm.jp/api/public/status-feed",
        ).strip()
        return cls(
            db_path=Path(os.getenv("STATUS_DB_PATH", "/data/status.db")),
            herta_ingest_secret=os.getenv("HERTA_INGEST_SECRET", ""),
            max_body_bytes=_env_int("INGEST_MAX_BODY_BYTES", 16_384),
            max_clock_skew_seconds=_env_int("INGEST_MAX_CLOCK_SKEW_SECONDS", 120),
            replay_ttl_seconds=_env_int("INGEST_REPLAY_TTL_SECONDS", 600),
            rate_limit_per_minute=_env_int("INGEST_RATE_LIMIT_PER_MINUTE", 10),
            history_retention_days=_env_int("STATUS_HISTORY_RETENTION_DAYS", 30),
            herta_stale_after_seconds=_env_int("HERTA_STALE_AFTER_SECONDS", 120),
            minecraft_stale_after_seconds=_env_int("MINECRAFT_STALE_AFTER_SECONDS", 300),
            minecraft_current_path=Path(
                os.getenv("MINECRAFT_CURRENT_PATH", "/data/minecraft/current.json")
            ),
            minecraft_history_path=Path(
                os.getenv("MINECRAFT_HISTORY_PATH", "/data/minecraft/history.json")
            ),
            minecraft_probe_connect_host=probe_host or None,
            minecraft_probe_server_address=os.getenv(
                "MINECRAFT_PROBE_SERVER_ADDRESS",
                "mc.ivrm.jp",
            ).strip()
            or "mc.ivrm.jp",
            minecraft_probe_port=_env_int("MINECRAFT_PROBE_PORT", 25_565),
            minecraft_probe_timeout_seconds=_env_float(
                "MINECRAFT_PROBE_TIMEOUT_SECONDS",
                2.0,
            ),
            minecraft_probe_cache_seconds=_env_int(
                "MINECRAFT_PROBE_CACHE_SECONDS",
                15,
            ),
            public_content_feed_url=content_feed_url or None,
            public_content_cache_path=Path(
                os.getenv(
                    "STATUS_PUBLIC_CONTENT_CACHE_PATH",
                    "/data/public-status-content.json",
                )
            ),
            public_content_timeout_seconds=_env_float(
                "STATUS_PUBLIC_CONTENT_TIMEOUT_SECONDS",
                1.5,
            ),
            public_content_refresh_seconds=_env_int(
                "STATUS_PUBLIC_CONTENT_REFRESH_SECONDS",
                30,
            ),
            public_content_stale_seconds=_env_int(
                "STATUS_PUBLIC_CONTENT_STALE_SECONDS",
                600,
            ),
            enable_docs=_env_bool("STATUS_ENABLE_DOCS", False),
        )

    def secret_for(self, service_id: str) -> str | None:
        if service_id == "herta-discord-bot":
            return self.herta_ingest_secret or None
        return None
