"""SQLite tracker and durable delivery outbox."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import re
import sqlite3
from typing import Dict, List, Optional, Sequence, Union

from .core import GateFailure, PrSnapshot, TrackerState


OUTBOX_STATUSES = (
    "pending",
    "failed",
    "dead",
    "uncertain",
    "unmapped",
    "sent",
    "suppressed",
)
GITEA_LOGIN_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?"
)
EMPLOYEE_NUMBER_PATTERN = re.compile(r"\d{8}")
WELINK_ACCOUNT_PATTERN = re.compile(r"[A-Za-z](\d{8})")


@dataclasses.dataclass(frozen=True)
class OutboxEvent:
    event_key: str
    pr_number: int
    author: str
    message: str
    receiver_hint: str = ""


@dataclasses.dataclass(frozen=True)
class OutboxRecord:
    id: int
    event_key: str
    pr_number: int
    author: str
    receiver: str
    recipient_employee_number: str
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


@dataclasses.dataclass(frozen=True)
class NotificationOptOut:
    employee_number: str
    created_at: str


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
        recipient_employee_number = (
            employee_number_from_welink(event.receiver_hint) if event is not None else None
        )
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
                        event_key, pr_number, author, receiver,
                        recipient_employee_number, message,
                        status, attempts, last_error, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        CASE WHEN EXISTS (
                            SELECT 1 FROM notification_opt_out
                            WHERE employee_number = ?
                        ) THEN 'suppressed' ELSE 'pending' END,
                        0, '', ?, ?
                    )
                    """,
                    (
                        event.event_key,
                        event.pr_number,
                        event.author,
                        event.receiver_hint,
                        recipient_employee_number or "",
                        event.message,
                        recipient_employee_number or "",
                        now,
                        now,
                    ),
                )
            if snapshot is not None:
                self.connection.execute(
                    """
                    INSERT INTO pr_snapshot (
                        pr_number, title, author, author_w3, head_sha, url,
                        latest_ci_command, latest_ci_command_at,
                        scanned_at, failures_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pr_number) DO UPDATE SET
                        title = excluded.title,
                        author = excluded.author,
                        author_w3 = excluded.author_w3,
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
                        snapshot.author_w3,
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
              AND NOT EXISTS (
                  SELECT 1 FROM notification_opt_out
                  WHERE employee_number = delivery_outbox.recipient_employee_number
                     OR employee_number = CASE
                         WHEN length(delivery_outbox.receiver) = 9
                          AND substr(delivery_outbox.receiver, 1, 1) GLOB '[A-Za-z]'
                         THEN substr(delivery_outbox.receiver, 2)
                         WHEN length(delivery_outbox.receiver) = 8
                          AND delivery_outbox.receiver NOT GLOB '*[^0-9]*'
                         THEN delivery_outbox.receiver
                         ELSE ''
                     END
                     OR employee_number = COALESCE((
                         SELECT CASE
                             WHEN length(pr_snapshot.author_w3) = 9
                              AND substr(pr_snapshot.author_w3, 1, 1) GLOB '[A-Za-z]'
                             THEN substr(pr_snapshot.author_w3, 2)
                             ELSE ''
                         END
                         FROM pr_snapshot
                         WHERE pr_snapshot.pr_number = delivery_outbox.pr_number
                     ), '')
              )
            ORDER BY id ASC
            """,
            (max_attempts,),
        ).fetchall()
        return [_outbox_record(row) for row in rows]

    def list_outbox(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        *,
        newest_first: bool = False,
        statuses: Optional[Sequence[str]] = None,
    ) -> List[OutboxRecord]:
        clauses = []
        parameters = []
        if status is not None and statuses is not None:
            raise ValueError("status and statuses cannot be supplied together")
        requested_statuses = [status] if status is not None else list(statuses or ())
        requested_statuses = list(dict.fromkeys(requested_statuses))
        for requested_status in requested_statuses:
            if requested_status not in OUTBOX_STATUSES:
                raise ValueError(f"unknown outbox status: {requested_status}")
        if requested_statuses:
            placeholders = ",".join("?" for _ in requested_statuses)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(requested_statuses)
        if limit is not None:
            if int(limit) <= 0:
                raise ValueError("outbox limit must be greater than zero")
            parameters.append(int(limit))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "DESC" if newest_first else "ASC"
        limit_sql = " LIMIT ?" if limit is not None else ""
        rows = self.connection.execute(
            f"SELECT * FROM delivery_outbox{where} ORDER BY id {order}{limit_sql}",
            parameters,
        ).fetchall()
        return [_outbox_record(row) for row in rows]

    def list_outbox_for_pr(self, pr_number: int) -> List[OutboxRecord]:
        rows = self.connection.execute(
            "SELECT * FROM delivery_outbox WHERE pr_number = ? ORDER BY id ASC",
            (int(pr_number),),
        ).fetchall()
        return [_outbox_record(row) for row in rows]

    def outbox_counts(self) -> Dict[str, int]:
        counts = {status: 0 for status in OUTBOX_STATUSES}
        total = 0
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM delivery_outbox GROUP BY status"
        ).fetchall()
        for row in rows:
            count = int(row["count"])
            total += count
            status = str(row["status"])
            if status in counts:
                counts[status] = count
        return {"total": total, **counts}

    def list_notification_opt_outs(self) -> List[NotificationOptOut]:
        rows = self.connection.execute(
            """
            SELECT employee_number, created_at
            FROM notification_opt_out
            ORDER BY employee_number
            """
        ).fetchall()
        return [
            NotificationOptOut(
                employee_number=row["employee_number"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def is_recipient_opted_out(self, welink_account: str) -> bool:
        employee_number = employee_number_from_welink(welink_account)
        if employee_number is None:
            return False
        row = self.connection.execute(
            "SELECT 1 FROM notification_opt_out WHERE employee_number = ?",
            (employee_number,),
        ).fetchone()
        return row is not None

    def employee_number_for_author(self, author: str) -> Optional[str]:
        login = normalize_gitea_login(author)
        rows = self.connection.execute(
            "SELECT author_w3 FROM pr_snapshot WHERE author = ? COLLATE NOCASE",
            (login,),
        ).fetchall()
        values = {
            employee_number
            for row in rows
            for employee_number in [employee_number_from_welink(row["author_w3"])]
            if employee_number is not None
        }
        if len(values) > 1:
            raise ValueError(f"multiple WeLink employee numbers found for Gitea author {login}")
        return next(iter(values), None)

    def add_notification_opt_out(self, value: str) -> int:
        employee_number = normalize_employee_number(value)
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO notification_opt_out (employee_number, created_at)
                VALUES (?, ?)
                """,
                (employee_number, now),
            )
            cursor = self.connection.execute(
                """
                UPDATE delivery_outbox
                SET status = 'suppressed',
                    last_error = 'notification suppressed by recipient preference',
                    updated_at = ?
                WHERE status IN ('pending', 'failed', 'unmapped')
                  AND (
                      recipient_employee_number = ?
                      OR receiver = ?
                      OR (
                          length(receiver) = 9
                          AND substr(receiver, 2) = ?
                          AND substr(receiver, 1, 1) GLOB '[A-Za-z]'
                      )
                      OR EXISTS (
                          SELECT 1 FROM pr_snapshot
                          WHERE pr_snapshot.pr_number = delivery_outbox.pr_number
                            AND length(pr_snapshot.author_w3) = 9
                            AND substr(pr_snapshot.author_w3, 2) = ?
                            AND substr(pr_snapshot.author_w3, 1, 1) GLOB '[A-Za-z]'
                      )
                  )
                """,
                (
                    now,
                    employee_number,
                    employee_number,
                    employee_number,
                    employee_number,
                ),
            )
        return cursor.rowcount

    def remove_notification_opt_out(self, value: str) -> bool:
        employee_number = normalize_employee_number(value)
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM notification_opt_out WHERE employee_number = ?",
                (employee_number,),
            )
        return cursor.rowcount == 1

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

    def mark_terminal_pending(self, pr_numbers: Sequence[int]) -> None:
        numbers = sorted({int(number) for number in pr_numbers if int(number) > 0})
        if not numbers:
            return
        now = _utc_now()
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO pr_terminal_pending (pr_number, created_at)
                VALUES (?, ?)
                """,
                ((number, now) for number in numbers),
            )

    def clear_terminal_pending(self, pr_number: int) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM pr_terminal_pending WHERE pr_number = ?",
                (int(pr_number),),
            )

    def list_terminal_pending(self) -> List[int]:
        rows = self.connection.execute(
            "SELECT pr_number FROM pr_terminal_pending ORDER BY pr_number"
        ).fetchall()
        return [int(row["pr_number"]) for row in rows]

    def is_terminal_pending(self, pr_number: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM pr_terminal_pending WHERE pr_number = ?",
            (int(pr_number),),
        ).fetchone()
        return row is not None

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
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_opt_out
                      WHERE employee_number = delivery_outbox.recipient_employee_number
                         OR employee_number = CASE
                             WHEN length(delivery_outbox.receiver) = 9
                              AND substr(delivery_outbox.receiver, 1, 1) GLOB '[A-Za-z]'
                             THEN substr(delivery_outbox.receiver, 2)
                             WHEN length(delivery_outbox.receiver) = 8
                              AND delivery_outbox.receiver NOT GLOB '*[^0-9]*'
                             THEN delivery_outbox.receiver
                             ELSE ''
                         END
                         OR employee_number = COALESCE((
                             SELECT CASE
                                 WHEN length(pr_snapshot.author_w3) = 9
                                  AND substr(pr_snapshot.author_w3, 1, 1) GLOB '[A-Za-z]'
                                 THEN substr(pr_snapshot.author_w3, 2)
                                 ELSE ''
                             END
                             FROM pr_snapshot
                             WHERE pr_snapshot.pr_number = delivery_outbox.pr_number
                         ), '')
                  )
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
                    author_w3 TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS pr_terminal_pending (
                    pr_number INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                "pr_snapshot",
                "author_w3",
                "TEXT NOT NULL DEFAULT ''",
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
            self._ensure_notification_opt_out_schema()
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    pr_number INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    recipient_employee_number TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            added_recipient_column = self._ensure_column(
                "delivery_outbox",
                "recipient_employee_number",
                "TEXT NOT NULL DEFAULT ''",
            )
            if added_recipient_column:
                rows = self.connection.execute(
                    "SELECT id, receiver FROM delivery_outbox"
                ).fetchall()
                for row in rows:
                    employee_number = employee_number_from_welink(row["receiver"])
                    if employee_number is not None:
                        self.connection.execute(
                            """
                            UPDATE delivery_outbox
                            SET recipient_employee_number = ?
                            WHERE id = ?
                            """,
                            (employee_number, row["id"]),
                        )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS delivery_outbox_status_id
                ON delivery_outbox (status, id)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS delivery_outbox_author_status
                ON delivery_outbox (author COLLATE NOCASE, status)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS delivery_outbox_recipient_status
                ON delivery_outbox (recipient_employee_number, status)
                """
            )

    def _ensure_column(self, table: str, column: str, declaration: str) -> bool:
        columns = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in columns:
            return False
        self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        return True

    def _ensure_notification_opt_out_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(notification_opt_out)"
            ).fetchall()
        }
        if not columns:
            self.connection.execute(
                """
                CREATE TABLE notification_opt_out (
                    employee_number TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            return
        if "employee_number" in columns:
            return

        legacy_rows = self.connection.execute(
            "SELECT login, created_at FROM notification_opt_out"
        ).fetchall()
        self.connection.execute(
            "ALTER TABLE notification_opt_out RENAME TO notification_opt_out_legacy"
        )
        self.connection.execute(
            """
            CREATE TABLE notification_opt_out (
                employee_number TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        for row in legacy_rows:
            employee_number = employee_number_from_welink(row["login"])
            if employee_number is not None:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_opt_out (
                        employee_number, created_at
                    ) VALUES (?, ?)
                    """,
                    (employee_number, row["created_at"]),
                )
        self.connection.execute("DROP TABLE notification_opt_out_legacy")


def _outbox_record(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(
        id=row["id"],
        event_key=row["event_key"],
        pr_number=row["pr_number"],
        author=row["author"],
        receiver=row["receiver"],
        recipient_employee_number=row["recipient_employee_number"],
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
        author_w3=row["author_w3"],
    )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize_gitea_login(value: str) -> str:
    login = str(value or "").strip().lower()
    if not GITEA_LOGIN_PATTERN.fullmatch(login):
        raise ValueError(
            "Gitea login must be 1-64 ASCII letters, digits, dots, underscores, or hyphens "
            "and must start and end with a letter or digit"
        )
    return login


def normalize_employee_number(value: str) -> str:
    raw = str(value or "").strip()
    if EMPLOYEE_NUMBER_PATTERN.fullmatch(raw):
        return raw
    match = WELINK_ACCOUNT_PATTERN.fullmatch(raw)
    if match:
        return match.group(1)
    raise ValueError(
        "WeLink recipient must be an 8-digit employee number or a letter followed by "
        "an 8-digit employee number"
    )


def employee_number_from_welink(value: str) -> Optional[str]:
    try:
        return normalize_employee_number(value)
    except ValueError:
        return None
