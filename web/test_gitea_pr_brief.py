import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitea_pr_brief as brief


class GiteaPrBriefTest(unittest.TestCase):
    def test_parse_pr_url_should_extract_owner_repo_and_number(self):
        parsed = brief.parse_pr_selector(
            "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1222"
        )

        self.assertEqual(parsed.owner, "SystemAgentDev")
        self.assertEqual(parsed.repo, "TaiChu")
        self.assertEqual(parsed.number, 1222)

    def test_latest_statuses_should_keep_newest_entry_per_context(self):
        statuses = [
            {
                "context": "taichu/pr-build",
                "state": "failure",
                "created_at": "2026-07-09T01:00:00Z",
                "description": "old failure",
            },
            {
                "context": "taichu/pr-build",
                "state": "success",
                "created_at": "2026-07-09T02:00:00Z",
                "description": "fixed",
            },
            {
                "context": "ci/merge-gate",
                "status": "error",
                "updated_at": "2026-07-09T03:00:00Z",
                "description": "coverage failed",
            },
        ]

        latest = brief.latest_statuses_by_context(statuses)

        self.assertEqual(latest["taichu/pr-build"]["state"], "success")
        self.assertEqual(latest["taichu/pr-build"]["description"], "fixed")
        self.assertEqual(latest["ci/merge-gate"]["state"], "error")

    def test_gate_items_should_hide_success_and_old_failures(self):
        latest_statuses = {
            "protected-file-approval": {
                "context": "protected-file-approval",
                "state": "success",
                "description": "approved",
            },
            "taichu/dev-cloud-preflight": {
                "context": "taichu/dev-cloud-preflight",
                "state": "failure",
                "description": "cloud-smoke failed",
                "target_url": "https://jenkins/job/1",
            },
        }

        gates = brief.gate_items(latest_statuses, [])

        self.assertEqual([item["context"] for item in gates], ["taichu/dev-cloud-preflight"])
        self.assertEqual(gates[0]["state"], "failure")
        self.assertIn("cloud-smoke failed", gates[0]["summary"])

    def test_gate_items_should_not_attach_comments_to_pending_status(self):
        latest_statuses = {
            "ci/merge-gate": {
                "context": "ci/merge-gate",
                "state": "pending",
                "description": "merge gate running",
            },
        }
        comments = [
            {
                "body": "### TaiChu 云侧 Preflight：通过\n内网 merge-gate run",
                "updated_at": "2026-07-09T03:00:00Z",
            }
        ]

        gates = brief.gate_items(latest_statuses, comments)

        self.assertEqual(gates[0]["summary"], "merge gate running")
        self.assertEqual(gates[0]["comment_url"], "")

    def test_gate_items_should_accept_protected_file_alias(self):
        latest_statuses = {
            "taichu/protected-file-approval": {
                "context": "taichu/protected-file-approval",
                "state": "failure",
                "description": "approval missing",
            },
        }

        gates = brief.gate_items(latest_statuses, [])

        self.assertEqual(gates[0]["context"], "protected-file-approval")
        self.assertEqual(gates[0]["summary"], "approval missing")

    def test_codex_test_review_failure_uses_its_own_rollout_comment(self):
        latest_statuses = {
            "taichu/codex-pr-review": {
                "context": "taichu/codex-pr-review",
                "status": "failure",
                "description": "Codex found 1 P0/P1 principle issue(s)",
            },
            "taichu/codex-pr-test-review": {
                "context": "taichu/codex-pr-test-review",
                "status": "failure",
                "description": "Codex found 1 P0/P1 test review issue(s)",
            },
        }
        comments = [
            {
                "body": (
                    "<!-- taichu-codex-pr-review -->\n"
                    "### Codex PR Review\n"
                    "#### 原则问题\n- P1 production issue"
                ),
                "html_url": "https://example.test/code-review",
                "updated_at": "2026-07-16T16:20:00+08:00",
            },
            {
                "body": (
                    "<!-- taichu-codex-pr-test-review -->\n"
                    "<!-- taichu-codex-pr-review-head:abcdef123456 -->\n"
                    "### Codex PR Review\n"
                    "| Status | `taichu/codex-pr-test-review` = `failure` |\n"
                    "#### 原则问题\n- P1 missing regression test"
                ),
                "html_url": "https://example.test/test-review",
                "updated_at": "2026-07-16T16:21:00+08:00",
            },
        ]

        gates = brief.gate_items(latest_statuses, comments)

        self.assertEqual(
            ["taichu/codex-pr-review", "taichu/codex-pr-test-review"],
            [item["context"] for item in gates],
        )
        self.assertEqual(
            "https://example.test/code-review",
            gates[0]["comment_url"],
        )
        self.assertEqual(
            "https://example.test/test-review",
            gates[1]["comment_url"],
        )

    def test_codex_test_review_success_is_hidden_and_absence_is_not_synthesized(self):
        successful = brief.gate_items(
            {
                "taichu/codex-pr-test-review": {
                    "context": "taichu/codex-pr-test-review",
                    "status": "success",
                    "description": "Codex found no P0/P1 test-validation issues",
                }
            },
            [],
        )
        legacy = brief.gate_items(
            {
                "taichu/pr-build": {
                    "context": "taichu/pr-build",
                    "status": "success",
                    "description": "build success",
                }
            },
            [],
        )

        self.assertEqual([], successful)
        self.assertEqual([], legacy)

    def test_codex_test_review_does_not_attach_an_old_head_comment(self):
        gates = brief.gate_items(
            {
                "taichu/codex-pr-test-review": {
                    "context": "taichu/codex-pr-test-review",
                    "status": "failure",
                    "description": "Codex found 1 P0/P1 test review issue(s)",
                }
            },
            [
                {
                    "body": (
                        "<!-- taichu-codex-pr-test-review -->\n"
                        "<!-- taichu-codex-pr-test-review-head:"
                        "aaaaaa1234567890 -->\n"
                        "#### 原则问题\n- P1 old-head-only detail"
                    ),
                    "html_url": "https://example.test/old-head",
                }
            ],
            "bbbbbb1234567890",
        )

        self.assertEqual(1, len(gates))
        self.assertEqual("", gates[0]["comment_url"])
        self.assertNotIn("old-head-only detail", gates[0]["summary"])

    def test_queue_events_should_prefer_recent_ci_comments(self):
        comments = [
            {
                "body": "ordinary review note",
                "created_at": "2026-07-09T01:00:00Z",
                "user": {"login": "alice"},
            },
            {
                "body": "/ci build queued: waiting for executor",
                "created_at": "2026-07-09T03:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            },
            {
                "body": "ci ingest stale_input for old sha",
                "updated_at": "2026-07-09T02:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            },
        ]

        events = brief.queue_events(comments, limit=2)

        self.assertEqual(len(events), 2)
        self.assertIn("queued", events[0]["summary"])
        self.assertEqual(events[0]["author"], "taichu-ci-bot")

    def test_queue_events_should_hide_success_gate_comments(self):
        comments = [
            {
                "body": "### Codex PR Review\n| Status | `taichu/codex-pr-review` = `success` |\n| Jenkins | https://example.invalid |",
                "updated_at": "2026-07-09T03:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            },
            {
                "body": "### TaiChu merge gate：排队状态\n**你的状态**：**正在执行 merge gate**",
                "updated_at": "2026-07-09T02:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            },
        ]

        events = brief.queue_events(comments, limit=3)

        self.assertEqual(len(events), 1)
        self.assertIn("排队状态", events[0]["summary"])

    def test_queue_events_should_hide_build_timing_comments(self):
        comments = [
            {
                "body": "说明：本条为 **TaiChu PR build** **build-timing**（构建阶段耗时表）\n阶段等待 12s",
                "updated_at": "2026-07-09T03:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            },
            {
                "body": "### TaiChu split build：等待已结束\n**当前状态**：槽位等待与 split 编译流程**成功**。",
                "updated_at": "2026-07-09T02:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            },
        ]

        self.assertEqual(brief.queue_events(comments, limit=3), [])

    def test_queue_events_should_hide_success_preflight_comments(self):
        comments = [
            {
                "body": "### TaiChu 云侧 Preflight：通过\n| Gitea Status | `taichu/dev-cloud-preflight` = `通过` |\n**当前正在执行 merge gate**",
                "updated_at": "2026-07-09T03:00:00Z",
                "user": {"login": "taichu-ci-bot"},
            }
        ]

        self.assertEqual(brief.queue_events(comments, limit=3), [])

    def test_failure_summary_should_skip_passing_rows_with_error_words(self):
        text = """
| 用例 | 检查点 | 结果 | 说明/失败原因 |
| AS 路由错误 | 覆盖未知 receiver | PASS | 用例通过 |
| 云侧 Smoke 总体 | 核心接口闭环 | FAIL | assistant reply timeout |
"""

        summary = brief.summarize_failure_text(text)

        self.assertNotIn("AS 路由错误", summary)
        self.assertIn("assistant reply timeout", summary)

    def test_summaries_should_strip_gitea_markup(self):
        summary = brief.summarize_comment(
            '### TaiChu PR build：排队状态\n<p><strong style="color:#d1242f;">本轮更新</strong></p>\n**目标分支**：`main`\n- [PR #1287](https://example.invalid/pr/1287) — [Jenkins](https://example.invalid/job)'
        )

        self.assertIn("TaiChu PR build：排队状态", summary)
        self.assertIn("本轮更新", summary)
        self.assertIn("目标分支：main", summary)
        self.assertIn("PR #1287 — Jenkins", summary)
        self.assertNotIn("https://example.invalid", summary)
        self.assertNotIn("<strong", summary)
        self.assertNotIn("**", summary)

    def test_dashboard_should_show_arbitrary_pr_jump_control(self):
        html = brief.dashboard_html(brief.PrSelector("SystemAgentDev", "TaiChu", 1287))

        self.assertIn('value="1287"', html)
        self.assertIn('const prNumber = 1287;', html)
        self.assertIn("支持任意 TaiChu PR 编号", html)
        self.assertIn("window.location.href = `/pr/${value}`;", html)

    def test_queue_events_should_keep_only_latest_exact_ci_command(self):
        comments = [
            {
                "body": "/ci build",
                "updated_at": "2026-07-09T03:00:00Z",
                "user": {"login": "youngandboor"},
            },
            {
                "body": "/ci build",
                "updated_at": "2026-07-09T02:00:00Z",
                "user": {"login": "youngandboor"},
            },
            {
                "body": "/ci merge",
                "updated_at": "2026-07-09T01:00:00Z",
                "user": {"login": "youngandboor"},
            },
        ]

        events = brief.queue_events(comments, limit=5)

        self.assertEqual([event["summary"] for event in events], ["/ci build", "/ci merge"])


if __name__ == "__main__":
    unittest.main()
