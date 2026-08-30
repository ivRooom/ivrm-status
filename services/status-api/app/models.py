from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PublicStatus(StrEnum):
    OPERATIONAL = "operational"
    MAINTENANCE = "maintenance"
    DEGRADED = "degraded"
    OUTAGE = "outage"
    UNKNOWN = "unknown"


STATUS_PRIORITY: dict[PublicStatus, int] = {
    PublicStatus.OPERATIONAL: 0,
    PublicStatus.MAINTENANCE: 1,
    PublicStatus.DEGRADED: 2,
    PublicStatus.OUTAGE: 3,
    PublicStatus.UNKNOWN: 4,
}


def worst_status(values: list[PublicStatus]) -> PublicStatus:
    if not values:
        return PublicStatus.UNKNOWN
    return max(values, key=lambda status: STATUS_PRIORITY[status])


class IncidentLifecycle(StrEnum):
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"


class IncidentImpact(StrEnum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class IncidentSource(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class MaintenanceState(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AnnouncementKind(StrEnum):
    INFO = "info"
    WARNING = "warning"


class IngestService(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    name: str = Field(min_length=1, max_length=80)
    group: str = Field(min_length=1, max_length=80)
    type: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,63}$")


class IngestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    service: IngestService
    status: PublicStatus
    checked_at: datetime
    version: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=160)

    @field_validator("checked_at")
    @classmethod
    def checked_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_registered_service_shape(self) -> "IngestPayload":
        if self.service.id == "herta-discord-bot":
            expected = {
                "name": "Herta",
                "group": "Discordサービス",
                "type": "discord_bot",
            }
            actual = {
                "name": self.service.name,
                "group": self.service.group,
                "type": self.service.type,
            }
            if actual != expected:
                raise ValueError("service metadata does not match the registered service")
        return self


class PublicIncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IncidentLifecycle
    message: str = Field(min_length=1, max_length=2_000)
    published_at: datetime


class PublicIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_id: str = Field(pattern=r"^INC-[A-F0-9]{12}$")
    title: str = Field(min_length=1, max_length=160)
    status: IncidentLifecycle
    impact: IncidentImpact
    affected_service_ids: list[str] = Field(max_length=32)
    started_at: datetime
    resolved_at: datetime | None = None
    updated_at: datetime
    summary: str = Field(min_length=1, max_length=2_000)
    source: IncidentSource
    updates: list[PublicIncidentUpdate] = Field(default_factory=list, max_length=500)

    @field_validator("affected_service_ids")
    @classmethod
    def validate_service_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("affected_service_ids must be unique")
        for service_id in value:
            if not service_id or len(service_id) > 64:
                raise ValueError("affected service id is invalid")
            if not service_id[0].isalnum() or not all(
                character.islower() or character.isdigit() or character == "-"
                for character in service_id
            ):
                raise ValueError("affected service id is invalid")
        return value


class PublicMaintenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_id: str = Field(pattern=r"^MNT-[A-F0-9]{12}$")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4_000)
    affected_service_ids: list[str] = Field(max_length=32)
    starts_at: datetime
    ends_at: datetime
    state: MaintenanceState
    updated_at: datetime


class PublicAnnouncement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_id: str = Field(pattern=r"^ANN-[A-F0-9]{12}$")
    kind: AnnouncementKind
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4_000)
    affected_service_ids: list[str] = Field(default_factory=list, max_length=32)
    published_at: datetime
    expires_at: datetime | None = None
    active: bool


class PublicTimelineBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_at: datetime
    end_at: datetime
    status: PublicStatus
    related_incident_ids: list[str] = Field(default_factory=list, max_length=32)
    summary: str | None = Field(default=None, max_length=2_000)


class PublicService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: str
    name: str
    description: str
    status: PublicStatus
    checked_at: datetime | None = None
    last_received_at: datetime | None = None
    timeline: list[PublicStatus]
    timeline_details: list[PublicTimelineBucket] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class PublicContentMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=r"^(live|cache|none)$")
    generated_at: datetime | None = None
    fetched_at: datetime | None = None
    stale: bool = False


class PublicStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    overall_status: PublicStatus
    services: list[PublicService]
    incidents: list[PublicIncident] = Field(default_factory=list)
    maintenance: list[PublicMaintenance] = Field(default_factory=list)
    announcements: list[PublicAnnouncement] = Field(default_factory=list)
    content_meta: PublicContentMeta = Field(
        default_factory=lambda: PublicContentMeta(source="none")
    )


class PublicHistoryDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    status: PublicStatus
    samples: int = Field(ge=0)
    availability_percent: float | None = Field(default=None, ge=0, le=100)


class PublicHistoryService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: str
    name: str
    description: str
    current_status: PublicStatus
    availability_percent: float | None = Field(default=None, ge=0, le=100)
    days: list[PublicHistoryDay]


class PublicHistoryRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(ge=1, le=30)
    from_date: date
    to_date: date


class PublicHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    range: PublicHistoryRange
    services: list[PublicHistoryService]
    incidents: list[PublicIncident] = Field(default_factory=list)
    maintenance: list[PublicMaintenance] = Field(default_factory=list)
    announcements: list[PublicAnnouncement] = Field(default_factory=list)
    content_meta: PublicContentMeta = Field(
        default_factory=lambda: PublicContentMeta(source="none")
    )
