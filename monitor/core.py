"""Gate classification and notification tracking ported from the Android client."""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


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

RELEASE_BUILD_GATE_CONTEXTS = (
    "protected-file-approval",
    "taichu/pr-build",
)

MERGE_GATE_CONTEXTS = (
    "taichu/dev-cloud-preflight",
    "ci/merge-gate",
)

RELEASE_MERGE_GATE_CONTEXTS = ("ci/merge-gate",)

TRUSTED_GATE_COMMENT_AUTHORS = frozenset({"taichu-ci-bot"})

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
    base_ref: str = ""
    merged: bool = False
    merged_at: str = ""


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
    base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
    user = pr.get("user") if isinstance(pr.get("user"), Mapping) else {}
    head_sha = _value(head.get("sha")).strip()
    pr_state = _value(pr.get("state")).strip().lower()
    merged_at = _value(pr.get("merged_at")).strip()
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
        if candidate and candidate.state == "failure":
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
        base_ref=_value(base.get("ref")).strip(),
        merged=(
            pr_state == "closed"
            and pr.get("merged") is True
            and bool(merged_at)
        ),
        merged_at=merged_at,
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
    terminal_merge = snapshot.latest_ci_command == "/ci merge" and snapshot.merged
    failures = () if terminal_merge else tuple(_stage_failures_after_command(snapshot))
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
        if snapshot.latest_ci_command == "/ci merge" and snapshot.merged:
            merge_success = True
        elif new_command_round or _happened_at_or_after_scan(
            completed_at,
            state.last_scanned_at,
        ):
            request_merge_comment = snapshot.latest_ci_command == "/ci build"
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
    if not stage:
        return ""
    if stage == "merge":
        if not snapshot.merged:
            return ""
    elif not _stage_succeeded_after_command(snapshot):
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
    gate = normalize_gate_context(context)
    raw_value = _value(value)
    structured = _notification_structured_text(value)
    extractors = {
        "protected-file-approval": _approval_notification_summary,
        "taichu/codex-pr-review": _codex_notification_summary,
        "taichu/pr-build": _build_notification_summary,
        "taichu/dev-cloud-preflight": _preflight_notification_summary,
    }
    if gate == "ci/merge-gate":
        candidate = _merge_notification_summary(
            structured,
            auto_merge_blocked=(
                "<!-- taichu-ci/auto-merge-blocked" in raw_value.lower()
            ),
        )
    else:
        extractor = extractors.get(gate)
        candidate = extractor(structured) if extractor is not None else ""

    if not candidate and gate != "taichu/dev-cloud-preflight":
        labeled = _labeled_failure_summary(structured)
        if labeled and not _is_generic_failure_summary(labeled):
            candidate = labeled

    if not candidate:
        plain = _strip_leading_timestamp(_notification_plain_text(structured))
        if _looks_like_gate_template(gate, structured):
            candidate = _default_gate_failure_summary(gate)
        else:
            candidate = plain

    return _finalize_notification_summary(candidate, gate)


def _notification_structured_text(value: str) -> str:
    text = _value(value)
    text = re.sub(
        r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>",
        "\n",
        text,
    )
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<[/!?]?[A-Za-z][^>\r\n]*>", "", text)
    text = html.unescape(text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip()


def _notification_plain_text(value: str) -> str:
    text = _notification_structured_text(value)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def _approval_notification_summary(value: str) -> str:
    return _section_first_bullet(value, ("结果",))


def _codex_notification_summary(value: str) -> str:
    match = re.search(
        r"Codex\s+found\s+(\d{1,3})\s+P0/P1\s+principle\s+issue(?:\(s\)|s)?",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        return f"发现 {match.group(1)} 个 P0/P1 原则问题"
    bullets = _section_bullets(value, ("原则问题",))
    blocking = [bullet for bullet in bullets if re.match(r"^P[01]\b", bullet)]
    if blocking:
        return f"发现 {len(blocking)} 个 P0/P1 原则问题"
    return _first_sentence(bullets[0]) if bullets else ""


def _build_notification_summary(value: str) -> str:
    tasks = _structured_failed_task_summary(value)
    if tasks:
        return tasks
    labeled = _labeled_failure_summary(value)
    return "" if _is_generic_failure_summary(labeled) else labeled


def _preflight_notification_summary(value: str) -> str:
    failures = _markdown_failure_rows(value)
    if failures:
        specific = [
            item
            for item in failures
            if not any(
                marker in item[0].lower()
                for marker in ("总体", "总览", "overall", "健康检查")
            )
        ]
        name, reason = (specific or failures)[0]
        return f"{name}：{reason}" if reason else f"{name}未通过"
    return _labeled_value(value, ("结论",))


def _merge_notification_summary(value: str, *, auto_merge_blocked: bool = False) -> str:
    tasks = _structured_failed_task_summary(value)
    if tasks:
        return tasks

    match = re.search(
        r"执行结果[ \t]*[：:][ \t]*失败[ \t]*[，,:：][ \t]*([^\n]+)",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    blocked_labels = ["未放行原因", "阻塞原因", "当前阻塞"]
    if auto_merge_blocked:
        blocked_labels.append("原因")
    blocked = _labeled_value(value, blocked_labels)
    if blocked:
        return _first_sentence(blocked)

    skipped = re.search(
        r"CI[ \t]*未执行测试[ \t]*[：:][ \t]*([^\n]+)",
        value,
        flags=re.IGNORECASE,
    )
    if skipped:
        return skipped.group(1).strip()

    labeled = _labeled_failure_summary(value)
    return "" if _is_generic_failure_summary(labeled) else labeled


def _structured_failed_task_summary(value: str) -> str:
    fields: Dict[int, Dict[str, str]] = {}
    for match in re.finditer(
        r"(?m)^\s*failed_task_(\d{1,2})\.([a-zA-Z0-9_]+)\s*=\s*([^\r\n]*)$",
        value,
    ):
        task = fields.setdefault(int(match.group(1)), {})
        task[match.group(2).lower()] = match.group(3).strip()
    if not fields:
        return ""

    count_match = re.search(r"(?m)^\s*failed_task_count\s*=\s*(\d{1,2})\s*$", value)
    declared_count = int(count_match.group(1)) if count_match else len(fields)
    summaries = [_failed_task_text(fields[index]) for index in sorted(fields)]
    summaries = [summary for summary in summaries if summary]
    if not summaries:
        return ""
    if declared_count > 1 or len(summaries) > 1:
        return f"{max(declared_count, len(summaries))} 项失败：" + "；".join(summaries[:2])
    return summaries[0]


def _failed_task_text(fields: Mapping[str, str]) -> str:
    path = []
    for key in ("task_label", "stage", "suite"):
        value = _safe_task_field(fields.get(key, ""))
        if value and value.casefold() not in {item.casefold() for item in path}:
            path.append(value)
    reason_type = fields.get("reason_type", "").strip().lower()
    reason = {
        "compile_error": "编译失败",
        "failed_suite": "测试任务失败",
        "ci_job_failure": "CI 任务失败",
        "smoke_failure": "冒烟失败",
        "test_failure": "测试失败",
        "timeout": "超时",
    }.get(reason_type, "失败")
    raw_exit_status = fields.get("exit_status", "").strip()
    exit_status = raw_exit_status if re.fullmatch(r"-?\d{1,6}", raw_exit_status) else ""
    prefix = "/".join(path)
    text = f"{prefix} {reason}".strip()
    return f"{text}（exit {exit_status}）" if exit_status else text


def _safe_task_field(value: str) -> str:
    text = _value(value).strip()
    if not text or len(text) > 64:
        return ""
    if re.search(
        r"(?i)(?:https?://|secret|token|password|credential|api[_-]?key|"
        r"session[_-]?id|user[_-]?id|uuid|@|=)",
        text,
    ):
        return ""
    return text if re.fullmatch(r"[A-Za-z0-9_+ .\-/\u4e00-\u9fff]+", text) else ""


def _markdown_failure_rows(value: str) -> List[Tuple[str, str]]:
    lines = _value(value).splitlines()
    failures = []
    for index, line in enumerate(lines):
        header = _markdown_cells(line)
        if not header:
            continue
        normalized = [re.sub(r"\s+", "", cell).lower() for cell in header]
        result_index = next(
            (
                item_index
                for item_index, name in enumerate(normalized)
                if name in {"结果", "状态", "result", "status"}
            ),
            -1,
        )
        if result_index < 0:
            continue
        reason_index = next(
            (
                item_index
                for item_index, name in enumerate(normalized)
                if "失败原因" in name or name in {"说明", "reason", "details", "detail"}
            ),
            -1,
        )
        name_index = next(
            (
                item_index
                for item_index, name in enumerate(normalized)
                if name in {"用例", "检查项", "项目", "case", "item"}
            ),
            0,
        )
        for row_line in lines[index + 1 :]:
            if not row_line.strip():
                if failures:
                    break
                continue
            row = _markdown_cells(row_line)
            if not row:
                break
            if _markdown_separator_row(row) or result_index >= len(row):
                continue
            result = re.sub(r"\s+", "", row[result_index]).lower()
            if not re.fullmatch(r"(?:fail(?:ed|ure)?|error|失败|未通过|不通过)", result):
                continue
            name = row[name_index].strip() if name_index < len(row) else ""
            reason = row[reason_index].strip() if 0 <= reason_index < len(row) else ""
            failures.append((name or "云侧用例", reason))
        if failures:
            break
    return failures


def _markdown_cells(line: str) -> List[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _markdown_separator_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _section_first_bullet(value: str, headings: Sequence[str]) -> str:
    bullets = _section_bullets(value, headings)
    return bullets[0] if bullets else ""


def _section_bullets(value: str, headings: Sequence[str]) -> List[str]:
    lines = _value(value).splitlines()
    wanted = {heading.casefold() for heading in headings}
    inside = False
    bullets = []
    for line in lines:
        heading = re.match(r"^\s*#{1,6}\s*(.*?)\s*$", line)
        if heading:
            title = heading.group(1).strip().casefold()
            if inside:
                break
            inside = title in wanted
            continue
        if not inside:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if bullet:
            bullets.append(bullet.group(1).strip())
    return bullets


def _first_sentence(value: str) -> str:
    text = _value(value).strip()
    match = re.match(r"(.+?[。！？!?])", text)
    return match.group(1).strip() if match else text


def _labeled_value(value: str, labels: Sequence[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)^\s*(?:[-*]\s*)?(?:{label_pattern})[ \t]*[：:][ \t]*([^\r\n]+)",
        value,
    )
    return match.group(1).strip() if match else ""


def _labeled_failure_summary(value: str) -> str:
    lines = _value(value).splitlines()
    label = re.compile(
        r"^\s*(?:[-*]\s*)?(?:失败摘要|失败原因|错误摘要|"
        r"failure\s+summary|failure\s+reason|error\s+summary)\s*[：:]\s*(.*)$",
        flags=re.IGNORECASE,
    )
    for line in lines:
        match = label.match(line)
        if not match:
            continue
        summary = match.group(1).strip()
        if summary:
            return summary.rstrip("；;")

    inline = re.search(
        r"(?:失败摘要|失败原因|错误摘要|failure\s+summary|failure\s+reason|"
        r"error\s+summary)[ \t]*[：:][ \t]*([^\r\n]+)",
        _value(value),
        flags=re.IGNORECASE,
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


def _is_generic_failure_summary(value: str) -> bool:
    normalized = re.sub(r"[\s，。；;,.]+", "", _value(value)).lower()
    return normalized in {
        "测试未通过请查看jenkins日志与测试报告",
        "测试未通过请查看日志与测试报告",
        "构建失败请查看jenkins日志与测试报告",
    }


def _strip_leading_timestamp(value: str) -> str:
    return re.sub(
        r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
        r"(?:[.,]\d+)?(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?\s*(?:\|+|[-–—])\s*",
        "",
        _value(value),
    ).strip()


def _looks_like_gate_template(context: str, value: str) -> bool:
    lower = _value(value).lower()
    markers = {
        "protected-file-approval": (
            "protected-file-approval",
            "pr approve检查",
            "taichu-protected-file-approval",
        ),
        "taichu/codex-pr-review": (
            "taichu/codex-pr-review",
            "codex pr review",
            "taichu-codex-pr-review",
        ),
        "taichu/pr-build": (
            "taichu/pr-build",
            "taichu pr build",
            "external-ci/jenkins-pr-build",
        ),
        "taichu/dev-cloud-preflight": (
            "taichu/dev-cloud-preflight",
            "云侧 preflight",
            "taichu-dev-cloud-preflight",
        ),
        "ci/merge-gate": (
            "ci/merge-gate",
            "taichu merge gate",
            "external-ci/jenkins-merge-gate-test",
        ),
    }
    return any(marker in lower for marker in markers.get(context, ()))


def _default_gate_failure_summary(context: str) -> str:
    return {
        "protected-file-approval": "受保护文件审批未通过，详情见 PR",
        "taichu/codex-pr-review": "Codex Review 未通过，详情见 PR",
        "taichu/pr-build": "PR Build 失败，详情见 PR",
        "taichu/dev-cloud-preflight": "云侧 Preflight 未通过，详情见 PR",
        "ci/merge-gate": "Merge Gate 未通过，详情见 PR",
    }.get(context, "门禁未通过，详情见 PR")


def _finalize_notification_summary(value: str, context: str) -> str:
    text = _notification_plain_text(value)
    text = re.sub(r"https?://[^\s<>()]+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(secret|token|password|credential|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=***",
        text,
    )
    text = re.sub(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "[id]",
        text,
    )
    text = re.sub(r"[，,；;]\s*详见.*$", "", text)
    text = re.sub(
        r"(?:Jenkins|详情链接|构建产物|产物链接)\s*[：:]\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[，,；;]?\s*(?:详情|日志|链接)\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ；;|")
    if not text:
        text = _default_gate_failure_summary(context)
    return notification_text(text)


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
    contexts = _stage_contexts(snapshot)
    for failure in snapshot.failures:
        if (
            failure.context in contexts
            and _failure_belongs_to_round(
                failure.updated_at,
                snapshot.latest_ci_command_at,
            )
        ):
            yield failure


def _stage_succeeded_after_command(snapshot: PrSnapshot) -> bool:
    contexts = _stage_contexts(snapshot)
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
    if snapshot.latest_ci_command == "/ci merge" and snapshot.merged:
        return snapshot.merged_at or snapshot.scanned_at
    contexts = _stage_contexts(snapshot)
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


def _failure_belongs_to_round(result_at: str, command_at: str) -> bool:
    return bool(
        result_at
        and command_at
        and _time_key(result_at) >= _time_key(command_at)
    )


def _stage_contexts(snapshot: PrSnapshot) -> Tuple[str, ...]:
    command = snapshot.latest_ci_command
    release_target = "release" in snapshot.base_ref.strip().casefold()
    if command == "/ci build":
        return RELEASE_BUILD_GATE_CONTEXTS if release_target else BUILD_GATE_CONTEXTS
    if command == "/ci merge":
        return RELEASE_MERGE_GATE_CONTEXTS if release_target else MERGE_GATE_CONTEXTS
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
    auto_merge_blocked = "<!-- taichu-ci/auto-merge-blocked" in body.lower()
    if (
        not _trusted_gate_comment(comment)
        or exact_ci_command(body)
        or (_is_queue_status_comment(body) and not auto_merge_blocked)
        or _is_build_timing_comment(body)
    ):
        return None

    context = _comment_gate_context(body)
    if not context or _references_different_head(body, current_head_sha):
        return None

    state = (
        "failure"
        if auto_merge_blocked
        else _state_from_comment(body, context=context)
    )
    summary = (
        notification_summary(context, body)
        if state == "failure"
        else _clean_comment_text(body)
    )
    return _GateCandidate(
        context=context,
        state=state,
        summary=summary,
        updated_at=_timestamp(comment),
        item_id=_integer(comment.get("id")),
    )


def _trusted_gate_comment(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    login = _value(user.get("login") if isinstance(user, Mapping) else "").strip()
    return login.casefold() in TRUSTED_GATE_COMMENT_AUTHORS


def _comment_gate_context(body: str) -> str:
    lower = _value(body).lower()
    marker_contexts = (
        ("<!-- taichu-protected-file-approval", "protected-file-approval"),
        ("<!-- taichu-codex-pr-review", "taichu/codex-pr-review"),
        ("<!-- external-ci/jenkins-pr-build", "taichu/pr-build"),
        ("<!-- taichu-dev-cloud-preflight", "taichu/dev-cloud-preflight"),
        ("<!-- external-ci/jenkins-merge-gate-test", "ci/merge-gate"),
        ("<!-- taichu-ci/auto-merge-blocked", "ci/merge-gate"),
    )
    for marker, context in marker_contexts:
        if marker in lower:
            return context

    explicit_patterns = (
        (
            r"^\s*(?:#{1,6}\s*)?(?:taichu/)?protected-file-approval\b|"
            r"^\s*#{1,6}\s*pr approve检查\s*$",
            "protected-file-approval",
        ),
        (
            r"^\s*(?:#{1,6}\s*)?taichu/codex-pr-review\b|"
            r"^\s*#{1,6}\s*codex pr review\s*$",
            "taichu/codex-pr-review",
        ),
        (
            r"^\s*(?:#{1,6}\s*)?taichu/pr-build\b|"
            r"^\s*#{1,6}\s*taichu pr build\s*[：:].*执行结果",
            "taichu/pr-build",
        ),
        (
            r"^\s*(?:#{1,6}\s*)?taichu(?:-|/)dev-cloud-preflight\b|"
            r"^\s*#{1,6}\s*taichu 云侧 preflight\s*[：:]",
            "taichu/dev-cloud-preflight",
        ),
        (
            r"^\s*(?:#{1,6}\s*)?ci/merge-gate\b|"
            r"^\s*#{1,6}\s*taichu merge gate\s*[：:].*执行结果",
            "ci/merge-gate",
        ),
    )
    for pattern, context in explicit_patterns:
        if re.search(pattern, body, flags=re.IGNORECASE | re.MULTILINE):
            return context
    return ""


def _state_from_comment(value: str, *, context: str = "") -> str:
    lower = _value(value).lower()
    if _is_build_timing_comment(value):
        return "unknown"
    if context == "taichu/codex-pr-review":
        return _codex_comment_state(value)
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
    if context == "taichu/dev-cloud-preflight":
        structured = _notification_structured_text(value)
        if re.search(
            r"preflight\s*[：:]\s*失败\b",
            structured,
            flags=re.IGNORECASE,
        ) or _markdown_failure_rows(structured):
            return "failure"
    if not _is_inactive_queue_comment(value) and any(
        marker in lower for marker in ("queued", "running", "排队", "运行中")
    ):
        return "pending"
    if "通过" in lower or "success" in lower:
        return "success"
    return "unknown"


def _codex_comment_state(value: str) -> str:
    structured = _notification_structured_text(value)
    lower = structured.lower()
    status = re.search(
        r"taichu/codex-pr-review\s*=\s*(?:failure|failed|success|successful)",
        lower,
    )
    if status:
        return "failure" if "fail" in status.group(0) else "success"
    if "本次不调用 codex，直接失败" in lower:
        return "failure"

    count = re.search(
        r"codex\s+found\s+(\d{1,3})\s+p0/p1\s+principle\s+issue(?:\(s\)|s)?",
        lower,
    )
    if count:
        return "failure" if int(count.group(1)) > 0 else "success"

    bullets = _section_bullets(structured, ("原则问题",))
    if any(re.match(r"^P[01]\b", bullet, flags=re.IGNORECASE) for bullet in bullets):
        return "failure"
    if any("未发现原则问题" in bullet for bullet in bullets):
        return "success"
    if any(
        marker in lower
        for marker in ("found no p0/p1", "no p0/p1 principle issues")
    ):
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
