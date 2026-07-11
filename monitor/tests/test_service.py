import pathlib
import tempfile
import unittest

from monitor.core import GateFailure, PrSnapshot, TrackerState
from monitor.service import (
    MergeMetrics,
    MonitorService,
    PollReport,
    RecipientDirectory,
    _merge_metrics_from_pull,
    format_message,
)
from monitor.state import MonitorStore, OutboxEvent, OutboxRecord
from monitor.welink import DeliveryResult


class FakeGiteaClient:
    def __init__(self):
        self.comment_attempts = []
        self.comment_error = None
        self.comment_responses = []
        self.pull_detail_attempts = []
        self.pull_detail_error = None
        self.pull_detail = {
            "number": 7,
            "created_at": "2026-07-10T10:00:00+08:00",
            "additions": 100,
            "deletions": 20,
            "changed_files": 2,
        }
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

    def get_pull(self, owner, repo, number):
        self.pull_detail_attempts.append(number)
        if self.pull_detail_error is not None:
            raise self.pull_detail_error
        return dict(self.pull_detail)

    def get_issue_comments(self, owner, repo, number, max_pages):
        if self.comment_responses:
            return list(self.comment_responses.pop(0))
        return list(self.comments)

    def create_issue_comment(self, owner, repo, number, body):
        self.comment_attempts.append((owner, repo, number, body))
        if self.comment_error is not None:
            raise self.comment_error
        return {"id": len(self.comment_attempts), "body": body}


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

    def test_multiple_failures_are_combined_in_the_confirmed_single_line_format(self):
        snapshot = PrSnapshot(
            number=1111,
            title="ignored title",
            author="w00123",
            head_sha="abcdef123456",
            url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1111",
            latest_ci_command="/ci build",
            latest_ci_command_at="2026-07-10T10:00:00+08:00",
            latest_ci_command_key="build-1",
            scanned_at="2026-07-10T10:05:00+08:00",
            failures=(),
        )
        failures = (
            GateFailure("protected-file-approval", "", "approval missing"),
            GateFailure("taichu/pr-build", "", "compile error in module foo"),
        )

        message = format_message(snapshot, failures)

        self.assertEqual(
            "[TaiChu PR 1111] 发现问题："
            "protected-file-approval：approval missing；"
            "taichu/pr-build：compile error in module foo "
            "【Taichu PRbot 自动发送，回复TD退订】 "
            "查看 https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1111",
            message,
        )
        self.assertNotIn("\n", message)

    def test_merge_success_copy_covers_all_duration_and_line_buckets(self):
        snapshot = PrSnapshot(
            number=1111,
            title="ignored title",
            author="w00123",
            head_sha="abcdef123456",
            url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1111",
            latest_ci_command="/ci merge",
            latest_ci_command_at="2026-07-10T10:00:00+08:00",
            latest_ci_command_key="merge-1",
            scanned_at="2026-07-10T10:05:00+08:00",
            failures=(),
        )
        line_samples = (499, 500, 1500, 2501)
        expected = (
            (
                ("Merge Successful 🔪", "老医生的刀法"),
                ("PR Merged 🚀", "机器跑得都没你脑子转得快"),
                ("Merged ⚡", "Review 居然挑不出什么毛病"),
                ("Merge Complete 🤯", "单兵突击能力太硬核"),
            ),
            (
                ("Code Integrated 💎", "并发和边界"),
                ("Merge Successful 🛠️", "团队很安心"),
                ("PR Merged 🚢", "给后续省了不少事"),
                ("Merged 🚀", "提振士气"),
            ),
            (
                ("Finally Merged 💣", "深水雷"),
                ("Merge Successful 🛡️", "最终方案非常优雅"),
                ("PR Merged 🛠️", "心肺复苏"),
                ("Merge Complete 🎉", "硬仗打赢了"),
            ),
            (
                ("Finally Merged 🧗", "四两拨千斤"),
                ("Merge Successful 🏆", "长线抗压"),
                ("Approved & Merged 🚢", "大山搬平了"),
                ("PR MERGED 👑", "真正的核心战力"),
            ),
        )

        for duration_days, duration_cases in enumerate(expected, start=1):
            for changed_lines, (title, anchor) in zip(line_samples, duration_cases):
                with self.subTest(days=duration_days, lines=changed_lines):
                    message = format_message(
                        snapshot,
                        (),
                        merge_success=True,
                        merge_metrics=MergeMetrics(changed_lines, duration_days),
                    )

                    self.assertTrue(
                        message.startswith(f"[TaiChu PR {snapshot.number}] {title} ")
                    )
                    self.assertIn(anchor, message)
                    self.assertNotIn("（变更 ", message)
                    self.assertNotIn(" 行 · 历时 ", message)
                    self.assertIn("【Taichu PRbot 自动发送，回复TD退订】", message)
                    self.assertEqual(1, message.count("https://"))
                    self.assertTrue(message.endswith(snapshot.url))
                    self.assertFalse(
                        any(
                            mark in message
                            for mark in ("\r", "\n", "\u2028", "\u2029")
                        )
                    )
                    self.assertNotIn("**", message)

    def test_merge_success_line_bucket_boundaries(self):
        snapshot = PrSnapshot(
            7,
            "title",
            "w00123",
            "abcdef123456",
            "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
            "/ci merge",
            "2026-07-10T10:00:00+08:00",
            "merge-1",
            "2026-07-10T10:05:00+08:00",
            (),
        )
        cases = (
            (0, "Merge Successful 🔪"),
            (499, "Merge Successful 🔪"),
            (500, "PR Merged 🚀"),
            (1499, "PR Merged 🚀"),
            (1500, "Merged ⚡"),
            (2500, "Merged ⚡"),
            (2501, "Merge Complete 🤯"),
        )

        for changed_lines, expected_title in cases:
            with self.subTest(lines=changed_lines):
                message = format_message(
                    snapshot,
                    (),
                    merge_success=True,
                    merge_metrics=MergeMetrics(changed_lines, 1),
                )
                self.assertIn(expected_title, message)

    def test_merge_metrics_use_diff_lines_and_notification_time_boundaries(self):
        pull = {
            "created_at": "2026-07-10T00:00:00Z",
            "additions": 7,
            "deletions": 3,
        }
        cases = (
            ("2026-07-10T00:00:00Z", 1),
            ("2026-07-11T00:00:00Z", 1),
            ("2026-07-11T00:00:01Z", 2),
            ("2026-07-12T00:00:00Z", 2),
            ("2026-07-12T00:00:01Z", 3),
            ("2026-07-13T00:00:00Z", 3),
            ("2026-07-13T00:00:01Z", 4),
        )

        for completed_at, expected_days in cases:
            with self.subTest(completed_at=completed_at):
                self.assertEqual(
                    MergeMetrics(10, expected_days),
                    _merge_metrics_from_pull(pull, completed_at),
                )

        offset_pull = dict(pull, created_at="2026-07-10T08:00:00+08:00")
        self.assertEqual(
            MergeMetrics(10, 1),
            _merge_metrics_from_pull(offset_pull, "2026-07-11T00:00:00Z"),
        )

    def test_invalid_merge_metrics_fall_back_to_generic_success_copy(self):
        snapshot = PrSnapshot(
            7,
            "title",
            "w00123",
            "abcdef123456",
            "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
            "/ci merge",
            "2026-07-10T10:00:00+08:00",
            "merge-1",
            "2026-07-10T10:05:00+08:00",
            (),
        )
        invalid_pulls = (
            {"created_at": "2026-07-10T00:00:00Z", "deletions": 1},
            {"created_at": "2026-07-10T00:00:00Z", "additions": -1, "deletions": 1},
            {
                "created_at": "2026-07-10T00:00:00Z",
                "additions": float("inf"),
                "deletions": 1,
            },
            {
                "created_at": "2026-07-10T00:00:00Z",
                "additions": 1.5,
                "deletions": 1,
            },
            {"created_at": "invalid", "additions": 1, "deletions": 1},
        )

        for pull in invalid_pulls:
            with self.subTest(pull=pull):
                self.assertIsNone(
                    _merge_metrics_from_pull(pull, "2026-07-11T00:00:00Z")
                )

        self.assertIsNone(
            _merge_metrics_from_pull(
                {
                    "created_at": "2026-07-12T00:00:00Z",
                    "additions": 1,
                    "deletions": 1,
                },
                "2026-07-11T00:00:00Z",
            )
        )
        fallback = format_message(snapshot, (), merge_success=True)
        self.assertIn("Merge 成功啦", fallback)
        self.assertTrue(fallback.endswith(snapshot.url))

    def test_gate_templates_are_reduced_to_actionable_failure_summaries(self):
        snapshot = PrSnapshot(
            number=1329,
            title="ignored title",
            author="w00123",
            head_sha="abcdef123456",
            url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1329",
            latest_ci_command="/ci build",
            latest_ci_command_at="2026-07-11T11:19:00+08:00",
            latest_ci_command_key="build-1329",
            scanned_at="2026-07-11T11:24:00+08:00",
            failures=(),
        )
        failures = (
            GateFailure(
                "taichu/codex-pr-review",
                "",
                "2026-07-11 11:20:46 | Codex found 2 P0/P1 principle issue(s)",
            ),
            GateFailure(
                "taichu/pr-build",
                "",
                "TaiChu PR build：执行结果：失败\n"
                "说明：本次测的是 PR 合进目标分支后的结果。\n"
                "失败摘要：测试未通过，请查看 Jenkins 日志与测试报告\n"
                "构建产物（若有）：https://example.invalid/artifact\n"
                "failed_task_count=1\n"
                "failed_task_1.task_label=Node B\n"
                "failed_task_1.stage=non_device\n"
                "failed_task_1.reason_type=compile_error\n"
                "failed_task_1.suite=rust-workspace\n"
                "failed_task_1.exit_status=101",
            ),
        )

        message = format_message(snapshot, failures)

        self.assertEqual(
            "[TaiChu PR 1329] 发现问题："
            "taichu/codex-pr-review：发现 2 个 P0/P1 原则问题；"
            "taichu/pr-build：Node B/non_device/rust-workspace 编译失败（exit 101） "
            "【Taichu PRbot 自动发送，回复TD退订】 "
            "查看 https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1329",
            message,
        )
        self.assertEqual(1, message.count("https://"))
        self.assertTrue(message.endswith(snapshot.url))

    def test_dispatch_rechecks_original_and_final_recipient_opt_outs(self):
        class DispatchStore:
            def __init__(self, record, opted_out):
                self.record = record
                self.opted_out = opted_out
                self.checked = []
                self.updated = []

            def list_dispatchable(self, max_attempts):
                return [self.record]

            def is_recipient_opted_out(self, receiver):
                self.checked.append(receiver)
                return receiver == self.opted_out

            def update_delivery(self, *args, **kwargs):
                self.updated.append((args, kwargs))

        record = OutboxRecord(
            id=1,
            event_key="event-1",
            pr_number=7,
            author="w00123",
            receiver="y00000001",
            recipient_employee_number="00000001",
            message="message",
            status="pending",
            attempts=0,
            last_error="",
            created_at="",
            updated_at="",
        )
        cases = (
            (
                "00000001",
                RecipientDirectory(
                    direct=False,
                    sender_account="y00000001",
                    self_fallback_receiver="y00000002",
                ),
                ["00000001"],
                "y00000002",
            ),
            (
                "y00000001",
                RecipientDirectory(direct=False),
                ["00000001", "y00000001"],
                "y00000001",
            ),
        )
        for opted_out, recipients, expected_checks, expected_receiver in cases:
            with self.subTest(opted_out=opted_out):
                store = DispatchStore(record, opted_out)
                sender = SequenceSender(["success"])
                service = MonitorService(
                    client=None,
                    store=store,
                    sender=sender,
                    recipients=recipients,
                )
                report = PollReport(scanned_at="2026-07-10T10:00:00+08:00")

                service._dispatch_outbox(report)

                self.assertEqual(expected_checks, store.checked)
                self.assertEqual([], sender.calls)
                args, kwargs = store.updated[0]
                self.assertEqual(
                    (
                        1,
                        "suppressed",
                        expected_receiver,
                        "notification suppressed by recipient preference",
                    ),
                    args,
                )
                self.assertEqual({"increment_attempt": False}, kwargs)

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

                self.assertEqual(0, first.new_notifications)
                self.assertEqual(1, second.new_notifications)
                self.assertEqual(1, len(sender.calls))
                self.assertEqual("w00123", sender.calls[0][0])
                self.assertEqual(
                    "[TaiChu PR 7] 发现问题：taichu/pr-build："
                    "compile error in module foo "
                    "【Taichu PRbot 自动发送，回复TD退订】 "
                    "查看 https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                    sender.calls[0][1],
                )
                self.assertNotIn("\n", sender.calls[0][1])
                snapshots = store.list_snapshots()
                self.assertEqual(1, len(snapshots))
                self.assertEqual("taichu/pr-build", snapshots[0].failures[0].context)

            restarted, reopened = self.make_service(
                temp_dir,
                client,
                sender,
                Clock("2026-07-10T10:11:00+08:00"),
            )
            with reopened:
                repeated = restarted.poll_once()

            self.assertEqual(0, repeated.new_notifications)
            self.assertEqual(1, len(sender.calls))

    def test_new_build_success_comments_merge_once_without_welink_or_restart_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                baseline = service.poll_once()
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci build",
                        "created_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 2,
                        "context": "protected-file-approval",
                        "state": "success",
                        "description": "approval success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                    {
                        "id": 3,
                        "context": "taichu/codex-pr-review",
                        "state": "success",
                        "description": "review success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                    {
                        "id": 4,
                        "context": "taichu/pr-build",
                        "state": "success",
                        "description": "build success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                ]

                succeeded = service.poll_once()

                self.assertEqual(0, baseline.new_notifications)
                self.assertEqual(0, succeeded.new_notifications)
                self.assertEqual([], sender.calls)
                self.assertEqual([], store.list_outbox())
                self.assertEqual(
                    [("SystemAgentDev", "TaiChu", 7, "/ci merge")],
                    client.comment_attempts,
                )

            restarted, reopened = self.make_service(
                temp_dir,
                client,
                sender,
                Clock("2026-07-10T10:11:00+08:00"),
            )
            with reopened:
                repeated = restarted.poll_once()

            self.assertEqual(0, repeated.new_notifications)
            self.assertEqual(1, len(client.comment_attempts))

    def test_build_success_does_not_duplicate_a_human_merge_comment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                build_comment = {
                    "id": 2,
                    "body": "/ci build",
                    "created_at": "2026-07-10T10:06:00+08:00",
                }
                human_merge_comment = {
                    "id": 3,
                    "body": "  /CI MERGE  ",
                    "created_at": "2026-07-10T10:07:30+08:00",
                }
                bot_reply = {
                    "id": 4,
                    "body": "Merge command accepted and waiting in queue",
                    "created_at": "2026-07-10T10:07:45+08:00",
                }
                client.comment_responses = [
                    [build_comment],
                    [build_comment, human_merge_comment, bot_reply],
                ]
                client.statuses = [
                    {
                        "id": index,
                        "context": context,
                        "state": "success",
                        "description": "success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    }
                    for index, context in enumerate(
                        (
                            "protected-file-approval",
                            "taichu/codex-pr-review",
                            "taichu/pr-build",
                        ),
                        start=2,
                    )
                ]

                service.poll_once()

            self.assertEqual([], client.comment_attempts)
            self.assertEqual([], sender.calls)

    def test_disabled_outbound_comments_do_not_post_ci_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock("2026-07-10T10:05:00+08:00"),
            )
            service.allow_merge_comments = False
            snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T10:00:00+08:00",
                latest_ci_command_key="build-1",
                scanned_at="2026-07-10T10:05:00+08:00",
                failures=(),
            )
            with store:
                service._try_comment_merge(snapshot)

            self.assertEqual([], client.comment_attempts)

    def test_build_success_ignores_old_merge_and_explanatory_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                old_merge_comment = {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:05:30+08:00",
                }
                build_comment = {
                    "id": 3,
                    "body": "/ci build",
                    "created_at": "2026-07-10T10:06:00+08:00",
                }
                explanatory_comment = {
                    "id": 4,
                    "body": "请在确认后评论 /ci merge",
                    "created_at": "2026-07-10T10:07:30+08:00",
                }
                client.comment_responses = [
                    [build_comment],
                    [old_merge_comment, build_comment, explanatory_comment],
                ]
                client.statuses = [
                    {
                        "id": index,
                        "context": context,
                        "state": "success",
                        "description": "success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    }
                    for index, context in enumerate(
                        (
                            "protected-file-approval",
                            "taichu/codex-pr-review",
                            "taichu/pr-build",
                        ),
                        start=2,
                    )
                ]

                service.poll_once()

            self.assertEqual(
                [("SystemAgentDev", "TaiChu", 7, "/ci merge")],
                client.comment_attempts,
            )

    def test_build_success_skips_comment_when_latest_comment_check_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci build",
                        "created_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": index,
                        "context": context,
                        "state": "success",
                        "description": "success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    }
                    for index, context in enumerate(
                        (
                            "protected-file-approval",
                            "taichu/codex-pr-review",
                            "taichu/pr-build",
                        ),
                        start=2,
                    )
                ]
                original_get_comments = client.get_issue_comments
                reads = 0

                def fail_second_read(owner, repo, number, max_pages):
                    nonlocal reads
                    reads += 1
                    if reads == 2:
                        raise RuntimeError("comments unavailable")
                    return original_get_comments(owner, repo, number, max_pages)

                client.get_issue_comments = fail_second_read
                service.poll_once()

            self.assertEqual([], client.comment_attempts)
            self.assertEqual([], sender.calls)

    def test_failed_merge_comment_is_only_warned_and_never_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comment_error = RuntimeError("comments forbidden")
            sender = SequenceSender([])
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
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci build",
                        "created_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": index,
                        "context": context,
                        "state": "success",
                        "description": "success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    }
                    for index, context in enumerate(
                        (
                            "protected-file-approval",
                            "taichu/codex-pr-review",
                            "taichu/pr-build",
                        ),
                        start=2,
                    )
                ]

                first = service.poll_once()
                repeated = service.poll_once()

            self.assertEqual([], first.errors)
            self.assertEqual([], repeated.errors)
            self.assertEqual(1, len(client.comment_attempts))
            self.assertEqual([], sender.calls)

    def test_merge_failure_then_success_sends_each_single_line_message_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            sender = SequenceSender(["success", "success"])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:11:00+08:00",
                    "2026-07-10T10:14:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci merge",
                        "created_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/dev-cloud-preflight",
                        "state": "failure",
                        "description": "missing cloud artifact",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    }
                ]
                failed = service.poll_once()
                client.statuses = [
                    {
                        "id": 3,
                        "context": "taichu/dev-cloud-preflight",
                        "state": "success",
                        "description": "preflight success",
                        "updated_at": "2026-07-10T10:09:00+08:00",
                    },
                    {
                        "id": 4,
                        "context": "ci/merge-gate",
                        "state": "success",
                        "description": "merge gate success",
                        "updated_at": "2026-07-10T10:09:00+08:00",
                    },
                ]
                succeeded = service.poll_once()
                repeated = service.poll_once()

                self.assertEqual(1, failed.new_notifications)
                self.assertEqual(1, succeeded.new_notifications)
                self.assertEqual(0, repeated.new_notifications)
                self.assertEqual(2, len(store.list_outbox()))

            self.assertEqual(2, len(sender.calls))
            self.assertEqual(
                "[TaiChu PR 7] 发现问题：taichu/dev-cloud-preflight："
                "missing cloud artifact "
                "【Taichu PRbot 自动发送，回复TD退订】 "
                "查看 https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                sender.calls[0][1],
            )
            self.assertEqual(
                "[TaiChu PR 7] Merge Successful 🔪 "
                "小几百行代码一天搞定，改得非常准。不需要冗长废话就能把痛点切掉，"
                "老医生的刀法。代码已上膛，干得漂亮！🍻 "
                "【Taichu PRbot 自动发送，回复TD退订】 "
                "查看 https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                sender.calls[1][1],
            )
            self.assertEqual([7], client.pull_detail_attempts)
            self.assertTrue(all("\n" not in message for _, message in sender.calls))

    def test_merge_metric_lookup_failure_still_sends_generic_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.pull_detail_error = RuntimeError("detail unavailable")
            sender = SequenceSender(["success"])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )
            with store:
                baseline = service.poll_once()
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci merge",
                        "created_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/dev-cloud-preflight",
                        "state": "success",
                        "description": "preflight success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                    {
                        "id": 3,
                        "context": "ci/merge-gate",
                        "state": "success",
                        "description": "merge gate success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                ]
                succeeded = service.poll_once()

            self.assertEqual([], baseline.errors)
            self.assertEqual([], succeeded.errors)
            self.assertEqual([7], client.pull_detail_attempts)
            self.assertEqual(1, len(sender.calls))
            self.assertIn("Merge 成功啦", sender.calls[0][1])
            self.assertNotIn("变更 ", sender.calls[0][1])

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

    def test_merge_success_retry_reuses_the_persisted_message_and_metrics(self):
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
                    "2026-07-14T10:11:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci merge",
                        "created_at": "2026-07-10T10:06:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 2,
                        "context": "taichu/dev-cloud-preflight",
                        "state": "success",
                        "description": "preflight success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                    {
                        "id": 3,
                        "context": "ci/merge-gate",
                        "state": "success",
                        "description": "merge gate success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                ]

                failed = service.poll_once()
                client.pull_detail = {
                    "number": 7,
                    "created_at": "2026-07-01T10:00:00+08:00",
                    "additions": 3000,
                    "deletions": 100,
                }
                retried = service.poll_once()

                self.assertEqual(1, failed.delivery_failures)
                self.assertEqual(1, retried.delivered)
                self.assertEqual([7], client.pull_detail_attempts)
                self.assertEqual(2, len(sender.calls))
                self.assertEqual(sender.calls[0][1], sender.calls[1][1])
                self.assertIn("Merge Successful 🔪", sender.calls[1][1])
                self.assertIn("老医生的刀法", sender.calls[1][1])
                self.assertNotIn("（变更 ", sender.calls[1][1])
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
