from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SECRET = "test-secret-with-sufficient-length"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    current = tmp_path / "current.json"
    history = tmp_path / "history.json"
    current.write_text(
        json.dumps(
            {
                "collected_at": "2026-07-25T03:00:00+00:00",
                "status": "online",
                "server": {
                    "name": "GT New Horizons",
                    "mode": "GTNH 2.8.4",
                    "connection": "mc.ivrm.jp",
                },
                "players": {"online": 1, "max": 5},
            }
        ),
        encoding="utf-8",
    )
    history.write_text("[]", encoding="utf-8")
    return Settings(
        db_path=tmp_path / "status.db",
        herta_ingest_secret=SECRET,
        max_body_bytes=16_384,
        max_clock_skew_seconds=120,
        replay_ttl_seconds=600,
        rate_limit_per_minute=100,
        history_retention_days=30,
        herta_stale_after_seconds=120,
        minecraft_stale_after_seconds=10_000_000,
        minecraft_current_path=current,
        minecraft_history_path=history,
        public_content_feed_url=None,
        public_content_cache_path=tmp_path / "public-status-content.json",
        enable_docs=False,
    )


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def payload(*, status: str = "operational", checked_at: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "service": {
            "id": "herta-discord-bot",
            "name": "Herta",
            "group": "Discordサービス",
            "type": "discord_bot",
        },
        "status": status,
        "checked_at": checked_at or "2026-07-25T03:00:00+00:00",
        "version": "0.1.0",
        "summary": "正常に稼働しています",
    }


def signed_request(
    data: dict[str, object],
    *,
    secret: str = SECRET,
    timestamp: int | None = None,
    request_id: str | None = None,
    service_id: str = "herta-discord-bot",
    body_override: bytes | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = body_override or json.dumps(
        data, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()
    resolved_timestamp = timestamp if timestamp is not None else int(time.time())
    resolved_request_id = request_id or str(uuid4())
    canonical = "\n".join(
        [
            "POST",
            "/api/internal/status-ingest",
            str(resolved_timestamp),
            resolved_request_id,
            service_id,
            body_hash,
        ]
    )
    signature = hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-IVRM-Service-Id": service_id,
        "X-IVRM-Timestamp": str(resolved_timestamp),
        "X-IVRM-Request-Id": resolved_request_id,
        "X-IVRM-Body-SHA256": body_hash,
        "X-IVRM-Signature": f"v1={signature}",
    }
    return body, headers
