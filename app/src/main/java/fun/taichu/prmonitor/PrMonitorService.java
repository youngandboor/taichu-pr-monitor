package fun.taichu.prmonitor;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

public class PrMonitorService extends Service {
    private static final String API_BASE = "https://taichu.fun/gitea/api/v1";
    private static final String OWNER = "SystemAgentDev";
    private static final String REPO = "TaiChu";
    private static final String STORE_NAME = "pr_monitor_auth";
    private static final String KEY_ACCESS_TOKEN = "oauth_access_token";
    private static final String KEY_MONITOR_PR_NUMBER = "monitor_pr_number";
    private static final String KEY_MONITOR_ENABLED = "monitor_enabled";
    private static final String KEY_OBSERVED_COMMAND_PREFIX = "observed_ci_command_";
    private static final String KEY_NOTIFIED_FAILURES_PREFIX = "notified_ci_failures_";
    private static final String KEY_TRACKER_INITIALIZED_PREFIX = "ci_tracker_initialized_";
    private static final String KEY_TRACKER_LAST_SCANNED_PREFIX = "ci_tracker_last_scanned_";
    private static final String KEY_MONITOR_LAST_POLL_AT = "monitor_last_poll_at";
    private static final String KEY_MONITOR_LAST_ERROR = "monitor_last_error";
    private static final String FAILURE_CHANNEL_ID = "ci_failures";
    private static final String STATUS_CHANNEL_ID = "pr_monitor_status";
    private static final int FOREGROUND_NOTIFICATION_ID = 1000;
    private static final long REFRESH_INTERVAL_MS = 60_000L;
    private static final String[] REQUIRED_GATES = {
            "protected-file-approval",
            "taichu/codex-pr-review",
            "taichu/pr-build",
            "taichu/dev-cloud-preflight",
            "ci/merge-gate"
    };

    private final Handler main = new Handler(Looper.getMainLooper());
    private ExecutorService executor;
    private SharedPreferences prefs;
    private Runnable pollRunnable;
    private boolean polling;
    private boolean active;

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences(STORE_NAME, MODE_PRIVATE);
        setupNotificationChannel();
        active = true;
        ensureExecutor();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        active = true;
        ensureExecutor();
        startForeground(FOREGROUND_NOTIFICATION_ID, foregroundNotification());
        prefs.edit().putBoolean(KEY_MONITOR_ENABLED, true).apply();
        schedulePoll(0);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        active = false;
        if (pollRunnable != null) {
            main.removeCallbacks(pollRunnable);
            pollRunnable = null;
        }
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void schedulePoll(long delayMs) {
        if (!active) {
            return;
        }
        if (pollRunnable != null) {
            main.removeCallbacks(pollRunnable);
        }
        pollRunnable = this::pollOnce;
        main.postDelayed(pollRunnable, delayMs);
    }

    private void pollOnce() {
        if (!active) {
            return;
        }
        if (polling) {
            schedulePoll(REFRESH_INTERVAL_MS);
            return;
        }
        polling = true;
        try {
            ensureExecutor().execute(() -> {
            try {
                startForeground(FOREGROUND_NOTIFICATION_ID, foregroundNotification());
                if (!prefs.getBoolean(KEY_MONITOR_ENABLED, false)) {
                    stopSelf();
                    return;
                }
                int prNumber = prefs.getInt(KEY_MONITOR_PR_NUMBER, 1);
                String token = prefs.getString(KEY_ACCESS_TOKEN, "");
                if (token == null || token.trim().isEmpty()) {
                    stopSelf();
                    return;
                }
                MonitorSummary summary = fetchSummary(prNumber, token.trim());
                notifyNewGateFailures(summary);
                prefs.edit()
                        .putString(KEY_MONITOR_LAST_POLL_AT, summary.fetchedAt)
                        .remove(KEY_MONITOR_LAST_ERROR)
                        .apply();
            } catch (Exception ignored) {
                prefs.edit()
                        .putString(KEY_MONITOR_LAST_POLL_AT, isoNow())
                        .putString(KEY_MONITOR_LAST_ERROR, ignored.getClass().getSimpleName() + ": " + valueOrEmpty(ignored.getMessage()))
                        .apply();
            } finally {
                polling = false;
                if (active && prefs.getBoolean(KEY_MONITOR_ENABLED, false)) {
                    main.post(() -> schedulePoll(REFRESH_INTERVAL_MS));
                }
            }
            });
        } catch (RejectedExecutionException error) {
            polling = false;
            if (active) {
                executor = null;
                schedulePoll(1000);
            }
        }
    }

    private ExecutorService ensureExecutor() {
        if (executor == null || executor.isShutdown() || executor.isTerminated()) {
            executor = Executors.newSingleThreadExecutor();
        }
        return executor;
    }

    private MonitorSummary fetchSummary(int prNumber, String token) throws Exception {
        JSONObject pr = requestObject(token, "/repos/" + OWNER + "/" + REPO + "/pulls/" + prNumber);
        JSONObject head = pr.optJSONObject("head");
        String headSha = head == null ? "" : head.optString("sha", "");
        if (headSha.isEmpty()) {
            throw new IOException("PR response has no head sha");
        }

        List<JSONObject> statuses = new ArrayList<>();
        try {
            statuses = requestArrayPages(token, "/repos/" + OWNER + "/" + REPO + "/statuses/" + headSha, 5);
        } catch (Exception ignored) {
            statuses = new ArrayList<>();
        }
        if (statuses.isEmpty()) {
            JSONObject combined = requestObject(token, "/repos/" + OWNER + "/" + REPO + "/commits/" + headSha + "/status");
            JSONArray arr = combined.optJSONArray("statuses");
            if (arr != null) {
                statuses = jsonArrayToList(arr);
            }
        }
        List<JSONObject> comments = requestArrayPages(token, "/repos/" + OWNER + "/" + REPO + "/issues/" + prNumber + "/comments", 3);

        MonitorSummary summary = new MonitorSummary();
        summary.number = prNumber;
        summary.headSha = headSha;
        summary.fetchedAt = isoNow();
        Map<String, GateStatus> latestByGate = new HashMap<>();
        for (JSONObject status : statuses) {
            String context = normalizeGateContext(status.optString("context", status.optString("name", "")));
            if (context.isEmpty()) {
                continue;
            }
            GateStatus gate = new GateStatus();
            gate.context = context;
            String rawState = firstNonEmpty(status.optString("state", ""), status.optString("status", ""));
            gate.summary = firstNonEmpty(status.optString("description", ""), rawState);
            gate.state = GateStateClassifier.effectiveState(rawState, gate.summary);
            gate.updatedAt = firstNonEmpty(status.optString("updated_at", ""), status.optString("created_at", ""));
            putLatest(latestByGate, gate);
        }
        for (JSONObject comment : comments) {
            updateLatestCiCommand(summary, comment);
            GateStatus gate = gateFromComment(comment, headSha);
            if (gate != null) {
                putLatest(latestByGate, gate);
            }
        }
        for (String context : REQUIRED_GATES) {
            GateStatus gate = latestByGate.get(context);
            if (gate != null && isActionableFailure(gate.state, gate.summary)) {
                summary.failures.add(gate);
            }
        }
        return summary;
    }

    private void notifyNewGateFailures(MonitorSummary summary) {
        if (summary.latestCiCommand.isEmpty() || summary.latestCiCommandKey.isEmpty()) {
            return;
        }
        String observedKeyName = KEY_OBSERVED_COMMAND_PREFIX + summary.number;
        String notifiedKeyName = KEY_NOTIFIED_FAILURES_PREFIX + summary.number;
        String initializedKeyName = KEY_TRACKER_INITIALIZED_PREFIX + summary.number;
        String lastScannedKeyName = KEY_TRACKER_LAST_SCANNED_PREFIX + summary.number;
        CiFailureTracker.State current = new CiFailureTracker.State(
                prefs.getString(observedKeyName, ""),
                prefs.getStringSet(notifiedKeyName, new HashSet<>()),
                prefs.getBoolean(initializedKeyName, false),
                prefs.getString(lastScannedKeyName, ""));
        CiFailureTracker.Result result = CiFailureTracker.backgroundPoll(current, notificationSnapshot(summary));
        for (CiFailureTracker.GateFailure failure : result.notifications) {
            postGateFailureNotification(summary, failure);
        }
        prefs.edit()
                .putString(observedKeyName, result.state.observedCommandKey)
                .putStringSet(notifiedKeyName, result.state.notifiedFailureKeys)
                .putBoolean(initializedKeyName, result.state.initialized)
                .putString(lastScannedKeyName, result.state.lastScannedAt)
                .apply();
    }

    private CiFailureTracker.Snapshot notificationSnapshot(MonitorSummary summary) {
        List<CiFailureTracker.GateFailure> failures = new ArrayList<>();
        for (GateStatus gate : summary.failures) {
            failures.add(new CiFailureTracker.GateFailure(gate.context, gate.updatedAt, gate.summary));
        }
        return new CiFailureTracker.Snapshot(
                summary.number,
                summary.latestCiCommand,
                summary.latestCiCommandAt,
                summary.latestCiCommandKey,
                summary.fetchedAt,
                failures);
    }

    private void postGateFailureNotification(MonitorSummary summary, CiFailureTracker.GateFailure gate) {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, gate.context + " 失败：" + CiFailureTracker.notificationText(gate.summary), Toast.LENGTH_LONG).show();
            return;
        }
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String message = CiFailureTracker.notificationText(gate.summary);
        Notification notification = new Notification.Builder(this, FAILURE_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_error)
                .setContentTitle("PR #" + summary.number + " " + gate.context + " 失败")
                .setContentText(message)
                .setStyle(new Notification.BigTextStyle().bigText(message))
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .setPriority(Notification.PRIORITY_HIGH)
                .setDefaults(Notification.DEFAULT_ALL)
                .build();
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(3000 + Math.abs((summary.number + gate.context).hashCode() % 100000), notification);
        }
    }

    private Notification foregroundNotification() {
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                1,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, STATUS_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("PR 监控中")
                .setContentText("后台每 60 秒检查 ci build / ci merge 后的新失败")
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .setPriority(Notification.PRIORITY_LOW)
                .build();
    }

    private void setupNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                FAILURE_CHANNEL_ID,
                "CI 失败提醒",
                NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("ci build / ci merge 失败时按门禁弹出摘要");
        NotificationChannel statusChannel = new NotificationChannel(
                STATUS_CHANNEL_ID,
                "PR 后台监控",
                NotificationManager.IMPORTANCE_LOW);
        statusChannel.setDescription("后台监控运行状态");
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.createNotificationChannel(channel);
            manager.createNotificationChannel(statusChannel);
        }
    }

    private void putLatest(Map<String, GateStatus> latestByGate, GateStatus gate) {
        GateStatus current = latestByGate.get(gate.context);
        if (current == null || gate.updatedAt.compareTo(current.updatedAt) >= 0) {
            latestByGate.put(gate.context, gate);
        }
    }

    private void updateLatestCiCommand(MonitorSummary summary, JSONObject comment) {
        String command = exactCiCommand(comment.optString("body", ""));
        if (command.isEmpty()) {
            return;
        }
        String updatedAt = firstNonEmpty(comment.optString("updated_at", ""), comment.optString("created_at", ""));
        String commentId = String.valueOf(comment.optLong("id", 0L));
        if (summary.latestCiCommandKey.isEmpty() || updatedAt.compareTo(summary.latestCiCommandAt) >= 0) {
            summary.latestCiCommand = command;
            summary.latestCiCommandAt = updatedAt;
            summary.latestCiCommandKey = summary.number + ":" + command + ":" + updatedAt + ":" + commentId;
        }
    }

    private GateStatus gateFromComment(JSONObject comment, String currentHeadSha) {
        String body = comment.optString("body", "");
        String lower = body.toLowerCase(Locale.ROOT);
        String context = "";
        if (lower.contains("protected-file-approval") || lower.contains("protected file")) {
            context = "protected-file-approval";
        } else if (lower.contains("taichu/codex-pr-review") || lower.contains("codex-pr-review")) {
            context = "taichu/codex-pr-review";
        } else if (lower.contains("taichu/pr-build") || lower.contains("pr-build") || lower.contains("/ci build")) {
            context = "taichu/pr-build";
        } else if (lower.contains("taichu-dev-cloud-preflight") || lower.contains("taichu/dev-cloud-preflight")) {
            context = "taichu/dev-cloud-preflight";
        } else if (lower.contains("ci/merge-gate") || lower.contains("merge-gate") || lower.contains("/ci merge")) {
            context = "ci/merge-gate";
        }
        if (context.isEmpty() || referencesDifferentHead(body, currentHeadSha)) {
            return null;
        }
        GateStatus gate = new GateStatus();
        gate.context = context;
        gate.state = stateFromComment(body);
        gate.summary = cleanCommentText(body);
        gate.updatedAt = firstNonEmpty(comment.optString("updated_at", ""), comment.optString("created_at", ""));
        return gate;
    }

    private JSONObject requestObject(String token, String path) throws Exception {
        String text = requestWithAuth(token, path, "GET", null, new HashMap<>());
        Object parsed = new JSONTokener(text).nextValue();
        if (parsed instanceof JSONObject) {
            return (JSONObject) parsed;
        }
        throw new IOException("expected object payload for " + path);
    }

    private List<JSONObject> requestArrayPages(String token, String path, int maxPages) throws Exception {
        List<JSONObject> items = new ArrayList<>();
        for (int page = 1; page <= maxPages; page++) {
            String separator = path.contains("?") ? "&" : "?";
            String text = requestWithAuth(token, path + separator + "limit=100&page=" + page, "GET", null, new HashMap<>());
            Object parsed = new JSONTokener(text).nextValue();
            if (!(parsed instanceof JSONArray)) {
                throw new IOException("expected list payload for " + path);
            }
            List<JSONObject> pageItems = jsonArrayToList((JSONArray) parsed);
            items.addAll(pageItems);
            if (pageItems.size() < 100) {
                break;
            }
        }
        return items;
    }

    private String requestWithAuth(String token, String path, String method, String body, Map<String, String> headers) throws Exception {
        try {
            return requestWithAuthScheme(token, path, method, body, headers, "bearer");
        } catch (AuthRequired error) {
            return requestWithAuthScheme(token, path, method, body, headers, "token");
        }
    }

    private String requestWithAuthScheme(String token, String path, String method, String body, Map<String, String> headers, String scheme) throws Exception {
        Map<String, String> requestHeaders = new HashMap<>(headers);
        requestHeaders.put("Authorization", scheme + " " + token);
        return requestRaw(API_BASE + path, method, body, requestHeaders);
    }

    private String requestRaw(String url, String method, String body, Map<String, String> headers) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(20000);
        connection.setRequestMethod(method);
        for (Map.Entry<String, String> entry : headers.entrySet()) {
            connection.setRequestProperty(entry.getKey(), entry.getValue());
        }
        if (body != null) {
            connection.setDoOutput(true);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            connection.setRequestProperty("Content-Length", String.valueOf(bytes.length));
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
        }
        int code = connection.getResponseCode();
        String text = readStream(code >= 400 ? connection.getErrorStream() : connection.getInputStream());
        connection.disconnect();
        if (code == 401 || code == 403) {
            throw new AuthRequired("Gitea API " + code);
        }
        if (code >= 400) {
            throw new IOException("Gitea API " + code + ": " + text);
        }
        return text;
    }

    private String readStream(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        StringBuilder builder = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                builder.append(line);
            }
        }
        return builder.toString();
    }

    private List<JSONObject> jsonArrayToList(JSONArray array) {
        List<JSONObject> items = new ArrayList<>();
        for (int index = 0; index < array.length(); index++) {
            JSONObject item = array.optJSONObject(index);
            if (item != null) {
                items.add(item);
            }
        }
        return items;
    }

    private String normalizeGateContext(String context) {
        String lower = valueOrEmpty(context).toLowerCase(Locale.ROOT);
        if (lower.contains("protected-file-approval")) return "protected-file-approval";
        if (lower.contains("taichu/codex-pr-review")) return "taichu/codex-pr-review";
        if (lower.contains("taichu/pr-build")) return "taichu/pr-build";
        if (lower.contains("taichu/dev-cloud-preflight")) return "taichu/dev-cloud-preflight";
        if (lower.contains("ci/merge-gate")) return "ci/merge-gate";
        return "";
    }

    private boolean isActionableFailure(String state, String summary) {
        return GateStateClassifier.isActionableFailure(state, summary);
    }

    private String stateFromComment(String value) {
        String lower = valueOrEmpty(value).toLowerCase(Locale.ROOT);
        if (lower.contains("执行结果：成功")
                || lower.contains("执行结果: 成功")
                || lower.contains("build success")
                || lower.contains("merge gate success")
                || lower.contains("preflight: 通过")
                || lower.contains("preflight：通过")) {
            return "success";
        }
        if (lower.contains("暂不能入队")
                || lower.contains("执行结果：失败")
                || lower.contains("执行结果: 失败")
                || lower.contains("失败摘要")
                || lower.contains("未通过")
                || lower.contains("failed")
                || lower.contains("failure")) {
            return "failure";
        }
        if (lower.contains("queued") || lower.contains("running") || lower.contains("排队") || lower.contains("运行中")) {
            return "pending";
        }
        if (lower.contains("通过") || lower.contains("success")) {
            return "success";
        }
        return "unknown";
    }

    private boolean referencesDifferentHead(String body, String currentHeadSha) {
        if (currentHeadSha == null || currentHeadSha.length() < 7) {
            return false;
        }
        String lower = body.toLowerCase(Locale.ROOT);
        String short7 = currentHeadSha.substring(0, 7).toLowerCase(Locale.ROOT);
        String short12 = currentHeadSha.substring(0, Math.min(12, currentHeadSha.length())).toLowerCase(Locale.ROOT);
        if (lower.contains(short7) || lower.contains(short12)) {
            return false;
        }
        return lower.contains("pr head")
                || lower.contains("当前 pr head")
                || lower.contains("当前 head")
                || lower.contains("顶端提交")
                || lower.contains("pr 顶端")
                || lower.contains("head |")
                || lower.contains("| head |");
    }

    private String exactCiCommand(String text) {
        String command = valueOrEmpty(text).trim().toLowerCase(Locale.ROOT);
        if ("/ci build".equals(command) || "/ci merge".equals(command)) {
            return command;
        }
        return "";
    }

    private String cleanCommentText(String value) {
        String cleaned = valueOrEmpty(value)
                .replaceAll("(?s)<!--.*?-->", "")
                .replaceAll("(?s)<[^>]*>", "")
                .replace("&nbsp;", " ")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&")
                .replaceAll("(?m)^\\s*#+\\s*", "")
                .replaceAll("\\n{3,}", "\n\n")
                .trim();
        return cleaned.isEmpty() ? "评论无可展示内容" : cleaned;
    }

    private String truncateOneLine(String value, int maxChars) {
        String text = valueOrEmpty(value).replaceAll("\\s+", " ").trim();
        if (text.length() <= maxChars) {
            return text;
        }
        return text.substring(0, Math.max(0, maxChars - 1)).trim() + "…";
    }

    private String firstNonEmpty(String first, String second) {
        return first == null || first.isEmpty() ? valueOrEmpty(second) : first;
    }

    private String isoNow() {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.ROOT);
        format.setTimeZone(TimeZone.getDefault());
        return format.format(new Date());
    }

    private String valueOrEmpty(String value) {
        return value == null ? "" : value;
    }

    private static class MonitorSummary {
        int number;
        String headSha = "";
        String fetchedAt = "";
        String latestCiCommand = "";
        String latestCiCommandAt = "";
        String latestCiCommandKey = "";
        List<GateStatus> failures = new ArrayList<>();
    }

    private static class GateStatus {
        String context = "";
        String state = "";
        String summary = "";
        String updatedAt = "";
    }

    private static class AuthRequired extends IOException {
        AuthRequired(String message) {
            super(message);
        }
    }
}
