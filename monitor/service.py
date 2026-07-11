"""One polling cycle and durable WeLink delivery orchestration."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from .core import (
    PrSnapshot,
    build_pr_snapshot,
    exact_ci_command,
    notification_summary,
    notification_text,
    poll_tracker,
)
from .gitea import DEFAULT_OWNER, DEFAULT_REPO, DEFAULT_WEB_BASE
from .state import MonitorStore, OutboxEvent


MERGE_SUCCESS_COPY = {
    1: (
        (
            "Merge Successful 🔪",
            "小几百行代码一天搞定，改得非常准。不需要冗长废话就能把痛点切掉，"
            "老医生的刀法。代码已上膛，干得漂亮！🍻",
        ),
        (
            "PR Merged 🚀",
            "一天内输出上千行，且逻辑闭环无 Bug。这手速和状态绝了，机器跑得都没你"
            "脑子转得快。今天必须早点下班 ☕",
        ),
        (
            "Merged ⚡",
            "一天爆肝如此多高质量代码，Review 居然挑不出什么毛病。这交付效率属实"
            "拉满了，大佬牛的 👏",
        ),
        (
            "Merge Complete 🤯",
            "24小时撸出快三千行代码，还能保证高标准的测试覆盖。这单兵突击能力太"
            "硬核了，赶紧让键盘和脑子都降降温 🧊",
        ),
    ),
    2: (
        (
            "Code Integrated 💎",
            "两天时间打磨这几百行核心逻辑，代码极其精炼。懂的都懂，脑子里估计把"
            "并发和边界推演了无数遍。极简就是最高级，辛苦 ☕",
        ),
        (
            "Merge Successful 🛠️",
            "两天的战术攻坚，千行级别的重组顺利合入。逻辑清晰，扩展性拉满，有这种"
            "大局观的高工操刀核心，团队很安心 🤝",
        ),
        (
            "PR Merged 🚢",
            "两天落地如此多的架构演进，吃透复杂业务还能丝滑落地。极其漂亮的硬核"
            "交付，给后续省了不少事 🍻",
        ),
        (
            "Merged 🚀",
            "短短两天顶住压力交付这么庞大的变更，逻辑依然严密。这波极限输出真的很"
            "提振士气，大佬辛苦了 🫡",
        ),
    ),
    3: (
        (
            "Finally Merged 💣",
            "连干三天就合这几百行，绝对是碰上了深水雷。能在底层的烂摊子里耐住性子"
            "排雷，定力太强了。恭喜安全着陆 🪂",
        ),
        (
            "Merge Successful 🛡️",
            "三天连续作战，千行级核心链路顺利合入。中间推演修改了这么多次，最终方案"
            "非常优雅。硬骨头啃得漂亮 🍻",
        ),
        (
            "PR Merged 🛠️",
            "历时三天的拉锯，重磅变更终于落地。这么大改动还能把上下游兼容做滴水"
            "不漏，相当于给这模块做了个心肺复苏 👏",
        ),
        (
            "Merge Complete 🎉",
            "三天高强度作战，扛住逼近三千行的变更。庞大上下文里还能保持逻辑严密，"
            "没全局观真做不到。硬仗打赢了，好好休息 🛌",
        ),
    ),
    4: (
        (
            "Finally Merged 🧗",
            "数天的长线拉锯，最后浓缩成这几百行精妙的解法。全链路推演的含金量全在"
            "里面了，四两拨千斤，这波是真的秀 🍵",
        ),
        (
            "Merge Successful 🏆",
            "漫长的攻坚战终于告捷。无数次边界 Review 和方案讨论，才换来这千行代码的"
            "平稳落地。长线抗压极其考验功底，辛苦了 🍻",
        ),
        (
            "Approved & Merged 🚢",
            "跨越数天的硬仗！如此多核心重构，顶着让人头皮发麻的冲突和回归压力终于"
            "合入。大山搬平了，今晚必须彻底放空 🎮",
        ),
        (
            "PR MERGED 👑",
            "恭喜！跨越数天、逼近极限的长跑终于合入。反复打磨、解无数冲突还能守住"
            "质量底线。真正的核心战力，致敬 🫡",
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

    def poll_once(self) -> PollReport:
        started = time.monotonic()
        scanned_at = self.clock()
        report = PollReport(scanned_at=scanned_at)
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
                    self.recipients.remember(snapshot.author, snapshot.author_w3)
                    if recipients_ready and not self.recipients.resolve(
                        snapshot.author,
                        snapshot.author_w3,
                    ):
                        missing_recipient_authors.add(snapshot.author)
                    current = self.store.get_tracker(snapshot.number)
                    result = poll_tracker(current, snapshot)
                    event = self._event_for(
                        snapshot,
                        result.notifications,
                        result.merge_success,
                    )
                    self.store.apply_poll(
                        snapshot.number,
                        result.state,
                        event,
                        snapshot=snapshot,
                    )
                    if result.request_merge_comment:
                        self._try_comment_merge(snapshot)
                    report.scanned_prs += 1
                    report.new_notifications += int(bool(result.notifications)) + int(
                        result.merge_success
                    )
                except Exception as error:
                    message = f"PR #{number or '?'} scan failed: {error}"
                    report.errors.append(message)
                    self.logger.error(message)

        for author in sorted(missing_recipient_authors):
            message = f"no W3 recipient could be derived or mapped for Gitea author {author}"
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
        merge_success: bool = False,
    ) -> Optional[OutboxEvent]:
        if not failures and not merge_success:
            return None
        kind = "merge-success" if merge_success else f"{snapshot.latest_ci_command}:failure"
        digest = hashlib.sha256(
            (
                str(snapshot.number)
                + "\0"
                + snapshot.latest_ci_command_key
                + "\0"
                + kind
            ).encode("utf-8")
        ).hexdigest()
        merge_metrics = self._load_merge_metrics(snapshot) if merge_success else None
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

    def _load_merge_metrics(self, snapshot: PrSnapshot) -> Optional[MergeMetrics]:
        try:
            pull = self.client.get_pull(self.owner, self.repo, snapshot.number)
            return _merge_metrics_from_pull(pull, snapshot.scanned_at)
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
        for record in self.store.list_dispatchable(self.max_send_attempts):
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
    title, body = MERGE_SUCCESS_COPY[duration_bucket][line_bucket]
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
