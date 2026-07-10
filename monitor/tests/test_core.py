import unittest

from monitor.core import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    GateFailure,
    PrSnapshot,
    TrackerState,
    build_pr_snapshot,
    effective_state,
    notification_text,
    poll_tracker,
)


class GateLogicTest(unittest.TestCase):
    def test_failure_text_overrides_success_state_like_android(self):
        summary = "TaiChu merge gate: 执行结果：失败，Cloud Preflight 未通过"

        self.assertEqual("failure", effective_state("success", summary))

    def test_success_aliases_and_summary_signals_match_android(self):
        self.assertEqual("success", effective_state("passed", ""))
        self.assertEqual("success", effective_state("", "当前 head 该门禁已通过。"))

    def test_build_snapshot_keeps_only_latest_current_head_gate_failures(self):
        pr = {
            "number": 1222,
            "title": "Fix current failures",
            "html_url": "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1222",
            "user": {"login": "w00123"},
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
        self.assertEqual("/ci build", snapshot.latest_ci_command)
        self.assertEqual(
            "1222:/ci build:2026-07-10T10:00:00+08:00:9",
            snapshot.latest_ci_command_key,
        )
        self.assertEqual(
            ["taichu/codex-pr-review", "taichu/dev-cloud-preflight"],
            [failure.context for failure in snapshot.failures],
        )

    def test_notification_text_strips_markup_and_truncates(self):
        text = "<!--hidden-->## **失败摘要** <b>boom</b> " + ("x" * 200)

        cleaned = notification_text(text)

        self.assertNotIn("hidden", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertLessEqual(len(cleaned), 162)
        self.assertTrue(cleaned.endswith("..."))

    def test_default_poll_interval_is_three_minutes(self):
        self.assertEqual(180, DEFAULT_POLL_INTERVAL_SECONDS)


class TrackerTest(unittest.TestCase):
    def snapshot(self, *, scanned_at, command_key="cmd-1", command_at="2026-07-10T10:00:00+08:00", failures=()):
        return PrSnapshot(
            number=7,
            title="PR title",
            author="w00123",
            head_sha="abcdef123456",
            url="https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/7",
            latest_ci_command="/ci build" if command_key else "",
            latest_ci_command_at=command_at if command_key else "",
            latest_ci_command_key=command_key,
            scanned_at=scanned_at,
            failures=tuple(failures),
        )

    def test_first_poll_builds_baseline_without_historical_alerts(self):
        snapshot = self.snapshot(
            scanned_at="2026-07-10T10:05:00+08:00",
            failures=(GateFailure("taichu/pr-build", "2026-07-10T10:02:00+08:00", "failed"),),
        )

        result = poll_tracker(TrackerState.empty(), snapshot)

        self.assertEqual((), result.notifications)
        self.assertTrue(result.state.initialized)
        self.assertEqual(1, len(result.state.notified_failure_keys))

    def test_new_failure_after_watermark_alerts_once(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:08:00+08:00",
            failures=(GateFailure("taichu/pr-build", "2026-07-10T10:06:00+08:00", "build failed"),),
        )

        first = poll_tracker(baseline, changed)
        second = poll_tracker(first.state, changed)

        self.assertEqual(["taichu/pr-build"], [item.context for item in first.notifications])
        self.assertEqual((), second.notifications)

    def test_old_failure_before_latest_command_is_ignored(self):
        baseline = poll_tracker(
            TrackerState.empty(),
            self.snapshot(scanned_at="2026-07-10T10:05:00+08:00"),
        ).state
        changed = self.snapshot(
            scanned_at="2026-07-10T10:12:00+08:00",
            command_key="cmd-2",
            command_at="2026-07-10T10:10:00+08:00",
            failures=(GateFailure("ci/merge-gate", "2026-07-10T10:09:00+08:00", "old failure"),),
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
