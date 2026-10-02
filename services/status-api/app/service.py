from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

from .config import Settings
from .db import Snapshot, StatusRepository
from .impact import bucket_statuses, significant_impacts
from .minecraft import MinecraftSource
from .minecraft_probe import MinecraftStatusProbe
from .models import (
    PublicAnnouncement,
    PublicHistoryDay,
    PublicHistoryRange,
    PublicHistoryResponse,
    PublicHistoryService,
    PublicIncident,
    PublicMaintenance,
    PublicService,
    PublicStatus,
    PublicStatusResponse,
    PublicTimelineBucket,
    worst_status,
)
from .public_content import PublicContentSource

# Daily history is aggregated on Japan calendar days. JST has no DST, so a fixed
# offset avoids depending on tzdata being present in the container image.
HISTORY_TIMEZONE_NAME = "Asia/Tokyo"
HISTORY_TIMEZONE = timezone(timedelta(hours=9), "JST")


class StatusService:
    def __init__(self, settings: Settings, repository: StatusRepository) -> None:
        self.settings = settings
        self.repository = repository
        minecraft_probe = (
            MinecraftStatusProbe(
                connect_host=settings.minecraft_probe_connect_host,
                server_address=settings.minecraft_probe_server_address,
                port=settings.minecraft_probe_port,
                timeout_seconds=settings.minecraft_probe_timeout_seconds,
                cache_seconds=settings.minecraft_probe_cache_seconds,
            )
            if settings.minecraft_probe_connect_host
            else None
        )
        self.minecraft = MinecraftSource(
            current_path=settings.minecraft_current_path,
            history_path=settings.minecraft_history_path,
            stale_after_seconds=settings.minecraft_stale_after_seconds,
            probe=minecraft_probe,
            min_impact_seconds=settings.history_min_impact_seconds,
        )
        self.public_content = PublicContentSource(
            feed_url=settings.public_content_feed_url,
            cache_path=settings.public_content_cache_path,
            timeout_seconds=settings.public_content_timeout_seconds,
            refresh_seconds=settings.public_content_refresh_seconds,
            stale_seconds=settings.public_content_stale_seconds,
        )

    def public_status(self, now: datetime | None = None) -> PublicStatusResponse:
        generated_at = (now or datetime.now(UTC)).astimezone(UTC)
        content = self.public_content.get(generated_at)
        services = [
            self.minecraft.public_service(generated_at),
            self._herta_service(generated_at),
        ]
        services = [
            service.model_copy(
                update={
                    "timeline_details": self._timeline_details(
                        service.id,
                        service.timeline,
                        generated_at,
                        content.incidents,
                    )
                }
            )
            for service in services
        ]
        return PublicStatusResponse(
            generated_at=generated_at,
            overall_status=worst_status([service.status for service in services]),
            services=services,
            incidents=content.incidents,
            maintenance=content.maintenance,
            announcements=content.announcements,
            content_meta=content.meta,
        )

    def public_history(
        self,
        days: int = 30,
        now: datetime | None = None,
    ) -> PublicHistoryResponse:
        if not 1 <= days <= 30:
            raise ValueError("days must be between 1 and 30")

        generated_at = (now or datetime.now(UTC)).astimezone(UTC)
        today = generated_at.astimezone(HISTORY_TIMEZONE).date()
        start_date = today - timedelta(days=days - 1)
        # Keep the bound in UTC: snapshots_since compares ISO strings in SQLite.
        start = datetime.combine(start_date, time.min, tzinfo=HISTORY_TIMEZONE).astimezone(UTC)
        current_status = self.public_status(generated_at)
        current_services = {service.id: service for service in current_status.services}

        minecraft = current_services["minecraft-network"]
        herta = current_services["herta-discord-bot"]
        minecraft_days, minecraft_availability = self._daily_history(
            self.minecraft.history_samples(start, generated_at),
            start_date,
            days,
            self.settings.history_min_impact_seconds,
            generated_at,
        )
        herta_days, herta_availability = self._daily_history(
            [
                (snapshot.received_at.astimezone(UTC), snapshot.status)
                for snapshot in self.repository.snapshots_since("herta-discord-bot", start)
                if snapshot.received_at.astimezone(UTC) <= generated_at
            ],
            start_date,
            days,
            self.settings.history_min_impact_seconds,
            generated_at,
        )

        return PublicHistoryResponse(
            generated_at=generated_at,
            range=PublicHistoryRange(
                days=days,
                from_date=start_date,
                to_date=today,
                timezone=HISTORY_TIMEZONE_NAME,
                min_impact_seconds=self.settings.history_min_impact_seconds,
            ),
            services=[
                PublicHistoryService(
                    id=minecraft.id,
                    group=minecraft.group,
                    name=minecraft.name,
                    description=minecraft.description,
                    current_status=minecraft.status,
                    availability_percent=minecraft_availability,
                    days=minecraft_days,
                ),
                PublicHistoryService(
                    id=herta.id,
                    group=herta.group,
                    name=herta.name,
                    description=herta.description,
                    current_status=herta.status,
                    availability_percent=herta_availability,
                    days=herta_days,
                ),
            ],
            incidents=self._history_incidents(current_status.incidents, start, generated_at),
            maintenance=self._history_maintenance(current_status.maintenance, start, generated_at),
            announcements=self._history_announcements(
                current_status.announcements,
                start,
                generated_at,
            ),
            content_meta=current_status.content_meta,
        )

    def _herta_service(self, now: datetime) -> PublicService:
        latest = self.repository.latest_snapshot("herta-discord-bot")
        since = now - timedelta(hours=24)
        timeline = self._timeline(
            self.repository.snapshots_since("herta-discord-bot", since),
            since,
            now,
            self.settings.history_min_impact_seconds,
        )

        if latest is None:
            return PublicService(
                id="herta-discord-bot",
                group="Discordサービス",
                name="Herta",
                description="ivRooom Discord Bot",
                status=PublicStatus.UNKNOWN,
                timeline=timeline,
                meta={"type": "discord_bot"},
            )

        age_seconds = (now - latest.received_at.astimezone(UTC)).total_seconds()
        status = (
            PublicStatus.UNKNOWN
            if age_seconds > self.settings.herta_stale_after_seconds
            else latest.status
        )
        meta: dict[str, str] = {"type": "discord_bot"}
        if latest.version:
            meta["version"] = latest.version

        return PublicService(
            id="herta-discord-bot",
            group="Discordサービス",
            name="Herta",
            description="ivRooom Discord Bot",
            status=status,
            checked_at=latest.checked_at,
            last_received_at=latest.received_at,
            timeline=timeline,
            meta=meta,
        )

    @staticmethod
    def _timeline(
        snapshots: list[Snapshot],
        start: datetime,
        end: datetime,
        min_impact_seconds: int = 0,
    ) -> list[PublicStatus]:
        samples = [
            (snapshot.received_at.astimezone(UTC), snapshot.status)
            for snapshot in snapshots
            if start <= snapshot.received_at.astimezone(UTC) <= end
        ]
        starts = [start + timedelta(hours=index) for index in range(24)]
        return bucket_statuses(samples, starts, timedelta(hours=1), min_impact_seconds, end)

    @staticmethod
    def _timeline_details(
        service_id: str,
        timeline: list[PublicStatus],
        end: datetime,
        incidents: list[PublicIncident],
    ) -> list[PublicTimelineBucket]:
        bucket_count = len(timeline)
        if bucket_count == 0:
            return []
        start = end - timedelta(hours=bucket_count)
        result: list[PublicTimelineBucket] = []
        for index, status in enumerate(timeline):
            bucket_start = start + timedelta(hours=index)
            bucket_end = min(end, bucket_start + timedelta(hours=1))
            related = [
                incident
                for incident in incidents
                if service_id in incident.affected_service_ids
                and incident.started_at.astimezone(UTC) < bucket_end
                and (
                    incident.resolved_at.astimezone(UTC)
                    if incident.resolved_at
                    else end
                ) > bucket_start
            ]
            related.sort(key=lambda item: item.updated_at, reverse=True)
            result.append(
                PublicTimelineBucket(
                    start_at=bucket_start,
                    end_at=bucket_end,
                    status=status,
                    related_incident_ids=[item.public_id for item in related[:32]],
                    summary=related[0].summary if related else None,
                )
            )
        return result

    @staticmethod
    def _history_incidents(
        incidents: list[PublicIncident],
        start: datetime,
        end: datetime,
    ) -> list[PublicIncident]:
        return [
            incident
            for incident in incidents
            if incident.started_at.astimezone(UTC) <= end
            and (
                incident.resolved_at.astimezone(UTC)
                if incident.resolved_at
                else end
            ) >= start
        ]

    @staticmethod
    def _history_maintenance(
        maintenance: list[PublicMaintenance],
        start: datetime,
        end: datetime,
    ) -> list[PublicMaintenance]:
        return [
            item
            for item in maintenance
            if item.starts_at.astimezone(UTC) <= end
            and item.ends_at.astimezone(UTC) >= start
        ]

    @staticmethod
    def _history_announcements(
        announcements: list[PublicAnnouncement],
        start: datetime,
        end: datetime,
    ) -> list[PublicAnnouncement]:
        return [
            item
            for item in announcements
            if start <= item.published_at.astimezone(UTC) <= end
        ]

    @staticmethod
    def _daily_history(
        samples: list[tuple[datetime, PublicStatus]],
        start_date: date,
        days: int,
        min_impact_seconds: int = 0,
        end: datetime | None = None,
    ) -> tuple[list[PublicHistoryDay], float | None]:
        """Daily status on Japan calendar days.

        A day is marked impacted only by an outage/degradation/maintenance run that
        lasted at least min_impact_seconds, so brief restarts do not turn a whole day
        red. availability_percent is still the plain share of operational samples.
        """
        buckets: dict[date, list[PublicStatus]] = {
            start_date + timedelta(days=index): [] for index in range(days)
        }
        for recorded_at, status in samples:
            sample_date = recorded_at.astimezone(HISTORY_TIMEZONE).date()
            if sample_date in buckets:
                buckets[sample_date].append(status)

        impacted: dict[date, list[PublicStatus]] = {day: [] for day in buckets}
        for run_start, run_end, status in significant_impacts(
            samples,
            min_impact_seconds,
            end or max((at for at, _ in samples), default=datetime.now(UTC)),
        ):
            first = run_start.astimezone(HISTORY_TIMEZONE).date()
            last = run_end.astimezone(HISTORY_TIMEZONE).date()
            day = first
            while day <= last:
                if day in impacted:
                    impacted[day].append(status)
                day += timedelta(days=1)

        result: list[PublicHistoryDay] = []
        operational_total = 0
        known_total = 0
        for day, values in buckets.items():
            known = [status for status in values if status != PublicStatus.UNKNOWN]
            operational = sum(status == PublicStatus.OPERATIONAL for status in known)
            day_availability = round((operational / len(known)) * 100, 1) if known else None
            result.append(
                PublicHistoryDay(
                    date=day,
                    status=(
                        worst_status(impacted[day])
                        if impacted[day]
                        else PublicStatus.OPERATIONAL
                        if known
                        else PublicStatus.UNKNOWN
                    ),
                    samples=len(values),
                    availability_percent=day_availability,
                )
            )
            operational_total += operational
            known_total += len(known)

        availability = (
            round((operational_total / known_total) * 100, 2)
            if known_total
            else None
        )
        return result, availability
