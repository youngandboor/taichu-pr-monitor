import base64
import json
import pathlib
import sqlite3
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
                GateFailure(
                    "taichu/codex-pr-test-review",
                    "2026-07-10T10:03:00+08:00",
                    "发现 1 个 P0/P1 测试审查问题",
                ),
            ),
            author_w3="y00123456",
        )

    def test_dashboard_payload_prioritizes_failures_and_delivery_attention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3") as store:
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
                self.assertEqual(0, payload["metrics"]["pending_delivery"])
                self.assertEqual(1, payload["outbox_counts"]["total"])
                self.assertEqual(1, payload["outbox_counts"]["uncertain"])
                self.assertEqual(42, payload["pull_requests"][0]["number"])
                self.assertEqual(
                    {
                        "taichu/dev-cloud-preflight",
                        "taichu/codex-pr-test-review",
                    },
                    {
                        item["context"]
                        for item in payload["pull_requests"][0]["failures"]
                    },
                )
                self.assertEqual("uncertain", payload["outbox"][0]["status"])
                self.assertEqual("w00123", payload["recipient_candidates"][0]["author"])
                self.assertEqual(
                    "00123456",
                    payload["recipient_candidates"][0]["employee_number"],
                )
                self.assertFalse(payload["author_candidates"][0]["opted_out"])
                self.assertGreater(payload["storage"]["database_bytes"], 0)
                self.assertGreater(payload["storage"]["disk"]["free_bytes"], 0)

    def test_requeue_resets_a_failed_or_uncertain_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3") as store:
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

    def test_opt_out_suppresses_queued_and_future_messages_but_removal_is_not_retroactive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3") as store:
                for index in range(4):
                    store.apply_poll(
                        index + 1,
                        TrackerState.empty(),
                        OutboxEvent(
                            f"event-{index}",
                            index + 1,
                            "Example.User",
                            "message",
                            "y00000001",
                        ),
                    )
                records = store.list_outbox()
                store.update_delivery(records[1].id, "failed", "", "failure", True)
                store.update_delivery(records[2].id, "unmapped", "", "missing", False)
                store.update_delivery(records[3].id, "dead", "w00123", "dead", True)

                self.assertEqual(3, store.add_notification_opt_out("00000001"))
                self.assertTrue(store.is_recipient_opted_out("Y00000001"))
                self.assertEqual(
                    "00000001",
                    store.list_notification_opt_outs()[0].employee_number,
                )
                self.assertEqual(
                    ["suppressed", "suppressed", "suppressed", "dead"],
                    [record.status for record in store.list_outbox()],
                )
                self.assertEqual([], store.list_dispatchable(max_attempts=3))
                self.assertFalse(store.requeue_delivery(records[3].id))

                store.apply_poll(
                    5,
                    TrackerState.empty(),
                    OutboxEvent(
                        "event-4",
                        5,
                        "example.user",
                        "future while opted out",
                        "y00000001",
                    ),
                )
                self.assertEqual("suppressed", store.list_outbox()[-1].status)

                self.assertTrue(store.remove_notification_opt_out("y00000001"))
                store.apply_poll(
                    6,
                    TrackerState.empty(),
                    OutboxEvent(
                        "event-5",
                        6,
                        "example.user",
                        "future after removal",
                        "y00000001",
                    ),
                )
                self.assertEqual("pending", store.list_outbox()[-1].status)
                self.assertEqual("suppressed", store.list_outbox()[-2].status)

    def test_dashboard_uses_exact_full_table_counts_and_returns_latest_500(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3") as store:
                timestamp = "2026-07-10T10:00:00+00:00"
                rows = [
                    (
                        f"event-{index}",
                        index,
                        "w00123",
                        "w00123",
                        f"message-{index}",
                        "sent",
                        1,
                        "",
                        timestamp,
                        timestamp,
                    )
                    for index in range(501)
                ]
                with store.connection:
                    store.connection.execute(
                        """
                        INSERT INTO delivery_outbox (
                            event_key, pr_number, author, receiver, message,
                            status, attempts, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "old-pending",
                            999,
                            "w00123",
                            "w00123",
                            "old pending message",
                            "pending",
                            0,
                            "",
                            timestamp,
                            timestamp,
                        ),
                    )
                    store.connection.executemany(
                        """
                        INSERT INTO delivery_outbox (
                            event_key, pr_number, author, receiver, message,
                            status, attempts, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )

                payload = dashboard_payload(store, {})
                pending_payload = dashboard_payload(
                    store,
                    {},
                    outbox_statuses=("pending", "failed"),
                )

                self.assertEqual(502, payload["outbox_counts"]["total"])
                self.assertEqual(501, payload["outbox_counts"]["sent"])
                self.assertEqual(501, payload["metrics"]["sent_messages"])
                self.assertEqual(500, len(payload["outbox"]))
                self.assertEqual("message-500", payload["outbox"][0]["message"])
                self.assertTrue(payload["outbox_query"]["truncated"])
                self.assertEqual(1, len(pending_payload["outbox"]))
                self.assertEqual("old pending message", pending_payload["outbox"][0]["message"])
                self.assertFalse(pending_payload["outbox_query"]["truncated"])

    def test_schema_upgrade_preserves_legacy_state_and_backfills_welink_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE notification_opt_out (login TEXT PRIMARY KEY, created_at TEXT)"
            )
            connection.execute(
                "INSERT INTO notification_opt_out VALUES ('y00000001', '2026-07-10')"
            )
            connection.execute(
                """
                CREATE TABLE delivery_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE,
                    pr_number INTEGER, author TEXT, receiver TEXT, message TEXT,
                    status TEXT, attempts INTEGER, last_error TEXT,
                    created_at TEXT, updated_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO delivery_outbox (
                    event_key, pr_number, author, receiver, message, status,
                    attempts, last_error, created_at, updated_at
                ) VALUES ('legacy', 1, 'author', 'y00000001', 'message',
                          'dead', 3, 'failed', '2026-07-10', '2026-07-10')
                """
            )
            connection.commit()
            connection.close()

            with MonitorStore(path) as store:
                self.assertEqual(
                    "00000001",
                    store.list_notification_opt_outs()[0].employee_number,
                )
                self.assertEqual(
                    "00000001",
                    store.list_outbox()[0].recipient_employee_number,
                )
                self.assertFalse(store.requeue_delivery(store.list_outbox()[0].id))


class DashboardRuntimeTest(unittest.TestCase):
    def test_stop_finishes_active_scan_then_pauses_until_resumed(self):
        runtime = DashboardRuntime(threading.Event())

        self.assertTrue(runtime.scan_started())
        self.assertTrue(runtime.request_stop())
        self.assertTrue(runtime.snapshot()["pause_requested"])
        self.assertFalse(runtime.snapshot()["paused"])

        runtime.scan_finished()

        self.assertTrue(runtime.snapshot()["paused"])
        self.assertFalse(runtime.snapshot()["pause_requested"])
        self.assertFalse(runtime.request_scan())
        self.assertFalse(runtime.scan_started())
        self.assertTrue(runtime.resume())
        self.assertFalse(runtime.snapshot()["paused"])
        self.assertTrue(runtime.scan_started())

    def test_update_request_is_claimed_and_records_result(self):
        wake_event = threading.Event()
        runtime = DashboardRuntime(wake_event)

        self.assertTrue(runtime.request_update())
        self.assertTrue(wake_event.is_set())
        self.assertFalse(runtime.request_update())
        self.assertTrue(runtime.claim_update())
        self.assertEqual("updating", runtime.snapshot()["update_status"])
        runtime.finish_update("current", "当前已经是最新版本", "abc", "abc")

        snapshot = runtime.snapshot()
        self.assertEqual("current", snapshot["update_status"])
        self.assertEqual("当前已经是最新版本", snapshot["update_message"])
        self.assertEqual("abc", snapshot["update_to_sha"])


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
        self.assertIn("需人工处理", html)
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

    def test_opt_out_can_be_added_and_removed_through_dashboard(self):
        with MonitorStore(self.state_path) as store:
            store.apply_poll(
                8,
                TrackerState.empty(),
                OutboxEvent(
                    "opt-out-event",
                    8,
                    "Example.User",
                    "message",
                    "y00000001",
                ),
            )

        add = urllib.request.Request(
            self.server.url + "/api/opt-outs/add",
            data=json.dumps({"employee_number": "00000001"}).encode("utf-8"),
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(add, timeout=2) as response:
            added = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(self.server.url + "/api/opt-outs", timeout=2) as response:
            listed = json.loads(response.read().decode("utf-8"))

        self.assertEqual("00000001", added["employee_number"])
        self.assertEqual(1, added["suppressed"])
        self.assertEqual("00000001", listed["opt_outs"][0]["employee_number"])
        with MonitorStore(self.state_path) as store:
            self.assertEqual("suppressed", store.list_outbox()[0].status)

        remove = urllib.request.Request(
            self.server.url + "/api/opt-outs/remove",
            data=json.dumps({"employee_number": "y00000001"}).encode("utf-8"),
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(remove, timeout=2) as response:
            removed = json.loads(response.read().decode("utf-8"))

        self.assertTrue(removed["removed"])
        with MonitorStore(self.state_path) as store:
            self.assertFalse(store.is_recipient_opted_out("y00000001"))
            self.assertEqual("suppressed", store.list_outbox()[0].status)

    def test_opt_out_rejects_invalid_employee_number(self):
        request = urllib.request.Request(
            self.server.url + "/api/opt-outs/add",
            data=json.dumps({"employee_number": "../../not a number"}).encode("utf-8"),
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=2)

        self.assertEqual(400, context.exception.code)

    def test_retry_rejects_message_for_opted_out_recipient(self):
        with MonitorStore(self.state_path) as store:
            store.apply_poll(
                9,
                TrackerState.empty(),
                OutboxEvent("do-not-retry", 9, "w00123", "message", "w00000001"),
            )
            record = store.list_outbox()[0]
            store.update_delivery(record.id, "dead", "w00123", "failed", True)
            store.add_notification_opt_out("00000001")

        request = urllib.request.Request(
            self.server.url + f"/api/outbox/{record.id}/retry",
            data=json.dumps({"confirm": True}).encode("utf-8"),
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=2)

        self.assertEqual(409, context.exception.code)

    def test_pause_and_resume_actions_update_runtime(self):
        pause = urllib.request.Request(
            self.server.url + "/api/monitor/pause",
            data=b"{}",
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(pause, timeout=2) as response:
            paused = json.loads(response.read().decode("utf-8"))
        self.assertTrue(paused["runtime"]["paused"])

        resume = urllib.request.Request(
            self.server.url + "/api/monitor/resume",
            data=b"{}",
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(resume, timeout=2) as response:
            resumed = json.loads(response.read().decode("utf-8"))
        self.assertFalse(resumed["runtime"]["paused"])

    def test_update_action_requires_confirmation_and_queues_update(self):
        missing_confirmation = urllib.request.Request(
            self.server.url + "/api/update",
            data=b"{}",
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(missing_confirmation, timeout=2)
        self.assertEqual(400, context.exception.code)

        confirmed = urllib.request.Request(
            self.server.url + "/api/update",
            data=json.dumps({"confirm": True}).encode("utf-8"),
            headers={"X-Monitor-Action": "1", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(confirmed, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["accepted"])
        self.assertEqual("requested", payload["runtime"]["update_status"])
        self.assertTrue(self.wake_event.is_set())

    def test_dashboard_access_token_protects_pages_and_api(self):
        protected = DashboardServer(
            host="127.0.0.1",
            port=0,
            state_path=self.state_path,
            runtime=DashboardRuntime(threading.Event()),
            access_token="secret-value",
        )
        protected.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(protected.url + "/", timeout=2)
            self.assertEqual(401, context.exception.code)
            self.assertIn(
                "Basic",
                context.exception.headers.get("WWW-Authenticate", ""),
            )

            credentials = base64.b64encode(b"monitor:secret-value").decode("ascii")
            request = urllib.request.Request(
                protected.url + "/api/dashboard",
                headers={"Authorization": f"Basic {credentials}"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(0, payload["metrics"]["open_prs"])
        finally:
            protected.close()


if __name__ == "__main__":
    unittest.main()
