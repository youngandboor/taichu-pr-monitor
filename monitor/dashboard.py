"""Local operational dashboard for the TaiChu PR monitor."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import mimetypes
import pathlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Union
from urllib.parse import urlparse

from .state import MonitorStore


STATIC_ROOT = pathlib.Path(__file__).resolve().parent / "static"
ACTION_HEADER = "X-Monitor-Action"


class DashboardRuntime:
    def __init__(self, wake_event: threading.Event) -> None:
        self.wake_event = wake_event
        self._lock = threading.Lock()
        self._scanning = False
        self._scan_requested = False

    def request_scan(self) -> bool:
        with self._lock:
            accepted = not self._scan_requested
            self._scan_requested = True
        self.wake_event.set()
        return accepted

    def scan_started(self) -> None:
        with self._lock:
            self._scanning = True
            self._scan_requested = False

    def scan_finished(self) -> None:
        with self._lock:
            self._scanning = False

    def snapshot(self) -> Dict[str, bool]:
        with self._lock:
            return {
                "scanning": self._scanning,
                "scan_requested": self._scan_requested,
            }


class DashboardServer:
    def __init__(
        self,
        host: str,
        port: int,
        state_path: Union[str, pathlib.Path],
        runtime: DashboardRuntime,
        logger: logging.Logger = None,
    ) -> None:
        self.host = host
        self.port = port
        self.state_path = pathlib.Path(state_path)
        self.runtime = runtime
        self.logger = logger or logging.getLogger(__name__)
        handler = _handler_factory(self.state_path, self.runtime, self.logger)
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
    runtime: Mapping[str, bool],
) -> Dict[str, Any]:
    snapshots = store.list_snapshots()
    outbox = list(reversed(store.list_outbox()))[:200]
    scan = store.latest_scan()
    attention_statuses = {"failed", "dead", "uncertain", "unmapped"}
    pending_statuses = {"pending", "failed", "dead", "uncertain", "unmapped"}
    failing_prs = sum(1 for snapshot in snapshots if snapshot.failures)
    delivery_attention = sum(1 for item in outbox if item.status in attention_statuses)
    pending_delivery = sum(1 for item in outbox if item.status in pending_statuses)
    sent_messages = sum(1 for item in outbox if item.status == "sent")
    open_prs = scan.open_prs if scan is not None else len(snapshots)
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
        },
        "pull_requests": [
            {
                "number": snapshot.number,
                "title": snapshot.title,
                "author": snapshot.author,
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


def _handler_factory(state_path, runtime, logger):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "TaiChuMonitorDashboard/1.0"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/dashboard":
                try:
                    with MonitorStore(state_path) as store:
                        payload = dashboard_payload(store, runtime.snapshot())
                    self._send_json(200, payload)
                except Exception as error:
                    logger.exception("dashboard API failed")
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
            if self.headers.get(ACTION_HEADER) != "1":
                self._send_json(403, {"error": "action header required"})
                return
            path = urlparse(self.path).path
            if path == "/api/scan":
                accepted = runtime.request_scan()
                self._send_json(202, {"accepted": accepted})
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

        def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format_string: str, *args) -> None:
            logger.debug("dashboard: " + format_string, *args)

    return DashboardHandler


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
