import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from monitor.core import GateFailure, PrSnapshot, TrackerState
from monitor.dashboard import DashboardRuntime, DashboardServer, dashboard_payload
from monitor.state import MonitorStore, OutboxEvent


class DashboardStoreTest(unittest.TestCase):
    def snapshot(self):
        return PrSnapshot(
            number=42,
            title="Repair cloud preflight",
            author="w00123",
            head_sha="abcdef123456",
            url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/42",
            latest_ci_command="/ci merge",
            latest_ci_command_at="2026-07-10T10:00:00+08:00",
            latest_ci_command_key="cmd-42",
            scanned_at="2026-07-10T10:05:00+08:00",
            failures=(
                GateFailure(
                    "taichu/dev-cloud-preflight",
                    "2026-07-10T10:04:00+08:00",
                    "preflight failed",
                ),
            ),
        )

    def test_dashboard_payload_prioritizes_failures_and_delivery_attention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            self.addCleanup(store.close)
            snapshot = self.snapshot()
            store.apply_poll(
                snapshot.number,
                TrackerState.empty(),
                OutboxEvent("event-1", 42, "w00123", "message"),
                snapshot=snapshot,
            )
            outbox_id = store.list_outbox()[0].id
            store.update_delivery(
                outbox_id,
                "uncertain",
                "w00123",
                "welink-cli timed out",
                increment_attempt=True,
            )
            store.record_scan(
                scanned_at="2026-07-10T10:05:00+08:00",
                duration_seconds=24.8,
                open_prs=106,
                scanned_prs=106,
                new_notifications=1,
                delivered=0,
                delivery_failures=0,
                delivery_uncertain=1,
                unmapped=0,
                errors=[],
            )

            payload = dashboard_payload(store, {"scanning": False, "scan_requested": False})

            self.assertEqual(106, payload["metrics"]["open_prs"])
            self.assertEqual(1, payload["metrics"]["failing_prs"])
            self.assertEqual(1, payload["metrics"]["delivery_attention"])
            self.assertEqual(42, payload["pull_requests"][0]["number"])
            self.assertEqual("uncertain", payload["outbox"][0]["status"])

    def test_requeue_resets_a_failed_or_uncertain_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            self.addCleanup(store.close)
            snapshot = self.snapshot()
            store.apply_poll(
                snapshot.number,
                TrackerState.empty(),
                OutboxEvent("event-1", 42, "w00123", "message"),
                snapshot=snapshot,
            )
            record = store.list_outbox()[0]
            store.update_delivery(
                record.id,
                "dead",
                "w00123",
                "three failures",
                increment_attempt=True,
            )

            self.assertTrue(store.requeue_delivery(record.id))

            retried = store.list_outbox()[0]
            self.assertEqual("pending", retried.status)
            self.assertEqual(0, retried.attempts)
            self.assertEqual("", retried.last_error)


class DashboardServerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = pathlib.Path(self.temp_dir.name) / "state.sqlite3"
        with MonitorStore(self.state_path) as store:
            store.record_scan(
                scanned_at="2026-07-10T10:05:00+08:00",
                duration_seconds=1.2,
                open_prs=0,
                scanned_prs=0,
                new_notifications=0,
                delivered=0,
                delivery_failures=0,
                delivery_uncertain=0,
                unmapped=0,
                errors=[],
            )
        self.wake_event = threading.Event()
        self.runtime = DashboardRuntime(self.wake_event)
        self.server = DashboardServer(
            host="127.0.0.1",
            port=0,
            state_path=self.state_path,
            runtime=self.runtime,
        )
        self.server.start()
        self.addCleanup(self.server.close)

    def test_serves_operational_dashboard_and_json_api(self):
        with urllib.request.urlopen(self.server.url + "/", timeout=2) as response:
            html = response.read().decode("utf-8")
            content_security_policy = response.headers.get("Content-Security-Policy")
        with urllib.request.urlopen(self.server.url + "/api/dashboard", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertIn("TaiChu PR Monitor", html)
        self.assertIn("立即扫描", html)
        self.assertIn("default-src 'self'", content_security_policy)
        self.assertEqual(0, payload["metrics"]["open_prs"])

    def test_scan_action_requires_same_origin_action_header(self):
        blocked = urllib.request.Request(self.server.url + "/api/scan", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(blocked, timeout=2)
        self.assertEqual(403, context.exception.code)

        allowed = urllib.request.Request(
            self.server.url + "/api/scan",
            data=b"{}",
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["accepted"])
        self.assertTrue(self.wake_event.is_set())

    def test_retry_action_requires_confirmation_and_requeues(self):
        with MonitorStore(self.state_path) as store:
            store.apply_poll(
                7,
                TrackerState.empty(),
                OutboxEvent("retry-event", 7, "w00123", "message"),
            )
            record = store.list_outbox()[0]
            store.update_delivery(
                record.id,
                "uncertain",
                "w00123",
                "timeout",
                increment_attempt=True,
            )

        request = urllib.request.Request(
            self.server.url + f"/api/outbox/{record.id}/retry",
            data=json.dumps({"confirm": True}).encode("utf-8"),
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        with MonitorStore(self.state_path) as store:
            retried = store.list_outbox()[0]
        self.assertTrue(payload["accepted"])
        self.assertEqual("pending", retried.status)
        self.assertEqual(0, retried.attempts)


if __name__ == "__main__":
    unittest.main()
