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
                "user": {"login": "taichu-ci-bot"},
            },
            {
                "id": 12,
                "body": "taichu merge gate：执行结果：失败\n当前 head deadbee",
                "created_at": "2026-07-10T10:07:00+08:00",
                "user": {"login": "taichu-ci-bot"},
            },
            {
                "id": 3,
                "body": (
                    "<!-- external-ci/jenkins-pr-build -->\n"
                    "## TaiChu PR build：执行结果：成功\n构建成功"
                ),
                "created_at": "2026-07-11T11:20:30+08:00",
                "user": {"login": "ordinary-pr-author"},
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

    def test_explicit_comment_marker_beats_incidental_gate_names(self):
        pr = {
            "number": 1329,
            "title": "Build failure",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
        }
        comments = [
            {
                "id": 1,
                "body": "/ci build",
                "created_at": "2026-07-11T11:19:00+08:00",
            },
            {
                "id": 2,
                "body": (
                    "<!-- external-ci/jenkins-pr-build -->\n"
                    "## TaiChu PR build：执行结果：失败\n"
                    "诊断日志提到了 protected-file-approval 和 taichu/codex-pr-review\n"
                    "失败摘要：compile error"
                ),
                "created_at": "2026-07-11T11:20:00+08:00",
                "user": {"login": "taichu-ci-bot"},
            },
        ]

        snapshot = build_pr_snapshot(
            pr,
            [],
            comments,
            scanned_at="2026-07-11T11:21:00+08:00",
        )

        self.assertEqual(["taichu/pr-build"], [item.context for item in snapshot.failures])

    def test_auto_merge_blocked_comments_are_terminal_merge_failures(self):
        pr = {
            "number": 347,
            "title": "Blocked merge",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
        }
        templates = (
            (
                "## TaiChu merge gate：等待审批\n"
                "**当前阻塞**：当前 PR head 尚未满足 allowlist 审批条件。\n"
                "**当前评估的 PR head**：`abcdef1…`",
                "当前 PR head 尚未满足 allowlist 审批条件。",
            ),
            (
                "## `/ci merge` 暂不能入队\n"
                "**原因**：当前 PR head 上仍有 Gitea 检查未全部为 success。\n"
                "**当前 PR head**：`abcdef1…`",
                "当前 PR head 上仍有 Gitea 检查未全部为 success。",
            ),
        )

        for index, (body, expected) in enumerate(templates, start=1):
            with self.subTest(body=body):
                marked_body = "<!-- taichu-ci/auto-merge-blocked -->\n" + body
                comments = [
                    {
                        "id": 1,
                        "body": "/ci merge",
                        "created_at": "2026-07-11T11:00:00+08:00",
                    },
                    {
                        "id": index + 1,
                        "body": marked_body,
                        "created_at": "2026-07-11T11:01:00+08:00",
                        "user": {"login": "taichu-ci-bot"},
                    },
                ]

                snapshot = build_pr_snapshot(
                    pr,
                    [],
                    comments,
                    scanned_at="2026-07-11T11:02:00+08:00",
                )

                self.assertEqual(
                    ["ci/merge-gate"],
                    [item.context for item in snapshot.failures],
                )
                self.assertEqual(
                    expected,
                    notification_summary("ci/merge-gate", marked_body),
                )

    def test_codex_marker_uses_principle_result_not_incidental_wording(self):
        pr = {
            "number": 1359,
            "title": "Codex result",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
        }
        cases = (
            (
                "- P1 `module.py:10`：失败路径仍会泄露信息。\n"
                "#### 建议\n- 修复后 shell 语法检查可通过。",
                "failure",
            ),
            (
                "- 未发现原则问题。\n"
                "#### 建议\n- 补充 error handling 的失败路径测试。",
                "success",
            ),
        )

        for index, (principles, expected_state) in enumerate(cases, start=1):
            with self.subTest(expected_state=expected_state):
                comments = [
                    {
                        "id": 1,
                        "body": "/ci build",
                        "created_at": "2026-07-11T11:00:00+08:00",
                    },
                    {
                        "id": index + 1,
                        "body": (
                            "<!-- taichu-codex-pr-review -->\n"
                            "<!-- taichu-codex-pr-review-head:"
                            "abcdef1234567890 -->\n"
                            "### Codex PR Review\n"
                            "| Head | `abcdef123456` |\n"
                            "| Status | `taichu/codex-pr-review` |\n"
                            "#### 原则问题\n"
                            f"{principles}"
                        ),
                        "created_at": "2026-07-11T11:01:00+08:00",
                        "user": {"login": "taichu-ci-bot"},
                    },
                ]

                snapshot = build_pr_snapshot(
                    pr,
                    [],
                    comments,
                    scanned_at="2026-07-11T11:02:00+08:00",
                )

                codex = next(
                    item
                    for item in snapshot.gate_results
                    if item.context == "taichu/codex-pr-review"
                )
                self.assertEqual(expected_state, codex.state)
                self.assertEqual(
                    expected_state == "failure",
                    any(
                        item.context == "taichu/codex-pr-review"
                        for item in snapshot.failures
                    ),
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
            "构建产物（若 Doc 测试失败）：https://example.invalid/artifact\n"
            "### 关键失败原因\n"
            "```text\n"
            "failed_task_count=1\n"
            "failed_task_1.task_label=Node B\n"
            "failed_task_1.stage=non_device\n"
            "failed_task_1.reason_type=compile_error\n"
            "failed_task_1.suite=rust-workspace\n"
            "failed_task_1.exit_status=101\n"
            "```"
        )

        self.assertEqual(
            "Node B/non_device/rust-workspace 编译失败（exit 101）",
            notification_summary("taichu/pr-build", build_comment),
        )
        self.assertEqual(
            "发现 2 个 P0/P1 原则问题",
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

    def test_notification_summary_keeps_special_codex_gate_reason(self):
        comment = (
            "### Codex PR Review\n"
            "#### 原则问题\n"
            "- PR 变更中有 `1` 个文本文件包含 CRLF 换行，本次不调用 Codex，直接失败。"
            " 原因：仓库文本源码要求使用 LF。\n"
            "#### 建议\n"
            "- 将文件转换为 LF 后重新提交。"
        )

        self.assertEqual(
            "PR 变更中有 1 个文本文件包含 CRLF 换行，本次不调用 Codex，直接失败。",
            notification_summary("taichu/codex-pr-review", comment),
        )

    def test_notification_summary_extracts_approval_result(self):
        comment = (
            "<!-- taichu-protected-file-approval -->\n"
            "### PR approve检查\n"
            "| Field | Value |\n"
            "| --- | --- |\n"
            "| Status | `taichu/protected-file-approval` = `failure` |\n"
            "#### 结果\n"
            "- P1 保护目录/文件缺少对应 owner approve；release 同步证据或 approve 不完整。\n"
            "##### 保护目录/文件 approve\n"
            "| 模块 | 状态 |\n| --- | --- |\n| libs/mobile | 缺少 approve |"
        )

        self.assertEqual(
            "P1 保护目录/文件缺少对应 owner approve；release 同步证据或 approve 不完整。",
            notification_summary("protected-file-approval", comment),
        )

    def test_notification_summary_extracts_preflight_failed_case_only(self):
        comment = (
            "<!-- taichu-dev-cloud-preflight -->\n"
            "### TaiChu 云侧 Preflight：失败\n"
            "#### 用例结果\n"
            "| 用例 | 检查点 | 结果 | 说明/失败原因 |\n"
            "| --- | --- | --- | --- |\n"
            "| AS 路由错误 | 名称包含错误但结果通过 | PASS | CASE PASS error route |\n"
            "| 云侧 Smoke 总体 | 核心接口闭环 | FAIL | 一个或多个具体用例失败 |\n"
            "| Heavy runtime ensure | ensure 返回 HTTP 200 | FAIL | ensure HTTP 检查失败：返回 HTTP 502，详情 https://internal.invalid/log |\n"
            "\n**失败原因：**\n```text\n包含内部地址的长日志\n```"
        )

        self.assertEqual(
            "Heavy runtime ensure：ensure HTTP 检查失败：返回 HTTP 502",
            notification_summary("taichu/dev-cloud-preflight", comment),
        )

    def test_preflight_failed_table_is_reachable_from_snapshot(self):
        pr = {
            "number": 364,
            "title": "Preflight failure",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
        }
        comments = [
            {
                "id": 1,
                "body": "/ci build",
                "created_at": "2026-07-11T11:00:00+08:00",
            },
            {
                "id": 2,
                "body": (
                    "<!-- taichu-dev-cloud-preflight -->\n"
                    "### TaiChu 云侧 Preflight：失败\n"
                    "| 用例 | 结果 | 说明/失败原因 |\n"
                    "| --- | --- | --- |\n"
                    "| Candidate 构建 | FAIL | candidate 构建失败 |"
                ),
                "created_at": "2026-07-11T11:01:00+08:00",
                "user": {"login": "taichu-ci-bot"},
            },
        ]

        snapshot = build_pr_snapshot(
            pr,
            [],
            comments,
            scanned_at="2026-07-11T11:02:00+08:00",
        )

        self.assertEqual(
            ["taichu/dev-cloud-preflight"],
            [item.context for item in snapshot.failures],
        )

    def test_notification_summary_uses_preflight_conclusion_before_raw_log(self):
        comment = (
            "<!-- taichu-dev-cloud-preflight -->\n"
            "### TaiChu 云侧 Preflight：失败\n"
            "结论：candidate 构建失败。\n"
            "失败原因：\n```text\nerror[E0425]: internal build detail\n```"
        )

        self.assertEqual(
            "candidate 构建失败。",
            notification_summary("taichu/dev-cloud-preflight", comment),
        )

    def test_notification_summary_extracts_merge_gate_failure(self):
        structured = (
            "<!-- external-ci/jenkins-merge-gate-test -->\n"
            "## TaiChu merge gate：执行结果：失败\n"
            "失败摘要：测试未通过，请查看 Jenkins 日志与测试报告\n"
            "failed_task_count=1\n"
            "failed_task_1.task_label=Smoke\n"
            "failed_task_1.stage=smoke\n"
            "failed_task_1.reason_type=smoke_failure\n"
            "failed_task_1.suite=vassistant-smoke\n"
            "failed_task_1.exit_status=1\n"
        )

        self.assertEqual(
            "Smoke/vassistant-smoke 冒烟失败（exit 1）",
            notification_summary("ci/merge-gate", structured),
        )
        self.assertEqual(
            "Cloud Preflight 未通过",
            notification_summary(
                "ci/merge-gate",
                "TaiChu merge gate: 执行结果：失败，Cloud Preflight 未通过",
            ),
        )

    def test_gate_templates_without_trusted_fields_fail_closed(self):
        cases = (
            (
                "protected-file-approval",
                "protected-file-approval\n执行结果：失败\n内部日志 secret=ABC123",
                "受保护文件审批未通过，详情见 PR",
            ),
            (
                "taichu/codex-pr-review",
                "taichu/codex-pr-review\n执行结果：失败\n内部日志 token=ABC123",
                "Codex Review 未通过，详情见 PR",
            ),
            (
                "taichu/pr-build",
                "taichu/pr-build\n执行结果：失败\n内部日志 password=ABC123",
                "PR Build 失败，详情见 PR",
            ),
            (
                "taichu/dev-cloud-preflight",
                "taichu/dev-cloud-preflight\n执行结果：失败\n内部日志 credential=ABC123",
                "云侧 Preflight 未通过，详情见 PR",
            ),
            (
                "ci/merge-gate",
                "ci/merge-gate\n执行结果：失败\n内部日志 api_key=ABC123",
                "Merge Gate 未通过，详情见 PR",
            ),
        )

        for context, comment, expected in cases:
            with self.subTest(context=context):
                summary = notification_summary(context, comment)
                self.assertEqual(expected, summary)
                self.assertNotIn("ABC123", summary)

    def test_empty_failure_labels_do_not_read_fenced_logs(self):
        cases = (
            (
                "taichu/pr-build",
                "taichu/pr-build\n失败原因：\n```text\nsecret=ABC123 internal.host\n```",
                "PR Build 失败，详情见 PR",
            ),
            (
                "taichu/dev-cloud-preflight",
                "taichu/dev-cloud-preflight\n结论：\n```text\ntoken=ABC123 internal.host\n```",
                "云侧 Preflight 未通过，详情见 PR",
            ),
            (
                "ci/merge-gate",
                "ci/merge-gate\n阻塞原因：\n```text\npassword=ABC123 internal.host\n```",
                "Merge Gate 未通过，详情见 PR",
            ),
        )

        for context, comment, expected in cases:
            with self.subTest(context=context):
                self.assertEqual(expected, notification_summary(context, comment))

    def test_structured_task_fields_reject_credentials_and_invalid_exit_status(self):
        comment = (
            "<!-- external-ci/jenkins-merge-gate-test -->\n"
            "## TaiChu merge gate：执行结果：失败\n"
            "failed_task_count=1\n"
            "failed_task_1.task_label=secret=ABC123\n"
            "failed_task_1.stage=https://internal.invalid/path\n"
            "failed_task_1.reason_type=smoke_failure\n"
            "failed_task_1.suite=token=XYZ\n"
            "failed_task_1.exit_status=1 token=XYZ"
        )

        summary = notification_summary("ci/merge-gate", comment)

        self.assertEqual("冒烟失败", summary)
        self.assertNotIn("ABC123", summary)
        self.assertNotIn("XYZ", summary)

    def test_structured_task_parser_ignores_unbounded_numeric_fields(self):
        huge = "9" * 5000
        comment = (
            "<!-- external-ci/jenkins-pr-build -->\n"
            "## TaiChu PR build：执行结果：失败\n"
            f"failed_task_count={huge}\n"
            f"failed_task_{huge}.task_label=Node A\n"
        )

        self.assertEqual(
            "PR Build 失败，详情见 PR",
            notification_summary("taichu/pr-build", comment),
        )

    def test_unknown_html_tags_and_attributes_do_not_enter_summary(self):
        value = (
            '<img src="https://internal.invalid/a" alt="token=ABC123">'
            "失败摘要：compile error"
        )

        self.assertEqual(
            "compile error",
            notification_summary("taichu/pr-build", value),
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
