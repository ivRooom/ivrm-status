from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import IngestPayload, PublicStatus


@dataclass(frozen=True, slots=True)
class Snapshot:
    service_id: str
    status: PublicStatus
    checked_at: datetime
    received_at: datetime
    version: str | None
    summary: str


class ReplayConflictError(RuntimeError):
    pass


class StatusRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self, herta_stale_after_seconds: int) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS services (
                    service_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    service_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    stale_after_seconds INTEGER NOT NULL CHECK (stale_after_seconds > 0),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS status_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL REFERENCES services(service_id),
                    status TEXT NOT NULL CHECK (
                        status IN ('operational', 'maintenance', 'degraded', 'outage', 'unknown')
                    ),
                    checked_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    version TEXT,
                    summary TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_status_snapshots_service_received
                    ON status_snapshots(service_id, received_at DESC);

                CREATE TABLE IF NOT EXISTS replay_requests (
                    request_id TEXT PRIMARY KEY,
                    service_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_replay_requests_expires
                    ON replay_requests(expires_at);

                CREATE TABLE IF NOT EXISTS minecraft_samples (
                    collected_at TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('operational', 'maintenance', 'degraded', 'outage', 'unknown')
                    )
                );
                """
            )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO services (
                    service_id, name, group_name, service_type, description,
                    stale_after_seconds, enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(service_id) DO UPDATE SET
                    name = excluded.name,
                    group_name = excluded.group_name,
                    service_type = excluded.service_type,
                    description = excluded.description,
                    stale_after_seconds = excluded.stale_after_seconds,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    "herta-discord-bot",
                    "Herta",
                    "Discordサービス",
                    "discord_bot",
                    "ivRooom Discord Bot",
                    herta_stale_after_seconds,
                    now,
                ),
            )
            connection.commit()

    def healthcheck(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def save_ingest(
        self,
        payload: IngestPayload,
        request_id: str,
        received_at: datetime,
        replay_ttl_seconds: int,
        history_retention_days: int,
    ) -> None:
        expires_at = received_at + timedelta(seconds=replay_ttl_seconds)
        retention_cutoff = received_at - timedelta(days=history_retention_days)

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM replay_requests WHERE expires_at < ?",
                (received_at.isoformat(),),
            )
            if connection.execute(
                "SELECT 1 FROM replay_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone():
                connection.rollback()
                raise ReplayConflictError(request_id)

            connection.execute(
                """
                INSERT INTO replay_requests(request_id, service_id, received_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    request_id,
                    payload.service.id,
                    received_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO status_snapshots(
                    service_id, status, checked_at, received_at, version, summary
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.service.id,
                    payload.status.value,
                    payload.checked_at.astimezone(UTC).isoformat(),
                    received_at.isoformat(),
                    payload.version,
                    payload.summary,
                ),
            )
            connection.execute(
                "DELETE FROM status_snapshots WHERE received_at < ?",
                (retention_cutoff.isoformat(),),
            )
            connection.commit()

    def save_minecraft_samples(
        self,
        samples: list[tuple[datetime, PublicStatus]],
        retention_cutoff: datetime,
    ) -> None:
        """Keep collector samples ourselves: the collector's history.json only holds ~24h."""
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO minecraft_samples (collected_at, status) VALUES (?, ?)",
                [
                    (at.astimezone(UTC).isoformat(timespec="microseconds"), status.value)
                    for at, status in samples
                ],
            )
            connection.execute(
                "DELETE FROM minecraft_samples WHERE collected_at < ?",
                (retention_cutoff.astimezone(UTC).isoformat(timespec="microseconds"),),
            )
            connection.commit()

    def minecraft_samples_since(self, since: datetime) -> list[tuple[datetime, PublicStatus]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT collected_at, status FROM minecraft_samples WHERE collected_at >= ? ORDER BY collected_at ASC",
                (since.astimezone(UTC).isoformat(timespec="microseconds"),),
            ).fetchall()
        return [(datetime.fromisoformat(row["collected_at"]), PublicStatus(row["status"])) for row in rows]

    def latest_snapshot(self, service_id: str) -> Snapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT service_id, status, checked_at, received_at, version, summary
                FROM status_snapshots
                WHERE service_id = ?
                ORDER BY received_at DESC, id DESC
                LIMIT 1
                """,
                (service_id,),
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def snapshots_since(self, service_id: str, since: datetime) -> list[Snapshot]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT service_id, status, checked_at, received_at, version, summary
                FROM status_snapshots
                WHERE service_id = ? AND received_at >= ?
                ORDER BY received_at ASC, id ASC
                """,
                (service_id, since.isoformat()),
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            service_id=row["service_id"],
            status=PublicStatus(row["status"]),
            checked_at=datetime.fromisoformat(row["checked_at"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            version=row["version"],
            summary=row["summary"],
        )
