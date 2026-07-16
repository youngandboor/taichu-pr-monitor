package fun.taichu.prmonitor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;

import org.junit.Test;

public class CiFailureTrackerTest {
    @Test
    public void foregroundBaselinePreventsOldFailuresFromAlerting() {
        CiFailureTracker.State state = new CiFailureTracker.State("", new HashSet<>());
        CiFailureTracker.Snapshot foreground = snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:10:00+08:00",
                "cmd-1",
                failure("taichu/codex-pr-review", "2026-07-09T20:11:00+08:00", "Codex found issue"));

        state = CiFailureTracker.foregroundBaseline(state, foreground);
        CiFailureTracker.Result result = CiFailureTracker.backgroundPoll(state, foreground);

        assertTrue(result.notifications.isEmpty());
        assertEquals(1, result.state.notifiedFailureKeys.size());
    }

    @Test
    public void backgroundNewGateFailureAlertsOncePerGate() {
        CiFailureTracker.State state = new CiFailureTracker.State("", new HashSet<>());
        CiFailureTracker.Snapshot foreground = snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:10:00+08:00",
                "cmd-1",
                failure("taichu/codex-pr-review", "2026-07-09T20:11:00+08:00", "Codex found issue"));
        state = CiFailureTracker.foregroundBaseline(state, foreground);

        CiFailureTracker.Snapshot background = snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:10:00+08:00",
                "cmd-1",
                failure("taichu/codex-pr-review", "2026-07-09T20:11:00+08:00", "Codex found issue"),
                failure("protected-file-approval", "2026-07-09T20:20:00+08:00", "approval missing"));

        CiFailureTracker.Result firstPoll = CiFailureTracker.backgroundPoll(state, background);
        CiFailureTracker.Result secondPoll = CiFailureTracker.backgroundPoll(firstPoll.state, background);

        assertEquals(1, firstPoll.notifications.size());
        assertEquals("protected-file-approval", firstPoll.notifications.get(0).context);
        assertTrue(secondPoll.notifications.isEmpty());
    }

    @Test
    public void foregroundRefreshAfterBaselineAlertsNewFailuresToo() {
        CiFailureTracker.State state = new CiFailureTracker.State("", new HashSet<>());
        state = CiFailureTracker.initializeBaseline(state, snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:10:00+08:00",
                "cmd-1",
                failure("taichu/codex-pr-review", "2026-07-09T20:11:00+08:00", "Codex found issue")));

        CiFailureTracker.Snapshot foregroundRefresh = snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:10:00+08:00",
                "cmd-1",
                failure("taichu/codex-pr-review", "2026-07-09T20:11:00+08:00", "Codex found issue"),
                failure("protected-file-approval", "2026-07-09T20:20:00+08:00", "approval missing"));

        CiFailureTracker.Result result = CiFailureTracker.poll(state, foregroundRefresh);

        assertEquals(1, result.notifications.size());
        assertEquals("protected-file-approval", result.notifications.get(0).context);
    }

    @Test
    public void newCiCommandInBackgroundAlertsAllCurrentGateFailuresByGate() {
        CiFailureTracker.State state = new CiFailureTracker.State("", new HashSet<>());
        state = CiFailureTracker.foregroundBaseline(state, snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:10:00+08:00",
                "cmd-1"));

        CiFailureTracker.Snapshot background = snapshot(
                1315,
                "/ci merge",
                "2026-07-09T20:30:00+08:00",
                "cmd-2",
                failure("taichu/dev-cloud-preflight", "2026-07-09T20:31:00+08:00", "preflight failed"),
                failure("ci/merge-gate", "2026-07-09T20:32:00+08:00", "merge gate failed"));

        CiFailureTracker.Result result = CiFailureTracker.backgroundPoll(state, background);

        assertEquals(2, result.notifications.size());
        assertEquals("taichu/dev-cloud-preflight", result.notifications.get(0).context);
        assertEquals("ci/merge-gate", result.notifications.get(1).context);
    }

    @Test
    public void commandAppearingAfterMonitoringStartedAlertsFailures() {
        CiFailureTracker.State state = new CiFailureTracker.State("", new HashSet<>());
        CiFailureTracker.Result noCommandPoll = CiFailureTracker.poll(state, snapshot(
                1315,
                "",
                "",
                ""));

        CiFailureTracker.Result commandPoll = CiFailureTracker.poll(noCommandPoll.state, snapshot(
                1315,
                "/ci build",
                "2026-07-09T20:30:00+08:00",
                "cmd-2",
                failure("taichu/pr-build", "2026-07-09T20:31:00+08:00", "build failed")));

        assertEquals(1, commandPoll.notifications.size());
        assertEquals("taichu/pr-build", commandPoll.notifications.get(0).context);
    }

    @Test
    public void switchingToAnotherPrBuildsBaselineWithoutOldAlerts() {
        CiFailureTracker.State stateForNewPr = new CiFailureTracker.State("", new HashSet<>(), false, "");

        CiFailureTracker.Result result = CiFailureTracker.poll(stateForNewPr, snapshot(
                1222,
                "/ci merge",
                "2026-07-09T20:30:00+08:00",
                "pr1222-cmd-1",
                "2026-07-09T22:00:00+08:00",
                failure("ci/merge-gate", "2026-07-09T20:35:00+08:00", "merge gate failed")));

        assertTrue(result.notifications.isEmpty());
        assertEquals("2026-07-09T22:00:00+08:00", result.state.lastScannedAt);
    }

    @Test
    public void scanWatermarkPreventsParserUpgradeFromAlertingOldFailures() {
        CiFailureTracker.State state = new CiFailureTracker.State(
                "cmd-1",
                new HashSet<>(),
                true,
                "2026-07-09T22:00:00+08:00");

        CiFailureTracker.Result result = CiFailureTracker.poll(state, snapshot(
                1222,
                "/ci merge",
                "2026-07-09T20:30:00+08:00",
                "cmd-1",
                "2026-07-09T22:05:00+08:00",
                failure("taichu/dev-cloud-preflight", "2026-07-09T20:55:59+08:00", "执行结果：失败")));

        assertTrue(result.notifications.isEmpty());
        assertEquals("2026-07-09T22:05:00+08:00", result.state.lastScannedAt);
    }

    @Test
    public void failuresAfterWatermarkStillAlert() {
        CiFailureTracker.State state = new CiFailureTracker.State(
                "cmd-1",
                new HashSet<>(),
                true,
                "2026-07-09T22:00:00+08:00");

        CiFailureTracker.Result result = CiFailureTracker.poll(state, snapshot(
                1222,
                "/ci merge",
                "2026-07-09T20:30:00+08:00",
                "cmd-1",
                "2026-07-09T22:05:00+08:00",
                failure("ci/merge-gate", "2026-07-09T22:01:00+08:00", "merge gate failed")));

        assertEquals(1, result.notifications.size());
        assertEquals("ci/merge-gate", result.notifications.get(0).context);
    }

    @Test
    public void failureAtWatermarkDoesNotAlertAgain() {
        CiFailureTracker.State state = new CiFailureTracker.State(
                "cmd-1",
                new HashSet<>(),
                true,
                "2026-07-09T22:00:00+08:00");

        CiFailureTracker.Result result = CiFailureTracker.poll(state, snapshot(
                1222,
                "/ci merge",
                "2026-07-09T20:30:00+08:00",
                "cmd-1",
                "2026-07-09T22:05:00+08:00",
                failure("ci/merge-gate", "2026-07-09T22:00:00+08:00", "merge gate failed")));

        assertTrue(result.notifications.isEmpty());
    }

    @Test
    public void codexTestReviewOnlyAlertsDuringMainBuild() {
        CiFailureTracker.GateFailure testReview = failure(
                "taichu/codex-pr-test-review",
                "2026-07-16T16:35:25+08:00",
                "Codex found 1 P0/P1 test review issue(s)");

        CiFailureTracker.Result mainBuild = CiFailureTracker.poll(
                new CiFailureTracker.State(
                        "build-1",
                        new HashSet<>(),
                        true,
                        "2026-07-16T16:31:00+08:00"),
                snapshotForBase("/ci build", "build-1", "main", testReview));
        CiFailureTracker.Result mainMerge = CiFailureTracker.poll(
                new CiFailureTracker.State(
                        "merge-1",
                        new HashSet<>(),
                        true,
                        "2026-07-16T16:31:00+08:00"),
                snapshotForBase("/ci merge", "merge-1", "main", testReview));
        CiFailureTracker.Result releaseBuild = CiFailureTracker.poll(
                new CiFailureTracker.State(
                        "build-1",
                        new HashSet<>(),
                        true,
                        "2026-07-16T16:31:00+08:00"),
                snapshotForBase(
                        "/ci build",
                        "build-1",
                        "Br_develop_device_release",
                        testReview));

        assertEquals(1, mainBuild.notifications.size());
        assertEquals(
                "taichu/codex-pr-test-review",
                mainBuild.notifications.get(0).context);
        assertTrue(mainMerge.notifications.isEmpty());
        assertTrue(releaseBuild.notifications.isEmpty());
    }

    private static CiFailureTracker.Snapshot snapshotForBase(
            String command,
            String commandKey,
            String baseRef,
            CiFailureTracker.GateFailure... failures
    ) {
        return new CiFailureTracker.Snapshot(
                1516,
                command,
                "2026-07-16T16:31:01+08:00",
                commandKey,
                "2026-07-16T16:36:00+08:00",
                baseRef,
                Arrays.asList(failures));
    }

    private static CiFailureTracker.Snapshot snapshot(
            int prNumber,
            String command,
            String commandAt,
            String commandKey,
            CiFailureTracker.GateFailure... failures
    ) {
        return new CiFailureTracker.Snapshot(
                prNumber,
                command,
                commandAt,
                commandKey,
                "",
                failures.length == 0 ? Collections.emptyList() : Arrays.asList(failures));
    }

    private static CiFailureTracker.Snapshot snapshot(
            int prNumber,
            String command,
            String commandAt,
            String commandKey,
            String scannedAt,
            CiFailureTracker.GateFailure... failures
    ) {
        return new CiFailureTracker.Snapshot(
                prNumber,
                command,
                commandAt,
                commandKey,
                scannedAt,
                failures.length == 0 ? Collections.emptyList() : Arrays.asList(failures));
    }

    private static CiFailureTracker.GateFailure failure(String context, String updatedAt, String summary) {
        return new CiFailureTracker.GateFailure(context, updatedAt, summary);
    }
}
