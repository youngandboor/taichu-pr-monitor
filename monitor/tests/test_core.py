import unittest

from monitor.core import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    GateFailure,
    GateResult,
    PROBLEM_FINGERPRINT_PREFIX,
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
            "base": {"ref": "main"},
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
        self.assertEqual("main", snapshot.base_ref)
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

    def test_codex_test_review_marker_is_a_distinct_build_gate(self):
        pr = {
            "number": 1516,
            "title": "Validate tests",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
            "base": {"ref": "main"},
        }
        comments = [
            {
                "id": 1,
                "body": "/ci build",
                "created_at": "2026-07-16T16:31:01+08:00",
            },
            {
                "id": 2,
                "body": (
                    "<!-- taichu-codex-pr-test-review -->\n"
                    "<!-- taichu-codex-pr-test-review-head:"
                    "abcdef1234567890 -->\n"
                    "### Codex PR Test Review\n"
                    "| Status | `taichu/codex-pr-test-review` |\n"
                    "#### 原则问题\n"
                    "- P1 `tests/test_case.py:10`：关键失败路径没有断言。\n"
                    "#### 建议\n"
                    "- 补充回归测试。"
                ),
                "created_at": "2026-07-16T16:35:24+08:00",
                "user": {"login": "taichu-ci-bot"},
            },
        ]

        snapshot = build_pr_snapshot(
            pr,
            [],
            comments,
            scanned_at="2026-07-16T16:36:00+08:00",
        )

        self.assertEqual(
            ["taichu/codex-pr-test-review"],
            [item.context for item in snapshot.failures],
        )
        self.assertEqual(
            "发现 1 个 P0/P1 测试审查问题",
            snapshot.failures[0].summary,
        )

    def test_codex_test_review_accepts_rollout_comment_with_legacy_head_marker(self):
        pr = {
            "number": 1487,
            "title": "Large change",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
            "base": {"ref": "main"},
        }
        body = (
            "<!-- taichu-codex-pr-test-review -->\n"
            "<!-- taichu-codex-pr-review-head:abcdef1234567890 -->\n"
            "### Codex PR Review\n"
            "| Status | `taichu/codex-pr-test-review` = `failure` |\n"
            "#### 原则问题\n"
            "- PR 变更规模超过 Codex 审查上限，本次不调用 Codex，直接失败。"
        )
        snapshot = build_pr_snapshot(
            pr,
            [],
            [
                {
                    "id": 1,
                    "body": "/ci build",
                    "created_at": "2026-07-16T10:00:00+08:00",
                },
                {
                    "id": 2,
                    "body": body,
                    "created_at": "2026-07-16T10:01:00+08:00",
                    "user": {"login": "taichu-ci-bot"},
                },
            ],
            scanned_at="2026-07-16T10:02:00+08:00",
        )

        self.assertEqual(
            ["taichu/codex-pr-test-review"],
            [item.context for item in snapshot.failures],
        )
        self.assertEqual(
            "PR 变更规模超过 Codex 审查上限，本次不调用 Codex，直接失败。",
            notification_summary("taichu/codex-pr-test-review", body),
        )

    def test_codex_test_review_rejects_an_async_result_for_an_old_head(self):
        pr = {
            "number": 1516,
            "title": "Validate tests",
            "user": {"login": "w00123"},
            "head": {"sha": "bbbbbb1234567890"},
            "base": {"ref": "main"},
        }
        snapshot = build_pr_snapshot(
            pr,
            [],
            [
                {
                    "id": 1,
                    "body": "/ci build",
                    "created_at": "2026-07-16T16:31:01+08:00",
                },
                {
                    "id": 2,
                    "body": (
                        "<!-- taichu-codex-pr-test-review -->\n"
                        "<!-- taichu-codex-pr-test-review-head:"
                        "aaaaaa1234567890 -->\n"
                        "<!-- taichu-codex-pr-review-head:"
                        "aaaaaa1234567890 -->\n"
                        "### Codex PR Test Review\n"
                        "| Status | `taichu/codex-pr-test-review` = `failure` |\n"
                        "#### 原则问题\n"
                        "- P1 这是旧 head 的异步结果。"
                    ),
                    "created_at": "2026-07-16T16:35:24+08:00",
                    "user": {"login": "taichu-ci-bot"},
                },
            ],
            scanned_at="2026-07-16T16:36:00+08:00",
        )

        self.assertFalse(
            any(
                item.context == "taichu/codex-pr-test-review"
                for item in snapshot.gate_results
            )
        )
        self.assertEqual((), snapshot.failures)

    def test_codex_test_review_success_comment_stays_non_actionable(self):
        pr = {
            "number": 1516,
            "title": "Validate tests",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
            "base": {"ref": "main"},
        }
        snapshot = build_pr_snapshot(
            pr,
            [],
            [
                {
                    "id": 1,
                    "body": "/ci build",
                    "created_at": "2026-07-16T16:31:01+08:00",
                },
                {
                    "id": 2,
                    "body": (
                        "<!-- taichu-codex-pr-test-review -->\n"
                        "<!-- taichu-codex-pr-test-review-head:"
                        "abcdef1234567890 -->\n"
                        "### Codex PR Test Review\n"
                        "| Status | `taichu/codex-pr-test-review` |\n"
                        "#### 原则问题\n"
                        "- 未发现原则问题。\n"
                        "#### 未验证风险\n"
                        "- 未执行真机 smoke。"
                    ),
                    "created_at": "2026-07-16T16:35:24+08:00",
                    "user": {"login": "taichu-ci-bot"},
                },
            ],
            scanned_at="2026-07-16T16:36:00+08:00",
        )

        result = next(
            item
            for item in snapshot.gate_results
            if item.context == "taichu/codex-pr-test-review"
        )
        self.assertEqual("success", result.state)
        self.assertEqual((), snapshot.failures)

    def test_old_pr_without_codex_test_review_keeps_existing_gate_results(self):
        pr = {
            "number": 1400,
            "title": "Legacy PR",
            "user": {"login": "w00123"},
            "head": {"sha": "abcdef1234567890"},
            "base": {"ref": "main"},
        }
        snapshot = build_pr_snapshot(
            pr,
            [
                {
                    "id": 1,
                    "context": "taichu/codex-pr-review",
                    "status": "success",
                    "description": "Codex found no P0/P1 principle issues",
                    "updated_at": "2026-07-15T10:01:00+08:00",
                }
            ],
            [
                {
                    "id": 1,
                    "body": "/ci build",
                    "created_at": "2026-07-15T10:00:00+08:00",
                }
            ],
            scanned_at="2026-07-15T10:02:00+08:00",
        )

        self.assertEqual((), snapshot.failures)
        self.assertEqual(
            ["taichu/codex-pr-review"],
            [item.context for item in snapshot.gate_results],
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
        self.assertEqual(
            "发现 2 个 P0/P1 测试审查问题",
            notification_summary(
                "taichu/codex-pr-test-review",
                "2026-07-16 16:20:46 | Codex found 2 P0/P1 "
                "test-validation issue(s)",
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
        base_ref="main",
        merged=False,
        merged_at="",
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
            base_ref=base_ref,
            merged=merged,
            merged_at=merged_at,
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

    def test_build_success_has_no_notification_or_follow_up_action(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        succeeded = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            gate_results=self.build_successes("2026-07-10T10:06:00+08:00"),
        )

        result = poll_tracker(baseline, succeeded)

        self.assertEqual((), result.notifications)
        self.assertFalse(result.merge_success)
        self.assertFalse(
            any("merge-comment" in key for key in result.state.notified_failure_keys)
        )

    def test_legacy_auto_merge_state_is_inert_and_expires_next_round(self):
        legacy_key = "cmd-1:build:merge-comment-attempted"
        legacy = TrackerState(
            "cmd-1",
            frozenset({legacy_key}),
            True,
            "2026-07-10T10:05:00+08:00",
        )
        same_round = poll_tracker(
            legacy,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                gate_results=self.build_successes("2026-07-10T10:06:00+08:00"),
            ),
        )
        next_round = poll_tracker(
            same_round.state,
            self.snapshot(
                scanned_at="2026-07-10T10:12:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                gate_results=self.build_successes("2026-07-10T10:11:00+08:00"),
            ),
        )

        self.assertEqual((), same_round.notifications)
        self.assertFalse(same_round.merge_success)
        self.assertIn(legacy_key, same_round.state.notified_failure_keys)
        self.assertNotIn(legacy_key, next_round.state.notified_failure_keys)
        self.assertFalse(next_round.merge_success)

    def test_first_poll_builds_baseline_without_historical_alerts(self):
        snapshot = self.snapshot(
            scanned_at="2026-07-10T10:05:00+08:00",
            failures=(GateFailure("taichu/pr-build", "2026-07-10T10:02:00+08:00", "failed"),),
        )

        result = poll_tracker(TrackerState.empty(), snapshot)

        self.assertEqual((), result.notifications)
        self.assertTrue(result.state.initialized)
        self.assertTrue(
            any(
                key.startswith(PROBLEM_FINGERPRINT_PREFIX)
                for key in result.state.notified_failure_keys
            )
        )

    def test_upgrade_does_not_replay_an_existing_codex_test_review_failure(self):
        existing = TrackerState(
            "cmd-1",
            frozenset(),
            True,
            "2026-07-16T17:00:00+08:00",
        )
        historical = self.snapshot(
            scanned_at="2026-07-16T17:03:00+08:00",
            failures=(
                GateFailure(
                    "taichu/codex-pr-test-review",
                    "2026-07-16T16:35:25+08:00",
                    "发现 1 个 P0/P1 测试审查问题",
                ),
            ),
        )

        upgraded = poll_tracker(existing, historical)
        repeated = poll_tracker(upgraded.state, historical)

        self.assertEqual((), upgraded.notifications)
        self.assertEqual((), repeated.notifications)
        self.assertTrue(
            any(
                "taichu/codex-pr-test-review" in key
                for key in upgraded.state.notified_failure_keys
            )
        )

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

    def test_approval_failure_is_not_repeated_in_later_build_rounds(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        first = poll_tracker(
            baseline,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:06:00+08:00",
                        "approval missing for config A",
                    ),
                ),
            ),
        )
        next_round = poll_tracker(
            first.state,
            self.snapshot(
                scanned_at="2026-07-10T10:13:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:12:00+08:00",
                        "approval missing for a different protected file",
                    ),
                ),
            ),
        )

        self.assertEqual(
            ["protected-file-approval"],
            [failure.context for failure in first.notifications],
        )
        self.assertEqual((), next_round.notifications)

    def test_baselined_approval_is_not_announced_in_a_later_round(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-10T10:05:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:02:00+08:00",
                        "historical approval failure",
                    ),
                ),
            ),
        )
        later = poll_tracker(
            baseline.state,
            self.snapshot(
                scanned_at="2026-07-10T10:13:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:12:00+08:00",
                        "approval failure after another change",
                    ),
                ),
            ),
        )

        self.assertEqual((), baseline.notifications)
        self.assertEqual((), later.notifications)

    def test_legacy_round_flag_silently_seeds_the_visible_problem(self):
        legacy = TrackerState(
            "cmd-1",
            frozenset({"cmd-1:build:failure-notified"}),
            True,
            "2026-07-10T10:08:00+08:00",
        )
        visible = self.snapshot(
            scanned_at="2026-07-10T10:11:00+08:00",
            failures=(
                GateFailure(
                    "taichu/pr-build",
                    "2026-07-10T10:07:00+08:00",
                    "compile error in module foo",
                ),
            ),
        )

        migrated = poll_tracker(legacy, visible)
        repeated = poll_tracker(
            migrated.state,
            self.snapshot(
                scanned_at="2026-07-10T10:16:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:13:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:15:00+08:00",
                        "compile error in module foo",
                    ),
                ),
            ),
        )

        self.assertEqual((), migrated.notifications)
        self.assertTrue(
            any(
                key.startswith(PROBLEM_FINGERPRINT_PREFIX)
                for key in migrated.state.notified_failure_keys
            )
        )
        self.assertEqual((), repeated.notifications)

    def test_same_cleaned_failure_summary_is_not_repeated_across_rounds(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        first = poll_tracker(
            baseline,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:06:00+08:00",
                        "2026-07-10 10:06:00 | compile error in module foo",
                    ),
                ),
            ),
        )
        repeated = poll_tracker(
            first.state,
            self.snapshot(
                scanned_at="2026-07-10T10:13:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:12:00+08:00",
                        "2026-07-10 10:12:00 | compile error in module foo",
                    ),
                ),
            ),
        )

        self.assertEqual(1, len(first.notifications))
        self.assertEqual((), repeated.notifications)

    def test_same_summary_from_a_different_gate_is_a_new_problem(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        build_failure = poll_tracker(
            baseline,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:06:00+08:00",
                        "shared failure summary",
                    ),
                ),
            ),
        )
        merge_failure = poll_tracker(
            build_failure.state,
            self.snapshot(
                scanned_at="2026-07-10T10:13:00+08:00",
                command="/ci merge",
                command_key="merge-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/dev-cloud-preflight",
                        "2026-07-10T10:12:00+08:00",
                        "shared failure summary",
                    ),
                ),
            ),
        )

        self.assertEqual(
            ["taichu/dev-cloud-preflight"],
            [failure.context for failure in merge_failure.notifications],
        )

    def test_new_failure_summary_is_the_only_problem_in_the_next_message(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        first = poll_tracker(
            baseline,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:06:00+08:00",
                        "approval missing",
                    ),
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:07:00+08:00",
                        "compile error in module foo",
                    ),
                ),
            ),
        )
        changed = poll_tracker(
            first.state,
            self.snapshot(
                scanned_at="2026-07-10T10:13:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:11:00+08:00",
                        "approval still missing",
                    ),
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:12:00+08:00",
                        "unit test failed in module bar",
                    ),
                ),
            ),
        )

        self.assertEqual(2, len(first.notifications))
        self.assertEqual(
            ["taichu/pr-build"],
            [failure.context for failure in changed.notifications],
        )
        self.assertEqual("unit test failed in module bar", changed.notifications[0].summary)

    def test_late_new_problem_can_notify_in_the_next_command_round(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        first = poll_tracker(
            baseline,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:06:00+08:00",
                        "approval missing",
                    ),
                ),
            ),
        )
        late = poll_tracker(
            first.state,
            self.snapshot(
                scanned_at="2026-07-10T10:11:00+08:00",
                failures=(
                    GateFailure(
                        "protected-file-approval",
                        "2026-07-10T10:06:00+08:00",
                        "approval missing",
                    ),
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:10:00+08:00",
                        "unit test failed in module bar",
                    ),
                ),
            ),
        )
        next_round = poll_tracker(
            late.state,
            self.snapshot(
                scanned_at="2026-07-10T10:16:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:13:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:15:00+08:00",
                        "unit test failed in module bar",
                    ),
                ),
            ),
        )

        self.assertEqual((), late.notifications)
        self.assertEqual(
            ["taichu/pr-build"],
            [failure.context for failure in next_round.notifications],
        )

    def test_seen_problem_does_not_consume_a_new_rounds_notification(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        first = poll_tracker(
            baseline,
            self.snapshot(
                scanned_at="2026-07-10T10:08:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:06:00+08:00",
                        "compile error in module foo",
                    ),
                ),
            ),
        )
        duplicate_only = poll_tracker(
            first.state,
            self.snapshot(
                scanned_at="2026-07-10T10:13:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:12:00+08:00",
                        "compile error in module foo",
                    ),
                ),
            ),
        )
        genuinely_new = poll_tracker(
            duplicate_only.state,
            self.snapshot(
                scanned_at="2026-07-10T10:16:00+08:00",
                command_key="cmd-2",
                command_at="2026-07-10T10:10:00+08:00",
                failures=(
                    GateFailure(
                        "taichu/pr-build",
                        "2026-07-10T10:15:00+08:00",
                        "unit test failed in module bar",
                    ),
                ),
            ),
        )

        self.assertEqual((), duplicate_only.notifications)
        self.assertEqual(
            ["unit test failed in module bar"],
            [failure.summary for failure in genuinely_new.notifications],
        )

    def test_new_build_does_not_reuse_old_precondition_failures(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-11T15:15:00+08:00",
                command_key="build-0926",
                command_at="2026-07-11T09:26:43+08:00",
            ),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-11T15:18:59+08:00",
            command_key="build-1517",
            command_at="2026-07-11T15:17:18+08:00",
            failures=(
                GateFailure(
                    "protected-file-approval",
                    "2026-07-11T09:33:17+08:00",
                    "protected file approval missing",
                ),
                GateFailure(
                    "taichu/codex-pr-review",
                    "2026-07-11T09:36:15+08:00",
                    "Codex found 3 P0/P1 principle issues",
                ),
                GateFailure(
                    "taichu/pr-build",
                    "2026-07-11T09:40:21+08:00",
                    "PR build failed",
                ),
            ),
        )

        result = poll_tracker(baseline, changed)

        self.assertEqual((), result.notifications)

        current_round_failure = self.snapshot(
            scanned_at="2026-07-11T15:22:00+08:00",
            command_key="build-1517",
            command_at="2026-07-11T15:17:18+08:00",
            failures=(
                changed.failures[1],
                GateFailure(
                    "protected-file-approval",
                    "2026-07-11T15:21:23+08:00",
                    "protected file approval missing",
                ),
            ),
        )

        notified = poll_tracker(result.state, current_round_failure)

        self.assertEqual(
            ["protected-file-approval"],
            [item.context for item in notified.notifications],
        )

    def test_release_build_and_merge_use_only_their_three_available_gates(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-10T10:05:00+08:00",
                base_ref="Br_develop_device_release",
            ),
        ).state
        build_complete = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            base_ref="Br_develop_device_release",
            failures=(
                GateFailure(
                    "taichu/codex-pr-review",
                    "2026-07-10T10:07:00+08:00",
                    "release does not run this gate",
                ),
            ),
            gate_results=(
                self.gate(
                    "protected-file-approval",
                    "success",
                    "2026-07-10T09:55:00+08:00",
                ),
                self.gate(
                    "taichu/pr-build",
                    "success",
                    "2026-07-10T10:07:00+08:00",
                ),
            ),
        )

        build_result = poll_tracker(baseline, build_complete)

        self.assertEqual((), build_result.notifications)
        self.assertFalse(build_result.merge_success)

        merge_complete = self.snapshot(
            scanned_at="2026-07-10T10:11:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:09:00+08:00",
            base_ref="Br_develop_device_release",
            merged=True,
            merged_at="2026-07-10T10:10:30+08:00",
            failures=(
                GateFailure(
                    "taichu/dev-cloud-preflight",
                    "2026-07-10T10:10:00+08:00",
                    "release does not run this gate",
                ),
            ),
            gate_results=(
                self.gate(
                    "ci/merge-gate",
                    "success",
                    "2026-07-10T10:10:00+08:00",
                ),
            ),
        )

        merge_result = poll_tracker(build_result.state, merge_complete)

        self.assertTrue(merge_result.merge_success)
        self.assertEqual((), merge_result.notifications)

    def test_codex_test_review_is_in_main_build_but_not_merge_or_release(self):
        failure = GateFailure(
            "taichu/codex-pr-test-review",
            "2026-07-16T16:35:24+08:00",
            "发现 1 个 P0/P1 测试审查问题",
        )

        main_baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-16T16:31:00+08:00"),
        ).state
        main_build = poll_tracker(
            main_baseline,
            self.snapshot(
                scanned_at="2026-07-16T16:36:00+08:00",
                failures=(failure,),
            ),
        )
        self.assertEqual(
            ["taichu/codex-pr-test-review"],
            [item.context for item in main_build.notifications],
        )

        merge_baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-16T16:31:00+08:00",
                command="/ci merge",
                command_key="merge-1",
            ),
        ).state
        merge_result = poll_tracker(
            merge_baseline,
            self.snapshot(
                scanned_at="2026-07-16T16:36:00+08:00",
                command="/ci merge",
                command_key="merge-1",
                failures=(failure,),
            ),
        )
        self.assertEqual((), merge_result.notifications)

        release_baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-16T16:31:00+08:00",
                base_ref="Br_develop_device_release",
            ),
        ).state
        release_result = poll_tracker(
            release_baseline,
            self.snapshot(
                scanned_at="2026-07-16T16:36:00+08:00",
                base_ref="Br_develop_device_release",
                failures=(failure,),
            ),
        )
        self.assertEqual((), release_result.notifications)

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
            merged=True,
            merged_at="2026-07-10T10:10:00+08:00",
        )

        failure_result = poll_tracker(baseline, failed)
        success_result = poll_tracker(failure_result.state, recovered)
        repeated = poll_tracker(success_result.state, recovered)

        self.assertEqual(1, len(failure_result.notifications))
        self.assertTrue(success_result.merge_success)
        self.assertFalse(repeated.merge_success)
        self.assertTrue(
            any(
                key.startswith(PROBLEM_FINGERPRINT_PREFIX)
                for key in success_result.state.notified_failure_keys
            )
        )

    def test_merge_gate_success_waits_for_actual_gitea_merge(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(
                scanned_at="2026-07-10T10:05:00+08:00",
                command="/ci merge",
                command_key="merge-1",
                command_at="2026-07-10T10:00:00+08:00",
                merged=False,
            ),
        ).state
        gate_complete_but_open = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:00:00+08:00",
            gate_results=self.merge_successes("2026-07-10T10:07:00+08:00"),
            merged=False,
        )

        gate_result = poll_tracker(baseline, gate_complete_but_open)

        self.assertFalse(gate_result.merge_success)
        self.assertEqual(frozenset(), gate_result.state.notified_failure_keys)

        actually_merged = self.snapshot(
            scanned_at="2026-07-10T10:11:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:00:00+08:00",
            gate_results=self.merge_successes("2026-07-10T10:07:00+08:00"),
            failures=(
                GateFailure(
                    "ci/merge-gate",
                    "2026-07-10T10:08:00+08:00",
                    "stale failure superseded by actual merge",
                ),
            ),
            merged=True,
            merged_at="2026-07-10T10:09:00+08:00",
        )

        merged_result = poll_tracker(gate_result.state, actually_merged)
        repeated = poll_tracker(merged_result.state, actually_merged)

        self.assertTrue(merged_result.merge_success)
        self.assertEqual((), merged_result.notifications)
        self.assertFalse(repeated.merge_success)

    def test_observed_open_pr_merge_is_not_lost_to_an_older_merged_at_watermark(self):
        observed_open = TrackerState(
            "merge-1",
            frozenset(),
            True,
            "2026-07-10T10:10:00+08:00",
        )
        terminal = self.snapshot(
            scanned_at="2026-07-10T10:13:00+08:00",
            command="/ci merge",
            command_key="merge-1",
            command_at="2026-07-10T10:00:00+08:00",
            merged=True,
            merged_at="2026-07-10T10:09:00+08:00",
        )

        result = poll_tracker(observed_open, terminal)

        self.assertTrue(result.merge_success)

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
