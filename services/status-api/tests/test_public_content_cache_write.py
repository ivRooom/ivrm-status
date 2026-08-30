from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.public_content import FeedPayload, PublicContentSource

FEED_URL = "https://console.ivrm.jp/api/public/status-feed"


def empty_feed(now: datetime) -> bytes:
    return json.dumps(
        {
            "schemaVersion": "1.0",
            "generatedAt": now.isoformat(),
            "incidents": [],
            "maintenance": [],
            "announcements": [],
        }
    ).encode("utf-8")


class CacheWriteFailingSource(PublicContentSource):
    def _write_cache(self, payload: FeedPayload, cached_at: datetime) -> None:
        raise OSError("disk unavailable")


def test_cache_write_failure_does_not_discard_valid_live_feed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 3, 30, tzinfo=UTC)
    source = CacheWriteFailingSource(
        feed_url=FEED_URL,
        cache_path=tmp_path / "unwritable.json",
        timeout_seconds=1.0,
        refresh_seconds=30,
        stale_seconds=600,
        fetch_bytes=lambda _url, _timeout: empty_feed(now),
    )

    snapshot = source.get(now)

    assert snapshot.meta.source == "live"
    assert snapshot.meta.stale is False
    assert snapshot.incidents == []
    assert not (tmp_path / "unwritable.json").exists()
