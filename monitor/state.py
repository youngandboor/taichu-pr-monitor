"""SQLite tracker and durable delivery outbox."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import sqlite3
from typing import List, Optional, Union

from .core import TrackerState


@dataclasses.dataclass(frozen=True)
class OutboxEvent:
    event_key: str
    pr_number: int
    author: str
    message: str


@dataclasses.dataclass(frozen=True)
class OutboxRecord:
    id: int
    event_key: str
    pr_number: int
    author: str
    receiver: str
    message: str
    status: str
    attempts: int
    last_error: str
    created_at: str
    updated_at: str


class MonitorStore:
    def __init__(self, path: Union[str, pathlib.Path]) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def __enter__(self) -> "MonitorStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def get_tracker(self, pr_number: int) -> TrackerState:
        row = self.connection.execute(
            "SELECT * FROM pr_tracker WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        if row is None:
            return TrackerState.empty()
        try:
            keys = frozenset(json.loads(row["notified_failure_keys"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            keys = frozenset()
        return TrackerState(
            row["observed_command_key"],
            keys,
            bool(row["initialized"]),
            row["last_scanned_at"],
        )

    def save_tracker(self, pr_number: int, state: TrackerState) -> None:
        self.apply_poll(pr_number, state, None)

    def apply_poll(
        self,
        pr_number: int,
        state: TrackerState,
        event: Optional[OutboxEvent],
    ) -> None:
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO pr_tracker (
                    pr_number, observed_command_key, notified_failure_keys,
                    initialized, last_scanned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pr_number) DO UPDATE SET
                    observed_command_key = excluded.observed_command_key,
                    notified_failure_keys = excluded.notified_failure_keys,
                    initialized = excluded.initialized,
                    last_scanned_at = excluded.last_scanned_at,
                    updated_at = excluded.updated_at
                """,
                (
                    pr_number,
                    state.observed_command_key,
                    json.dumps(sorted(state.notified_failure_keys), ensure_ascii=False),
                    1 if state.initialized else 0,
                    state.last_scanned_at,
                    now,
                ),
            )
            if event is not None:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO delivery_outbox (
                        event_key, pr_number, author, receiver, message,
                        status, attempts, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, '', ?, 'pending', 0, '', ?, ?)
                    """,
                    (
                        event.event_key,
                        event.pr_number,
                        event.author,
                        event.message,
                        now,
                        now,
                    ),
                )

    def list_dispatchable(self, max_attempts: int) -> List[OutboxRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE status IN ('pending', 'failed', 'unmapped') AND attempts < ?
            ORDER BY id ASC
            """,
            (max_attempts,),
        ).fetchall()
        return [_outbox_record(row) for row in rows]

    def list_outbox(self) -> List[OutboxRecord]:
        rows = self.connection.execute(
            "SELECT * FROM delivery_outbox ORDER BY id ASC"
        ).fetchall()
        return [_outbox_record(row) for row in rows]

    def update_delivery(
        self,
        record_id: int,
        status: str,
        receiver: str,
        last_error: str,
        increment_attempt: bool,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE delivery_outbox
                SET status = ?, receiver = ?, last_error = ?,
                    attempts = attempts + ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    receiver,
                    last_error[:1000],
                    1 if increment_attempt else 0,
                    _utc_now(),
                    record_id,
                ),
            )

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pr_tracker (
                    pr_number INTEGER PRIMARY KEY,
                    observed_command_key TEXT NOT NULL,
                    notified_failure_keys TEXT NOT NULL,
                    initialized INTEGER NOT NULL,
                    last_scanned_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    pr_number INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )


def _outbox_record(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(
        id=row["id"],
        event_key=row["event_key"],
        pr_number=row["pr_number"],
        author=row["author"],
        receiver=row["receiver"],
        message=row["message"],
        status=row["status"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
