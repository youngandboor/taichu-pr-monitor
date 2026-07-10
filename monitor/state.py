"""SQLite tracker and durable delivery outbox."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import sqlite3
from typing import List, Optional, Sequence, Union

from .core import GateFailure, PrSnapshot, TrackerState


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


@dataclasses.dataclass(frozen=True)
class ScanRecord:
    scanned_at: str
    completed_at: str
    duration_seconds: float
    open_prs: int
    scanned_prs: int
    new_notifications: int
    delivered: int
    delivery_failures: int
    delivery_uncertain: int
    unmapped: int
    errors: tuple


class MonitorStore:
    def __init__(self, path: Union[str, pathlib.Path]) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
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
        snapshot: Optional[PrSnapshot] = None,
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
            if snapshot is not None:
                self.connection.execute(
                    """
                    INSERT INTO pr_snapshot (
                        pr_number, title, author, head_sha, url,
                        latest_ci_command, latest_ci_command_at,
                        scanned_at, failures_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pr_number) DO UPDATE SET
                        title = excluded.title,
                        author = excluded.author,
                        head_sha = excluded.head_sha,
                        url = excluded.url,
                        latest_ci_command = excluded.latest_ci_command,
                        latest_ci_command_at = excluded.latest_ci_command_at,
                        scanned_at = excluded.scanned_at,
                        failures_json = excluded.failures_json
                    """,
                    (
                        snapshot.number,
                        snapshot.title,
                        snapshot.author,
                        snapshot.head_sha,
                        snapshot.url,
                        snapshot.latest_ci_command,
                        snapshot.latest_ci_command_at,
                        snapshot.scanned_at,
                        json.dumps(
                            [dataclasses.asdict(failure) for failure in snapshot.failures],
                            ensure_ascii=False,
                        ),
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

    def list_snapshots(self) -> List[PrSnapshot]:
        rows = self.connection.execute(
            "SELECT * FROM pr_snapshot ORDER BY pr_number DESC"
        ).fetchall()
        snapshots = [_snapshot_record(row) for row in rows]
        failing = sorted(
            (item for item in snapshots if item.failures),
            key=lambda item: max(failure.updated_at for failure in item.failures),
            reverse=True,
        )
        clear = sorted(
            (item for item in snapshots if not item.failures),
            key=lambda item: item.number,
            reverse=True,
        )
        return failing + clear

    def prune_snapshots(self, open_pr_numbers: Sequence[int]) -> None:
        numbers = sorted({int(number) for number in open_pr_numbers if int(number) > 0})
        with self.connection:
            if not numbers:
                self.connection.execute("DELETE FROM pr_snapshot")
                return
            placeholders = ",".join("?" for _ in numbers)
            self.connection.execute(
                f"DELETE FROM pr_snapshot WHERE pr_number NOT IN ({placeholders})",
                numbers,
            )

    def record_scan(
        self,
        *,
        scanned_at: str,
        duration_seconds: float,
        open_prs: int,
        scanned_prs: int,
        new_notifications: int,
        delivered: int,
        delivery_failures: int,
        delivery_uncertain: int,
        unmapped: int,
        errors: Sequence[str],
    ) -> None:
        completed_at = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO monitor_scan (
                    singleton, scanned_at, completed_at, duration_seconds,
                    open_prs, scanned_prs, new_notifications, delivered,
                    delivery_failures, delivery_uncertain, unmapped, errors_json
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    scanned_at = excluded.scanned_at,
                    completed_at = excluded.completed_at,
                    duration_seconds = excluded.duration_seconds,
                    open_prs = excluded.open_prs,
                    scanned_prs = excluded.scanned_prs,
                    new_notifications = excluded.new_notifications,
                    delivered = excluded.delivered,
                    delivery_failures = excluded.delivery_failures,
                    delivery_uncertain = excluded.delivery_uncertain,
                    unmapped = excluded.unmapped,
                    errors_json = excluded.errors_json
                """,
                (
                    scanned_at,
                    completed_at,
                    float(duration_seconds),
                    int(open_prs),
                    int(scanned_prs),
                    int(new_notifications),
                    int(delivered),
                    int(delivery_failures),
                    int(delivery_uncertain),
                    int(unmapped),
                    json.dumps(list(errors), ensure_ascii=False),
                ),
            )

    def latest_scan(self) -> Optional[ScanRecord]:
        row = self.connection.execute(
            "SELECT * FROM monitor_scan WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        try:
            errors = tuple(json.loads(row["errors_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            errors = ()
        return ScanRecord(
            scanned_at=row["scanned_at"],
            completed_at=row["completed_at"],
            duration_seconds=float(row["duration_seconds"]),
            open_prs=row["open_prs"],
            scanned_prs=row["scanned_prs"],
            new_notifications=row["new_notifications"],
            delivered=row["delivered"],
            delivery_failures=row["delivery_failures"],
            delivery_uncertain=row["delivery_uncertain"],
            unmapped=row["unmapped"],
            errors=errors,
        )

    def requeue_delivery(self, record_id: int) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE delivery_outbox
                SET status = 'pending', receiver = '', attempts = 0,
                    last_error = '', updated_at = ?
                WHERE id = ? AND status IN ('failed', 'dead', 'uncertain', 'unmapped')
                """,
                (_utc_now(), record_id),
            )
        return cursor.rowcount == 1

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
                CREATE TABLE IF NOT EXISTS pr_snapshot (
                    pr_number INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    url TEXT NOT NULL,
                    latest_ci_command TEXT NOT NULL,
                    latest_ci_command_at TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    failures_json TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_scan (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    scanned_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    open_prs INTEGER NOT NULL,
                    scanned_prs INTEGER NOT NULL,
                    new_notifications INTEGER NOT NULL,
                    delivered INTEGER NOT NULL,
                    delivery_failures INTEGER NOT NULL,
                    delivery_uncertain INTEGER NOT NULL,
                    unmapped INTEGER NOT NULL,
                    errors_json TEXT NOT NULL
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


def _snapshot_record(row: sqlite3.Row) -> PrSnapshot:
    try:
        payload = json.loads(row["failures_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = []
    failures = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        failures.append(
            GateFailure(
                str(item.get("context") or ""),
                str(item.get("updated_at") or ""),
                str(item.get("summary") or ""),
            )
        )
    return PrSnapshot(
        number=row["pr_number"],
        title=row["title"],
        author=row["author"],
        head_sha=row["head_sha"],
        url=row["url"],
        latest_ci_command=row["latest_ci_command"],
        latest_ci_command_at=row["latest_ci_command_at"],
        latest_ci_command_key="",
        scanned_at=row["scanned_at"],
        failures=tuple(failures),
    )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
