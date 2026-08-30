from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    AnnouncementKind,
    IncidentImpact,
    IncidentLifecycle,
    IncidentSource,
    MaintenanceState,
    PublicAnnouncement,
    PublicContentMeta,
    PublicIncident,
    PublicIncidentUpdate,
    PublicMaintenance,
)

MAX_FEED_BYTES = 1_048_576
EXPECTED_FEED_HOST = "console.ivrm.jp"
EXPECTED_FEED_PATH = "/api/public/status-feed"
ServiceId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")]
IncidentPublicId = Annotated[str, Field(pattern=r"^INC-[A-F0-9]{12}$")]
MaintenancePublicId = Annotated[str, Field(pattern=r"^MNT-[A-F0-9]{12}$")]
AnnouncementPublicId = Annotated[str, Field(pattern=r"^ANN-[A-F0-9]{12}$")]


class FeedIncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IncidentLifecycle
    message: str = Field(min_length=1, max_length=2_000)
    published_at: datetime = Field(alias="publishedAt")


class FeedIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_id: IncidentPublicId = Field(alias="publicId")
    title: str = Field(min_length=1, max_length=160)
    status: IncidentLifecycle
    impact: IncidentImpact
    affected_service_ids: list[ServiceId] = Field(alias="affectedServiceIds", max_length=32)
    started_at: datetime = Field(alias="startedAt")
    resolved_at: datetime | None = Field(default=None, alias="resolvedAt")
    updated_at: datetime = Field(alias="updatedAt")
    summary: str = Field(min_length=1, max_length=2_000)
    source: IncidentSource
    updates: list[FeedIncidentUpdate] = Field(default_factory=list, max_length=500)


class FeedMaintenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_id: MaintenancePublicId = Field(alias="publicId")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4_000)
    affected_service_ids: list[ServiceId] = Field(alias="affectedServiceIds", max_length=32)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime = Field(alias="endsAt")
    state: MaintenanceState
    updated_at: datetime = Field(alias="updatedAt")


class FeedAnnouncement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_id: AnnouncementPublicId = Field(alias="publicId")
    kind: AnnouncementKind
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4_000)
    affected_service_ids: list[ServiceId] = Field(alias="affectedServiceIds", max_length=32)
    published_at: datetime = Field(alias="publishedAt")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    active: bool


class FeedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    generated_at: datetime = Field(alias="generatedAt")
    incidents: list[FeedIncident] = Field(default_factory=list, max_length=500)
    maintenance: list[FeedMaintenance] = Field(default_factory=list, max_length=500)
    announcements: list[FeedAnnouncement] = Field(default_factory=list, max_length=500)


class CachedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cached_at: datetime
    feed: FeedPayload


@dataclass(frozen=True, slots=True)
class PublicContentSnapshot:
    incidents: list[PublicIncident]
    maintenance: list[PublicMaintenance]
    announcements: list[PublicAnnouncement]
    meta: PublicContentMeta


FetchBytes = Callable[[str, float], bytes]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("public content timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _validate_feed_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != EXPECTED_FEED_HOST
        or parsed.port not in (None, 443)
        or parsed.path != EXPECTED_FEED_PATH
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("STATUS_PUBLIC_CONTENT_FEED_URL is not an allowed public feed URL")
    return url


def _default_fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "ivrm-status-api/public-content-v1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"public content feed returned HTTP {response.status}")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_FEED_BYTES:
            raise ValueError("public content feed is too large")
        body = response.read(MAX_FEED_BYTES + 1)
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("public content feed is too large")
    return body


def _to_snapshot(
    feed: FeedPayload,
    *,
    source: Literal["live", "cache"],
    fetched_at: datetime,
    now: datetime,
    stale_seconds: int,
) -> PublicContentSnapshot:
    generated_at = _ensure_utc(feed.generated_at)
    fetched = _ensure_utc(fetched_at)
    resolved_now = _ensure_utc(now)
    stale = max(
        (resolved_now - generated_at).total_seconds(),
        (resolved_now - fetched).total_seconds(),
    ) > stale_seconds
    return PublicContentSnapshot(
        incidents=[
            PublicIncident(
                public_id=item.public_id,
                title=item.title,
                status=item.status,
                impact=item.impact,
                affected_service_ids=list(item.affected_service_ids),
                started_at=_ensure_utc(item.started_at),
                resolved_at=_ensure_utc(item.resolved_at) if item.resolved_at else None,
                updated_at=_ensure_utc(item.updated_at),
                summary=item.summary,
                source=item.source,
                updates=[
                    PublicIncidentUpdate(
                        status=update.status,
                        message=update.message,
                        published_at=_ensure_utc(update.published_at),
                    )
                    for update in item.updates
                ],
            )
            for item in feed.incidents
        ],
        maintenance=[
            PublicMaintenance(
                public_id=item.public_id,
                title=item.title,
                summary=item.summary,
                affected_service_ids=list(item.affected_service_ids),
                starts_at=_ensure_utc(item.starts_at),
                ends_at=_ensure_utc(item.ends_at),
                state=item.state,
                updated_at=_ensure_utc(item.updated_at),
            )
            for item in feed.maintenance
        ],
        announcements=[
            PublicAnnouncement(
                public_id=item.public_id,
                kind=item.kind,
                title=item.title,
                body=item.body,
                affected_service_ids=list(item.affected_service_ids),
                published_at=_ensure_utc(item.published_at),
                expires_at=_ensure_utc(item.expires_at) if item.expires_at else None,
                active=item.active,
            )
            for item in feed.announcements
        ],
        meta=PublicContentMeta(
            source=source,
            generated_at=generated_at,
            fetched_at=fetched,
            stale=stale,
        ),
    )


class PublicContentSource:
    def __init__(
        self,
        *,
        feed_url: str | None,
        cache_path: Path,
        timeout_seconds: float,
        refresh_seconds: int,
        stale_seconds: int,
        fetch_bytes: FetchBytes | None = None,
    ) -> None:
        self.feed_url = _validate_feed_url(feed_url) if feed_url else None
        self.cache_path = cache_path
        self.timeout_seconds = timeout_seconds
        self.refresh_seconds = refresh_seconds
        self.stale_seconds = stale_seconds
        self.fetch_bytes = fetch_bytes or _default_fetch
        self._lock = threading.Lock()
        self._last_attempt_monotonic = float("-inf")
        self._snapshot: PublicContentSnapshot | None = None

    def get(self, now: datetime | None = None) -> PublicContentSnapshot:
        resolved_now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            elapsed = time.monotonic() - self._last_attempt_monotonic
            if self._snapshot is not None and elapsed < self.refresh_seconds:
                return self._snapshot
            self._last_attempt_monotonic = time.monotonic()

            if self.feed_url:
                try:
                    body = self.fetch_bytes(self.feed_url, self.timeout_seconds)
                    payload = FeedPayload.model_validate_json(body)
                    snapshot = _to_snapshot(
                        payload,
                        source="live",
                        fetched_at=resolved_now,
                        now=resolved_now,
                        stale_seconds=self.stale_seconds,
                    )
                except Exception:
                    snapshot = None
                if snapshot is not None:
                    try:
                        self._write_cache(payload, resolved_now)
                    except OSError:
                        pass
                    self._snapshot = snapshot
                    return snapshot

            cached = self._read_cache(resolved_now)
            if cached is not None:
                self._snapshot = cached
                return cached

            self._snapshot = PublicContentSnapshot(
                incidents=[],
                maintenance=[],
                announcements=[],
                meta=PublicContentMeta(source="none"),
            )
            return self._snapshot

    def _read_cache(self, now: datetime) -> PublicContentSnapshot | None:
        try:
            body = self.cache_path.read_bytes()
            if len(body) > MAX_FEED_BYTES + 32_768:
                return None
            cached = CachedPayload.model_validate_json(body)
            return _to_snapshot(
                cached.feed,
                source="cache",
                fetched_at=_ensure_utc(cached.cached_at),
                now=now,
                stale_seconds=self.stale_seconds,
            )
        except (OSError, ValueError):
            return None

    def _write_cache(self, payload: FeedPayload, cached_at: datetime) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        content = CachedPayload(
            cached_at=cached_at,
            feed=payload,
        ).model_dump_json(by_alias=True).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.cache_path.name}.",
            dir=self.cache_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.cache_path)
        finally:
            temporary_path.unlink(missing_ok=True)
