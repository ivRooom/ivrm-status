from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.db import StatusRepository
from app.models import (
    IncidentImpact,
    IncidentLifecycle,
    IncidentSource,
    PublicContentMeta,
    PublicIncident,
    PublicIncidentUpdate,
)
from app.public_content import PublicContentSnapshot, PublicContentSource
from app.service import StatusService

FEED_URL = "https://console.ivrm.jp/api/public/status-feed"


def feed_bytes(*, generated_at: datetime, extra_incident: dict[str, object] | None = None) -> bytes:
    incident: dict[str, object] = {
        "publicId": "INC-ABCDEF123456",
        "title": "Minecraft Network 接続障害",
        "status": "identified",
        "impact": "major",
        "affectedServiceIds": ["minecraft-network"],
        "startedAt": (generated_at - timedelta(minutes=20)).isoformat(),
        "resolvedAt": None,
        "updatedAt": (generated_at - timedelta(minutes=5)).isoformat(),
        "summary": "公開Ping失敗の原因を特定しました",
        "source": "manual",
        "updates": [
            {
                "status": "investigating",
                "message": "接続障害を調査しています",
                "publishedAt": (generated_at - timedelta(minutes=20)).isoformat(),
            },
            {
                "status": "identified",
                "message": "公開Ping失敗の原因を特定しました",
                "publishedAt": (generated_at - timedelta(minutes=5)).isoformat(),
            },
        ],
    }
    if extra_incident:
        incident.update(extra_incident)
    return json.dumps(
        {
            "schemaVersion": "1.0",
            "generatedAt": generated_at.isoformat(),
            "incidents": [incident],
            "maintenance": [],
            "announcements": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def test_live_feed_is_validated_and_written_to_cache(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)
    calls: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append((url, timeout))
        return feed_bytes(generated_at=now)

    cache_path = tmp_path / "public-content.json"
    source = PublicContentSource(
        feed_url=FEED_URL,
        cache_path=cache_path,
        timeout_seconds=1.5,
        refresh_seconds=30,
        stale_seconds=600,
        fetch_bytes=fetch,
    )

    snapshot = source.get(now)

    assert calls == [(FEED_URL, 1.5)]
    assert snapshot.meta.source == "live"
    assert snapshot.meta.stale is False
    assert snapshot.incidents[0].public_id == "INC-ABCDEF123456"
    assert snapshot.incidents[0].affected_service_ids == ["minecraft-network"]
    assert cache_path.exists()
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["feed"]["schemaVersion"] == "1.0"
    assert "sourceRef" not in json.dumps(cached)


def test_feed_failure_falls_back_to_last_known_good_cache(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)
    cache_path = tmp_path / "public-content.json"
    live = PublicContentSource(
        feed_url=FEED_URL,
        cache_path=cache_path,
        timeout_seconds=1.0,
        refresh_seconds=30,
        stale_seconds=600,
        fetch_bytes=lambda _url, _timeout: feed_bytes(generated_at=now),
    )
    assert live.get(now).meta.source == "live"

    def failing_fetch(_url: str, _timeout: float) -> bytes:
        raise OSError("upstream unavailable")

    fallback = PublicContentSource(
        feed_url=FEED_URL,
        cache_path=cache_path,
        timeout_seconds=1.0,
        refresh_seconds=30,
        stale_seconds=600,
        fetch_bytes=failing_fetch,
    )
    snapshot = fallback.get(now + timedelta(minutes=11))

    assert snapshot.meta.source == "cache"
    assert snapshot.meta.stale is True
    assert snapshot.incidents[0].summary == "公開Ping失敗の原因を特定しました"


def test_invalid_feed_does_not_replace_valid_cache(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)
    cache_path = tmp_path / "public-content.json"
    live = PublicContentSource(
        feed_url=FEED_URL,
        cache_path=cache_path,
        timeout_seconds=1.0,
        refresh_seconds=30,
        stale_seconds=600,
        fetch_bytes=lambda _url, _timeout: feed_bytes(generated_at=now),
    )
    live.get(now)
    before = cache_path.read_bytes()

    invalid = PublicContentSource(
        feed_url=FEED_URL,
        cache_path=cache_path,
        timeout_seconds=1.0,
        refresh_seconds=30,
        stale_seconds=600,
        fetch_bytes=lambda _url, _timeout: feed_bytes(
            generated_at=now,
            extra_incident={"internalSecret": "must-not-be-accepted"},
        ),
    )
    snapshot = invalid.get(now + timedelta(minutes=1))

    assert snapshot.meta.source == "cache"
    assert cache_path.read_bytes() == before


def test_missing_or_corrupt_cache_returns_empty_content(tmp_path: Path) -> None:
    cache_path = tmp_path / "public-content.json"
    cache_path.write_text("{broken", encoding="utf-8")
    source = PublicContentSource(
        feed_url=None,
        cache_path=cache_path,
        timeout_seconds=1.0,
        refresh_seconds=30,
        stale_seconds=600,
    )

    snapshot = source.get(datetime(2026, 8, 30, 3, 0, tzinfo=UTC))

    assert snapshot.meta.source == "none"
    assert snapshot.incidents == []
    assert snapshot.maintenance == []
    assert snapshot.announcements == []


def test_public_feed_url_is_strictly_allowlisted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allowed public feed URL"):
        PublicContentSource(
            feed_url="https://example.com/api/public/status-feed",
            cache_path=tmp_path / "cache.json",
            timeout_seconds=1.0,
            refresh_seconds=30,
            stale_seconds=600,
        )


def test_incident_is_associated_with_timeline_without_overriding_overall_status(
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    repository = StatusRepository(settings.db_path)
    repository.initialize(settings.herta_stale_after_seconds)
    service = StatusService(settings, repository)
    baseline = service.public_status(now).overall_status
    incident = PublicIncident(
        public_id="INC-ABCDEF123456",
        title="Minecraft Network 接続障害",
        status=IncidentLifecycle.IDENTIFIED,
        impact=IncidentImpact.MAJOR,
        affected_service_ids=["minecraft-network"],
        started_at=now - timedelta(minutes=20),
        resolved_at=None,
        updated_at=now - timedelta(minutes=5),
        summary="公開Ping失敗の原因を特定しました",
        source=IncidentSource.MANUAL,
        updates=[
            PublicIncidentUpdate(
                status=IncidentLifecycle.IDENTIFIED,
                message="公開Ping失敗の原因を特定しました",
                published_at=now - timedelta(minutes=5),
            )
        ],
    )

    class FixedContent:
        def get(self, _now: datetime) -> PublicContentSnapshot:
            return PublicContentSnapshot(
                incidents=[incident],
                maintenance=[],
                announcements=[],
                meta=PublicContentMeta(
                    source="live",
                    generated_at=now,
                    fetched_at=now,
                    stale=False,
                ),
            )

    service.public_content = FixedContent()  # type: ignore[assignment]
    result = service.public_status(now)
    minecraft = next(item for item in result.services if item.id == "minecraft-network")

    assert result.overall_status == baseline
    assert any(
        "INC-ABCDEF123456" in bucket.related_incident_ids
        for bucket in minecraft.timeline_details
    )
    related = [
        bucket for bucket in minecraft.timeline_details
        if "INC-ABCDEF123456" in bucket.related_incident_ids
    ]
    assert related[-1].summary == "公開Ping失敗の原因を特定しました"
    herta = next(item for item in result.services if item.id == "herta-discord-bot")
    assert all(not bucket.related_incident_ids for bucket in herta.timeline_details)
