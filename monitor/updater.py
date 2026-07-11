"""Conservative fast-forward updates for the monitor's own checkout."""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
from typing import Callable, Optional


@dataclasses.dataclass(frozen=True)
class UpdateResult:
    status: str
    message: str
    before_sha: str = ""
    after_sha: str = ""


class RepositoryUpdater:
    """Update a clean ``main`` checkout without overwriting local work."""

    def __init__(
        self,
        repo_root: pathlib.Path,
        timeout_seconds: float = 180.0,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        self.repo_root = pathlib.Path(repo_root).resolve()
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run

    def update(self) -> UpdateResult:
        try:
            inside = self._git("rev-parse", "--is-inside-work-tree")
            if inside.stdout.strip().lower() != "true":
                return UpdateResult("failed", "当前目录不是 Git 仓库")

            branch = self._git("branch", "--show-current").stdout.strip()
            if branch != "main":
                return UpdateResult("failed", f"当前分支是 {branch or '(detached)'}，只能更新 main")

            dirty = self._git("status", "--porcelain", "--untracked-files=all").stdout.strip()
            if dirty:
                return UpdateResult("failed", "检测到本地代码改动，已拒绝自动更新")

            before = self._git("rev-parse", "HEAD").stdout.strip()
            self._git("fetch", "--quiet", "origin", "main")
            remote = self._git("rev-parse", "origin/main").stdout.strip()
            if before == remote:
                return UpdateResult("current", "当前已经是最新版本", before, before)

            ancestor = self._git(
                "merge-base",
                "--is-ancestor",
                "HEAD",
                "origin/main",
                allow_failure=True,
            )
            if ancestor.returncode != 0:
                return UpdateResult("failed", "本地 main 与 origin/main 已分叉，无法安全快进", before, remote)

            self._git("merge", "--ff-only", "origin/main")
            after = self._git("rev-parse", "HEAD").stdout.strip()
            return UpdateResult("updated", "更新完成，正在重启监控", before, after)
        except (OSError, subprocess.SubprocessError) as error:
            return UpdateResult("failed", _safe_error(error))

    def _git(
        self,
        *arguments: str,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess:
        process = self.runner(
            ["git", *arguments],
            cwd=str(self.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )
        if process.returncode != 0 and not allow_failure:
            detail = (process.stderr or process.stdout or "git command failed").strip()
            raise subprocess.SubprocessError(detail[:500])
        return process


def _safe_error(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return f"更新失败：{text[:500]}"
