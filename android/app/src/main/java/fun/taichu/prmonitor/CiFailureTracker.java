package fun.taichu.prmonitor;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

final class CiFailureTracker {
    private CiFailureTracker() {
    }

    static State initializeBaseline(State state, Snapshot snapshot) {
        if (snapshot.latestCiCommandKey.isEmpty()) {
            return new State(state.observedCommandKey, state.notifiedFailureKeys, true, scanWatermark(state, snapshot));
        }
        Set<String> notified = snapshot.latestCiCommandKey.equals(state.observedCommandKey)
                ? new HashSet<>(state.notifiedFailureKeys)
                : new HashSet<>();
        for (GateFailure failure : failuresAfterCommand(snapshot)) {
            notified.add(failureKey(snapshot, failure));
        }
        return new State(snapshot.latestCiCommandKey, notified, true, scanWatermark(state, snapshot));
    }

    static State foregroundBaseline(State state, Snapshot snapshot) {
        return initializeBaseline(state, snapshot);
    }

    static Result poll(State state, Snapshot snapshot) {
        if (snapshot.latestCiCommandKey.isEmpty()) {
            return new Result(new State(state.observedCommandKey, state.notifiedFailureKeys, true, scanWatermark(state, snapshot)), new ArrayList<>());
        }
        if (!state.initialized) {
            return new Result(initializeBaseline(state, snapshot), new ArrayList<>());
        }

        Set<String> notified = snapshot.latestCiCommandKey.equals(state.observedCommandKey)
                ? new HashSet<>(state.notifiedFailureKeys)
                : new HashSet<>();
        List<GateFailure> notifications = new ArrayList<>();
        for (GateFailure failure : failuresAfterCommand(snapshot)) {
            if (!state.lastScannedAt.isEmpty() && !happenedAfterScan(failure.updatedAt, state.lastScannedAt)) {
                notified.add(failureKey(snapshot, failure));
                continue;
            }
            String key = failureKey(snapshot, failure);
            if (notified.contains(key)) {
                continue;
            }
            notified.add(key);
            notifications.add(failure);
        }
        return new Result(new State(snapshot.latestCiCommandKey, notified, true, scanWatermark(state, snapshot)), notifications);
    }

    static Result backgroundPoll(State state, Snapshot snapshot) {
        return poll(state, snapshot);
    }

    static String failureKey(Snapshot snapshot, GateFailure failure) {
        return snapshot.latestCiCommandKey + ":"
                + failure.context + ":"
                + failure.updatedAt + ":"
                + notificationText(failure.summary);
    }

    private static List<GateFailure> failuresAfterCommand(Snapshot snapshot) {
        List<GateFailure> result = new ArrayList<>();
        for (GateFailure failure : snapshot.failures) {
            if (happenedAfter(failure.updatedAt, snapshot.latestCiCommandAt)) {
                result.add(failure);
            }
        }
        return result;
    }

    private static boolean happenedAfter(String eventAt, String commandAt) {
        return eventAt == null || eventAt.isEmpty()
                || commandAt == null || commandAt.isEmpty()
                || eventAt.compareTo(commandAt) >= 0;
    }

    private static boolean happenedAfterScan(String eventAt, String lastScannedAt) {
        return eventAt == null || eventAt.isEmpty()
                || lastScannedAt == null || lastScannedAt.isEmpty()
                || eventAt.compareTo(lastScannedAt) > 0;
    }

    private static String scanWatermark(State state, Snapshot snapshot) {
        if (snapshot.scannedAt != null && !snapshot.scannedAt.isEmpty()) {
            return snapshot.scannedAt;
        }
        String watermark = state.lastScannedAt;
        if (snapshot.latestCiCommandAt != null && snapshot.latestCiCommandAt.compareTo(watermark) > 0) {
            watermark = snapshot.latestCiCommandAt;
        }
        for (GateFailure failure : snapshot.failures) {
            if (failure.updatedAt.compareTo(watermark) > 0) {
                watermark = failure.updatedAt;
            }
        }
        return watermark;
    }

    static String notificationText(String value) {
        String text = value == null ? "" : value;
        text = text.replaceAll("(?s)<!--.*?-->", "")
                .replaceAll("(?s)<[^>]*>", "")
                .replace("&nbsp;", " ")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&")
                .replaceAll("(?m)^\\s*#+\\s*", "")
                .replace("**", "")
                .replace("__", "")
                .replace("`", "")
                .replaceAll("\\s+", " ")
                .trim();
        if (text.isEmpty()) {
            return "评论无可展示内容";
        }
        return text.length() <= 160 ? text : text.substring(0, 159).trim() + "...";
    }

    static final class State {
        final String observedCommandKey;
        final Set<String> notifiedFailureKeys;
        final boolean initialized;
        final String lastScannedAt;

        State(String observedCommandKey, Set<String> notifiedFailureKeys) {
            this(observedCommandKey, notifiedFailureKeys, !((observedCommandKey == null || observedCommandKey.isEmpty())
                    && notifiedFailureKeys.isEmpty()), "");
        }

        State(String observedCommandKey, Set<String> notifiedFailureKeys, boolean initialized) {
            this(observedCommandKey, notifiedFailureKeys, initialized, "");
        }

        State(String observedCommandKey, Set<String> notifiedFailureKeys, boolean initialized, String lastScannedAt) {
            this.observedCommandKey = observedCommandKey == null ? "" : observedCommandKey;
            this.notifiedFailureKeys = new HashSet<>(notifiedFailureKeys);
            this.initialized = initialized;
            this.lastScannedAt = lastScannedAt == null ? "" : lastScannedAt;
        }
    }

    static final class Snapshot {
        final int prNumber;
        final String latestCiCommand;
        final String latestCiCommandAt;
        final String latestCiCommandKey;
        final String scannedAt;
        final List<GateFailure> failures;

        Snapshot(
                int prNumber,
                String latestCiCommand,
                String latestCiCommandAt,
                String latestCiCommandKey,
                List<GateFailure> failures
        ) {
            this(prNumber, latestCiCommand, latestCiCommandAt, latestCiCommandKey, "", failures);
        }

        Snapshot(
                int prNumber,
                String latestCiCommand,
                String latestCiCommandAt,
                String latestCiCommandKey,
                String scannedAt,
                List<GateFailure> failures
        ) {
            this.prNumber = prNumber;
            this.latestCiCommand = latestCiCommand == null ? "" : latestCiCommand;
            this.latestCiCommandAt = latestCiCommandAt == null ? "" : latestCiCommandAt;
            this.latestCiCommandKey = latestCiCommandKey == null ? "" : latestCiCommandKey;
            this.scannedAt = scannedAt == null ? "" : scannedAt;
            this.failures = new ArrayList<>(failures);
        }
    }

    static final class GateFailure {
        final String context;
        final String updatedAt;
        final String summary;

        GateFailure(String context, String updatedAt, String summary) {
            this.context = context == null ? "" : context;
            this.updatedAt = updatedAt == null ? "" : updatedAt;
            this.summary = summary == null ? "" : summary;
        }
    }

    static final class Result {
        final State state;
        final List<GateFailure> notifications;

        Result(State state, List<GateFailure> notifications) {
            this.state = state;
            this.notifications = new ArrayList<>(notifications);
        }
    }
}
