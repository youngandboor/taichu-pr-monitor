"""One polling cycle and durable WeLink delivery orchestration."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Iterable, List, Optional

from .core import (
    GateFailure,
    GATE_CONTEXTS,
    PROBLEM_HISTORY_MARKER,
    PrSnapshot,
    TrackerState,
    build_pr_snapshot,
    exact_ci_command,
    notification_summary,
    notification_text,
    poll_tracker,
    round_failure_key,
    seed_problem_history,
    stage_failures,
)
from .gitea import DEFAULT_OWNER, DEFAULT_REPO, DEFAULT_WEB_BASE
from .state import MonitorStore, OutboxEvent


IGNORED_PR_AUTHORS = frozenset({"taichu-ci-bot"})


MERGE_SUCCESS_COPY = {
    1: (
        (
            "Merge Successful 🔪",
            "一天搞定 {row_count} 行代码，改得非常准。不需要冗长废话就能把痛点切掉，"
            "老医生的刀法。代码已上膛，干得漂亮！🍻",
        ),
        (
            "PR Merged 🚀",
            "一天内输出 {row_count} 行代码，且逻辑闭环无 Bug。这手速和状态绝了，"
            "机器跑得都没你脑子转得快。今天必须早点下班 ☕",
        ),
        (
            "Merged ⚡",
            "一天爆肝完成 {row_count} 行高质量代码，Review 居然挑不出什么毛病。"
            "这交付效率属实拉满了，大佬牛的 👏",
        ),
        (
            "Merge Complete 🤯",
            "24 小时撸出 {row_count} 行代码，还能保证高标准的测试覆盖。"
            "这单兵突击能力太硬核了，赶紧让键盘和脑子都降降温 🧊",
        ),
    ),
    2: (
        (
            "Code Integrated 💎",
            "两天时间打磨 {row_count} 行核心逻辑，代码极其精炼。懂的都懂，"
            "脑子里估计把并发和边界推演了无数遍。极简就是最高级，辛苦 ☕",
        ),
        (
            "Merge Successful 🛠️",
            "两天战术攻坚，{row_count} 行代码顺利合入。逻辑清晰，扩展性拉满，"
            "有这种大局观的高工操刀核心，团队很安心 🤝",
        ),
        (
            "PR Merged 🚢",
            "两天落地 {row_count} 行变更，吃透复杂业务还能丝滑落地。极其漂亮的硬核"
            "交付，给后续省了不少事 🍻",
        ),
        (
            "Merged 🚀",
            "短短两天顶住压力扛下 {row_count} 行变更，逻辑依然严密。"
            "这波极限输出真的很提振士气，大佬辛苦了 🫡",
        ),
    ),
    3: (
        (
            "Finally Merged 💣",
            "连干三天，最后把解法收进 {row_count} 行代码，绝对是碰上了深水雷。"
            "能在底层的烂摊子里耐住性子排雷，定力太强了。恭喜安全着陆 🪂",
        ),
        (
            "Merge Successful 🛡️",
            "三天连续作战，核心链路累计 {row_count} 行变更顺利合入。"
            "中间反复推演和修改，最终方案非常优雅。硬骨头啃得漂亮 🍻",
        ),
        (
            "PR Merged 🛠️",
            "历时三天的拉锯，{row_count} 行变更终于落地。上下游兼容还能做得"
            "滴水不漏，相当于给这模块做了个心肺复苏 👏",
        ),
        (
            "Merge Complete 🎉",
            "三天高强度作战，扛下 {row_count} 行变更。面对这份复杂上下文还能保持"
            "逻辑严密，没全局观真做不到。硬仗打赢了，好好休息 🛌",
        ),
    ),
    4: (
        (
            "Finally Merged 🧗",
            "数天的长线拉锯，最后浓缩成 {row_count} 行精妙的解法。"
            "全链路推演的含金量都在里面了，四两拨千斤，这波是真的秀 🍵",
        ),
        (
            "Merge Successful 🏆",
            "漫长的攻坚战终于告捷。无数次边界 Review 和方案讨论，才换来 "
            "{row_count} 行代码平稳落地。长线抗压极其考验功底，辛苦了 🍻",
        ),
        (
            "Approved & Merged 🚢",
            "跨越数天的硬仗！{row_count} 行核心重构终于合入。顶着让人头皮发麻的"
            "冲突和回归压力走到这里，大山总算搬平了，今晚必须彻底放空 🎮",
        ),
        (
            "PR MERGED 👑",
            "恭喜！跨越数天的硬仗！{row_count} 行变更终于顺利合入。反复打磨、"
            "解无数冲突还能守住质量底线。真正的核心战力，致敬 🫡",
        ),
    ),
}


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


@dataclasses.dataclass(frozen=True)
class MergeMetrics:
    changed_lines: int
    duration_days: int


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
        self.inferred: Dict[str, str] = {}

    def refresh(self) -> None:
        self.inferred = {}
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

    def remember(self, author: str, receiver: str) -> None:
        author = (author or "").strip()
        receiver = (receiver or "").strip()
        if not author or not receiver:
            return
        existing = self.inferred.get(author)
        if existing and existing.casefold() != receiver.casefold():
            raise ValueError(f"conflicting derived W3 recipients for Gitea author {author}")
        self.inferred[author] = receiver

    def resolve(self, author: str, inferred_receiver: str = "") -> Optional[str]:
        if author in self.mapping:
            receiver = self.mapping[author]
        elif inferred_receiver:
            receiver = inferred_receiver
        elif author in self.inferred:
            receiver = self.inferred[author]
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
        allow_merge_comments: bool = True,
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
        self.allow_merge_comments = allow_merge_comments
        self.clock = clock or _utc_now
        self.logger = logger or logging.getLogger(__name__)
        self._previous_snapshots = None
        self._confirmed_merged_prs = set()

    def poll_once(self) -> PollReport:
        started = time.monotonic()
        scanned_at = self.clock()
        report = PollReport(scanned_at=scanned_at)
        previous_snapshots = self._previous_snapshots
        missing_recipient_authors = set()
        recipients_ready = True
        listing_succeeded = False
        try:
            self.recipients.refresh()
        except ValueError as error:
            recipients_ready = False
            report.errors.append(str(error))
            self.logger.error("recipient mapping error: %s", error)

        try:
            listed_pulls = self.client.list_open_pulls(
                self.owner,
                self.repo,
                max_pages=self.max_pull_pages,
                limit=100,
            )
            pulls = [pr for pr in listed_pulls if not _ignored_pr_author(pr)]
            open_pr_numbers = {_pr_number(pr) for pr in pulls}
            report.open_prs = len(pulls)
            listing_succeeded = True
        except Exception as error:  # Keep pending deliveries moving during a Gitea outage.
            message = f"failed to list open pull requests: {error}"
            report.errors.append(message)
            self.logger.error(message)
            pulls = []
            open_pr_numbers = set()

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
                    self._process_snapshot(
                        snapshot,
                        report,
                        missing_recipient_authors,
                        recipients_ready,
                        count_as_open_scan=True,
                    )
                except Exception as error:
                    message = f"PR #{number or '?'} scan failed: {error}"
                    report.errors.append(message)
                    self.logger.error(message)

        retained_terminal_prs = set()
        if listing_succeeded:
            pending_terminal_prs = set(self.store.list_terminal_pending())
            for number in pending_terminal_prs & open_pr_numbers:
                self.store.clear_terminal_pending(number)
            pending_terminal_prs -= open_pr_numbers
            if previous_snapshots is not None:
                disappeared = set(previous_snapshots) - open_pr_numbers
                self.store.mark_terminal_pending(disappeared)
                pending_terminal_prs.update(disappeared)
            retained_terminal_prs = self._check_disappeared_pulls(
                pending_terminal_prs,
                scanned_at,
                report,
                missing_recipient_authors,
                recipients_ready,
            )

        for author in sorted(missing_recipient_authors):
            message = f"no W3 recipient could be derived or mapped for Gitea author {author}"
            report.errors.append(message)
            self.logger.error(message)

        if recipients_ready:
            self._dispatch_outbox(report)
        if listing_succeeded:
            self.store.prune_snapshots(open_pr_numbers | retained_terminal_prs)
            self._previous_snapshots = {
                snapshot.number: snapshot for snapshot in self.store.list_snapshots()
            }
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

    def _process_snapshot(
        self,
        snapshot: PrSnapshot,
        report: PollReport,
        missing_recipient_authors,
        recipients_ready: bool,
        *,
        count_as_open_scan: bool,
        merge_pull=None,
    ):
        self.recipients.remember(snapshot.author, snapshot.author_w3)
        if recipients_ready and not self.recipients.resolve(
            snapshot.author,
            snapshot.author_w3,
        ):
            missing_recipient_authors.add(snapshot.author)
        current = self.store.get_tracker(snapshot.number)
        current = self._migrate_problem_history(snapshot, current)
        result = poll_tracker(current, snapshot)
        event = self._event_for(
            snapshot,
            result.notifications,
            result.merge_success,
            merge_pull=merge_pull,
        )
        self.store.apply_poll(
            snapshot.number,
            result.state,
            event,
            snapshot=snapshot,
        )
        if result.request_merge_comment and not snapshot.merged:
            self._try_comment_merge(snapshot)
        if count_as_open_scan:
            report.scanned_prs += 1
        report.new_notifications += int(bool(result.notifications)) + int(
            result.merge_success
        )
        return result

    def _migrate_problem_history(
        self,
        snapshot: PrSnapshot,
        state: TrackerState,
    ) -> TrackerState:
        if PROBLEM_HISTORY_MARKER in state.notified_failure_keys:
            return state
        records = self.store.list_outbox_for_pr(snapshot.number)
        messages = [record.message for record in records]
        previous_snapshot = self.store.get_snapshot(snapshot.number)
        baselined_failures = ()
        if previous_snapshot and state.observed_command_key:
            previous_round = dataclasses.replace(
                previous_snapshot,
                latest_ci_command_key=state.observed_command_key,
            )
            previous_failure_key = round_failure_key(previous_round)
            previous_event_key = _notification_event_key(
                snapshot.number,
                state.observed_command_key,
                f"{previous_snapshot.latest_ci_command}:failure",
            )
            if (
                previous_failure_key in state.notified_failure_keys
                and not any(
                    record.event_key == previous_event_key for record in records
                )
            ):
                # Old baselines consumed a round without creating an outbox item.
                baselined_failures = stage_failures(previous_snapshot)
        historical_failures = tuple(_messaged_failures(messages))
        approval_notified = any(
            failure.context == "protected-file-approval"
            for failure in baselined_failures
        ) or any(
            failure.context == "protected-file-approval"
            for failure in historical_failures
        )
        return seed_problem_history(
            state,
            (*baselined_failures, *historical_failures),
            approval_notified=approval_notified,
        )

    def _check_disappeared_pulls(
        self,
        terminal_pr_numbers,
        scanned_at: str,
        report: PollReport,
        missing_recipient_authors,
        recipients_ready: bool,
    ):
        retained = set()
        for number in sorted(terminal_pr_numbers):
            try:
                pull = self.client.get_pull(self.owner, self.repo, number)
                if _ignored_pr_author(pull):
                    self.store.clear_terminal_pending(number)
                    continue
                if _pull_is_open(pull):
                    retained.add(number)
                    continue
                if _pull_state(pull) != "closed":
                    raise ValueError("pull request terminal response has no valid state")
                merged_flag = pull.get("merged")
                has_merged_at = bool(str(pull.get("merged_at") or "").strip())
                if not isinstance(merged_flag, bool) or merged_flag != has_merged_at:
                    raise ValueError("pull request terminal merge fields are inconsistent")
                if not _pull_is_merged(pull):
                    self.store.clear_terminal_pending(number)
                    continue
                snapshot = self._fetch_snapshot(pull, scanned_at)
                self._process_snapshot(
                    snapshot,
                    report,
                    missing_recipient_authors,
                    recipients_ready,
                    count_as_open_scan=False,
                    merge_pull=pull,
                )
                self._reconcile_merged_outbox(number)
                self._confirmed_merged_prs.add(number)
                self.store.clear_terminal_pending(number)
            except Exception as error:
                retained.add(number)
                message = f"PR #{number} terminal check failed: {error}"
                report.errors.append(message)
                self.logger.error(message)
        return retained

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
        merge_success: bool = False,
        merge_pull=None,
    ) -> Optional[OutboxEvent]:
        if not failures and not merge_success:
            return None
        kind = "merge-success" if merge_success else f"{snapshot.latest_ci_command}:failure"
        digest = _notification_event_key(
            snapshot.number,
            snapshot.latest_ci_command_key,
            kind,
        )
        merge_metrics = (
            self._load_merge_metrics(snapshot, merge_pull) if merge_success else None
        )
        return OutboxEvent(
            event_key=digest,
            pr_number=snapshot.number,
            author=snapshot.author,
            message=format_message(
                snapshot,
                failures,
                merge_success=merge_success,
                merge_metrics=merge_metrics,
            ),
            receiver_hint=snapshot.author_w3,
        )

    def _load_merge_metrics(
        self,
        snapshot: PrSnapshot,
        pull=None,
    ) -> Optional[MergeMetrics]:
        try:
            detail = (
                pull
                if pull is not None
                else self.client.get_pull(self.owner, self.repo, snapshot.number)
            )
            completed_at = snapshot.merged_at or snapshot.scanned_at
            return _merge_metrics_from_pull(detail, completed_at)
        except Exception as error:
            self.logger.warning(
                "could not load merge metrics for PR #%s; using the fallback "
                "success message: %s",
                snapshot.number,
                error,
            )
            return None

    def _try_comment_merge(self, snapshot: PrSnapshot) -> None:
        if not self.allow_merge_comments:
            self.logger.info(
                "skipping automatic /ci merge on PR #%s because outbound "
                "comments are disabled",
                snapshot.number,
            )
            return
        try:
            comments = self.client.get_issue_comments(
                self.owner,
                self.repo,
                snapshot.number,
                max_pages=self.max_comment_pages,
            )
        except Exception as error:
            self.logger.warning(
                "could not verify the latest comment on PR #%s; "
                "skipping automatic /ci merge: %s",
                snapshot.number,
                error,
            )
            return

        if _latest_ci_command(comments) == "/ci merge":
            self.logger.info(
                "skipping automatic /ci merge on PR #%s because the latest "
                "CI command is already /ci merge",
                snapshot.number,
            )
            return

        try:
            self.client.create_issue_comment(
                self.owner,
                self.repo,
                snapshot.number,
                "/ci merge",
            )
        except Exception as error:
            self.logger.warning(
                "could not comment /ci merge on PR #%s; this round will not retry: %s",
                snapshot.number,
                error,
            )

    def _dispatch_outbox(self, report: PollReport) -> None:
        records = self.store.list_dispatchable(self.max_send_attempts)
        latest_merge_success = {}
        for record in records:
            if (
                record.author.strip().casefold() not in IGNORED_PR_AUTHORS
                and _is_merge_success_message(record.message)
            ):
                latest_merge_success[record.pr_number] = record

        merge_confirmed = {
            pr_number: self._merge_confirmed(record, report)
            for pr_number, record in latest_merge_success.items()
        }

        for record in records:
            if record.author.strip().casefold() in IGNORED_PR_AUTHORS:
                self.store.update_delivery(
                    record.id,
                    "suppressed",
                    record.receiver,
                    "notification suppressed for ignored PR author",
                    increment_attempt=False,
                )
                continue
            if (
                record.pr_number in self._confirmed_merged_prs
                and _is_failure_message(record.message)
            ):
                self.store.update_delivery(
                    record.id,
                    "suppressed",
                    record.receiver,
                    "failure notification superseded by actual PR merge",
                    increment_attempt=False,
                )
                continue
            if (
                _is_merge_success_message(record.message)
                and latest_merge_success.get(record.pr_number) != record
            ):
                self.store.update_delivery(
                    record.id,
                    "suppressed",
                    record.receiver,
                    "merge success superseded by a newer message",
                    increment_attempt=False,
                )
                continue
            receiver = self.recipients.resolve(record.author, record.receiver)
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
            original_recipient_opted_out = bool(
                record.recipient_employee_number
                and self.store.is_recipient_opted_out(
                    record.recipient_employee_number
                )
            )
            if original_recipient_opted_out or self.store.is_recipient_opted_out(receiver):
                self.store.update_delivery(
                    record.id,
                    "suppressed",
                    receiver,
                    "notification suppressed by recipient preference",
                    increment_attempt=False,
                )
                continue
            if _is_merge_success_message(record.message) and not merge_confirmed.get(
                record.pr_number,
                False,
            ):
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

    def _merge_confirmed(self, record, report: PollReport) -> bool:
        if self.store.is_terminal_pending(record.pr_number):
            return False
        if record.pr_number in self._confirmed_merged_prs:
            return self._reconcile_merged_outbox(
                record.pr_number,
                preferred_success_id=record.id,
            ) == record.id
        try:
            pull = self.client.get_pull(self.owner, self.repo, record.pr_number)
        except Exception as error:
            message = (
                f"PR #{record.pr_number} merge confirmation failed for "
                f"outbox #{record.id}: {error}"
            )
            report.errors.append(message)
            self.logger.error(message)
            return False

        state = _pull_state(pull)
        if state == "open":
            return False
        merged_flag = pull.get("merged") if isinstance(pull, dict) else None
        has_merged_at = bool(
            str(pull.get("merged_at") or "").strip()
        ) if isinstance(pull, dict) else False
        if state != "closed" or not isinstance(merged_flag, bool) or (
            merged_flag != has_merged_at
        ):
            message = (
                f"PR #{record.pr_number} returned inconsistent merge fields for "
                f"outbox #{record.id}"
            )
            report.errors.append(message)
            self.logger.error(message)
            return False
        if not merged_flag:
            self.store.update_delivery(
                record.id,
                "suppressed",
                receiver=record.receiver,
                last_error="merge success suppressed because PR closed without merging",
                increment_attempt=False,
            )
            return False

        canonical_success_id = self._reconcile_merged_outbox(
            record.pr_number,
            preferred_success_id=record.id,
        )
        self._confirmed_merged_prs.add(record.pr_number)
        return canonical_success_id == record.id

    def _reconcile_merged_outbox(
        self,
        pr_number: int,
        preferred_success_id: Optional[int] = None,
    ) -> Optional[int]:
        records = self.store.list_outbox_for_pr(pr_number)
        successes = [
            record for record in records if _is_merge_success_message(record.message)
        ]
        sent = [record for record in successes if record.status == "sent"]
        uncertain = [
            record for record in successes if record.status == "uncertain"
        ]
        preferred = next(
            (
                record
                for record in successes
                if record.id == preferred_success_id
                and record.status not in {"sent", "suppressed"}
            ),
            None,
        )
        candidates = [
            record
            for record in successes
            if record.status not in {"sent", "suppressed"}
        ]
        if sent:
            canonical = max(sent, key=lambda record: record.id)
        elif uncertain:
            canonical = max(uncertain, key=lambda record: record.id)
        elif preferred is not None:
            canonical = preferred
        else:
            canonical = max(candidates, key=lambda record: record.id) if candidates else None

        for record in records:
            if record.status in {"sent", "suppressed"}:
                continue
            stale_failure = _is_failure_message(record.message)
            stale_success = (
                _is_merge_success_message(record.message)
                and (canonical is None or record.id != canonical.id)
            )
            if not stale_failure and not stale_success:
                continue
            reason = (
                "failure notification superseded by actual PR merge"
                if stale_failure
                else "merge success superseded by the canonical PR notification"
            )
            self.store.update_delivery(
                record.id,
                "suppressed",
                record.receiver,
                reason,
                increment_attempt=False,
            )
        return canonical.id if canonical is not None else None


def format_message(
    snapshot: PrSnapshot,
    failures,
    merge_success: bool = False,
    merge_metrics: Optional[MergeMetrics] = None,
) -> str:
    footer = "【Taichu PRbot 自动发送，回复TD退订】"
    if merge_success:
        return _format_merge_success(snapshot, footer, merge_metrics)

    problems = "；".join(
        f"{failure.context}：{notification_summary(failure.context, failure.summary)}"
        for failure in failures
    )
    return (
        f"[TaiChu PR {snapshot.number}] 发现问题：{problems} "
        f"{footer} 查看 {snapshot.url}"
    )


def _format_merge_success(
    snapshot: PrSnapshot,
    footer: str,
    metrics: Optional[MergeMetrics],
) -> str:
    if metrics is None:
        return (
            f"🎉🎊 [TaiChu PR {snapshot.number}] Merge 成功啦！"
            "这一关真的不容易，反复排障、耐心等待和一次次坚持都没有白费。"
            "所有门禁终于全部通过，恭喜顺利合入！"
            "辛苦了，为你鼓掌，这一刻值得好好庆祝！ 🥳✨🏆 "
            f"{footer} 查看 {snapshot.url}"
        )

    if metrics.changed_lines < 500:
        line_bucket = 0
    elif metrics.changed_lines < 1500:
        line_bucket = 1
    elif metrics.changed_lines <= 2500:
        line_bucket = 2
    else:
        line_bucket = 3
    duration_bucket = metrics.duration_days if metrics.duration_days <= 3 else 4
    title, body_template = MERGE_SUCCESS_COPY[duration_bucket][line_bucket]
    body = body_template.format(row_count=str(metrics.changed_lines))
    return (
        f"[TaiChu PR {snapshot.number}] {title} "
        f"{body} "
        f"{footer} 查看 {snapshot.url}"
    )


def _merge_metrics_from_pull(
    pull,
    completed_at: str,
) -> Optional[MergeMetrics]:
    additions = (
        _nonnegative_integer(pull.get("additions"))
        if isinstance(pull, dict)
        else None
    )
    deletions = (
        _nonnegative_integer(pull.get("deletions"))
        if isinstance(pull, dict)
        else None
    )
    if additions is None or deletions is None:
        return None
    duration_days = _duration_days(str(pull.get("created_at") or ""), completed_at)
    if duration_days is None:
        return None
    return MergeMetrics(additions + deletions, duration_days)


def _nonnegative_integer(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not raw.isascii() or not raw.isdigit():
            return None
        parsed = int(raw)
    else:
        return None
    return parsed if parsed >= 0 else None


def _duration_days(created_at: str, completed_at: str) -> Optional[int]:
    created = _parse_timestamp(created_at)
    completed = _parse_timestamp(completed_at)
    if created is None or completed is None or completed < created:
        return None
    elapsed_days = math.ceil((completed - created).total_seconds() / 86400)
    return max(1, elapsed_days)


def _parse_timestamp(value: str) -> Optional[dt.datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=dt.timezone.utc)
    )


def _delivery_error(result) -> str:
    detail = (result.stderr or result.stdout or "welink-cli returned no detail").strip()
    code = "none" if result.exit_code is None else str(result.exit_code)
    return f"status={result.status}, exit={code}: {detail}"


def _pr_number(pr) -> int:
    try:
        return int(pr.get("number") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _ignored_pr_author(pr) -> bool:
    user = pr.get("user") if isinstance(pr, dict) else None
    login = user.get("login") if isinstance(user, dict) else ""
    return str(login or "").strip().casefold() in IGNORED_PR_AUTHORS


def _pull_is_merged(pull) -> bool:
    if not isinstance(pull, dict):
        return False
    return pull.get("merged") is True and bool(
        str(pull.get("merged_at") or "").strip()
    )


def _pull_is_open(pull) -> bool:
    return _pull_state(pull) == "open"


def _pull_state(pull) -> str:
    if not isinstance(pull, dict):
        return ""
    return str(pull.get("state") or "").strip().lower()


def _is_merge_success_message(message: str) -> bool:
    text = str(message or "")
    if "发现问题：" in text or "[TaiChu PR " not in text:
        return False
    return any(
        marker in text
        for marker in (
            "Merge Successful",
            "PR Merged",
            "Merged ",
            "Merge Complete",
            "Code Integrated",
            "Finally Merged",
            "Approved & Merged",
            "PR MERGED",
            "Merge 成功啦",
        )
    )


def _is_failure_message(message: str) -> bool:
    text = str(message or "")
    return "[TaiChu PR " in text and "发现问题：" in text


def _notification_event_key(
    pr_number: int,
    command_key: str,
    kind: str,
) -> str:
    return hashlib.sha256(
        f"{pr_number}\0{command_key}\0{kind}".encode("utf-8")
    ).hexdigest()


def _messaged_failures(messages: Iterable[str]) -> Iterable[GateFailure]:
    contexts = "|".join(re.escape(context) for context in GATE_CONTEXTS)
    end = (
        rf"(?=[；;]\s*(?:{contexts})[：:]|"
        rf"\n\s*-\s*(?:{contexts})[：:]|"
        rf"[；;]?\s*【Taichu|"
        rf"[；;]?\s*查看\s+(?:https?://|\[https?://)|"
        rf"\n\s*下一步[：:]|"
        rf"\n\s*查看[：:]\s*(?:https?://|\[https?://)|$)"
    )
    for message in messages:
        text = str(message or "")
        for context in GATE_CONTEXTS:
            match = re.search(
                rf"(?:发现问题：|[；;]|\n\s*-\s*)\s*"
                rf"{re.escape(context)}[：:]\s*(.*?){end}",
                text,
                flags=re.DOTALL,
            )
            if match is None:
                continue
            summary = match.group(1).strip(" \t\r\n；;")
            if summary:
                yield GateFailure(context, "", summary)


def _latest_ci_command(comments) -> str:
    latest_command = ""
    latest_key = ((0, 0.0, ""), -1, -1)
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            continue
        command = exact_ci_command(comment.get("body"))
        if not command:
            continue
        try:
            comment_id = int(comment.get("id") or -1)
        except (TypeError, ValueError):
            comment_id = -1
        key = (_comment_created_key(comment.get("created_at")), comment_id, index)
        if key >= latest_key:
            latest_command = command
            latest_key = key
    return latest_command


def _comment_created_key(value):
    raw = str(value or "").strip()
    if not raw:
        return (0, 0.0, "")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return (1, parsed.timestamp(), raw)
    except ValueError:
        return (0, 0.0, raw)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
