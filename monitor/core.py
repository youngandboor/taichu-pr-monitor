"""Gate classification and notification tracking ported from the Android client."""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


DEFAULT_POLL_INTERVAL_SECONDS = 180

GATE_CONTEXTS = (
    "protected-file-approval",
    "taichu/codex-pr-review",
    "taichu/pr-build",
    "taichu/dev-cloud-preflight",
    "ci/merge-gate",
)

BUILD_GATE_CONTEXTS = (
    "protected-file-approval",
    "taichu/codex-pr-review",
    "taichu/pr-build",
)

BUILD_PRECONDITION_CONTEXTS = (
    "protected-file-approval",
    "taichu/codex-pr-review",
)

MERGE_GATE_CONTEXTS = (
    "taichu/dev-cloud-preflight",
    "ci/merge-gate",
)

# Validated surname readings cover current TaiChu authors plus common surnames.
# Unknown or ambiguous data fails closed and can be handled by a recipient override.
_SURNAME_INITIALS = {
    "安": "a", "白": "b", "鲍": "b", "毕": "b", "卞": "b", "卜": "b", "柏": "b",
    "曹": "c", "常": "c", "陈": "c", "程": "c", "崔": "c", "蔡": "c", "岑": "c", "褚": "c",
    "戴": "d", "邓": "d", "丁": "d", "董": "d", "杜": "d", "窦": "d", "段": "d",
    "范": "f", "方": "f", "费": "f", "冯": "f", "傅": "f", "符": "f",
    "高": "g", "葛": "g", "龚": "g", "顾": "g", "郭": "g",
    "韩": "h", "何": "h", "贺": "h", "郝": "h", "胡": "h", "黄": "h", "华": "h", "侯": "h", "洪": "h",
    "纪": "j", "贾": "j", "姜": "j", "江": "j", "蒋": "j", "金": "j",
    "康": "k", "孔": "k", "柯": "k", "邝": "k",
    "赖": "l", "雷": "l", "黎": "l", "李": "l", "连": "l", "廉": "l", "梁": "l", "廖": "l", "林": "l", "凌": "l", "刘": "l", "柳": "l", "龙": "l", "卢": "l", "鲁": "l", "陆": "l", "罗": "l", "吕": "l",
    "马": "m", "毛": "m", "孟": "m", "苗": "m", "莫": "m", "穆": "m",
    "倪": "n", "聂": "n", "宁": "n", "牛": "n",
    "欧": "o", "区": "o",
    "潘": "p", "彭": "p", "皮": "p", "平": "p", "蒲": "p",
    "钱": "q", "秦": "q", "齐": "q", "乔": "q", "邱": "q", "仇": "q",
    "任": "r", "饶": "r",
    "单": "s", "邵": "s", "沈": "s", "施": "s", "石": "s", "史": "s", "时": "s", "宋": "s", "苏": "s", "孙": "s",
    "谭": "t", "唐": "t", "汤": "t", "陶": "t", "滕": "t", "田": "t", "童": "t",
    "万": "w", "汪": "w", "王": "w", "韦": "w", "魏": "w", "温": "w", "文": "w", "翁": "w", "巫": "w", "邬": "w", "吴": "w", "伍": "w", "武": "w", "兀": "w",
    "夏": "x", "萧": "x", "肖": "x", "谢": "x", "辛": "x", "熊": "x", "徐": "x", "许": "x", "薛": "x", "解": "x",
    "严": "y", "闫": "y", "颜": "y", "杨": "y", "姚": "y", "叶": "y", "易": "y", "殷": "y", "尹": "y", "尤": "y", "于": "y", "余": "y", "俞": "y", "袁": "y", "岳": "y", "乐": "y",
    "臧": "z", "曾": "z", "翟": "z", "詹": "z", "张": "z", "章": "z", "赵": "z", "郑": "z", "钟": "z", "周": "z", "朱": "z", "祝": "z", "庄": "z", "邹": "z", "查": "z",
}


@dataclasses.dataclass(frozen=True)
class GateFailure:
    context: str
    updated_at: str
    summary: str


@dataclasses.dataclass(frozen=True)
class GateResult:
    context: str
    state: str
    updated_at: str
    summary: str


@dataclasses.dataclass(frozen=True)
class PrSnapshot:
    number: int
    title: str
    author: str
    head_sha: str
    url: str
    latest_ci_command: str
    latest_ci_command_at: str
    latest_ci_command_key: str
    scanned_at: str
    failures: Tuple[GateFailure, ...]
    author_w3: str = ""
    pr_build_state: str = ""
    pr_build_updated_at: str = ""
    pr_build_summary: str = ""
    gate_results: Tuple[GateResult, ...] = ()


@dataclasses.dataclass(frozen=True)
class TrackerState:
    observed_command_key: str
    notified_failure_keys: frozenset
    initialized: bool
    last_scanned_at: str

    @classmethod
    def empty(cls) -> "TrackerState":
        return cls("", frozenset(), False, "")


@dataclasses.dataclass(frozen=True)
class TrackerResult:
    state: TrackerState
    notifications: Tuple[GateFailure, ...]
    request_merge_comment: bool = False
    merge_success: bool = False


@dataclasses.dataclass(frozen=True)
class _GateCandidate:
    context: str
    state: str
    summary: str
    updated_at: str
    item_id: int


def effective_state(state: str, summary: str) -> str:
    """Return the same effective gate state used by GateStateClassifier.java."""
    normalized_state = _value(state).strip().lower()
    lower_summary = _value(summary).lower()
    if _has_definitive_failure_signal(normalized_state, lower_summary):
        return "failure"
    if _has_definitive_success_signal(lower_summary):
        return "success"
    if any(marker in lower_summary for marker in ("failed", "failure", "error")):
        return "failure"
    if normalized_state in {"successful", "passed", "passing", "ok"}:
        return "success"
    if normalized_state:
        return normalized_state
    if _has_success_signal(lower_summary):
        return "success"
    return "unknown"


def is_actionable_failure(state: str, summary: str) -> bool:
    return effective_state(state, summary) == "failure"


def normalize_gate_context(context: str) -> str:
    lower = _value(context).lower()
    if "protected-file-approval" in lower:
        return "protected-file-approval"
    if "taichu/codex-pr-review" in lower:
        return "taichu/codex-pr-review"
    if "taichu/pr-build" in lower:
        return "taichu/pr-build"
    if "taichu/dev-cloud-preflight" in lower:
        return "taichu/dev-cloud-preflight"
    if "ci/merge-gate" in lower:
        return "ci/merge-gate"
    return ""


def build_pr_snapshot(
    pr: Mapping[str, Any],
    statuses: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    scanned_at: str,
    web_base: str = "https://taichu.fun/gitea",
    owner: str = "SystemAgentDev",
    repo: str = "TaiChu",
) -> PrSnapshot:
    number = _integer(pr.get("number"))
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    user = pr.get("user") if isinstance(pr.get("user"), Mapping) else {}
    head_sha = _value(head.get("sha")).strip()
    author = _value(user.get("login")).strip()
    if number <= 0:
        raise ValueError("pull request response has no valid number")
    if not head_sha:
        raise ValueError("pull request response has no head sha")
    if not author:
        raise ValueError("pull request response has no author login")

    latest_by_context: Dict[str, _GateCandidate] = {}
    for status in statuses:
        context = normalize_gate_context(status.get("context") or status.get("name") or "")
        if not context:
            continue
        raw_state = _value(status.get("state") or status.get("status"))
        summary = _value(status.get("description") or raw_state)
        candidate = _GateCandidate(
            context=context,
            state=effective_state(raw_state, summary),
            summary=summary,
            updated_at=_timestamp(status),
            item_id=_integer(status.get("id")),
        )
        _put_latest(latest_by_context, candidate)

    latest_command = ""
    latest_command_at = ""
    latest_command_key = ""
    latest_command_id = -1
    for comment in comments:
        command = exact_ci_command(comment.get("body"))
        updated_at = _timestamp(comment)
        comment_id = _integer(comment.get("id"))
        if command and (_time_key(updated_at), comment_id) >= (
            _time_key(latest_command_at),
            latest_command_id,
        ):
            latest_command = command
            latest_command_at = updated_at
            latest_command_id = comment_id
            latest_command_key = f"{number}:{command}:{updated_at}:{comment_id}"

        candidate = _gate_from_comment(comment, head_sha)
        if candidate is not None:
            _put_latest(latest_by_context, candidate)

    failures = []
    for context in GATE_CONTEXTS:
        candidate = latest_by_context.get(context)
        if candidate and is_actionable_failure(candidate.state, candidate.summary):
            failures.append(GateFailure(context, candidate.updated_at, candidate.summary))

    pr_build = latest_by_context.get("taichu/pr-build")
    gate_results = tuple(
        GateResult(
            candidate.context,
            candidate.state,
            candidate.updated_at,
            candidate.summary,
        )
        for context in GATE_CONTEXTS
        if (candidate := latest_by_context.get(context)) is not None
    )

    url = _value(pr.get("html_url")).strip()
    if not url:
        url = f"{web_base.rstrip('/')}/{owner}/{repo}/pulls/{number}"
    return PrSnapshot(
        number=number,
        title=_value(pr.get("title")).strip(),
        author=author,
        head_sha=head_sha,
        url=url,
        latest_ci_command=latest_command,
        latest_ci_command_at=latest_command_at,
        latest_ci_command_key=latest_command_key,
        scanned_at=_value(scanned_at),
        failures=tuple(failures),
        author_w3=derive_w3_account(user),
        pr_build_state=pr_build.state if pr_build else "",
        pr_build_updated_at=pr_build.updated_at if pr_build else "",
        pr_build_summary=pr_build.summary if pr_build else "",
        gate_results=gate_results,
    )


def derive_w3_account(user: Mapping[str, Any]) -> str:
    """Derive the internal W3 account without guessing missing identity data."""
    full_name = _value(user.get("full_name")).strip()
    explicit = re.search(r"\b([A-Za-z]\d{8})\s*$", full_name)
    if explicit:
        return explicit.group(1).lower()
    employee_number = re.search(r"(\d{8})\s*$", full_name)
    if not employee_number:
        return ""
    display_name = full_name[: employee_number.start()].strip()
    if not display_name:
        return ""
    first = display_name[0]
    if first.isascii() and first.isalpha():
        initial = first.lower()
    else:
        initial = _SURNAME_INITIALS.get(first, "")
    return initial + employee_number.group(1) if initial else ""


def poll_tracker(state: TrackerState, snapshot: PrSnapshot) -> TrackerResult:
    if not snapshot.latest_ci_command_key:
        next_state = TrackerState(
            state.observed_command_key,
            state.notified_failure_keys,
            True,
            _scan_watermark(state, snapshot),
        )
        return TrackerResult(next_state, ())

    if not state.initialized:
        return TrackerResult(_initialize_baseline(state, snapshot), ())

    notified = (
        set(state.notified_failure_keys)
        if snapshot.latest_ci_command_key == state.observed_command_key
        else set()
    )
    new_command_round = snapshot.latest_ci_command_key != state.observed_command_key
    failures = tuple(_stage_failures_after_command(snapshot))
    notifications: Tuple[GateFailure, ...] = ()
    failure_round_key = round_failure_key(snapshot)
    if failures and failure_round_key not in notified:
        command_started_after_scan = new_command_round
        if command_started_after_scan or any(
            _happened_at_or_after_scan(failure.updated_at, state.last_scanned_at)
            for failure in failures
        ):
            notifications = failures
        notified.add(failure_round_key)

    request_merge_comment = False
    merge_success = False
    completion_key = stage_success_key(snapshot)
    if completion_key and completion_key not in notified:
        completed_at = _stage_completed_at(snapshot)
        if new_command_round or _happened_at_or_after_scan(
            completed_at,
            state.last_scanned_at,
        ):
            if snapshot.latest_ci_command == "/ci build":
                request_merge_comment = True
            elif snapshot.latest_ci_command == "/ci merge":
                merge_success = True
        notified.add(completion_key)

    next_state = TrackerState(
        snapshot.latest_ci_command_key,
        frozenset(notified),
        True,
        _scan_watermark(state, snapshot),
    )
    return TrackerResult(
        next_state,
        notifications,
        request_merge_comment=request_merge_comment,
        merge_success=merge_success,
    )


def failure_key(snapshot: PrSnapshot, failure: GateFailure) -> str:
    return ":".join(
        (
            snapshot.latest_ci_command_key,
            failure.context,
            failure.updated_at,
            notification_text(failure.summary),
        )
    )


def round_failure_key(snapshot: PrSnapshot) -> str:
    stage = _command_stage(snapshot.latest_ci_command)
    if not stage or not snapshot.latest_ci_command_key:
        return ""
    return f"{snapshot.latest_ci_command_key}:{stage}:failure-notified"


def stage_success_key(snapshot: PrSnapshot) -> str:
    stage = _command_stage(snapshot.latest_ci_command)
    if not stage or not _stage_succeeded_after_command(snapshot):
        return ""
    action = "merge-comment-attempted" if stage == "build" else "success-notified"
    return f"{snapshot.latest_ci_command_key}:{stage}:{action}"


def build_success_key(snapshot: PrSnapshot) -> str:
    """Compatibility alias for the persisted build-completion round flag."""
    if snapshot.latest_ci_command != "/ci build":
        return ""
    return stage_success_key(snapshot)


def notification_text(value: str) -> str:
    text = _notification_plain_text(value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "评论无可展示内容"
    return text if len(text) <= 160 else text[:159].strip() + "..."


def notification_summary(context: str, value: str) -> str:
    text = _notification_plain_text(value)
    labeled = _labeled_failure_summary(text)
    if labeled:
        return notification_text(labeled)

    if normalize_gate_context(context) == "taichu/codex-pr-review":
        match = re.search(
            r"Codex\s+found\s+\d+\s+P0/P1\s+principle\s+issue(?:\(s\)|s)?",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(0).strip()

    text = re.sub(
        r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
        r"(?:[.,]\d+)?(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?\s*(?:\|+|[-–—])\s*",
        "",
        text,
    )
    return notification_text(text)


def _notification_plain_text(value: str) -> str:
    text = _value(value)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]*>", "", text, flags=re.DOTALL)
    text = html.unescape(text)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip()


def _labeled_failure_summary(value: str) -> str:
    lines = _value(value).splitlines()
    label = re.compile(
        r"^\s*(?:[-*]\s*)?(?:失败摘要|失败原因|错误摘要|"
        r"failure\s+summary|failure\s+reason|error\s+summary)\s*[：:]\s*(.*)$",
        flags=re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = label.match(line)
        if not match:
            continue
        summary = match.group(1).strip()
        if not summary:
            for following in lines[index + 1 :]:
                summary = following.strip(" -*\t")
                if summary:
                    break
        if summary:
            return summary.rstrip("；;")

    inline = re.search(
        r"(?:失败摘要|失败原因|错误摘要|failure\s+summary|failure\s+reason|"
        r"error\s+summary)\s*[：:]\s*(.+)",
        _value(value),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not inline:
        return ""
    summary = inline.group(1).strip()
    summary = re.split(
        r"\s+(?=(?:构建产物|产物链接|详情链接|当前\s*(?:PR\s*)?head|"
        r"PR\s*head|head\s*sha)\s*(?:[：:(（]|$))",
        summary,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return summary.rstrip("；;").strip()


def exact_ci_command(value: Any) -> str:
    command = _value(value).strip().lower()
    return command if command in {"/ci build", "/ci merge"} else ""


def _initialize_baseline(state: TrackerState, snapshot: PrSnapshot) -> TrackerState:
    notified = (
        set(state.notified_failure_keys)
        if snapshot.latest_ci_command_key == state.observed_command_key
        else set()
    )
    if any(_stage_failures_after_command(snapshot)):
        notified.add(round_failure_key(snapshot))
    success_key = stage_success_key(snapshot)
    if success_key:
        notified.add(success_key)
    return TrackerState(
        snapshot.latest_ci_command_key,
        frozenset(notified),
        True,
        _scan_watermark(state, snapshot),
    )


def _stage_failures_after_command(snapshot: PrSnapshot) -> Iterable[GateFailure]:
    contexts = _stage_contexts(snapshot.latest_ci_command)
    for failure in snapshot.failures:
        if (
            failure.context in contexts
            and _gate_result_belongs_to_round(
                snapshot.latest_ci_command,
                failure.context,
                failure.updated_at,
                snapshot.latest_ci_command_at,
            )
        ):
            yield failure


def _stage_succeeded_after_command(snapshot: PrSnapshot) -> bool:
    contexts = _stage_contexts(snapshot.latest_ci_command)
    if (
        not contexts
        or not snapshot.latest_ci_command_key
        or not snapshot.latest_ci_command_at
    ):
        return False
    results = {result.context: result for result in snapshot.gate_results}
    return all(
        context in results
        and results[context].state == "success"
        and _gate_result_belongs_to_round(
            snapshot.latest_ci_command,
            context,
            results[context].updated_at,
            snapshot.latest_ci_command_at,
        )
        for context in contexts
    )


def _stage_completed_at(snapshot: PrSnapshot) -> str:
    contexts = _stage_contexts(snapshot.latest_ci_command)
    candidates = [
        result.updated_at
        for result in snapshot.gate_results
        if result.context in contexts and result.updated_at
    ]
    return max(candidates, key=_time_key) if candidates else ""


def _gate_result_belongs_to_round(
    command: str,
    context: str,
    result_at: str,
    command_at: str,
) -> bool:
    if command == "/ci build" and context in BUILD_PRECONDITION_CONTEXTS:
        return True
    return bool(
        result_at
        and command_at
        and _time_key(result_at) >= _time_key(command_at)
    )


def _stage_contexts(command: str) -> Tuple[str, ...]:
    if command == "/ci build":
        return BUILD_GATE_CONTEXTS
    if command == "/ci merge":
        return MERGE_GATE_CONTEXTS
    return ()


def _command_stage(command: str) -> str:
    if command == "/ci build":
        return "build"
    if command == "/ci merge":
        return "merge"
    return ""


def _scan_watermark(state: TrackerState, snapshot: PrSnapshot) -> str:
    if snapshot.scanned_at:
        return snapshot.scanned_at
    watermark = state.last_scanned_at
    if _time_key(snapshot.latest_ci_command_at) > _time_key(watermark):
        watermark = snapshot.latest_ci_command_at
    for failure in snapshot.failures:
        if _time_key(failure.updated_at) > _time_key(watermark):
            watermark = failure.updated_at
    for result in snapshot.gate_results:
        if _time_key(result.updated_at) > _time_key(watermark):
            watermark = result.updated_at
    return watermark


def _happened_at_or_after_scan(event_at: str, last_scanned_at: str) -> bool:
    return (
        not event_at
        or not last_scanned_at
        or _time_key(event_at) >= _time_key(last_scanned_at)
    )


def _gate_from_comment(comment: Mapping[str, Any], current_head_sha: str) -> Optional[_GateCandidate]:
    body = _value(comment.get("body"))
    lower = body.lower()
    if exact_ci_command(body) or _is_queue_status_comment(body) or _is_build_timing_comment(body):
        return None

    context = ""
    if "protected-file-approval" in lower or "protected file" in lower:
        context = "protected-file-approval"
    elif "taichu/codex-pr-review" in lower or "codex-pr-review" in lower:
        context = "taichu/codex-pr-review"
    elif "taichu/pr-build" in lower or "pr-build" in lower:
        context = "taichu/pr-build"
    elif "taichu-dev-cloud-preflight" in lower or "taichu/dev-cloud-preflight" in lower:
        context = "taichu/dev-cloud-preflight"
    elif (
        "external-ci/jenkins-merge-gate-test" in lower
        or "taichu merge gate：执行结果" in lower
        or "taichu merge gate: 执行结果" in lower
        or "taichu-ci/auto-merge-blocked" in lower
        or "ci/merge-gate" in lower
        or "merge-gate" in lower
    ) and not any(
        marker in lower
        for marker in ("merge-gate-onboard", "merge-gate-queue-status", "merge-gate-build-timing")
    ):
        context = "ci/merge-gate"
    if not context or _references_different_head(body, current_head_sha):
        return None

    summary = _clean_comment_text(body)
    return _GateCandidate(
        context=context,
        state=_state_from_comment(body),
        summary=summary,
        updated_at=_timestamp(comment),
        item_id=_integer(comment.get("id")),
    )


def _state_from_comment(value: str) -> str:
    lower = _value(value).lower()
    if _is_build_timing_comment(value):
        return "unknown"
    if any(
        marker in lower
        for marker in (
            "执行结果：成功",
            "执行结果: 成功",
            "build success",
            "merge gate success",
            "preflight: 通过",
            "preflight：通过",
        )
    ):
        return "success"
    if any(
        marker in lower
        for marker in (
            "暂不能入队",
            "执行结果：失败",
            "执行结果: 失败",
            "失败摘要",
            "未通过",
            "failed",
            "failure",
        )
    ):
        return "failure"
    if not _is_inactive_queue_comment(value) and any(
        marker in lower for marker in ("queued", "running", "排队", "运行中")
    ):
        return "pending"
    if "通过" in lower or "success" in lower:
        return "success"
    return "unknown"


def _is_queue_status_comment(value: str) -> bool:
    lower = _value(value).lower()
    if _is_inactive_queue_comment(value) or _is_build_timing_comment(value):
        return False
    return any(
        marker in lower
        for marker in (
            "merge-gate-queue-status",
            "pr-build-queue-status",
            "queue status",
            "排队状态",
            "入队成功",
            "已入队",
            "暂不能入队",
        )
    )


def _is_inactive_queue_comment(value: str) -> bool:
    lower = _value(value).lower()
    return any(
        marker in lower
        for marker in ("当前不在", "已离开活动队列", "not in", "not currently in", "no longer in")
    )


def _is_build_timing_comment(value: str) -> bool:
    lower = _value(value).lower()
    return (
        "build-timing" in lower
        or "构建阶段耗时表" in value
        or "与主结果评论分开发帖" in value
        or "testreport/build-timing" in lower
    )


def _references_different_head(body: str, current_head_sha: str) -> bool:
    if len(current_head_sha) < 7:
        return False
    lower = _value(body).lower()
    if current_head_sha[:7].lower() in lower or current_head_sha[:12].lower() in lower:
        return False
    return any(
        marker in lower
        for marker in (
            "pr head",
            "当前 pr head",
            "当前 head",
            "顶端提交",
            "pr 顶端",
            "head |",
            "| head |",
        )
    )


def _clean_comment_text(value: str) -> str:
    text = re.sub(r"<!--.*?-->", "", _value(value), flags=re.DOTALL)
    text = re.sub(r"<[^>]*>", "", text, flags=re.DOTALL)
    text = html.unescape(text)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or "评论无可展示内容"


def _put_latest(target: Dict[str, _GateCandidate], candidate: _GateCandidate) -> None:
    current = target.get(candidate.context)
    if current is None or (_time_key(candidate.updated_at), candidate.item_id) >= (
        _time_key(current.updated_at),
        current.item_id,
    ):
        target[candidate.context] = candidate


def _has_success_signal(lower_summary: str) -> bool:
    return any(
        marker in lower_summary
        for marker in (
            "执行结果：成功",
            "执行结果: 成功",
            "build success",
            "merge gate success",
            "preflight: 通过",
            "preflight：通过",
            "passed",
            "satisfied",
            "found no p0/p1",
            "no p0/p1 principle issues",
            "当前 head 该门禁已通过",
            "通过",
            "success",
        )
    )


def _has_definitive_failure_signal(normalized_state: str, lower_summary: str) -> bool:
    return normalized_state in {"failure", "failed", "error"} or any(
        marker in lower_summary
        for marker in (
            "暂不能入队",
            "执行结果：失败",
            "执行结果: 失败",
            "失败摘要",
            "未通过",
            "result: failure",
            "result：failure",
            "status: failure",
            "status：failure",
            "= `failure`",
            "build failed",
            "merge gate failed",
            "preflight failed",
            "not passed",
            "did not pass",
            "unsatisfied",
            "not satisfied",
        )
    )


def _has_definitive_success_signal(lower_summary: str) -> bool:
    return any(
        marker in lower_summary
        for marker in (
            "执行结果：成功",
            "执行结果: 成功",
            "build success",
            "merge gate success",
            "preflight: 通过",
            "preflight：通过",
            "found no p0/p1",
            "no p0/p1 principle issues",
            "当前 head 该门禁已通过",
        )
    )


def _timestamp(item: Mapping[str, Any]) -> str:
    return _value(
        item.get("updated_at")
        or item.get("created_at")
        or item.get("submitted_at")
        or item.get("date")
    )


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _time_key(value: str):
    raw = _value(value).strip()
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


def _value(value: Any) -> str:
    return "" if value is None else str(value)
