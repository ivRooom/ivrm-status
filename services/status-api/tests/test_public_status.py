from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import StatusRepository
from app.main import create_app
from app.models import IngestPayload
from app.service import StatusService
from conftest import payload, signed_request


def test_public_status_contains_minecraft_and_unknown_herta(client: TestClient) -> None:
    response = client.get("/api/status.json")
    assert response.status_code == 200
    data = response.json()
    service_ids = {service["id"] for service in data["services"]}
    assert service_ids == {"minecraft-network", "herta-discord-bot"}
    herta = next(service for service in data["services"] if service["id"] == "herta-discord-bot")
    assert herta["status"] == "unknown"
    assert "checks" not in herta


def test_ingested_herta_is_exposed_without_internal_checks(client: TestClient) -> None:
    body, headers = signed_request(payload())
    assert client.post("/api/internal/status-ingest", content=body, headers=headers).status_code == 202
    data = client.get("/api/status.json").json()
    herta = next(service for service in data["services"] if service["id"] == "herta-discord-bot")
    assert herta["status"] == "operational"
    assert herta["meta"] == {"type": "discord_bot", "version": "0.1.0"}
    serialized = json.dumps(data)
    assert "database" not in serialized
    assert "redis" not in serialized
    assert "worker" not in serialized


def test_public_history_defaults_to_thirty_days(client: TestClient) -> None:
    response = client.get("/api/status-history.json")
    assert response.status_code == 200
    data = response.json()
    assert data["range"]["days"] == 30
    assert {service["id"] for service in data["services"]} == {
        "minecraft-network",
        "herta-discord-bot",
    }
    assert all(len(service["days"]) == 30 for service in data["services"])
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_public_history_accepts_range_and_includes_ingest(client: TestClient) -> None:
    body, headers = signed_request(payload())
    assert client.post("/api/internal/status-ingest", content=body, headers=headers).status_code == 202

    response = client.get("/api/status-history.json?days=7")
    assert response.status_code == 200
    data = response.json()
    assert data["range"]["days"] == 7
    herta = next(service for service in data["services"] if service["id"] == "herta-discord-bot")
    assert len(herta["days"]) == 7
    assert herta["availability_percent"] == 100.0
    assert herta["days"][-1]["status"] == "operational"
    assert herta["days"][-1]["samples"] == 1


def test_public_history_rejects_invalid_range(client: TestClient) -> None:
    assert client.get("/api/status-history.json?days=0").status_code == 400
    assert client.get("/api/status-history.json?days=31").status_code == 400


def test_herta_becomes_unknown_when_stale(settings: Settings) -> None:
    repository = StatusRepository(settings.db_path)
    repository.initialize(settings.herta_stale_after_seconds)
    now = datetime.now(UTC)
    old = now - timedelta(seconds=settings.herta_stale_after_seconds + 1)
    repository.save_ingest(
        IngestPayload.model_validate(payload(checked_at=old.isoformat())),
        request_id="22222222-2222-4222-8222-222222222222",
        received_at=old,
        replay_ttl_seconds=settings.replay_ttl_seconds,
        history_retention_days=settings.history_retention_days,
    )
    result = StatusService(settings, repository).public_status(now)
    herta = next(service for service in result.services if service.id == "herta-discord-bot")
    assert herta.status.value == "unknown"


def test_corrupt_minecraft_does_not_break_herta(settings: Settings) -> None:
    settings.minecraft_current_path.write_text("{broken", encoding="utf-8")
    app = create_app(settings)
    client = TestClient(app)
    body, headers = signed_request(payload())
    assert client.post("/api/internal/status-ingest", content=body, headers=headers).status_code == 202
    response = client.get("/api/status.json")
    assert response.status_code == 200
    statuses = {service["id"]: service["status"] for service in response.json()["services"]}
    assert statuses["minecraft-network"] == "unknown"
    assert statuses["herta-discord-bot"] == "operational"


def test_healthz_checks_sqlite(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_public_history_groups_days_by_japan_calendar_date(settings: Settings) -> None:
    repository = StatusRepository(settings.db_path)
    repository.initialize(settings.herta_stale_after_seconds)
    # 2026-10-01 16:30 UTC is 2026-10-02 01:30 JST.
    sample_at = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
    now = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)
    repository.save_ingest(
        IngestPayload.model_validate(payload(status="outage", checked_at=sample_at.isoformat())),
        request_id="33333333-3333-4333-8333-333333333333",
        received_at=sample_at,
        replay_ttl_seconds=settings.replay_ttl_seconds,
        history_retention_days=settings.history_retention_days,
    )

    result = StatusService(settings, repository).public_history(days=2, now=now)

    assert result.range.timezone == "Asia/Tokyo"
    assert str(result.range.to_date) == "2026-10-02"
    herta = next(service for service in result.services if service.id == "herta-discord-bot")
    days = {str(day.date): day for day in herta.days}
    assert days["2026-10-02"].status.value == "outage"
    assert days["2026-10-02"].samples == 1
    assert days["2026-10-01"].samples == 0


def _run(samples, *, min_seconds=300, days=2, end=None):
    from datetime import date

    from app.models import PublicStatus

    start_date = date(2026, 10, 1)
    result, availability = StatusService._daily_history(
        [(at, PublicStatus(status)) for at, status in samples],
        start_date,
        days,
        min_seconds,
        end or samples[-1][0] + timedelta(minutes=1),
    )
    return {str(day.date): day for day in result}, availability


def _every_minute(start, minutes, status):
    return [(start + timedelta(minutes=offset), status) for offset in range(minutes)]


def test_brief_restart_does_not_mark_the_day_impacted() -> None:
    base = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # 09:00 JST
    samples = (
        _every_minute(base, 60, "operational")
        + _every_minute(base + timedelta(minutes=60), 2, "outage")  # ~2 min restart
        + _every_minute(base + timedelta(minutes=62), 60, "operational")
    )
    days, availability = _run(samples)
    assert days["2026-10-01"].status.value == "operational"
    # availability keeps reporting the real share of operational samples
    assert availability is not None and availability < 100


def test_sustained_outage_marks_the_day() -> None:
    base = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    samples = (
        _every_minute(base, 30, "operational")
        + _every_minute(base + timedelta(minutes=30), 8, "outage")
        + _every_minute(base + timedelta(minutes=38), 30, "operational")
    )
    days, _ = _run(samples)
    assert days["2026-10-01"].status.value == "outage"


def test_outage_crossing_japan_midnight_marks_both_days() -> None:
    # 14:55 UTC to 15:10 UTC is 23:55 to 00:10 JST.
    base = datetime(2026, 10, 1, 14, 50, tzinfo=UTC)
    samples = (
        _every_minute(base, 5, "operational")
        + _every_minute(base + timedelta(minutes=5), 15, "outage")
        + _every_minute(base + timedelta(minutes=20), 5, "operational")
    )
    days, _ = _run(samples)
    assert days["2026-10-01"].status.value == "outage"
    assert days["2026-10-02"].status.value == "outage"


def test_collector_gap_does_not_inflate_a_brief_blip() -> None:
    base = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    samples = [
        (base, "operational"),
        (base + timedelta(minutes=1), "outage"),
        (base + timedelta(hours=5), "operational"),  # collector was down for hours
    ]
    days, _ = _run(samples)
    # Counted as at most the 10 minute gap cap, so a 1 sample blip with a long gap
    # is still capped; with a 15 minute threshold it must not mark the day.
    days_strict, _ = _run(samples, min_seconds=900)
    assert days_strict["2026-10-01"].status.value == "operational"
    assert days["2026-10-01"].status.value == "outage"


def test_zero_threshold_keeps_previous_behavior() -> None:
    base = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    samples = _every_minute(base, 5, "operational") + _every_minute(base + timedelta(minutes=5), 1, "degraded")
    days, _ = _run(samples, min_seconds=0)
    assert days["2026-10-01"].status.value == "degraded"
