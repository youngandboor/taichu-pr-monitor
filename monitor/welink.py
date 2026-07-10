"""Subprocess adapter for the documented welink-cli send-to-user command."""

from __future__ import annotations

import base64
import dataclasses
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import List, Mapping, Optional, Sequence


@dataclasses.dataclass(frozen=True)
class DeliveryResult:
    status: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float


class WeLinkCli:
    def __init__(
        self,
        command: Sequence[str],
        timeout_seconds: float = 20.0,
        env: Optional[Mapping[str, str]] = None,
        platform: Optional[str] = None,
    ) -> None:
        if not command:
            raise ValueError("welink-cli command must not be empty")
        self.command = list(command)
        self.timeout_seconds = timeout_seconds
        self.env = {**os.environ, **(dict(env) if env else {})}
        self.platform = platform or sys.platform

    def build_command(self, receiver: str, message: str) -> List[str]:
        prefix = self._resolved_prefix(receiver, message)
        if prefix is not None:
            return prefix
        return self.command + [
            "im",
            "send-to-user",
            "--receiver",
            receiver,
            "--text",
            message,
        ]

    def send(self, receiver: str, message: str) -> DeliveryResult:
        started = time.monotonic()
        command = self.build_command(receiver, message)
        try:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self.env,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            return DeliveryResult(
                "timeout",
                None,
                _timeout_text(error.stdout),
                _timeout_text(error.stderr) or f"timed out after {self.timeout_seconds:g}s",
                time.monotonic() - started,
            )
        except OSError as error:
            return DeliveryResult(
                "failure",
                None,
                "",
                str(error),
                time.monotonic() - started,
            )
        status = "success" if process.returncode == 0 else "failure"
        return DeliveryResult(
            status,
            process.returncode,
            process.stdout,
            process.stderr,
            time.monotonic() - started,
        )

    def _resolved_prefix(self, receiver: str, message: str) -> Optional[List[str]]:
        if self.platform != "win32" or len(self.command) != 1:
            return None
        executable = shutil.which(self.command[0]) or self.command[0]
        if pathlib.Path(executable).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
            self.command[0] = executable
            return None
        wrapper = pathlib.Path(__file__).resolve().parent / "windows" / "invoke_welink.ps1"
        encoded = base64.b64encode(message.encode("utf-8")).decode("ascii")
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"
        return [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "-CliPath",
            executable,
            "-Receiver",
            receiver,
            "-TextBase64",
            encoded,
        ]


class DryRunSender:
    def send(self, receiver: str, message: str) -> DeliveryResult:
        print(f"[dry-run receiver={receiver}]\n{message}\n")
        return DeliveryResult("success", 0, "dry-run", "", 0.0)


def _timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
