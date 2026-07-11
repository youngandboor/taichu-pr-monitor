import unittest

from monitor.core import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    GateFailure,
    GateResult,
    PrSnapshot,
    TrackerState,
    build_pr_snapshot,
    derive_w3_account,
    effective_state,
    notification_text,
    notification_summary,
    poll_tracker,
)


class GateLogicTest(unittest.TestCase):
    def test_w3_account_is_derived_from_surname_initial_and_employee_number(self):
        user = {
            "full_name": "杨示例 00123456",
            "email": "unrelated-prefix.example@company.test",
        }

        self.assertEqual("y00123456", derive_w3_account(user))

    def test_embedded_w3_account_wins_without_surname_conversion(self):
        self.assertEqual(
            "z00123456",
            derive_w3_account({"full_name": "示例用户 z00123456"}),
        )

    def test_surname_initial_does_not_trust_an_unrelated_email_prefix(self):
        user = {
            "full_name": "刘示例 00123456",
            "email": "hwxx.example@company.test",
        }

        self.assertEqual("l00123456", derive_w3_account(user))

    def test_w3_account_derivation_fails_closed_when_identity_data_is_incomplete(self):
        self.assertEqual("", derive_w3_account({"full_name": "杨示例"}))
        self.assertEqual(
            "",
            derive_w3_account(
                {"full_name": "龘示例 00123456", "email": "example@company.test"}
            ),
        )

    def test_failure_text_overrides_success_state_like_android(self):
        summary = "TaiChu merge gate: 执行结果：失败，Cloud Preflight 未通过"

        self.assertEqual("failure", effective_state("success", summary))

    def test_success_aliases_and_summary_signals_match_android(self):
        self.assertEqual("success", effective_state("passed", ""))
        self.assertEqual("success", effective_state("", "当前 head 该门禁已通过。"))

    def test_explicit_success_is_not_overridden_by_artifact_error_filename(self):
        summary = (
            "TaiChu PR build：执行结果：成功\n"
            "构建成功\n"
            "testreport/error.txt：merge-gate 状态摘要"
        )

        self.assertEqual("success", effective_state("success", summary))

    def test_build_snapshot_keeps_only_latest_current_head_gate_failures(self):
        pr = {
            "number": 1222,
            "title": "Fix current failures",
            "html_url": "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1222",
            "user": {
                "login": "w00123",
                "full_name": "杨示例 00123456",
                "email": "unrelated-prefix.example@company.test",
            },
            "head": {"sha": "abcdef1234567890"},
        }
        statuses = [
            {
                "id": 1,
                "context": "taichu/codex-pr-review",
                "state": "success",
                "description": "执行结果：失败，发现 P1 问题",
                "updated_at": "2026-07-10T10:02:00+08:00",
            },
            {
                "id": 2,
                "context": "taichu/pr-build",
                "state": "failure",
                "description": "old build failure",
                "updated_at": "2026-07-10T10:03:00+08:00",
            },
            {
                "id": 3,
                "context": "taichu/pr-build",
                "state": "success",
                "description": "build success",
                "updated_at": "2026-07-10T10:04:00+08:00",
            },
        ]
        comments = [
            {
                "id": 9,
                "body": "/ci build",
                "created_at": "2026-07-10T10:00:00+08:00",
            },
            {
                "id": 10,
                "body": "<!-- merge-gate-queue-status -->\n暂不能入队",
                "created_at": "2026-07-10T10:05:00+08:00",
            },
            {
                "id": 11,
                "body": "taichu-dev-cloud-preflight\n当前 head abcdef1\n执行结果：失败\n缺少制品",
                "created_at": "2026-07-10T10:06:00+08:00",
            },
            {
                "id": 12,
                "body": "taichu merge gate：执行结果：失败\n当前 head deadbee",
                "created_at": "2026-07-10T10:07:00+08:00",
            },
        ]

        snapshot = build_pr_snapshot(
            pr,
            statuses,
            comments,
            scanned_at="2026-07-10T10:08:00+08:00",
        )

        self.assertEqual("w00123", snapshot.author)
        self.assertEqual("y00123456", snapshot.author_w3)
        self.assertEqual("/ci build", snapshot.latest_ci_command)
        self.assertEqual("success", snapshot.pr_build_state)
        self.assertEqual("2026-07-10T10:04:00+08:00", snapshot.pr_build_updated_at)
        self.assertEqual("build success", snapshot.pr_build_summary)
        self.assertEqual(
            "1222:/ci build:2026-07-10T10:00:00+08:00:9",
            snapshot.latest_ci_command_key,
        )
        self.assertEqual(
            ["taichu/codex-pr-review", "taichu/dev-cloud-preflight"],
            [failure.context for failure in snapshot.failures],
        )
        self.assertEqual(
            [
                "taichu/codex-pr-review",
                "taichu/pr-build",
                "taichu/dev-cloud-preflight",
            ],
            [result.context for result in snapshot.gate_results],
        )

    def test_notification_text_strips_markup_and_truncates(self):
        text = "<!--hidden-->## **失败摘要** <b>boom</b> " + ("x" * 200)

        cleaned = notification_text(text)

        self.assertNotIn("hidden", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertLessEqual(len(cleaned), 162)
        self.assertTrue(cleaned.endswith("..."))

    def test_notification_summary_extracts_structured_gate_failures(self):
        build_comment = (
            "本轮更新：2026-07-11 11:23:03\n"
            "TaiChu PR build：执行结果：失败\n"
            "说明：本次测的是 PR 合进目标分支后的结果。\n"
            "Taichu PR build 构建失败（时间：2026-07-11 11:23:03）\n"
            "失败摘要：测试未通过，请查看 Jenkins 日志与测试报告\n"
            "构建产物（若 Doc 测试失败）：https://example.invalid/artifact"
        )

        self.assertEqual(
            "测试未通过，请查看 Jenkins 日志与测试报告",
            notification_summary("taichu/pr-build", build_comment),
        )
        self.assertEqual(
            "Codex found 2 P0/P1 principle issue(s)",
            notification_summary(
                "taichu/codex-pr-review",
                "2026-07-11 11:20:46 | Codex found 2 P0/P1 principle issue(s)",
            ),
        )

    def test_notification_summary_uses_inline_failure_summary(self):
        text = (
            "TaiChu PR build：执行结果：失败 失败摘要：compile error in module foo "
            "构建产物（若有）：https://example.invalid/artifact"
        )

        self.assertEqual(
            "compile error in module foo",
            notification_summary("taichu/pr-build", text),
        )

    def test_default_poll_interval_is_three_minutes(self):
        self.assertEqual(180, DEFAULT_POLL_INTERVAL_SECONDS)


class TrackerTest(unittest.TestCase):
    def snapshot(
        self,
        *,
        scanned_at,
        command_key="cmd-1",
        command="/ci build",
        command_at="2026-07-10T10:00:00+08:00",
        failures=(),
        gate_results=(),
    ):
        return PrSnapshot(
            number=7,
            title="PR title",
            author="w00123",
            head_sha="abcdef123456",
            url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
            latest_ci_command=command if command_key else "",
            latest_ci_command_at=command_at if command_key else "",
            latest_ci_command_key=command_key,
            scanned_at=scanned_at,
            failures=tuple(failures),
            gate_results=tuple(gate_results),
        )

    def gate(self, context, state, updated_at, summary=""):
        return GateResult(context, state, updated_at, summary or state)

    def build_successes(self, updated_at):
        return (
            self.gate("protected-file-approval", "success", updated_at),
            self.gate("taichu/codex-pr-review", "success", updated_at),
            self.gate("taichu/pr-build", "success", updated_at),
        )

    def merge_successes(self, updated_at):
        return (
            self.gate("taichu/dev-cloud-preflight", "success", updated_at),
            self.gate("ci/merge-gate", "success", updated_at),
        )

    def test_first_poll_baselines_historical_build_completion(self):
        snapshot = self.snapshot(
            scanned_at="2026-07-10T10:05:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:02:00+08:00"),
        )

        result = poll_tracker(TrackerState.empty(), snapshot)

        self.assertFalse(result.request_merge_comment)
        self.assertTrue(result.state.initialized)
        self.assertEqual(1, len(result.state.notified_failure_keys))

    def test_new_build_completion_requests_merge_comment_once(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-10T10:05:00+08:00",
            ),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:06:00+08:00"),
        )

        first = poll_tracker(baseline, changed)
        second = poll_tracker(first.state, changed)

        self.assertTrue(first.request_merge_comment)
        self.assertFalse(first.merge_success)
        self.assertFalse(second.request_merge_comment)

    def test_preconditions_may_pass_before_command_when_pr_build_is_new(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            gate_results=(
                self.gate(
                    "protected-file-approval",
                    "success",
                    "2026-07-10T09:50:00+08:00",
                ),
                self.gate(
                    "taichu/codex-pr-review",
                    "success",
                    "2026-07-10T09:55:00+08:00",
                ),
                self.gate(
                    "taichu/pr-build",
                    "success",
                    "2026-07-10T10:06:00+08:00",
                ),
            ),
        )

        result = poll_tracker(baseline, changed)

        self.assertTrue(result.request_merge_comment)

    def test_late_precondition_success_completes_build_after_pr_build(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-10T10:06:00+08:00",
                gate_results=(
                    self.gate(
                        "protected-file-approval",
                        "pending",
                        "2026-07-10T10:04:00+08:00",
                    ),
                    self.gate(
                        "taichu/codex-pr-review",
                        "success",
                        "2026-07-10T09:55:00+08:00",
                    ),
                    self.gate(
                        "taichu/pr-build",
                        "success",
                        "2026-07-10T10:05:00+08:00",
                    ),
                ),
            ),
        ).state
        completed = self.snapshot(
            scanned_at="2026-07-10T10:09:00+08:00",
            gate_results=(
                self.gate(
                    "protected-file-approval",
                    "success",
                    "2026-07-10T10:08:00+08:00",
                ),
                self.gate(
                    "taichu/codex-pr-review",
                    "success",
                    "2026-07-10T09:55:00+08:00",
                ),
                self.gate(
                    "taichu/pr-build",
                    "success",
                    "2026-07-10T10:05:00+08:00",
                ),
            ),
        )

        result = poll_tracker(baseline, completed)

        self.assertTrue(result.request_merge_comment)

    def test_build_completion_in_same_second_as_watermark_is_not_lost(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        completed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:05:00+08:00"),
        )

        result = poll_tracker(baseline, completed)

        self.assertTrue(result.request_merge_comment)

    def test_parser_upgrade_does_not_comment_for_old_build_completion(self):
        legacy_state = TrackerState(
            "cmd-1",
            frozenset(),
            True,
            "2026-07-10T10:05:00+08:00",
        )
        historical_success = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:02:00+08:00"),
        )

        result = poll_tracker(legacy_state, historical_success)

        self.assertFalse(result.request_merge_comment)
        self.assertEqual(1, len(result.state.notified_failure_keys))

    def test_build_results_before_latest_build_command_are_ignored(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:12:00+08:00",
            command_key="cmd-2",
            command_at="2026-07-10T10:10:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:09:00+08:00"),
        )

        result = poll_tracker(baseline, changed)

        self.assertFalse(result.request_merge_comment)

    def test_build_results_are_not_processed_during_merge_stage(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:06:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:07:00+08:00"),
        )

        result = poll_tracker(baseline, changed)

        self.assertFalse(result.request_merge_comment)
        self.assertFalse(result.merge_success)

    def test_first_poll_builds_baseline_without_historical_alerts(self):
        snapshot = self.snapshot(
            scanned_at="2026-07-10T10:05:00+08:00",
            failures=(GateFailure("taichu/pr-build", "2026-07-10T10:02:00+08:00", "failed"),),
        )

        result = poll_tracker(TrackerState.empty(), snapshot)

        self.assertEqual((), result.notifications)
        self.assertTrue(result.state.initialized)
        self.assertEqual(1, len(result.state.notified_failure_keys))

    def test_build_round_combines_current_failures_and_notifies_only_once(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            failures=(
                GateFailure(
                    "protected-file-approval",
                    "2026-07-10T10:06:00+08:00",
                    "approval failed",
                ),
                GateFailure(
                    "taichu/pr-build",
                    "2026-07-10T10:07:00+08:00",
                    "build failed",
                ),
                GateFailure(
                    "ci/merge-gate",
                    "2026-07-10T10:07:00+08:00",
                    "wrong stage",
                ),
            ),
        )

        first = poll_tracker(baseline, changed)
        later = self.snapshot(
            scanned_at="2026-07-10T10:11:00+08:00",
            failures=changed.failures
            + (
                GateFailure(
                    "taichu/codex-pr-review",
                    "2026-07-10T10:09:00+08:00",
                    "review failed later",
                ),
            ),
        )
        second = poll_tracker(first.state, later)

        self.assertEqual(
            ["protected-file-approval", "taichu/pr-build"],
            [item.context for item in first.notifications],
        )
        self.assertEqual((), second.notifications)

    def test_new_build_command_reports_an_existing_precondition_failure_once(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            command_key="cmd-2",
            command_at="2026-07-10T10:06:00+08:00",
            failures=(
                GateFailure(
                    "protected-file-approval",
                    "2026-07-10T09:50:00+08:00",
                    "approval still missing",
                ),
            ),
        )

        first = poll_tracker(baseline, changed)
        repeated = poll_tracker(first.state, changed)

        self.assertEqual(
            ["protected-file-approval"],
            [item.context for item in first.notifications],
        )
        self.assertEqual((), repeated.notifications)

    def test_new_build_does_not_reuse_an_old_pr_build_failure(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:12:00+08:00",
            command_key="cmd-2",
            command_at="2026-07-10T10:10:00+08:00",
            failures=(
                GateFailure(
                    "taichu/pr-build",
                    "2026-07-10T10:09:00+08:00",
                    "previous build failed",
                ),
            ),
        )

        result = poll_tracker(baseline, changed)

        self.assertEqual((), result.notifications)

    def test_merge_failure_then_success_notifies_each_outcome_once(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        failed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:06:00+08:00",
            failures=(
                GateFailure(
                    "taichu/dev-cloud-preflight",
                    "2026-07-10T10:07:00+08:00",
                    "missing artifact",
                ),
            ),
        )
        recovered = self.snapshot(
            scanned_at="2026-07-10T10:11:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:06:00+08:00",
            gate_results=self.merge_successes("2026-07-10T10:09:00+08:00"),
        )

        failure_result = poll_tracker(baseline, failed)
        success_result = poll_tracker(failure_result.state, recovered)
        repeated = poll_tracker(success_result.state, recovered)

        self.assertEqual(1, len(failure_result.notifications))
        self.assertTrue(success_result.merge_success)
        self.assertFalse(repeated.merge_success)
        self.assertEqual(2, len(success_result.state.notified_failure_keys))

    def test_old_failure_before_latest_command_is_ignored(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:12:00+08:00",
            command_key="cmd-2",
            command_at="2026-07-10T10:10:00+08:00",
            failures=(
                GateFailure(
                    "taichu/pr-build",
                    "2026-07-10T10:09:00+08:00",
                    "old failure",
                ),
            ),
        )

        result = poll_tracker(baseline, changed)

        self.assertEqual((), result.notifications)

    def test_scan_watermark_compares_timezone_offsets_as_instants(self):
        state = TrackerState(
            "cmd-1",
            frozenset(),
            True,
            "2026-07-10T05:05:00+00:00",
        )
        old_failure = self.snapshot(
            scanned_at="2026-07-10T05:08:00+00:00",
            command_at="2026-07-10T07:50:00+08:00",
            failures=(
                GateFailure(
                    "taichu/pr-build",
                    "2026-07-10T08:00:00+08:00",
                    "parser discovered an old failure",
                ),
            ),
        )

        result = poll_tracker(state, old_failure)

        self.assertEqual((), result.notifications)


if __name__ == "__main__":
    unittest.main()
