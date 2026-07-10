import pathlib
import tempfile
import unittest

from monitor.core import TrackerState
from monitor.service import MonitorService, RecipientDirectory
from monitor.state import MonitorStore, OutboxEvent
from monitor.welink import DeliveryResult


class FakeGiteaClient:
    def __init__(self):
        self.user = {"login": "w00123"}
        self.statuses = [
            {
                "id": 1,
                "context": "taichu/pr-build",
                "state": "success",
                "description": "build success",
                "updated_at": "2026-07-10T10:01:00+08:00",
            }
        ]
        self.comments = [
            {
                "id": 1,
                "body": "/ci build",
                "created_at": "2026-07-10T10:00:00+08:00",
            }
        ]

    def list_open_pulls(self, owner, repo, max_pages=10, limit=100):
        return [
            {
                "number": 7,
                "title": "Repair build",
                "html_url": "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                "user": dict(self.user),
                "head": {"sha": "abcdef123456"},
            }
        ]

    def get_statuses(self, owner, repo, sha):
        return list(self.statuses)

    def get_issue_comments(self, owner, repo, number, max_pages):
        return list(self.comments)


class SequenceSender:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def send(self, receiver, message):
        self.calls.append((receiver, message))
        status = self.statuses.pop(0) if self.statuses else "success"
        if status == "success":
            return DeliveryResult("success", 0, "ok", "", 0.01)
        if status == "timeout":
            return DeliveryResult("timeout", None, "", "timeout", 0.05)
        return DeliveryResult("failure", 23, "", "failed", 0.01)


class Clock:
    def __init__(self, *values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class StoreTest(unittest.TestCase):
    def test_tracker_state_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "state.sqlite3"
            state = TrackerState("cmd-1", frozenset({"failure-1"}), True, "2026-07-10T10:00:00+08:00")
            with MonitorStore(path) as store:
                store.save_tracker(7, state)

            with MonitorStore(path) as reopened:
                restored = reopened.get_tracker(7)

            self.assertEqual(state, restored)


class MonitorServiceTest(unittest.TestCase):
    def make_service(self, temp_dir, client, sender, clock):
        store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
        service = MonitorService(
            client=client,
            store=store,
            sender=sender,
            recipients=RecipientDirectory(direct=True),
            clock=clock,
        )
        return service, store

    def test_baselines_then_sends_new_failure_once_to_author_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender(["success"])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:11:00+08:00",
                ),
            )
            with store:
                first = service.poll_once()
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "compile error in module foo",
                        "updated_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                second = service.poll_once()
                self.assertEqual(1, store.latest_scan().new_notifications)
                third = service.poll_once()

                self.assertEqual(0, first.new_notifications)
                self.assertEqual(1, second.new_notifications)
                self.assertEqual(0, third.new_notifications)
                self.assertEqual(1, len(sender.calls))
                self.assertEqual("w00123", sender.calls[0][0])
                self.assertIn("PR #7", sender.calls[0][1])
                self.assertIn("taichu/pr-build", sender.calls[0][1])
                self.assertIn("compile error", sender.calls[0][1])
                snapshots = store.list_snapshots()
                self.assertEqual(1, len(snapshots))
                self.assertEqual("taichu/pr-build", snapshots[0].failures[0].context)

    def test_gitea_derived_sender_self_recipient_is_routed_to_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.user = {
                "login": "w00123",
                "full_name": "杨示例 00000001",
                "email": "unrelated-prefix.example@company.test",
            }
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(
                    direct=False,
                    sender_account="y00000001",
                    self_fallback_receiver="y00000002",
                ),
                clock=Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "new failure",
                        "updated_at": "2026-07-10T10:06:00+08:00",
                    }
                ]

                report = service.poll_once()

                self.assertEqual(1, report.delivered)
                self.assertEqual("y00000002", sender.calls[0][0])
                self.assertEqual("y00000002", store.list_outbox()[0].receiver)

    def test_legacy_pending_record_uses_w3_discovered_from_current_pr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.user = {
                "login": "w00123",
                "full_name": "杨示例 00000001",
            }
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            store.apply_poll(
                7,
                TrackerState.empty(),
                OutboxEvent("legacy-event", 7, "w00123", "legacy message"),
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=False),
                clock=Clock("2026-07-10T10:05:00+08:00"),
            )

            with store:
                report = service.poll_once()

                self.assertEqual(1, report.delivered)
                self.assertEqual("y00000001", sender.calls[0][0])
                self.assertEqual("sent", store.list_outbox()[0].status)

    def test_strict_recipient_mode_reports_missing_w3_during_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock("2026-07-10T10:05:00+08:00"),
            )
            service.recipients = RecipientDirectory(direct=False)
            with store:
                report = service.poll_once()

            self.assertTrue(any("no W3 recipient" in error for error in report.errors))
            self.assertEqual([], sender.calls)

    def test_explicit_mapping_overrides_gitea_derived_w3(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = pathlib.Path(temp_dir) / "recipients.json"
            mapping.write_text('{"w00123": "z00000003"}', encoding="utf-8")
            directory = RecipientDirectory(path=mapping, direct=False)
            directory.refresh()

            self.assertEqual("z00000003", directory.resolve("w00123", "e00000001"))

    def test_self_fallback_cannot_equal_sender_account(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            RecipientDirectory(
                sender_account="y00000001",
                self_fallback_receiver="Y00000001",
            )

    def test_self_fallback_and_sender_must_be_configured_together(self):
        for values in (
            {"sender_account": "y00000001"},
            {"self_fallback_receiver": "y00000002"},
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "configured together"):
                    RecipientDirectory(**values)

    def test_failed_delivery_retries_from_durable_outbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender(["failure", "success"])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:11:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "new failure",
                        "updated_at": "2026-07-10T10:06:00+08:00",
                    }
                ]

                failed = service.poll_once()
                retried = service.poll_once()

                self.assertEqual(1, failed.delivery_failures)
                self.assertEqual(1, retried.delivered)
                self.assertEqual(2, len(sender.calls))
                self.assertEqual("sent", store.list_outbox()[0].status)

    def test_timeout_is_uncertain_and_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender(["timeout", "success"])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:11:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "new failure",
                        "updated_at": "2026-07-10T10:06:00+08:00",
                    }
                ]

                timed_out = service.poll_once()
                service.poll_once()

                self.assertEqual(1, timed_out.delivery_uncertain)
                self.assertEqual(1, len(sender.calls))
                self.assertEqual("uncertain", store.list_outbox()[0].status)

    def test_invalid_mapping_fails_closed_without_sending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = pathlib.Path(temp_dir) / "recipients.json"
            mapping.write_text("not-json", encoding="utf-8")
            client = FakeGiteaClient()
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(path=mapping, direct=True),
                clock=Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "new failure",
                        "updated_at": "2026-07-10T10:06:00+08:00",
                    }
                ]

                report = service.poll_once()

                self.assertTrue(report.errors)
                self.assertEqual([], sender.calls)
                self.assertEqual("pending", store.list_outbox()[0].status)


if __name__ == "__main__":
    unittest.main()
