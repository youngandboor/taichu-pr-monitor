"""Local operational dashboard for the TaiChu PR monitor."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hmac
import ipaddress
import json
import logging
import mimetypes
import pathlib
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Union
from urllib.parse import parse_qs, unquote, urlparse

from .state import (
    MonitorStore,
    OUTBOX_STATUSES,
    employee_number_from_welink,
    normalize_employee_number,
)


STATIC_ROOT = pathlib.Path(__file__).resolve().parent / "static"
ACTION_HEADER = "X-Monitor-Action"


class DashboardRuntime:
    def __init__(self, wake_event: threading.Event) -> None:
        self.wake_event = wake_event
        self._lock = threading.Lock()
        self._scanning = False
        self._scan_requested = False
        self._paused = False
        self._pause_requested = False
        self._update_requested = False
        self._update_status = "idle"
        self._update_message = ""
        self._update_from_sha = ""
        self._update_to_sha = ""

    def request_scan(self) -> bool:
        with self._lock:
            if self._paused or self._pause_requested:
                return False
            accepted = not self._scan_requested
            self._scan_requested = True
        self.wake_event.set()
        return accepted

    def request_pause(self) -> bool:
        with self._lock:
            if self._paused or self._pause_requested:
                return False
            if self._scanning:
                self._pause_requested = True
            else:
                self._paused = True
        self.wake_event.set()
        return True

    def request_stop(self) -> bool:
        """Stop future scans while keeping the dashboard process available."""
        return self.request_pause()

    def resume(self) -> bool:
        with self._lock:
            changed = self._paused or self._pause_requested
            self._paused = False
            self._pause_requested = False
        self.wake_event.set()
        return changed

    def request_update(self) -> bool:
        with self._lock:
            if self._update_requested or self._update_status == "updating":
                return False
            self._update_requested = True
            self._update_status = "requested"
            self._update_message = "已安排程序更新"
            self._update_from_sha = ""
            self._update_to_sha = ""
        self.wake_event.set()
        return True

    def claim_update(self) -> bool:
        with self._lock:
            if not self._update_requested:
                return False
            self._update_requested = False
            self._update_status = "updating"
            self._update_message = "正在检查 origin/main"
            return True

    def finish_update(
        self,
        status: str,
        message: str,
        before_sha: str = "",
        after_sha: str = "",
    ) -> None:
        with self._lock:
            self._update_status = status
            self._update_message = message
            self._update_from_sha = before_sha
            self._update_to_sha = after_sha

    def scan_started(self) -> bool:
        with self._lock:
            if self._paused or self._pause_requested:
                return False
            self._scanning = True
            self._scan_requested = False
        return True

    def scan_finished(self) -> None:
        with self._lock:
            self._scanning = False
            if self._pause_requested:
                self._pause_requested = False
                self._paused = True

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused or self._pause_requested

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "scanning": self._scanning,
                "scan_requested": self._scan_requested,
                "paused": self._paused,
                "pause_requested": self._pause_requested,
                "update_requested": self._update_requested,
                "update_status": self._update_status,
                "update_message": self._update_message,
                "update_from_sha": self._update_from_sha,
                "update_to_sha": self._update_to_sha,
            }


class DashboardServer:
    def __init__(
        self,
        host: str,
        port: int,
        state_path: Union[str, pathlib.Path],
        runtime: DashboardRuntime,
        logger: logging.Logger = None,
        allow_remote_actions: bool = False,
        access_token: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.state_path = pathlib.Path(state_path)
        self.runtime = runtime
        self.logger = logger or logging.getLogger(__name__)
        handler = _handler_factory(
            self.state_path,
            self.runtime,
            self.logger,
            allow_remote_actions=allow_remote_actions,
            access_token=access_token,
        )
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="taichu-monitor-dashboard",
            daemon=True,
        )

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{display_host}:{self.httpd.server_port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def dashboard_payload(
    store: MonitorStore,
    runtime: Mapping[str, Any],
    *,
    outbox_statuses: Optional[tuple] = None,
    outbox_limit: int = 500,
) -> Dict[str, Any]:
    snapshots = store.list_snapshots()
    outbox = store.list_outbox(
        limit=outbox_limit,
        newest_first=True,
        statuses=outbox_statuses,
    )
    outbox_counts = store.outbox_counts()
    opt_outs = store.list_notification_opt_outs()
    opted_out_numbers = {item.employee_number for item in opt_outs}
    scan = store.latest_scan()
    attention_statuses = {"dead", "uncertain", "unmapped"}
    pending_statuses = {"pending", "failed"}
    failing_prs = sum(1 for snapshot in snapshots if snapshot.failures)
    delivery_attention = sum(outbox_counts[status] for status in attention_statuses)
    pending_delivery = sum(outbox_counts[status] for status in pending_statuses)
    sent_messages = outbox_counts["sent"]
    open_prs = scan.open_prs if scan is not None else len(snapshots)
    author_counts: Dict[tuple, int] = {}
    for snapshot in snapshots:
        key = (snapshot.author, snapshot.author_w3)
        author_counts[key] = author_counts.get(key, 0) + 1
    recipient_candidates = []
    for (author, author_w3), count in sorted(
        author_counts.items(),
        key=lambda item: (item[0][0].lower(), item[0][1].lower()),
    ):
        employee_number = employee_number_from_welink(author_w3) or ""
        recipient_candidates.append(
            {
                "author": author,
                "author_w3": author_w3,
                "welink_account": author_w3,
                "employee_number": employee_number,
                "open_prs": count,
                "opted_out": bool(
                    employee_number and employee_number in opted_out_numbers
                ),
            }
        )
    available_outbox = (
        sum(outbox_counts[status] for status in outbox_statuses)
        if outbox_statuses
        else outbox_counts["total"]
    )
    return {
        "generated_at": _utc_now(),
        "runtime": dict(runtime),
        "scan": dataclasses.asdict(scan) if scan is not None else None,
        "metrics": {
            "open_prs": open_prs,
            "failing_prs": failing_prs,
            "pending_delivery": pending_delivery,
            "delivery_attention": delivery_attention,
            "sent_messages": sent_messages,
            "outbox_counts": outbox_counts,
        },
        "outbox_counts": outbox_counts,
        "outbox_query": {
            "statuses": list(outbox_statuses or ()),
            "limit": outbox_limit,
            "returned": len(outbox),
            "available": available_outbox,
            "truncated": available_outbox > len(outbox),
        },
        "opt_outs": [dataclasses.asdict(item) for item in opt_outs],
        "recipient_candidates": recipient_candidates,
        "author_candidates": recipient_candidates,
        "storage": _storage_payload(store),
        "pull_requests": [
            {
                "number": snapshot.number,
                "title": snapshot.title,
                "author": snapshot.author,
                "author_w3": snapshot.author_w3,
                "head_sha": snapshot.head_sha,
                "url": snapshot.url,
                "latest_ci_command": snapshot.latest_ci_command,
                "latest_ci_command_at": snapshot.latest_ci_command_at,
                "scanned_at": snapshot.scanned_at,
                "failures": [dataclasses.asdict(failure) for failure in snapshot.failures],
            }
            for snapshot in snapshots
        ],
        "outbox": [dataclasses.asdict(item) for item in outbox],
    }


def _handler_factory(
    state_path,
    runtime,
    logger,
    *,
    allow_remote_actions=False,
    access_token="",
):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "TaiChuMonitorDashboard/1.0"

        def do_GET(self) -> None:
            if not self._authorize():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/dashboard":
                try:
                    query = parse_qs(parsed.query)
                    statuses = _outbox_statuses(
                        _single_query_value(query, "outbox_status")
                    )
                    limit = _outbox_limit(_single_query_value(query, "outbox_limit"))
                    with MonitorStore(state_path) as store:
                        payload = dashboard_payload(
                            store,
                            runtime.snapshot(),
                            outbox_statuses=statuses,
                            outbox_limit=limit,
                        )
                    self._send_json(200, payload)
                except ValueError as error:
                    self._send_json(400, {"error": str(error)})
                except Exception as error:
                    logger.exception("dashboard API failed")
                    self._send_json(500, {"error": str(error)})
                return
            if path == "/api/opt-outs":
                try:
                    with MonitorStore(state_path) as store:
                        values = [
                            dataclasses.asdict(item)
                            for item in store.list_notification_opt_outs()
                        ]
                    self._send_json(200, {"opt_outs": values})
                except Exception as error:
                    logger.exception("opt-out API failed")
                    self._send_json(500, {"error": str(error)})
                return
            assets = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.css": "app.css",
                "/app.js": "app.js",
                "/brand.png": "brand.png",
            }
            asset = assets.get(path)
            if asset is None:
                self._send_json(404, {"error": "not found"})
                return
            self._send_asset(STATIC_ROOT / asset)

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if self.headers.get(ACTION_HEADER) != "1":
                self._send_json(403, {"error": "action header required"})
                return
            if not allow_remote_actions and not self._is_local_client():
                self._send_json(403, {"error": "management actions are local-only"})
                return
            path = urlparse(self.path).path
            if path == "/api/scan":
                accepted = runtime.request_scan()
                self._send_json(202, {"accepted": accepted})
                return
            if path in {"/api/monitor/pause", "/api/pause"}:
                accepted = runtime.request_pause()
                self._send_json(202, {"accepted": accepted, "runtime": runtime.snapshot()})
                return
            if path in {"/api/monitor/stop", "/api/stop"}:
                accepted = runtime.request_stop()
                self._send_json(202, {"accepted": accepted, "runtime": runtime.snapshot()})
                return
            if path in {"/api/monitor/resume", "/api/resume"}:
                accepted = runtime.resume()
                self._send_json(202, {"accepted": accepted, "runtime": runtime.snapshot()})
                return
            if path == "/api/update":
                body = self._read_json()
                if not body.get("confirm"):
                    self._send_json(400, {"error": "explicit confirmation required"})
                    return
                accepted = runtime.request_update()
                self._send_json(202, {"accepted": accepted, "runtime": runtime.snapshot()})
                return
            if path in {"/api/opt-outs", "/api/opt-outs/add"}:
                self._change_opt_out(add=True)
                return
            if path == "/api/opt-outs/remove":
                self._change_opt_out(add=False)
                return
            match = re.fullmatch(r"/api/outbox/(\d+)/retry", path)
            if match:
                body = self._read_json()
                if not body.get("confirm"):
                    self._send_json(400, {"error": "explicit confirmation required"})
                    return
                with MonitorStore(state_path) as store:
                    updated = store.requeue_delivery(int(match.group(1)))
                if not updated:
                    self._send_json(409, {"error": "delivery cannot be retried"})
                    return
                runtime.request_scan()
                self._send_json(202, {"accepted": True})
                return
            self._send_json(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            if not self._authorize():
                return
            if self.headers.get(ACTION_HEADER) != "1":
                self._send_json(403, {"error": "action header required"})
                return
            if not allow_remote_actions and not self._is_local_client():
                self._send_json(403, {"error": "management actions are local-only"})
                return
            match = re.fullmatch(r"/api/opt-outs/([^/]+)", urlparse(self.path).path)
            if not match:
                self._send_json(404, {"error": "not found"})
                return
            self._change_opt_out(add=False, login=unquote(match.group(1)))

        def _change_opt_out(self, *, add: bool, login: str = "") -> None:
            body = self._read_json() if not login else {}
            try:
                with MonitorStore(state_path) as store:
                    employee_number = _opt_out_employee_number(store, body, login)
                    if add:
                        suppressed = store.add_notification_opt_out(employee_number)
                        response = {
                            "employee_number": employee_number,
                            "opted_out": True,
                            "suppressed": suppressed,
                        }
                    else:
                        removed = store.remove_notification_opt_out(employee_number)
                        response = {
                            "employee_number": employee_number,
                            "opted_out": False,
                            "removed": removed,
                        }
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            except Exception as error:
                logger.exception("opt-out update failed")
                self._send_json(500, {"error": str(error)})
                return
            self._send_json(200, response)

        def _is_local_client(self) -> bool:
            try:
                address = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            if address.is_loopback:
                return True
            mapped = getattr(address, "ipv4_mapped", None)
            return bool(mapped and mapped.is_loopback)

        def _authorize(self) -> bool:
            if not access_token:
                return True
            encoded = base64.b64encode(
                f"monitor:{access_token}".encode("utf-8")
            ).decode("ascii")
            expected = f"Basic {encoded}"
            supplied = self.headers.get("Authorization", "")
            if hmac.compare_digest(supplied, expected):
                return True
            self._send_json(
                401,
                {"error": "authentication required"},
                extra_headers={
                    "WWW-Authenticate": 'Basic realm="TaiChu PR Monitor", charset="UTF-8"'
                },
            )
            return False

        def _read_json(self) -> Dict[str, Any]:
            try:
                length = max(0, min(int(self.headers.get("Content-Length", "0")), 4096))
                payload = self.rfile.read(length) if length else b"{}"
                value = json.loads(payload.decode("utf-8"))
                return value if isinstance(value, dict) else {}
            except (ValueError, json.JSONDecodeError):
                return {}

        def _send_asset(self, path: pathlib.Path) -> None:
            try:
                payload = path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "asset not found"})
                return
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type.endswith("javascript") else ""))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(
            self,
            status: int,
            value: Mapping[str, Any],
            extra_headers: Optional[Mapping[str, str]] = None,
        ) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for name, header_value in (extra_headers or {}).items():
                self.send_header(name, header_value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format_string: str, *args) -> None:
            logger.debug("dashboard: " + format_string, *args)

    return DashboardHandler


def _opt_out_employee_number(
    store: MonitorStore,
    body: Mapping[str, Any],
    path_value: str = "",
) -> str:
    if path_value:
        return normalize_employee_number(path_value)

    for key in ("employee_number", "welink", "welink_account", "author_w3"):
        value = str(body.get(key) or "").strip()
        if value:
            return normalize_employee_number(value)

    for key in ("author", "login"):
        value = str(body.get(key) or "").strip()
        if not value:
            continue
        try:
            return normalize_employee_number(value)
        except ValueError:
            employee_number = store.employee_number_for_author(value)
            if employee_number is not None:
                return employee_number
            raise ValueError(
                f"no WeLink employee number is known for Gitea author {value}"
            )

    raise ValueError("employee_number is required")


def _storage_payload(store: MonitorStore) -> Dict[str, Any]:
    database_path = store.path.resolve()
    file_sizes = {}
    paths = {
        "database": database_path,
        "wal": pathlib.Path(str(database_path) + "-wal"),
        "shm": pathlib.Path(str(database_path) + "-shm"),
        "journal": pathlib.Path(str(database_path) + "-journal"),
    }
    for name, path in paths.items():
        try:
            file_sizes[name] = path.stat().st_size
        except OSError:
            file_sizes[name] = 0

    page_size = int(store.connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(store.connection.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(store.connection.execute("PRAGMA freelist_count").fetchone()[0])
    disk = shutil.disk_usage(database_path.parent)
    free_ratio = (disk.free / disk.total) if disk.total else 0.0
    warning_level = "ok"
    warnings = []
    if disk.free < 512 * 1024 * 1024 or free_ratio < 0.01:
        warning_level = "critical"
        warnings.append("disk space is critically low")
    elif disk.free < 2 * 1024 * 1024 * 1024 or free_ratio < 0.05:
        warning_level = "warning"
        warnings.append("disk space is running low")

    return {
        "path": str(database_path),
        "database_bytes": file_sizes["database"],
        "sidecar_bytes": {
            "wal": file_sizes["wal"],
            "shm": file_sizes["shm"],
            "journal": file_sizes["journal"],
        },
        "total_bytes": sum(file_sizes.values()),
        "reclaimable_bytes": page_size * freelist_count,
        "free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "sqlite": {
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "allocated_bytes": page_size * page_count,
            "reclaimable_bytes": page_size * freelist_count,
        },
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "free_percent": round(free_ratio * 100, 2),
        },
        "warning": warning_level != "ok",
        "warning_level": warning_level,
        "warnings": warnings,
    }


def _single_query_value(query: Mapping[str, list], name: str) -> str:
    values = query.get(name, [])
    if len(values) > 1:
        raise ValueError(f"query parameter {name} may only be supplied once")
    return str(values[0]).strip() if values else ""


def _outbox_limit(value: str) -> int:
    if not value:
        return 500
    try:
        limit = int(value)
    except ValueError as error:
        raise ValueError("outbox_limit must be an integer") from error
    if limit < 1 or limit > 5000:
        raise ValueError("outbox_limit must be between 1 and 5000")
    return limit


def _outbox_statuses(value: str) -> Optional[tuple]:
    if not value:
        return None
    statuses = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not statuses or any(status not in OUTBOX_STATUSES for status in statuses):
        raise ValueError("invalid outbox status")
    return statuses


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
