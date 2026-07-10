#!/usr/bin/env python3
"""
Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

Local read-only bridge for a compact Gitea PR status page.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


DEFAULT_API_BASE = "https://www.taichu.fun/gitea/api/v1"
DEFAULT_WEB_BASE = "https://www.taichu.fun/gitea"
DEFAULT_OWNER = "SystemAgentDev"
DEFAULT_REPO = "TaiChu"

GATE_CONTEXTS = [
    "protected-file-approval",
    "taichu/codex-pr-review",
    "taichu/pr-build",
    "taichu/dev-cloud-preflight",
    "ci/merge-gate",
]

GATE_CONTEXT_ALIASES = {
    "protected-file-approval": ["protected-file-approval", "taichu/protected-file-approval"],
}

SUCCESS_STATES = {"success", "ok"}
FAILURE_STATES = {"failure", "failed", "error"}
ACTIVE_STATES = {"pending", "running"}

CONTEXT_COMMENT_HINTS = {
    "protected-file-approval": ["protected-file", "protected file", "approval"],
    "taichu/codex-pr-review": ["codex-pr-review", "taichu-pr-codex-review", "codex review"],
    "taichu/pr-build": ["taichu/pr-build", "pr-build", "/ci build", "ci build"],
    "taichu/dev-cloud-preflight": [
        "taichu-dev-cloud-preflight",
        "taichu/dev-cloud-preflight",
    ],
    "ci/merge-gate": ["ci/merge-gate", "merge-gate", "/ci merge", "ci merge"],
}

QUEUE_KEYWORDS = [
    "/ci build",
    "/ci merge",
    "queue",
    "queued",
    "waiting",
    "pending",
    "running",
    "ingest",
    "stale_input",
    "排队",
    "队列",
    "等待",
    "正在执行",
    "运行中",
]

QUEUE_NEGATIVE_KEYWORDS = [
    "已离开活动队列",
    "当前不在",
    "build-timing",
    "耗时表",
    "等待已结束",
    "：通过",
    "= `通过`",
    "= `success`",
    "执行结果：成功",
]

FAILURE_KEYWORDS = [
    "fail",
    "failed",
    "failure",
    "error",
    "stale_input",
    "timeout",
    "缺失",
    "失败",
    "错误",
    "未通过",
    "阻塞",
]


@dataclasses.dataclass(frozen=True)
class PrSelector:
    owner: str
    repo: str
    number: int


@dataclasses.dataclass(frozen=True)
class Credential:
    scheme: str
    value: str


class GiteaApiError(RuntimeError):
    pass


def parse_pr_selector(
    raw: str, default_owner: str = DEFAULT_OWNER, default_repo: str = DEFAULT_REPO
) -> PrSelector:
    value = raw.strip()
    if value.isdigit():
        return PrSelector(default_owner, default_repo, int(value))

    parsed = urllib.parse.urlparse(value)
    match = re.search(r"/gitea/([^/]+)/([^/]+)/pulls/(\d+)(?:/|$)", parsed.path)
    if not match:
        match = re.search(r"/([^/]+)/([^/]+)/pulls/(\d+)(?:/|$)", parsed.path)
    if not match:
        raise ValueError(f"cannot parse PR selector: {raw!r}")
    owner, repo, number = match.groups()
    return PrSelector(owner, repo, int(number))


def normalize_state(status: dict[str, Any]) -> str:
    return str(status.get("state") or status.get("status") or "").strip().lower()


def status_timestamp(item: dict[str, Any]) -> str:
    return str(
        item.get("updated_at")
        or item.get("created_at")
        or item.get("submitted_at")
        or item.get("date")
        or ""
    )


def latest_statuses_by_context(statuses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for original in statuses:
        context = str(original.get("context") or original.get("name") or "").strip()
        if not context:
            continue
        current = dict(original)
        current["context"] = context
        current["state"] = normalize_state(current)
        existing = latest.get(context)
        if existing is None or _status_sort_key(current) >= _status_sort_key(existing):
            latest[context] = current
    return latest


def gate_items(
    latest_statuses: dict[str, dict[str, Any]], comments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    items = []
    for context in GATE_CONTEXTS:
        status = latest_status_for_gate(latest_statuses, context)
        if not status:
            continue
        state = normalize_state(status)
        if state in SUCCESS_STATES:
            continue
        comment = latest_relevant_comment(context, comments) if state in FAILURE_STATES else None
        summary_parts = []
        description = clean_text(str(status.get("description") or ""))
        if description:
            summary_parts.append(description)
        comment_summary = ""
        if comment:
            comment_summary = summarize_failure_text(str(comment.get("body") or ""))
        if comment_summary and comment_summary not in summary_parts:
            summary_parts.append(comment_summary)
        items.append(
            {
                "context": context,
                "state": state or "unknown",
                "summary": "\n\n".join(summary_parts) or "No failure detail was published.",
                "target_url": status.get("target_url") or "",
                "created_at": status.get("created_at") or "",
                "updated_at": status.get("updated_at") or "",
                "comment_url": comment.get("html_url") if comment else "",
            }
        )
    return items


def queue_events(comments: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    events = []
    seen_command_comments = set()
    for comment in sorted(comments, key=_comment_sort_key, reverse=True):
        body = str(comment.get("body") or "")
        if not is_queue_comment(body):
            continue
        command_key = exact_ci_command(body)
        if command_key:
            if command_key in seen_command_comments:
                continue
            seen_command_comments.add(command_key)
        events.append(
            {
                "author": (comment.get("user") or {}).get("login") or "",
                "created_at": comment.get("created_at") or "",
                "updated_at": comment.get("updated_at") or "",
                "html_url": comment.get("html_url") or "",
                "summary": summarize_comment(body, max_chars=700),
            }
        )
        if len(events) >= limit:
            break
    return events


def is_queue_comment(body: str) -> bool:
    lowered = body.lower()
    if any(keyword in lowered or keyword in body for keyword in QUEUE_NEGATIVE_KEYWORDS):
        return False
    return any(keyword in lowered or keyword in body for keyword in QUEUE_KEYWORDS)


def exact_ci_command(body: str) -> str:
    command = body.strip().lower()
    if command in {"/ci build", "/ci merge"}:
        return command
    return ""


def hidden_success_contexts(latest_statuses: dict[str, dict[str, Any]]) -> list[str]:
    contexts = []
    for context in GATE_CONTEXTS:
        status = latest_status_for_gate(latest_statuses, context)
        if status and normalize_state(status) in SUCCESS_STATES:
            contexts.append(context)
    return contexts


def latest_status_for_gate(
    latest_statuses: dict[str, dict[str, Any]], context: str
) -> Optional[dict[str, Any]]:
    candidates = []
    for alias in GATE_CONTEXT_ALIASES.get(context, [context]):
        status = latest_statuses.get(alias)
        if status:
            candidates.append(status)
    if not candidates:
        return None
    return max(candidates, key=_status_sort_key)


def build_summary(
    client: "GiteaClient", selector: PrSelector, comment_pages: int
) -> dict[str, Any]:
    pr = client.get_pr(selector)
    head_sha = (((pr.get("head") or {}).get("sha")) or "").strip()
    if not head_sha:
        raise GiteaApiError(f"PR #{selector.number} response has no head sha")

    statuses = client.get_statuses(selector, head_sha)
    comments = client.get_issue_comments(selector, max_pages=comment_pages)
    latest = latest_statuses_by_context(statuses)
    gates = gate_items(latest, comments)
    queues = queue_events(comments)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return {
        "fetched_at": now,
        "pr": {
            "number": selector.number,
            "title": pr.get("title") or "",
            "state": pr.get("state") or "",
            "body": pr.get("body") or "",
            "html_url": pr.get("html_url")
            or f"{client.web_base}/{selector.owner}/{selector.repo}/pulls/{selector.number}",
            "head_sha": head_sha,
            "head_ref": (pr.get("head") or {}).get("ref") or "",
            "base_ref": (pr.get("base") or {}).get("ref") or "",
            "updated_at": pr.get("updated_at") or "",
            "author": (pr.get("user") or {}).get("login") or "",
        },
        "queue": queues,
        "gates": gates,
        "hidden_success_contexts": hidden_success_contexts(latest),
        "watched_contexts": GATE_CONTEXTS,
    }


def latest_relevant_comment(
    context: str, comments: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    hints = CONTEXT_COMMENT_HINTS.get(context, [context])
    sorted_comments = sorted(comments, key=_comment_sort_key, reverse=True)
    for comment in sorted_comments:
        body = str(comment.get("body") or "")
        lowered = body.lower()
        if any(hint.lower() in lowered for hint in hints):
            return comment
    return None


def summarize_failure_text(text: str, max_chars: int = 1000) -> str:
    lines = meaningful_lines(text)
    selected = []
    for line in lines:
        lowered = line.lower()
        if "| pass |" in lowered or " pass " in lowered:
            continue
        if "说明/失败原因" in line:
            continue
        if any(keyword in lowered or keyword in line for keyword in FAILURE_KEYWORDS):
            selected.append(line)
    if not selected:
        selected = lines[:8]
    return truncate("\n".join(selected[:12]), max_chars)


def summarize_comment(text: str, max_chars: int = 700) -> str:
    return truncate("\n".join(meaningful_lines(text)[:10]), max_chars)


def meaningful_lines(text: str) -> list[str]:
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines = []
    for raw_line in stripped.splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        if line in {"| --- | --- |", "| --- | --- | --- |", "```text", "```"}:
            continue
        lines.append(line)
    return lines


def clean_text(text: str) -> str:
    value = html.unescape(text)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value)
    value = value.replace("**", "").replace("__", "")
    value = value.replace("`", "")
    return re.sub(r"\s+", " ", value).strip()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _status_sort_key(status: dict[str, Any]) -> tuple[str, int]:
    raw_id = status.get("id") or 0
    try:
        status_id = int(raw_id)
    except (TypeError, ValueError):
        status_id = 0
    return (status_timestamp(status), status_id)


def _comment_sort_key(comment: dict[str, Any]) -> tuple[str, int]:
    raw_id = comment.get("id") or 0
    try:
        comment_id = int(raw_id)
    except (TypeError, ValueError):
        comment_id = 0
    return (str(comment.get("updated_at") or comment.get("created_at") or ""), comment_id)


class GiteaClient:
    def __init__(
        self,
        api_base: str,
        web_base: str,
        credential: Optional[Credential],
        timeout_seconds: int = 20,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.web_base = web_base.rstrip("/")
        self.credential = credential
        self.timeout_seconds = timeout_seconds

    def get_pr(self, selector: PrSelector) -> dict[str, Any]:
        return self.api_get(f"/repos/{selector.owner}/{selector.repo}/pulls/{selector.number}")

    def get_issue_comments(self, selector: PrSelector, max_pages: int) -> list[dict[str, Any]]:
        return self.api_get_pages(
            f"/repos/{selector.owner}/{selector.repo}/issues/{selector.number}/comments",
            max_pages=max_pages,
        )

    def get_statuses(self, selector: PrSelector, sha: str) -> list[dict[str, Any]]:
        try:
            statuses = self.api_get_pages(
                f"/repos/{selector.owner}/{selector.repo}/statuses/{sha}",
                max_pages=5,
            )
            if statuses:
                return statuses
        except GiteaApiError:
            pass
        combined = self.api_get(f"/repos/{selector.owner}/{selector.repo}/commits/{sha}/status")
        statuses = combined.get("statuses") if isinstance(combined, dict) else None
        return statuses if isinstance(statuses, list) else []

    def api_get_pages(self, path: str, max_pages: int, limit: int = 100) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            query = urllib.parse.urlencode({"limit": str(limit), "page": str(page)})
            payload = self.api_get(f"{path}?{query}")
            if not isinstance(payload, list):
                raise GiteaApiError(f"expected list payload for {path}")
            all_items.extend(payload)
            if len(payload) < limit:
                break
        return all_items

    def api_get(self, path: str) -> Any:
        url = self.api_base + path
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:500]
            raise GiteaApiError(f"Gitea API {error.code} for {path}: {body}") from error
        except urllib.error.URLError as error:
            raise GiteaApiError(f"Gitea API request failed for {path}: {error}") from error
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise GiteaApiError(f"Gitea API returned invalid JSON for {path}") from error

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.credential:
            headers["Authorization"] = f"{self.credential.scheme} {self.credential.value}"
        return headers


def resolve_credential(web_base: str, owner: str, repo: str) -> Optional[Credential]:
    token = os.environ.get("TAICHU_GITEA_TOKEN") or os.environ.get("GITEA_TOKEN")
    if token:
        return Credential("token", token.strip())

    username = os.environ.get("TAICHU_GITEA_USERNAME") or os.environ.get("GITEA_USERNAME")
    password = os.environ.get("TAICHU_GITEA_PASSWORD") or os.environ.get("GITEA_PASSWORD")
    if username and password:
        return basic_credential(username, password)

    return credential_from_git(web_base, owner, repo)


def credential_from_git(web_base: str, owner: str, repo: str) -> Optional[Credential]:
    parsed = urllib.parse.urlparse(web_base)
    if not parsed.scheme or not parsed.hostname:
        return None
    hosts = [parsed.hostname]
    if parsed.hostname.startswith("www."):
        hosts.append(parsed.hostname.removeprefix("www."))
    else:
        hosts.append(f"www.{parsed.hostname}")
    candidates = [
        f"{parsed.path.strip('/')}/{owner}/{repo}.git".strip("/"),
        parsed.path.strip("/"),
        "",
    ]
    commands = [["git", "credential", "fill"]]
    if sys.platform == "darwin" and os.path.exists("/usr/bin/git"):
        commands.append(
            [
                "/usr/bin/git",
                "-c",
                "credential.helper=osxkeychain",
                "credential",
                "fill",
            ]
        )
    for host in dict.fromkeys(hosts):
        for path in candidates:
            lines = [f"protocol={parsed.scheme}", f"host={host}"]
            if path:
                lines.append(f"path={path}")
            payload = "\n".join(lines) + "\n\n"
            for command in commands:
                credential = _credential_from_git_command(command, payload)
                if credential:
                    return credential
    return None


def _credential_from_git_command(command: list[str], payload: str) -> Optional[Credential]:
    try:
        proc = subprocess.run(
            command,
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    values = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    if values.get("username") and values.get("password"):
        return basic_credential(values["username"], values["password"])
    return None


def basic_credential(username: str, password: str) -> Credential:
    raw = f"{username}:{password}".encode("utf-8")
    return Credential("Basic", base64.b64encode(raw).decode("ascii"))


def serve(
    client: GiteaClient,
    selector: PrSelector,
    host: str,
    port: int,
    comment_pages: int,
) -> None:
    handler = make_handler(client, selector, comment_pages)
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address[:2]
    print(f"PR brief listening on http://{actual_host}:{actual_port}/pr/{selector.number}")
    server.serve_forever()


def make_handler(
    client: GiteaClient, default_selector: PrSelector, comment_pages: int
) -> type[BaseHTTPRequestHandler]:
    class PrBriefHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send_html(dashboard_html(default_selector))
                return
            pr_match = re.fullmatch(r"/pr/(\d+)/?", parsed.path)
            if pr_match:
                selector = dataclasses.replace(default_selector, number=int(pr_match.group(1)))
                self._send_html(dashboard_html(selector))
                return
            api_match = re.fullmatch(r"/api/pr/(\d+)/summary", parsed.path)
            if api_match:
                selector = dataclasses.replace(default_selector, number=int(api_match.group(1)))
                self._send_json(build_summary(client, selector, comment_pages))
                return
            if parsed.path == "/healthz":
                self._send_json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[gitea-pr-brief] {self.address_string()} {format % args}", file=sys.stderr)

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return PrBriefHandler


def dashboard_html(selector: PrSelector) -> str:
    watched = ", ".join(GATE_CONTEXTS)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TaiChu PR #{selector.number} Brief</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f6f8;
      --panel: #ffffff;
      --panel-soft: #fafbfc;
      --ink: #151922;
      --muted: #667085;
      --faint: #8a93a3;
      --line: #d8dee8;
      --line-soft: #eaedf2;
      --danger: #b42318;
      --danger-bg: #fff4f2;
      --warn: #9a6700;
      --warn-bg: #fff8e6;
      --ok: #14733f;
      --ok-bg: #effaf3;
      --link: #175cd3;
      --shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
      --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-width: 320px; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(1280px, calc(100vw - 32px));
      margin: 18px auto 48px;
    }}
    header {{
      display: flex;
      gap: 16px;
      justify-content: space-between;
      align-items: flex-start;
      padding: 8px 0 14px;
      border-bottom: 1px solid var(--line);
    }}
    header > * {{
      min-width: 0;
    }}
    .eyebrow {{
      margin-bottom: 4px;
      color: var(--faint);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0 0 8px;
      max-width: 900px;
      font-size: 20px;
      line-height: 1.25;
      font-weight: 700;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 0;
      font-size: 13px;
      line-height: 1.2;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      color: #363f4f;
    }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    button {{
      min-height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
      transition: transform 140ms var(--ease-out), border-color 140ms var(--ease-out), background 140ms var(--ease-out);
    }}
    button:disabled {{ cursor: wait; opacity: 0.68; }}
    button:active {{ transform: scale(0.97); }}
    code {{
      padding: 2px 5px;
      border-radius: 5px;
      background: #eef2f7;
      color: #273244;
      font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .title-block {{
      min-width: 0;
      flex: 1 1 auto;
    }}
    .meta {{
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      gap: 6px 10px;
      align-items: center;
      font-size: 13px;
    }}
    .meta span {{
      display: inline-flex;
      gap: 6px;
      align-items: center;
      min-width: 0;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 380px;
      grid-template-areas:
        "blockers queue"
        "body queue";
      gap: 16px;
      margin-top: 16px;
      align-items: start;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
    }}
    .blockers-section {{ grid-area: blockers; }}
    .body-section {{ grid-area: body; }}
    .queue-section {{ grid-area: queue; }}
    .section-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .hint {{
      color: var(--faint);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .snapshot {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin-top: 14px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    .metric {{
      min-width: 0;
      padding: 12px 14px;
      border-right: 1px solid var(--line-soft);
      background: transparent;
    }}
    .metric:last-child {{
      border-right: 0;
    }}
    .metric-label {{
      margin-bottom: 6px;
      color: var(--faint);
      font-size: 11px;
      font-weight: 700;
      line-height: 1;
      text-transform: uppercase;
    }}
    .metric-value {{
      min-height: 24px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 17px;
      font-weight: 700;
      line-height: 1.3;
    }}
    .metric-detail {{
      margin-top: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .metric-danger {{
      background: var(--danger-bg);
    }}
    .metric-warn {{
      background: var(--warn-bg);
    }}
    .metric-ok {{
      background: var(--ok-bg);
    }}
    .failure {{
      position: relative;
      padding: 12px 12px 12px 14px;
      border: 1px solid #ffd2cc;
      border-left: 4px solid var(--danger);
      background: var(--danger-bg);
      border-radius: 6px;
      margin-top: 10px;
    }}
    .pending {{
      border-left-color: var(--warn);
      border-color: #f4dca6;
      background: var(--warn-bg);
    }}
    .gate-title {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    .gate-title strong {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .state {{
      display: inline-flex;
      min-width: 66px;
      justify-content: center;
      padding: 2px 8px;
      border-radius: 999px;
      background: #f2d6d3;
      color: var(--danger);
      font-size: 11px;
      font-weight: 700;
      line-height: 1.5;
      text-transform: uppercase;
    }}
    .pending .state {{ background: #f8e6b7; color: var(--warn); }}
    .empty {{
      margin: 0;
      padding: 14px;
      border: 1px solid #ccebd6;
      border-radius: 6px;
      background: var(--ok-bg);
      color: var(--ok);
      font-weight: 600;
    }}
    .queue-item {{
      padding: 12px 0;
      border-top: 1px solid var(--line);
    }}
    .queue-item:first-child {{ border-top: 0; padding-top: 0; }}
    .queue-title {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: baseline;
    }}
    .queue-title strong {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    pre {{
      margin: 8px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .failure pre, .queue-item pre {{
      font: 13px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #283142;
    }}
    .failure pre, .queue-item pre {{
      max-height: 220px;
      overflow: auto;
    }}
    .body-pre {{
      max-height: 62vh;
      overflow: auto;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-soft);
    }}
    .small {{ color: var(--muted); font-size: 12px; }}
    .tools {{
      display: flex;
      flex: 0 0 auto;
      height: 50px;
      gap: 0;
      align-items: stretch;
      justify-content: flex-end;
      margin-top: 20px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.9);
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05), 0 8px 24px rgba(23, 92, 211, 0.06);
      white-space: nowrap;
    }}
    .pr-jump {{
      display: flex;
      flex: 0 0 auto;
      gap: 6px;
      align-items: center;
      min-height: 50px;
      padding: 7px 8px 7px 10px;
      border: 0;
      border-right: 1px solid var(--line-soft);
      border-radius: 0;
      background: transparent;
    }}
    .pr-jump label {{
      color: var(--faint);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    .pr-jump input {{
      width: 78px;
      min-height: 36px;
      border: 0;
      border-radius: 7px;
      background: #f3f6fa;
      color: var(--ink);
      font: 12px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      outline: none;
      padding: 0 10px;
    }}
    .pr-jump input:focus {{ box-shadow: 0 0 0 2px #c8d7ff; }}
    .pr-jump button {{
      min-height: 36px;
      padding: 0 12px;
      border-color: #d8e1ef;
      border-radius: 7px;
      background: #fff;
      font-weight: 600;
      white-space: nowrap;
    }}
    .tools > button {{
      min-height: 50px;
      padding: 0 16px;
      border: 0;
      border-right: 1px solid var(--line-soft);
      border-radius: 0;
      background: transparent;
      color: var(--muted);
      font-weight: 600;
    }}
    .tools a {{
      display: inline-flex;
      flex: 0 0 auto;
      min-height: 50px;
      align-items: center;
      justify-content: center;
      padding: 0 18px;
      border: 0;
      border-radius: 0;
      background: linear-gradient(180deg, #edf4ff 0%, #e5efff 100%);
      color: var(--link);
      font-weight: 600;
      white-space: nowrap;
    }}
    .tools a:active {{ transform: scale(0.97); }}
    .link-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .link-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      padding: 0 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      font-size: 12px;
      font-weight: 600;
    }}
    @media (hover: hover) and (pointer: fine) {{
      button:hover {{ border-color: #aab3c2; background: #fbfcfe; }}
      .tools > button:hover {{ background: #f6f8fb; color: var(--ink); }}
      .tools a:hover {{ text-decoration: none; background: linear-gradient(180deg, #e5efff 0%, #dbe8ff 100%); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      button, .tools a {{ transition: none; }}
      button:active, .tools a:active {{ transform: none; }}
    }}
    @media (max-width: 980px) {{
      .layout {{
        grid-template-columns: 1fr;
        grid-template-areas:
          "blockers"
          "queue"
          "body";
      }}
      .snapshot {{ grid-template-columns: 1fr; }}
      .metric {{
        border-right: 0;
        border-bottom: 1px solid var(--line-soft);
      }}
      .metric:last-child {{ border-bottom: 0; }}
    }}
    @media (max-width: 1040px) {{
      header {{ flex-direction: column; align-items: stretch; }}
      .tools {{ width: 100%; justify-content: flex-start; margin-top: 0; }}
    }}
    @media (max-width: 720px) {{
      main {{ width: min(100vw - 20px, 1180px); margin-top: 12px; }}
      h1 {{ font-size: 18px; }}
      .meta {{ font-size: 12px; }}
      section {{ padding: 14px; }}
      .tools {{
        height: auto;
        min-height: 50px;
        flex-wrap: wrap;
        gap: 6px;
        overflow: visible;
        border: 0;
        border-radius: 0;
        background: transparent;
        box-shadow: none;
      }}
      .tools a, .tools > button {{ flex: 1; justify-content: center; }}
      .tools > button, .tools a {{
        min-height: 46px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
      }}
      .tools a {{ background: #eaf1ff; }}
      .pr-jump {{
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: #fff;
      }}
      .pr-jump input {{ flex: 1; width: auto; }}
      .body-pre {{ max-height: 54vh; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="title-block">
        <div class="eyebrow">TaiChu PR Brief</div>
        <h1 id="title">PR #{selector.number}</h1>
        <div class="meta" id="meta">Loading…</div>
      </div>
      <div class="tools">
        <form class="pr-jump" id="pr-jump" title="支持任意 TaiChu PR 编号">
          <label for="pr-number">PR</label>
          <input id="pr-number" name="pr" inputmode="numeric" pattern="[0-9]+" value="{selector.number}" aria-label="PR number">
          <button type="submit">打开</button>
        </form>
        <button id="refresh" type="button">刷新</button>
        <a id="open-pr" href="#" target="_blank" rel="noreferrer">打开原 PR</a>
      </div>
    </header>
    <div class="snapshot" id="snapshot" aria-live="polite"></div>
    <div class="layout">
      <section class="blockers-section">
        <div class="section-head">
          <h2>Latest Blockers</h2>
          <div class="hint" title="{html.escape(watched)}">只看关键门禁</div>
        </div>
        <div id="gates"></div>
      </section>
      <section class="queue-section">
        <div class="section-head">
          <h2>Queue</h2>
          <div class="hint">最新 3 条</div>
        </div>
        <div id="queue"></div>
      </section>
      <section class="body-section">
        <div class="section-head">
          <h2>PR Body</h2>
          <div class="hint">原文保留</div>
        </div>
        <pre class="body-pre" id="body"></pre>
      </section>
    </div>
  </main>
  <script>
    const prNumber = {selector.number};
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }}[ch]));
    const link = (url, label) => url ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{escapeHtml(label)}}</a>` : '';
    async function load() {{
      document.getElementById('refresh').disabled = true;
      try {{
        const res = await fetch(`/api/pr/${{prNumber}}/summary`, {{ cache: 'no-store' }});
        if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
        render(await res.json());
      }} catch (err) {{
        document.getElementById('gates').innerHTML = `<div class="failure"><strong>加载失败</strong><pre>${{escapeHtml(err.message || err)}}</pre></div>`;
        document.getElementById('snapshot').innerHTML = snapshotMetric('metric-danger', 'Status', 'Load failed', err.message || err);
      }} finally {{
        document.getElementById('refresh').disabled = false;
      }}
    }}
    function render(data) {{
      const pr = data.pr;
      document.title = `TaiChu PR #${{pr.number}} Brief`;
      document.getElementById('title').textContent = `#${{pr.number}} ${{pr.title || ''}}`;
      document.getElementById('open-pr').href = pr.html_url;
      document.getElementById('meta').innerHTML = [
        `head <code>${{escapeHtml((pr.head_sha || '').slice(0, 12))}}</code>`,
        `branch <code>${{escapeHtml(pr.head_ref || '-')}}</code> -> <code>${{escapeHtml(pr.base_ref || '-')}}</code>`,
        `updated ${{escapeHtml(pr.updated_at || '-')}}`,
        `fetched ${{escapeHtml(data.fetched_at || '-')}}`
      ].join('<span>·</span>');
      renderSnapshot(data);
      renderGates(data.gates || []);
      renderQueue(data.queue || []);
      document.getElementById('body').textContent = pr.body || '(empty)';
    }}
    function renderSnapshot(data) {{
      const gates = data.gates || [];
      const queue = data.queue || [];
      const pr = data.pr || {{}};
      const firstGate = gates[0];
      const cls = !firstGate ? 'metric-ok' : ['pending', 'running', 'unknown'].includes(firstGate.state) ? 'metric-warn' : 'metric-danger';
      const gateValue = !firstGate ? 'Clear' : `${{firstGate.context}}`;
      const gateDetail = !firstGate ? 'watched gates have no current failure' : `${{firstGate.state}} · ${{firstGate.summary || ''}}`;
      const queueTop = queue[0];
      document.getElementById('snapshot').innerHTML = [
        snapshotMetric(cls, 'Blocker', gateValue, gateDetail),
        snapshotMetric(queueTop ? 'metric-warn' : '', 'Queue', queueTop ? firstLine(queueTop.summary) : 'No active queue clue', queueTop ? queueTop.updated_at || queueTop.created_at || '' : ''),
        snapshotMetric('', 'Head', (pr.head_sha || '').slice(0, 12) || '-', `${{pr.head_ref || '-'}} -> ${{pr.base_ref || '-'}}`)
      ].join('');
    }}
    function snapshotMetric(cls, label, value, detail) {{
      return `<div class="metric ${{cls}}">
        <div class="metric-label">${{escapeHtml(label)}}</div>
        <div class="metric-value" title="${{escapeHtml(value)}}">${{escapeHtml(value)}}</div>
        <div class="metric-detail" title="${{escapeHtml(detail || '')}}">${{escapeHtml(detail || '-')}}</div>
      </div>`;
    }}
    function firstLine(value) {{
      return String(value || '').split('\\n').find(Boolean) || '-';
    }}
    function renderGates(gates) {{
      const target = document.getElementById('gates');
      if (!gates.length) {{
        target.innerHTML = '<p class="empty">五个关键门禁当前没有最新失败。</p>';
        return;
      }}
      target.innerHTML = gates.map((gate) => {{
        const cls = ['pending', 'running', 'unknown'].includes(gate.state) ? 'failure pending' : 'failure';
        return `<div class="${{cls}}">
          <div class="gate-title"><span class="state">${{escapeHtml(gate.state)}}</span> <strong>${{escapeHtml(gate.context)}}</strong></div>
          <pre>${{escapeHtml(gate.summary)}}</pre>
          <div class="link-row">${{linkPill(gate.target_url, 'status')}} ${{linkPill(gate.comment_url, 'comment')}}</div>
        </div>`;
      }}).join('');
    }}
    function renderQueue(events) {{
      const target = document.getElementById('queue');
      if (!events.length) {{
        target.innerHTML = '<p class="small">没有找到最新排队或运行评论。</p>';
        return;
      }}
      target.innerHTML = events.map((event) => `<div class="queue-item">
        <div class="queue-title"><strong>${{escapeHtml(event.author || '-')}}</strong> <span class="small">${{escapeHtml(event.updated_at || event.created_at || '-')}}</span></div>
        <pre>${{escapeHtml(event.summary)}}</pre>
        <div class="link-row">${{linkPill(event.html_url, 'comment')}}</div>
      </div>`).join('');
    }}
    function linkPill(url, label) {{
      return url ? `<a class="link-pill" href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{escapeHtml(label)}}</a>` : '';
    }}
    document.getElementById('refresh').addEventListener('click', load);
    document.getElementById('pr-jump').addEventListener('submit', (event) => {{
      event.preventDefault();
      const value = document.getElementById('pr-number').value.trim();
      if (/^\\d+$/.test(value)) {{
        window.location.href = `/pr/${{value}}`;
      }}
    }});
    load();
  </script>
</body>
</html>"""


def print_text_summary(summary: dict[str, Any]) -> None:
    pr = summary["pr"]
    print(f"PR #{pr['number']} {pr['title']}")
    print(f"head {pr['head_sha'][:12]} {pr['head_ref']} -> {pr['base_ref']}")
    print(f"fetched {summary['fetched_at']}")
    print()
    print("Latest failures:")
    if summary["gates"]:
        for gate in summary["gates"]:
            print(f"- {gate['context']} [{gate['state']}]")
            print(textwrap.indent(gate["summary"], "  "))
            if gate.get("target_url"):
                print(f"  status: {gate['target_url']}")
            if gate.get("comment_url"):
                print(f"  comment: {gate['comment_url']}")
    else:
        print("- none")
    print()
    print("Queue / running:")
    if summary["queue"]:
        for event in summary["queue"]:
            when = event.get("updated_at") or event.get("created_at") or "-"
            print(f"- {when} {event.get('author') or '-'}")
            print(textwrap.indent(event["summary"], "  "))
    else:
        print("- none")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact read-only Gitea PR brief.")
    parser.add_argument("pr", help="PR number or Gitea PR URL, for example 1222")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--web-base", default=DEFAULT_WEB_BASE)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--comment-pages", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument("--serve", action="store_true", help="Start the local dashboard server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    selector = parse_pr_selector(args.pr, args.owner, args.repo)
    credential = resolve_credential(args.web_base, selector.owner, selector.repo)
    client = GiteaClient(args.api_base, args.web_base, credential)
    if args.serve:
        serve(client, selector, args.host, args.port, args.comment_pages)
        return 0
    summary = build_summary(client, selector, args.comment_pages)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_text_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
