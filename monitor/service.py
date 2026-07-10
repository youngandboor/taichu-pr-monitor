"""One polling cycle and durable WeLink delivery orchestration."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from .core import (
    PrSnapshot,
    build_pr_snapshot,
    failure_key,
    notification_text,
    poll_tracker,
)
from .gitea import DEFAULT_OWNER, DEFAULT_REPO, DEFAULT_WEB_BASE
from .state import MonitorStore, OutboxEvent


@dataclasses.dataclass
class PollReport:
    scanned_at: str
    duration_seconds: float = 0.0
    open_prs: int = 0
    scanned_prs: int = 0
    new_notifications: int = 0
    delivered: int = 0
    delivery_failures: int = 0
    delivery_uncertain: int = 0
    unmapped: int = 0
    errors: List[str] = dataclasses.field(default_factory=list)


class RecipientDirectory:
    """Resolve authors and reroute a WeLink sender's unsupported self-message."""

    def __init__(
        self,
        path: Optional[pathlib.Path] = None,
        direct: bool = True,
        sender_account: Optional[str] = None,
        self_fallback_receiver: Optional[str] = None,
    ) -> None:
        self.path = pathlib.Path(path) if path else None
        self.direct = direct
        self.sender_account = (sender_account or "").strip()
        self.self_fallback_receiver = (self_fallback_receiver or "").strip()
        if bool(self.sender_account) != bool(self.self_fallback_receiver):
            raise ValueError(
                "WeLink sender and self-fallback receiver must be configured together"
            )
        if (
            self.sender_account
            and self.sender_account.casefold() == self.self_fallback_receiver.casefold()
        ):
            raise ValueError("WeLink self-fallback receiver must differ from sender account")
        self.mapping: Dict[str, str] = {}

    def refresh(self) -> None:
        if self.path is None:
            self.mapping = {}
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read recipient mapping {self.path}: {error}") from error
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid recipient mapping JSON {self.path}: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("recipient mapping must be a JSON object")
        mapping = {}
        for author, receiver in payload.items():
            if not isinstance(author, str) or not isinstance(receiver, str):
                raise ValueError("recipient mapping keys and values must be strings")
            if author.strip() and receiver.strip():
                mapping[author.strip()] = receiver.strip()
        self.mapping = mapping

    def resolve(self, author: str) -> Optional[str]:
        if author in self.mapping:
            receiver = self.mapping[author]
        else:
            receiver = author if self.direct and author else None
        if (
            receiver
            and self.sender_account
            and receiver.casefold() == self.sender_account.casefold()
        ):
            return self.self_fallback_receiver
        return receiver


class MonitorService:
    def __init__(
        self,
        client,
        store: MonitorStore,
        sender,
        recipients: RecipientDirectory,
        owner: str = DEFAULT_OWNER,
        repo: str = DEFAULT_REPO,
        web_base: str = DEFAULT_WEB_BASE,
        max_pull_pages: int = 100,
        max_comment_pages: int = 3,
        max_send_attempts: int = 3,
        fetch_workers: int = 6,
        clock: Optional[Callable[[], str]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.sender = sender
        self.recipients = recipients
        self.owner = owner
        self.repo = repo
        self.web_base = web_base
        self.max_pull_pages = max_pull_pages
        self.max_comment_pages = max_comment_pages
        self.max_send_attempts = max_send_attempts
        self.fetch_workers = max(1, fetch_workers)
        self.clock = clock or _utc_now
        self.logger = logger or logging.getLogger(__name__)

    def poll_once(self) -> PollReport:
        started = time.monotonic()
        scanned_at = self.clock()
        report = PollReport(scanned_at=scanned_at)
        recipients_ready = True
        listing_succeeded = False
        try:
            self.recipients.refresh()
        except ValueError as error:
            recipients_ready = False
            report.errors.append(str(error))
            self.logger.error("recipient mapping error: %s", error)

        try:
            pulls = self.client.list_open_pulls(
                self.owner,
                self.repo,
                max_pages=self.max_pull_pages,
                limit=100,
            )
            report.open_prs = len(pulls)
            listing_succeeded = True
        except Exception as error:  # Keep pending deliveries moving during a Gitea outage.
            message = f"failed to list open pull requests: {error}"
            report.errors.append(message)
            self.logger.error(message)
            pulls = []

        worker_count = min(self.fetch_workers, len(pulls)) if pulls else 1
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self._fetch_snapshot, pr, scanned_at): pr
                for pr in pulls
            }
            for future in as_completed(futures):
                pr = futures[future]
                number = _pr_number(pr)
                try:
                    snapshot = future.result()
                    current = self.store.get_tracker(snapshot.number)
                    result = poll_tracker(current, snapshot)
                    event = self._event_for(snapshot, result.notifications)
                    self.store.apply_poll(
                        snapshot.number,
                        result.state,
                        event,
                        snapshot=snapshot,
                    )
                    report.scanned_prs += 1
                    report.new_notifications += len(result.notifications)
                except Exception as error:
                    message = f"PR #{number or '?'} scan failed: {error}"
                    report.errors.append(message)
                    self.logger.error(message)

        if recipients_ready:
            self._dispatch_outbox(report)
        if listing_succeeded:
            self.store.prune_snapshots(_pr_number(pr) for pr in pulls)
        report.duration_seconds = time.monotonic() - started
        self.store.record_scan(
            scanned_at=report.scanned_at,
            duration_seconds=report.duration_seconds,
            open_prs=report.open_prs,
            scanned_prs=report.scanned_prs,
            new_notifications=report.new_notifications,
            delivered=report.delivered,
            delivery_failures=report.delivery_failures,
            delivery_uncertain=report.delivery_uncertain,
            unmapped=report.unmapped,
            errors=report.errors,
        )
        return report

    def _fetch_snapshot(self, pr, scanned_at: str) -> PrSnapshot:
        number = _pr_number(pr)
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head_sha = str(head.get("sha") or "").strip()
        if not head_sha:
            raise ValueError("pull request response has no head sha")
        statuses = self.client.get_statuses(self.owner, self.repo, head_sha)
        comments = self.client.get_issue_comments(
            self.owner,
            self.repo,
            number,
            max_pages=self.max_comment_pages,
        )
        return build_pr_snapshot(
            pr,
            statuses,
            comments,
            scanned_at=scanned_at,
            web_base=self.web_base,
            owner=self.owner,
            repo=self.repo,
        )

    def _event_for(
        self,
        snapshot: PrSnapshot,
        failures,
    ) -> Optional[OutboxEvent]:
        if not failures:
            return None
        keys = sorted(failure_key(snapshot, failure) for failure in failures)
        digest = hashlib.sha256(
            (str(snapshot.number) + "\0" + "\0".join(keys)).encode("utf-8")
        ).hexdigest()
        return OutboxEvent(
            event_key=digest,
            pr_number=snapshot.number,
            author=snapshot.author,
            message=format_message(snapshot, failures),
        )

    def _dispatch_outbox(self, report: PollReport) -> None:
        for record in self.store.list_dispatchable(self.max_send_attempts):
            receiver = self.recipients.resolve(record.author)
            if not receiver:
                self.store.update_delivery(
                    record.id,
                    "unmapped",
                    "",
                    f"no WeLink recipient for Gitea author {record.author}",
                    increment_attempt=False,
                )
                report.unmapped += 1
                continue
            result = self.sender.send(receiver, record.message)
            if result.status == "success":
                self.store.update_delivery(
                    record.id,
                    "sent",
                    receiver,
                    "",
                    increment_attempt=True,
                )
                report.delivered += 1
                continue
            error = _delivery_error(result)
            if result.status == "timeout":
                self.store.update_delivery(
                    record.id,
                    "uncertain",
                    receiver,
                    error,
                    increment_attempt=True,
                )
                report.delivery_uncertain += 1
                self.logger.error("WeLink delivery outcome uncertain for outbox #%s", record.id)
                continue
            next_attempt = record.attempts + 1
            status = "dead" if next_attempt >= self.max_send_attempts else "failed"
            self.store.update_delivery(
                record.id,
                status,
                receiver,
                error,
                increment_attempt=True,
            )
            report.delivery_failures += 1
            self.logger.error("WeLink delivery failed for outbox #%s", record.id)


def format_message(snapshot: PrSnapshot, failures) -> str:
    lines = [
        f"[TaiChu PR #{snapshot.number}] 发现 {len(failures)} 个新问题",
        f"标题：{snapshot.title or '(无标题)'}",
        f"提交人：{snapshot.author}",
        f"Head：{snapshot.head_sha[:7]}",
    ]
    for failure in failures:
        lines.append(f"- {failure.context}：{notification_text(failure.summary)}")
    lines.append(f"查看：{snapshot.url}")
    return "\n".join(lines)


def _delivery_error(result) -> str:
    detail = (result.stderr or result.stdout or "welink-cli returned no detail").strip()
    code = "none" if result.exit_code is None else str(result.exit_code)
    return f"status={result.status}, exit={code}: {detail}"


def _pr_number(pr) -> int:
    try:
        return int(pr.get("number") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
