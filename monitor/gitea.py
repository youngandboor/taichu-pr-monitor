"""Small standard-library Gitea client for the all-open-PR monitor."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional


DEFAULT_API_BASE = "https://taichu.fun/gitea/api/v1"
DEFAULT_WEB_BASE = "https://taichu.fun/gitea"
DEFAULT_OWNER = "SystemAgentDev"
DEFAULT_REPO = "TaiChu"


@dataclasses.dataclass(frozen=True)
class Credential:
    scheme: str
    value: str


class GiteaApiError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ApiResponse:
    payload: Any
    headers: Mapping[str, str]


class GiteaClient:
    def __init__(
        self,
        api_base: str,
        credential: Optional[Credential],
        timeout_seconds: float = 60,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.credential = credential
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def list_open_pulls(
        self,
        owner: str,
        repo: str,
        max_pages: int = 100,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return self.api_get_pages(
            f"/repos/{owner}/{repo}/pulls?state=open&sort=recentupdate",
            max_pages=max_pages,
            limit=limit,
            require_complete=True,
        )

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        max_pages: int = 3,
    ) -> List[Dict[str, Any]]:
        return self.api_get_pages(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            max_pages=max_pages,
            recent=True,
        )

    def get_statuses(self, owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
        try:
            statuses = self.api_get_pages(
                f"/repos/{owner}/{repo}/statuses/{sha}",
                max_pages=5,
                recent=True,
            )
            if statuses:
                return statuses
        except GiteaApiError:
            pass
        combined = self.api_get(f"/repos/{owner}/{repo}/commits/{sha}/status")
        statuses = combined.get("statuses") if isinstance(combined, dict) else None
        return [item for item in statuses if isinstance(item, dict)] if isinstance(statuses, list) else []

    def api_get_pages(
        self,
        path: str,
        max_pages: int,
        limit: int = 100,
        recent: bool = False,
        require_complete: bool = False,
    ) -> List[Dict[str, Any]]:
        first = self.api_get_response(_paged_path(path, limit, 1))
        if not isinstance(first.payload, list):
            raise GiteaApiError(f"expected list payload for {path}")

        last_page = _last_page(first.headers, len(first.payload))
        if last_page is not None:
            if require_complete and last_page > max_pages:
                raise GiteaApiError(
                    f"{path} has {last_page} pages, above configured maximum {max_pages}"
                )
            if recent:
                first_target = max(1, last_page - max_pages + 1)
                target_pages = list(range(first_target, last_page + 1))
            else:
                target_pages = list(range(1, min(last_page, max_pages) + 1))
            items: List[Dict[str, Any]] = []
            if 1 in target_pages:
                items.extend(item for item in first.payload if isinstance(item, dict))
            for page in target_pages:
                if page == 1:
                    continue
                response = self.api_get_response(_paged_path(path, limit, page))
                if not isinstance(response.payload, list):
                    raise GiteaApiError(f"expected list payload for {path}")
                items.extend(item for item in response.payload if isinstance(item, dict))
            return items

        items = [item for item in first.payload if isinstance(item, dict)]
        if len(first.payload) < limit:
            return items
        for page in range(2, max_pages + 1):
            response = self.api_get_response(_paged_path(path, limit, page))
            if not isinstance(response.payload, list):
                raise GiteaApiError(f"expected list payload for {path}")
            items.extend(item for item in response.payload if isinstance(item, dict))
            if len(response.payload) < limit:
                break
        return items

    def api_get(self, path: str) -> Any:
        return self.api_get_response(path).payload

    def api_get_response(self, path: str) -> ApiResponse:
        request = urllib.request.Request(
            self.api_base + path,
            headers=self._headers(),
            method="GET",
        )
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    data = response.read()
                    headers = dict(response.headers.items())
                break
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")[:500]
                raise GiteaApiError(f"Gitea API {error.code} for {path}: {body}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt >= total_attempts:
                    raise GiteaApiError(
                        f"Gitea API request failed for {path} after "
                        f"{total_attempts} attempts: {error}"
                    ) from error
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                if delay:
                    time.sleep(delay)
        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise GiteaApiError(f"Gitea API returned invalid JSON for {path}") from error
        return ApiResponse(payload, headers)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "taichu-pr-monitor/0.2",
        }
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
        hosts.append(parsed.hostname[4:])
    else:
        hosts.append("www." + parsed.hostname)
    paths = [
        f"{parsed.path.strip('/')}/{owner}/{repo}.git".strip("/"),
        parsed.path.strip("/"),
        "",
    ]
    commands = [["git", "credential", "fill"]]
    if sys.platform == "darwin" and os.path.exists("/usr/bin/git"):
        commands.append(
            ["/usr/bin/git", "-c", "credential.helper=osxkeychain", "credential", "fill"]
        )
    for host in dict.fromkeys(hosts):
        for path in paths:
            lines = [f"protocol={parsed.scheme}", f"host={host}"]
            if path:
                lines.append(f"path={path}")
            payload = "\n".join(lines) + "\n\n"
            for command in commands:
                credential = _credential_from_git_command(command, payload)
                if credential:
                    return credential
    return None


def basic_credential(username: str, password: str) -> Credential:
    raw = f"{username}:{password}".encode("utf-8")
    return Credential("Basic", base64.b64encode(raw).decode("ascii"))


def _credential_from_git_command(command: List[str], payload: str) -> Optional[Credential]:
    try:
        process = subprocess.run(
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
    if process.returncode != 0:
        return None
    values = {}
    for line in process.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if values.get("username") and values.get("password"):
        return basic_credential(values["username"], values["password"])
    return None


def _paged_path(path: str, limit: int, page: int) -> str:
    separator = "&" if "?" in path else "?"
    query = urllib.parse.urlencode({"limit": str(limit), "page": str(page)})
    return path + separator + query


def _last_page(headers: Mapping[str, str], first_page_count: int) -> Optional[int]:
    link = _header(headers, "Link")
    if link:
        for part in link.split(","):
            if 'rel="last"' not in part:
                continue
            match = part.split(";", 1)[0].strip().strip("<>")
            values = urllib.parse.parse_qs(urllib.parse.urlparse(match).query)
            try:
                return max(1, int(values.get("page", ["1"])[0]))
            except (TypeError, ValueError):
                break
        if 'rel="next"' not in link:
            return 1

    total_text = _header(headers, "X-Total-Count")
    if total_text:
        try:
            total = int(total_text)
            if total <= first_page_count:
                return 1
            page_size = max(1, first_page_count)
            return (total + page_size - 1) // page_size
        except ValueError:
            pass
    return None


def _header(headers: Mapping[str, str], name: str) -> str:
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return str(value)
    return ""
