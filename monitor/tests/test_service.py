import pathlib
import tempfile
import unittest

from monitor.core import (
    APPROVAL_PROBLEM_KEY,
    PROBLEM_FINGERPRINT_PREFIX,
    PROBLEM_HISTORY_MARKER,
    GateFailure,
    PrSnapshot,
    TrackerState,
)
from monitor.service import (
    MergeMetrics,
    MonitorService,
    PollReport,
    RecipientDirectory,
    _merge_metrics_from_pull,
    _messaged_failures,
    _notification_event_key,
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
            "state": "open",
            "merged": False,
            "merged_at": None,
            "created_at": "2026-07-10T10:00:00+08:00",
            "additions": 100,
            "deletions": 20,
            "changed_files": 2,
        }
        self.open_pulls = True
        self.list_error = None
        self.user = {"login": "w00123"}
        self.base_ref = "main"
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
        if self.list_error is not None:
            raise self.list_error
        return [self._pull_payload()] if self.open_pulls else []

    def get_statuses(self, owner, repo, sha):
        return list(self.statuses)

    def get_pull(self, owner, repo, number):
        self.pull_detail_attempts.append(number)
        if self.pull_detail_error is not None:
            raise self.pull_detail_error
        return self._pull_payload(include_detail=True)

    def _pull_payload(self, include_detail=False):
        payload = {
            "number": 7,
            "title": "Repair build",
            "state": "open",
            "merged": False,
            "merged_at": None,
            "html_url": "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
            "user": dict(self.user),
            "head": {"sha": "abcdef123456"},
            "base": {"ref": self.base_ref},
        }
        if include_detail:
            payload.update(self.pull_detail)
        return payload

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

    def test_terminal_pending_state_survives_restart_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "state.sqlite3"
            with MonitorStore(path) as store:
                store.mark_terminal_pending([9, 7, 9])

            with MonitorStore(path) as reopened:
                self.assertEqual([7, 9], reopened.list_terminal_pending())
                reopened.clear_terminal_pending(7)
                self.assertEqual([9], reopened.list_terminal_pending())


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

    def test_legacy_build_success_footer_is_not_part_of_failure_summary(self):
        failures = tuple(
            _messaged_failures(
                [
                    "[TaiChu PR #7] CI Build 已通过\n"
                    "标题：Repair build\n"
                    "同时发现 1 个新问题：\n"
                    "- taichu/codex-pr-review："
                    "Codex found 1 P0/P1 principle issue(s)\n"
                    "下一步：打开 PR，确认后评论 /ci merge\n"
                    "查看：https://example.test/7"
                ]
            )
        )

        self.assertEqual(
            (
                GateFailure(
                    "taichu/codex-pr-review",
                    "",
                    "Codex found 1 P0/P1 principle issue(s)",
                ),
            ),
            failures,
        )

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
                ("Merge Successful 🔪", "一天搞定 {row_count} 行代码"),
                ("PR Merged 🚀", "一天内输出 {row_count} 行代码"),
                ("Merged ⚡", "一天爆肝完成 {row_count} 行高质量代码"),
                ("Merge Complete 🤯", "24 小时撸出 {row_count} 行代码"),
            ),
            (
                ("Code Integrated 💎", "两天时间打磨 {row_count} 行核心逻辑"),
                (
                    "Merge Successful 🛠️",
                    "两天战术攻坚，{row_count} 行代码顺利合入",
                ),
                ("PR Merged 🚢", "两天落地 {row_count} 行变更"),
                ("Merged 🚀", "短短两天顶住压力扛下 {row_count} 行变更"),
            ),
            (
                ("Finally Merged 💣", "最后把解法收进 {row_count} 行代码"),
                (
                    "Merge Successful 🛡️",
                    "核心链路累计 {row_count} 行变更顺利合入",
                ),
                (
                    "PR Merged 🛠️",
                    "历时三天的拉锯，{row_count} 行变更终于落地",
                ),
                ("Merge Complete 🎉", "三天高强度作战，扛下 {row_count} 行变更"),
            ),
            (
                ("Finally Merged 🧗", "最后浓缩成 {row_count} 行精妙的解法"),
                ("Merge Successful 🏆", "才换来 {row_count} 行代码平稳落地"),
                (
                    "Approved & Merged 🚢",
                    "跨越数天的硬仗！{row_count} 行核心重构终于合入",
                ),
                (
                    "PR MERGED 👑",
                    "跨越数天的硬仗！{row_count} 行变更终于顺利合入",
                ),
            ),
        )

        for duration_days, duration_cases in enumerate(expected, start=1):
            for changed_lines, (title, anchor_template) in zip(
                line_samples, duration_cases
            ):
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
                    row_count = str(changed_lines)
                    self.assertIn(
                        anchor_template.format(row_count=row_count),
                        message,
                    )
                    self.assertEqual(1, message.count(f"{row_count} 行"))
                    if changed_lines >= 1000:
                        self.assertNotIn(f"{changed_lines:,} 行", message)
                    self.assertNotIn("{row_count}", message)
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
                self.assertIn(f"{changed_lines} 行", message)

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

            client.comments = [
                {
                    "id": 2,
                    "body": "/ci build",
                    "created_at": "2026-07-10T10:09:00+08:00",
                }
            ]
            client.statuses = [
                {
                    "id": 3,
                    "context": "taichu/pr-build",
                    "state": "failure",
                    "description": "compile error in module foo",
                    "updated_at": "2026-07-10T10:10:00+08:00",
                }
            ]
            restarted, reopened = self.make_service(
                temp_dir,
                client,
                sender,
                Clock(
                    "2026-07-10T10:11:00+08:00",
                    "2026-07-10T10:14:00+08:00",
                ),
            )
            with reopened:
                repeated = restarted.poll_once()
                client.statuses = [
                    {
                        "id": 4,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "unit test failed in module bar",
                        "updated_at": "2026-07-10T10:13:00+08:00",
                    }
                ]
                changed = restarted.poll_once()

            self.assertEqual(0, repeated.new_notifications)
            self.assertEqual(1, changed.new_notifications)
            self.assertEqual(2, len(sender.calls))
            self.assertIn("unit test failed in module bar", sender.calls[1][1])

    def test_repeated_approval_is_removed_but_dashboard_keeps_all_failures(self):
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
                    "2026-07-10T10:13:00+08:00",
                ),
            )
            with store:
                service.poll_once()
                client.statuses = [
                    {
                        "id": 2,
                        "context": "protected-file-approval",
                        "state": "failure",
                        "description": "approval missing",
                        "updated_at": "2026-07-10T10:06:00+08:00",
                    },
                    {
                        "id": 3,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "compile error in module foo",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                ]
                first = service.poll_once()
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci build",
                        "created_at": "2026-07-10T10:10:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 4,
                        "context": "protected-file-approval",
                        "state": "failure",
                        "description": "approval missing for another file",
                        "updated_at": "2026-07-10T10:11:00+08:00",
                    },
                    {
                        "id": 5,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "unit test failed in module bar",
                        "updated_at": "2026-07-10T10:12:00+08:00",
                    },
                ]
                second = service.poll_once()
                snapshot = store.list_snapshots()[0]

            self.assertEqual(1, first.new_notifications)
            self.assertEqual(1, second.new_notifications)
            self.assertIn("protected-file-approval", sender.calls[0][1])
            self.assertNotIn("protected-file-approval", sender.calls[1][1])
            self.assertIn("unit test failed in module bar", sender.calls[1][1])
            self.assertEqual(
                {"protected-file-approval", "taichu/pr-build"},
                {failure.context for failure in snapshot.failures},
            )

    def test_legacy_approval_outbox_seeds_the_pr_lifetime_dedupe_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
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
                    "state": "failure",
                    "description": "approval missing again",
                    "updated_at": "2026-07-10T10:07:00+08:00",
                },
                {
                    "id": 3,
                    "context": "taichu/pr-build",
                    "state": "failure",
                    "description": "compile error in module foo",
                    "updated_at": "2026-07-10T10:07:30+08:00",
                },
            ]
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            old_snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-build",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "legacy-build",
                    frozenset({"legacy-build:build:failure-notified"}),
                    True,
                    old_snapshot.scanned_at,
                ),
                OutboxEvent(
                    "legacy-approval",
                    7,
                    "w00123",
                    "[TaiChu PR 7] 发现问题：protected-file-approval：approval missing；"
                    "taichu/pr-build：compile error in module foo "
                    "【Taichu PRbot 自动发送，回复TD退订】 查看 https://example.test/7",
                ),
                snapshot=old_snapshot,
            )
            legacy_record = store.list_outbox()[0]
            store.update_delivery(
                legacy_record.id,
                "sent",
                "w00123",
                "",
                increment_attempt=True,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock("2026-07-10T10:08:00+08:00"),
            )

            with store:
                report = service.poll_once()
                tracker = store.get_tracker(7)
                records = store.list_outbox()

            self.assertEqual(0, report.new_notifications)
            self.assertEqual([], sender.calls)
            self.assertEqual(1, len(records))
            self.assertEqual("sent", records[0].status)
            self.assertIn(PROBLEM_HISTORY_MARKER, tracker.notified_failure_keys)
            self.assertIn(APPROVAL_PROBLEM_KEY, tracker.notified_failure_keys)

    def test_legacy_baselined_approval_is_not_announced_after_upgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
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
                    "state": "failure",
                    "description": "approval still missing",
                    "updated_at": "2026-07-10T10:07:00+08:00",
                }
            ]
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            old_snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-build",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T09:56:00+08:00",
                        "approval missing",
                    ),
                ),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "legacy-build",
                    frozenset({"legacy-build:build:failure-notified"}),
                    True,
                    old_snapshot.scanned_at,
                ),
                None,
                snapshot=old_snapshot,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock("2026-07-10T10:08:00+08:00"),
            )

            with store:
                report = service.poll_once()
                tracker = store.get_tracker(7)
                records = store.list_outbox()

            self.assertEqual(0, report.new_notifications)
            self.assertEqual([], sender.calls)
            self.assertEqual([], records)
            self.assertIn(PROBLEM_HISTORY_MARKER, tracker.notified_failure_keys)
            self.assertIn(APPROVAL_PROBLEM_KEY, tracker.notified_failure_keys)

    def test_legacy_baselined_build_failure_is_not_announced_after_upgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
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
                    "context": "taichu/pr-build",
                    "state": "failure",
                    "description": "compile error in module foo",
                    "updated_at": "2026-07-10T10:07:00+08:00",
                }
            ]
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            old_snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-build",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T09:56:00+08:00",
                        "compile error in module foo",
                    ),
                ),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "legacy-build",
                    frozenset({"legacy-build:build:failure-notified"}),
                    True,
                    old_snapshot.scanned_at,
                ),
                None,
                snapshot=old_snapshot,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock("2026-07-10T10:08:00+08:00"),
            )

            with store:
                report = service.poll_once()
                tracker = store.get_tracker(7)

            self.assertEqual(0, report.new_notifications)
            self.assertEqual([], sender.calls)
            self.assertTrue(
                any(
                    key.startswith(
                        f"{PROBLEM_FINGERPRINT_PREFIX}taichu/pr-build:"
                    )
                    for key in tracker.notified_failure_keys
                )
            )

    def test_legacy_late_unmessaged_failure_remains_eligible_next_round(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci build",
                    "created_at": "2026-07-10T10:06:00+08:00",
                }
            ]
            client.statuses = [
                {
                    "id": 3,
                    "context": "taichu/pr-build",
                    "state": "failure",
                    "description": "unit test failed in module bar",
                    "updated_at": "2026-07-10T10:07:00+08:00",
                }
            ]
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            old_snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-build",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/codex-pr-review",
                        "2026-07-10T09:56:00+08:00",
                        "Codex found 1 P0/P1 principle issue(s)",
                    ),
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T09:59:00+08:00",
                        "unit test failed in module bar",
                    ),
                ),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "legacy-build",
                    frozenset({"legacy-build:build:failure-notified"}),
                    True,
                    old_snapshot.scanned_at,
                ),
                OutboxEvent(
                    _notification_event_key(
                        7,
                        "legacy-build",
                        "/ci build:failure",
                    ),
                    7,
                    "w00123",
                    "[TaiChu PR 7] 发现问题：taichu/codex-pr-review："
                    "Codex found 1 P0/P1 principle issue(s) "
                    "【Taichu PRbot 自动发送，回复TD退订】 查看 https://example.test/7",
                ),
                snapshot=old_snapshot,
            )
            old_record = store.list_outbox()[0]
            store.update_delivery(
                old_record.id,
                "sent",
                "w00123",
                "",
                increment_attempt=True,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock("2026-07-10T10:08:00+08:00"),
            )

            with store:
                report = service.poll_once()

            self.assertEqual(1, report.new_notifications)
            self.assertEqual(1, len(sender.calls))
            self.assertIn("unit test failed in module bar", sender.calls[0][1])

    def test_resolved_legacy_message_is_deduped_if_it_reappears(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci build",
                    "created_at": "2026-07-10T10:06:00+08:00",
                }
            ]
            client.statuses = []
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            old_snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-build",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "legacy-build",
                    frozenset({"legacy-build:build:failure-notified"}),
                    True,
                    old_snapshot.scanned_at,
                ),
                OutboxEvent(
                    _notification_event_key(
                        7,
                        "legacy-build",
                        "/ci build:failure",
                    ),
                    7,
                    "w00123",
                    "[TaiChu PR #7] 发现 1 个新问题\n"
                    "标题：Repair build\n"
                    "- taichu/pr-build：compile error in module foo\n"
                    "查看：https://example.test/7",
                ),
                snapshot=old_snapshot,
            )
            old_record = store.list_outbox()[0]
            store.update_delivery(
                old_record.id,
                "sent",
                "w00123",
                "",
                increment_attempt=True,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock(
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:12:00+08:00",
                ),
            )

            with store:
                upgraded = service.poll_once()
                client.comments = [
                    {
                        "id": 3,
                        "body": "/ci build",
                        "created_at": "2026-07-10T10:09:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 3,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "compile error in module foo",
                        "updated_at": "2026-07-10T10:10:00+08:00",
                    }
                ]
                repeated = service.poll_once()

            self.assertEqual(0, upgraded.new_notifications)
            self.assertEqual(0, repeated.new_notifications)
            self.assertEqual([], sender.calls)

    def test_gate_summary_mention_does_not_count_as_legacy_approval_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
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
                    "state": "failure",
                    "description": "approval missing",
                    "updated_at": "2026-07-10T10:07:00+08:00",
                }
            ]
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            old_snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci build",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-build",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T09:56:00+08:00",
                        "protected-file-approval: diagnostic only",
                    ),
                ),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "legacy-build",
                    frozenset({"legacy-build:build:failure-notified"}),
                    True,
                    old_snapshot.scanned_at,
                ),
                OutboxEvent(
                    _notification_event_key(
                        7,
                        "legacy-build",
                        "/ci build:failure",
                    ),
                    7,
                    "w00123",
                    "[TaiChu PR 7] 发现问题：taichu/pr-build："
                    "protected-file-approval： diagnostic only "
                    "【Taichu PRbot 自动发送，回复TD退订】 查看 https://example.test/7",
                ),
                snapshot=old_snapshot,
            )
            old_record = store.list_outbox()[0]
            store.update_delivery(
                old_record.id,
                "sent",
                "w00123",
                "",
                increment_attempt=True,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock("2026-07-10T10:08:00+08:00"),
            )

            with store:
                report = service.poll_once()

            self.assertEqual(1, report.new_notifications)
            self.assertEqual(1, len(sender.calls))
            self.assertIn("protected-file-approval：approval missing", sender.calls[0][1])

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

    def test_release_build_success_comments_merge_without_codex_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.base_ref = "Br_develop_cloud_release"
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
                        "id": 2,
                        "context": "protected-file-approval",
                        "state": "success",
                        "description": "approval success",
                        "updated_at": "2026-07-10T09:55:00+08:00",
                    },
                    {
                        "id": 3,
                        "context": "taichu/pr-build",
                        "state": "success",
                        "description": "build success",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    },
                ]

                report = service.poll_once()

            self.assertEqual([], report.errors)
            self.assertEqual(
                [("SystemAgentDev", "TaiChu", 7, "/ci merge")],
                client.comment_attempts,
            )
            self.assertEqual([], sender.calls)

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
                    "2026-07-10T10:17:00+08:00",
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
                gate_completed = service.poll_once()
                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:12:00+08:00",
                    }
                )
                succeeded = service.poll_once()
                repeated = service.poll_once()

                self.assertEqual(1, failed.new_notifications)
                self.assertEqual(0, gate_completed.new_notifications)
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
                "一天搞定 120 行代码，改得非常准。不需要冗长废话就能把痛点切掉，"
                "老医生的刀法。代码已上膛，干得漂亮！🍻 "
                "【Taichu PRbot 自动发送，回复TD退订】 "
                "查看 https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                sender.calls[1][1],
            )
            self.assertEqual([7], client.pull_detail_attempts)
            self.assertTrue(all("\n" not in message for _, message in sender.calls))

    def test_actual_merge_suppresses_an_undelivered_failure_before_success(self):
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

                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:09:00+08:00",
                    }
                )
                merged = service.poll_once()
                repeated = service.poll_once()
                records = store.list_outbox()

            self.assertEqual(1, failed.delivery_failures)
            self.assertEqual(1, merged.delivered)
            self.assertEqual(0, repeated.delivered)
            self.assertEqual(["suppressed", "sent"], [record.status for record in records])
            self.assertIn("发现问题：", sender.calls[0][1])
            self.assertIn("Merge Successful", sender.calls[1][1])
            self.assertEqual(2, len(sender.calls))

    def test_actual_merge_persistently_suppresses_an_uncertain_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = pathlib.Path(temp_dir) / "state.sqlite3"
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
                        "context": "ci/merge-gate",
                        "state": "failure",
                        "description": "merge blocked",
                        "updated_at": "2026-07-10T10:07:00+08:00",
                    }
                ]
                uncertain = service.poll_once()
                failure_id = store.list_outbox()[0].id

                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:09:00+08:00",
                    }
                )
                merged = service.poll_once()
                records = store.list_outbox()

            with MonitorStore(state_path) as restarted_store:
                can_retry_stale_failure = restarted_store.requeue_delivery(failure_id)

            self.assertEqual(1, uncertain.delivery_uncertain)
            self.assertEqual(1, merged.delivered)
            self.assertEqual(["suppressed", "sent"], [record.status for record in records])
            self.assertFalse(can_retry_stale_failure)
            self.assertEqual(2, len(sender.calls))

    def test_terminal_fetch_failure_holds_legacy_success_until_reconciled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:00:00+08:00",
                }
            ]
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci merge",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-merge",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(),
            )
            store.apply_poll(
                7,
                TrackerState("legacy-merge", frozenset(), True, snapshot.scanned_at),
                OutboxEvent(
                    "legacy-merge-success",
                    7,
                    "w00123",
                    format_message(snapshot, (), merge_success=True),
                ),
                snapshot=snapshot,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:11:00+08:00",
                ),
            )

            with store:
                service.poll_once()
                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:07:00+08:00",
                    }
                )
                original_get_statuses = client.get_statuses

                def fail_terminal_snapshot(owner, repo, sha):
                    raise RuntimeError("statuses unavailable")

                client.get_statuses = fail_terminal_snapshot
                failed = service.poll_once()
                calls_during_failure = list(sender.calls)
                client.get_statuses = original_get_statuses
                recovered = service.poll_once()
                records = store.list_outbox()

            self.assertTrue(any("terminal check failed" in item for item in failed.errors))
            self.assertEqual([], calls_during_failure)
            self.assertEqual(1, recovered.delivered)
            self.assertEqual(["suppressed", "sent"], [record.status for record in records])
            self.assertEqual(1, len(sender.calls))

    def test_actual_merge_suppresses_a_dead_legacy_success_before_current_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = pathlib.Path(temp_dir) / "state.sqlite3"
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:00:00+08:00",
                }
            ]
            sender = SequenceSender(["success"])
            store = MonitorStore(state_path)
            snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci merge",
                latest_ci_command_at="2026-07-10T09:55:00+08:00",
                latest_ci_command_key="legacy-merge",
                scanned_at="2026-07-10T10:00:00+08:00",
                failures=(),
            )
            store.apply_poll(
                7,
                TrackerState("legacy-merge", frozenset(), True, snapshot.scanned_at),
                OutboxEvent(
                    "legacy-dead-success",
                    7,
                    "w00123",
                    format_message(snapshot, (), merge_success=True),
                ),
                snapshot=snapshot,
            )
            legacy_id = store.list_outbox()[0].id
            store.update_delivery(
                legacy_id,
                "dead",
                "w00123",
                "old delivery exhausted",
                increment_attempt=True,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock(
                    "2026-07-10T10:05:00+08:00",
                    "2026-07-10T10:08:00+08:00",
                ),
            )

            with store:
                service.poll_once()
                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:07:00+08:00",
                    }
                )
                merged = service.poll_once()
                records = store.list_outbox()

            with MonitorStore(state_path) as restarted_store:
                can_retry_legacy = restarted_store.requeue_delivery(legacy_id)

            self.assertEqual(1, merged.delivered)
            self.assertEqual(["suppressed", "sent"], [record.status for record in records])
            self.assertFalse(can_retry_legacy)
            self.assertEqual(1, len(sender.calls))

    def test_invalid_merge_metrics_still_send_generic_success(self):
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
                gate_completed = service.poll_once()
                client.open_pulls = False
                client.pull_detail = {
                    "number": 7,
                    "state": "closed",
                    "merged": True,
                    "merged_at": "2026-07-10T10:09:00+08:00",
                    "created_at": "2026-07-10T10:00:00+08:00",
                    "deletions": 20,
                }
                succeeded = service.poll_once()

            self.assertEqual([], baseline.errors)
            self.assertEqual([], gate_completed.errors)
            self.assertEqual([], succeeded.errors)
            self.assertEqual([7], client.pull_detail_attempts)
            self.assertEqual(1, len(sender.calls))
            self.assertIn("Merge 成功啦", sender.calls[0][1])
            self.assertNotIn("变更 ", sender.calls[0][1])

    def test_legacy_queued_success_waits_until_the_pr_is_actually_merged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:00:00+08:00",
                }
            ]
            sender = SequenceSender(["success"])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            snapshot = PrSnapshot(
                number=7,
                title="Repair build",
                author="w00123",
                head_sha="abcdef123456",
                url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
                latest_ci_command="/ci merge",
                latest_ci_command_at="2026-07-10T10:00:00+08:00",
                latest_ci_command_key="merge-1",
                scanned_at="2026-07-10T10:05:00+08:00",
                failures=(),
            )
            store.apply_poll(
                7,
                TrackerState(
                    "merge-1",
                    frozenset({"merge-1:merge:success-notified"}),
                    True,
                    "2026-07-10T10:05:00+08:00",
                ),
                OutboxEvent(
                    "legacy-merge-success",
                    7,
                    "w00123",
                    format_message(snapshot, (), merge_success=True),
                ),
                snapshot=snapshot,
            )
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock(
                    "2026-07-10T10:08:00+08:00",
                    "2026-07-10T10:11:00+08:00",
                ),
            )

            with store:
                still_open = service.poll_once()
                pending = store.list_outbox()[0]
                calls_while_open = list(sender.calls)

                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:09:00+08:00",
                    }
                )
                merged = service.poll_once()
                terminal_records = store.list_outbox()

            self.assertEqual(0, still_open.delivered)
            self.assertEqual("pending", pending.status)
            self.assertEqual([], calls_while_open)
            self.assertEqual(1, merged.delivered)
            self.assertEqual(
                {"sent", "suppressed"},
                {record.status for record in terminal_records},
            )
            self.assertEqual(1, len(sender.calls))
            self.assertEqual([7, 7], client.pull_detail_attempts)

    def test_closed_unmerged_pr_is_dropped_without_a_success_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:00:00+08:00",
                }
            ]
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
                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": False,
                        "merged_at": None,
                    }
                )
                closed = service.poll_once()
                snapshots = store.list_snapshots()

            self.assertEqual(0, closed.new_notifications)
            self.assertEqual([], closed.errors)
            self.assertEqual([], sender.calls)
            self.assertEqual([], snapshots)
            self.assertEqual([7], client.pull_detail_attempts)

    def test_failed_terminal_lookup_is_retained_and_retried_next_poll(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:00:00+08:00",
                }
            ]
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
                service.poll_once()
                client.open_pulls = False
                client.pull_detail_error = RuntimeError("detail unavailable")
                failed = service.poll_once()
                retained = store.list_snapshots()
                pending = store.list_terminal_pending()

                client.pull_detail_error = None
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:09:00+08:00",
                    }
                )
                restarted = MonitorService(
                    client=client,
                    store=store,
                    sender=sender,
                    recipients=RecipientDirectory(direct=True),
                    clock=Clock("2026-07-10T10:11:00+08:00"),
                )
                recovered = restarted.poll_once()
                pruned = store.list_snapshots()
                cleared = store.list_terminal_pending()

            self.assertTrue(any("terminal check failed" in item for item in failed.errors))
            self.assertEqual(1, len(retained))
            self.assertEqual([7], pending)
            self.assertEqual(1, recovered.new_notifications)
            self.assertEqual([], recovered.errors)
            self.assertEqual([], pruned)
            self.assertEqual([], cleared)
            self.assertEqual(1, len(sender.calls))
            self.assertEqual([7, 7], client.pull_detail_attempts)

    def test_restart_does_not_treat_an_unmarked_stale_snapshot_as_a_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.comments = [
                {
                    "id": 2,
                    "body": "/ci merge",
                    "created_at": "2026-07-10T10:00:00+08:00",
                }
            ]
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            first = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=True),
                clock=Clock("2026-07-10T10:05:00+08:00"),
            )

            with store:
                first.poll_once()
                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:09:00+08:00",
                    }
                )
                restarted = MonitorService(
                    client=client,
                    store=store,
                    sender=sender,
                    recipients=RecipientDirectory(direct=True),
                    clock=Clock("2026-07-10T12:00:00+08:00"),
                )
                report = restarted.poll_once()
                snapshots = store.list_snapshots()

            self.assertEqual(0, report.new_notifications)
            self.assertEqual([], report.errors)
            self.assertEqual([], snapshots)
            self.assertEqual([], client.pull_detail_attempts)
            self.assertEqual([], sender.calls)

    def test_open_pull_listing_failure_does_not_trigger_terminal_checks_or_pruning(self):
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
                client.list_error = RuntimeError("Gitea unavailable")
                failed = service.poll_once()
                snapshots = store.list_snapshots()

            self.assertTrue(any("failed to list" in item for item in failed.errors))
            self.assertEqual(1, len(snapshots))
            self.assertEqual([], client.pull_detail_attempts)
            self.assertEqual([], sender.calls)

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

    def test_bot_authored_pr_is_ignored_before_scanning_or_recipient_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.user = {"login": "  TAICHU-CI-BOT  "}

            def unexpected_fetch(*args, **kwargs):
                raise AssertionError("ignored bot PR must not be fetched")

            client.get_statuses = unexpected_fetch
            client.get_issue_comments = unexpected_fetch
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            service = MonitorService(
                client=client,
                store=store,
                sender=sender,
                recipients=RecipientDirectory(direct=False),
                clock=Clock("2026-07-10T10:05:00+08:00"),
            )

            with store:
                report = service.poll_once()
                snapshots = store.list_snapshots()

            self.assertEqual(0, report.open_prs)
            self.assertEqual(0, report.scanned_prs)
            self.assertEqual([], report.errors)
            self.assertEqual([], snapshots)
            self.assertEqual([], sender.calls)

    def test_similar_bot_login_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.user = {"login": "taichu-ci-bot-extra"}
            sender = SequenceSender([])
            service, store = self.make_service(
                temp_dir,
                client,
                sender,
                Clock("2026-07-10T10:05:00+08:00"),
            )

            with store:
                report = service.poll_once()

            self.assertEqual(1, report.open_prs)
            self.assertEqual(1, report.scanned_prs)
            self.assertEqual([], report.errors)

    def test_legacy_bot_outbox_states_are_suppressed_without_welink_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeGiteaClient()
            client.user = {"login": "taichu-ci-bot"}
            sender = SequenceSender([])
            store = MonitorStore(pathlib.Path(temp_dir) / "state.sqlite3")
            for index, status in enumerate(("pending", "failed", "unmapped"), start=1):
                event_key = f"legacy-bot-{status}"
                store.apply_poll(
                    index,
                    TrackerState.empty(),
                    OutboxEvent(
                        event_key,
                        index,
                        "Taichu-CI-Bot",
                        "legacy bot message",
                    ),
                )
                record = next(
                    item for item in store.list_outbox() if item.event_key == event_key
                )
                if status != "pending":
                    store.update_delivery(
                        record.id,
                        status,
                        "",
                        "legacy error",
                        increment_attempt=False,
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
                records = store.list_outbox()

            self.assertEqual(
                {"suppressed"},
                {record.status for record in records},
            )
            self.assertTrue(all(record.attempts == 0 for record in records))
            self.assertEqual(0, report.unmapped)
            self.assertEqual([], report.errors)
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
                client.comments = [
                    {
                        "id": 2,
                        "body": "/ci build",
                        "created_at": "2026-07-10T10:09:00+08:00",
                    }
                ]
                client.statuses = [
                    {
                        "id": 3,
                        "context": "taichu/pr-build",
                        "state": "failure",
                        "description": "new failure",
                        "updated_at": "2026-07-10T10:10:00+08:00",
                    }
                ]
                retried = service.poll_once()

                self.assertEqual(1, failed.delivery_failures)
                self.assertEqual(0, retried.new_notifications)
                self.assertEqual(1, retried.delivered)
                self.assertEqual(2, len(sender.calls))
                self.assertEqual(1, len(store.list_outbox()))
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
                    "2026-07-14T10:14:00+08:00",
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

                gate_completed = service.poll_once()
                client.open_pulls = False
                client.pull_detail.update(
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-10T10:09:00+08:00",
                    }
                )
                failed = service.poll_once()
                client.pull_detail = {
                    "number": 7,
                    "created_at": "2026-07-01T10:00:00+08:00",
                    "additions": 3000,
                    "deletions": 100,
                }
                retried = service.poll_once()

                self.assertEqual(0, gate_completed.new_notifications)
                self.assertEqual(1, failed.delivery_failures)
                self.assertEqual(1, retried.delivered)
                self.assertEqual([7], client.pull_detail_attempts)
                self.assertEqual(2, len(sender.calls))
                self.assertEqual(sender.calls[0][1], sender.calls[1][1])
                self.assertIn("Merge Successful 🔪", sender.calls[1][1])
                self.assertIn("老医生的刀法", sender.calls[1][1])
                self.assertIn("一天搞定 120 行代码", sender.calls[1][1])
                self.assertNotIn("3100 行", sender.calls[1][1])
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
