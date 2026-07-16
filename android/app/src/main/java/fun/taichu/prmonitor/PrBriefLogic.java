package fun.taichu.prmonitor;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

final class PrBriefLogic {
    private static final List<String> GATE_CONTEXTS = Arrays.asList(
            "protected-file-approval",
            "taichu/codex-pr-review",
            "taichu/codex-pr-test-review",
            "taichu/pr-build",
            "taichu/dev-cloud-preflight",
            "ci/merge-gate"
    );

    private static final Set<String> SUCCESS_STATES = new HashSet<>(Arrays.asList("success", "ok"));
    private static final Set<String> FAILURE_STATES = new HashSet<>(Arrays.asList("failure", "failed", "error"));

    private static final List<String> QUEUE_KEYWORDS = Arrays.asList(
            "/ci build", "/ci merge", "queue", "queued", "waiting", "pending", "running",
            "ingest", "stale_input", "排队", "队列", "等待", "正在执行", "运行中"
    );
    private static final List<String> QUEUE_NEGATIVE_KEYWORDS = Arrays.asList(
            "已离开活动队列", "当前不在", "build-timing", "耗时表", "等待已结束",
            "：通过", "= `通过`", "= `success`", "执行结果：成功"
    );
    private static final List<String> FAILURE_KEYWORDS = Arrays.asList(
            "fail", "failed", "failure", "error", "stale_input", "timeout",
            "缺失", "失败", "错误", "未通过", "阻塞"
    );

    private static final Pattern HTML_TAG = Pattern.compile("<[^>]+>");
    private static final Pattern MARKDOWN_LINK = Pattern.compile("\\[([^\\]]+)]\\([^)]+\\)");
    private static final Pattern MARKDOWN_HEADING = Pattern.compile("^\\s{0,3}#{1,6}\\s*");
    private static final Pattern HTML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);

    private PrBriefLogic() {
    }

    static PrBriefModels.Summary buildSummary(
            int prNumber,
            JSONObject pr,
            JSONArray statuses,
            JSONArray comments
    ) {
        Map<String, JSONObject> latest = latestStatusesByContext(statuses);
        List<JSONObject> commentList = jsonObjectList(comments);

        PrBriefModels.Summary summary = new PrBriefModels.Summary();
        summary.fetchedAt = OffsetDateTime.now().toString();
        summary.pr = prInfo(prNumber, pr);
        summary.gates = gateItems(latest, commentList, summary.pr.headSha);
        summary.queue = queueEvents(commentList, 3);
        summary.hiddenSuccessContexts = hiddenSuccessContexts(latest);
        return summary;
    }

    private static PrBriefModels.PrInfo prInfo(int prNumber, JSONObject pr) {
        JSONObject head = pr.optJSONObject("head");
        JSONObject base = pr.optJSONObject("base");
        JSONObject user = pr.optJSONObject("user");
        PrBriefModels.PrInfo info = new PrBriefModels.PrInfo();
        info.number = prNumber;
        info.title = pr.optString("title", "");
        info.state = pr.optString("state", "");
        info.body = pr.optString("body", "");
        info.htmlUrl = pr.optString(
                "html_url",
                GiteaConfig.WEB_BASE + "/" + GiteaConfig.OWNER + "/" + GiteaConfig.REPO + "/pulls/" + prNumber);
        info.headSha = head == null ? "" : head.optString("sha", "");
        info.headRef = head == null ? "" : head.optString("ref", "");
        info.baseRef = base == null ? "" : base.optString("ref", "");
        info.updatedAt = pr.optString("updated_at", "");
        info.author = user == null ? "" : user.optString("login", "");
        return info;
    }

    private static Map<String, JSONObject> latestStatusesByContext(JSONArray statuses) {
        Map<String, JSONObject> latest = new HashMap<>();
        List<JSONObject> objects = jsonObjectList(statuses);
        for (JSONObject status : objects) {
            String context = firstNonEmpty(status.optString("context", ""), status.optString("name", "")).trim();
            if (context.isEmpty()) {
                continue;
            }
            JSONObject current = status;
            putQuietly(current, "context", context);
            putQuietly(current, "state", normalizeState(current));
            JSONObject existing = latest.get(context);
            if (existing == null || statusComparator().compare(current, existing) >= 0) {
                latest.put(context, current);
            }
        }
        return latest;
    }

    private static List<PrBriefModels.GateItem> gateItems(
            Map<String, JSONObject> latestStatuses,
            List<JSONObject> comments,
            String currentHeadSha
    ) {
        List<PrBriefModels.GateItem> items = new ArrayList<>();
        for (String context : GATE_CONTEXTS) {
            JSONObject status = latestStatusForGate(latestStatuses, context);
            if (status == null) {
                continue;
            }
            String state = normalizeState(status);
            if (SUCCESS_STATES.contains(state)) {
                continue;
            }

            JSONObject comment = FAILURE_STATES.contains(state)
                    ? latestRelevantComment(context, comments, currentHeadSha)
                    : null;
            String description = cleanText(status.optString("description", ""));
            String commentSummary = comment == null ? "" : summarizeFailureText(comment.optString("body", ""), 1000);
            PrBriefModels.GateItem item = new PrBriefModels.GateItem();
            item.context = context;
            item.state = state.isEmpty() ? "unknown" : state;
            item.summary = joinDistinct(description, commentSummary);
            if (item.summary.isEmpty()) {
                item.summary = "No failure detail was published.";
            }
            item.targetUrl = status.optString("target_url", "");
            item.updatedAt = firstNonEmpty(status.optString("updated_at", ""), status.optString("created_at", ""));
            item.commentUrl = comment == null ? "" : comment.optString("html_url", "");
            items.add(item);
        }
        return items;
    }

    private static List<PrBriefModels.QueueEvent> queueEvents(List<JSONObject> comments, int limit) {
        List<JSONObject> sorted = new ArrayList<>(comments);
        sorted.sort(commentComparator().reversed());
        List<PrBriefModels.QueueEvent> events = new ArrayList<>();
        Set<String> seenCommands = new HashSet<>();
        for (JSONObject comment : sorted) {
            String body = comment.optString("body", "");
            if (!isQueueComment(body)) {
                continue;
            }
            String command = exactCiCommand(body);
            if (!command.isEmpty() && seenCommands.contains(command)) {
                continue;
            }
            if (!command.isEmpty()) {
                seenCommands.add(command);
            }
            JSONObject user = comment.optJSONObject("user");
            PrBriefModels.QueueEvent event = new PrBriefModels.QueueEvent();
            event.author = user == null ? "" : user.optString("login", "");
            event.createdAt = comment.optString("created_at", "");
            event.updatedAt = comment.optString("updated_at", "");
            event.htmlUrl = comment.optString("html_url", "");
            event.summary = summarizeComment(body, 700);
            events.add(event);
            if (events.size() >= limit) {
                break;
            }
        }
        return events;
    }

    private static List<String> hiddenSuccessContexts(Map<String, JSONObject> latestStatuses) {
        List<String> contexts = new ArrayList<>();
        for (String context : GATE_CONTEXTS) {
            JSONObject status = latestStatusForGate(latestStatuses, context);
            if (status != null && SUCCESS_STATES.contains(normalizeState(status))) {
                contexts.add(context);
            }
        }
        return contexts;
    }

    private static JSONObject latestStatusForGate(Map<String, JSONObject> latestStatuses, String context) {
        List<String> aliases = new ArrayList<>();
        if ("protected-file-approval".equals(context)) {
            aliases.add("protected-file-approval");
            aliases.add("taichu/protected-file-approval");
        } else {
            aliases.add(context);
        }
        JSONObject best = null;
        for (String alias : aliases) {
            JSONObject status = latestStatuses.get(alias);
            if (status != null && (best == null || statusComparator().compare(status, best) >= 0)) {
                best = status;
            }
        }
        return best;
    }

    private static JSONObject latestRelevantComment(
            String context,
            List<JSONObject> comments,
            String currentHeadSha
    ) {
        List<String> hints = contextHints(context);
        List<JSONObject> sorted = new ArrayList<>(comments);
        sorted.sort(commentComparator().reversed());
        for (JSONObject comment : sorted) {
            String body = comment.optString("body", "");
            String lowered = body.toLowerCase(Locale.ROOT);
            if (GateHeadMatcher.referencesDifferentHead(body, currentHeadSha)) {
                continue;
            }
            if ("taichu/codex-pr-review".equals(context)
                    && (lowered.contains("taichu-codex-pr-test-review")
                    || lowered.contains("taichu/codex-pr-test-review"))) {
                continue;
            }
            for (String hint : hints) {
                if (lowered.contains(hint.toLowerCase(Locale.ROOT))) {
                    return comment;
                }
            }
        }
        return null;
    }

    private static List<String> contextHints(String context) {
        if ("protected-file-approval".equals(context)) {
            return Arrays.asList("protected-file", "protected file", "approval");
        }
        if ("taichu/codex-pr-review".equals(context)) {
            return Arrays.asList("codex-pr-review", "taichu-pr-codex-review", "codex review");
        }
        if ("taichu/codex-pr-test-review".equals(context)) {
            return Arrays.asList(
                    "taichu-codex-pr-test-review",
                    "taichu/codex-pr-test-review",
                    "codex pr test review");
        }
        if ("taichu/pr-build".equals(context)) {
            return Arrays.asList("taichu/pr-build", "pr-build", "/ci build", "ci build");
        }
        if ("taichu/dev-cloud-preflight".equals(context)) {
            return Arrays.asList("taichu-dev-cloud-preflight", "taichu/dev-cloud-preflight");
        }
        if ("ci/merge-gate".equals(context)) {
            return Arrays.asList("ci/merge-gate", "merge-gate", "/ci merge", "ci merge");
        }
        return Collections.singletonList(context);
    }

    private static boolean isQueueComment(String body) {
        String lowered = body.toLowerCase(Locale.ROOT);
        for (String keyword : QUEUE_NEGATIVE_KEYWORDS) {
            if (lowered.contains(keyword.toLowerCase(Locale.ROOT)) || body.contains(keyword)) {
                return false;
            }
        }
        for (String keyword : QUEUE_KEYWORDS) {
            if (lowered.contains(keyword.toLowerCase(Locale.ROOT)) || body.contains(keyword)) {
                return true;
            }
        }
        return false;
    }

    private static String exactCiCommand(String body) {
        String command = body.trim().toLowerCase(Locale.ROOT);
        return "/ci build".equals(command) || "/ci merge".equals(command) ? command : "";
    }

    private static String summarizeFailureText(String text, int maxChars) {
        List<String> lines = meaningfulLines(text);
        List<String> selected = new ArrayList<>();
        for (String line : lines) {
            String lowered = line.toLowerCase(Locale.ROOT);
            if (lowered.contains("| pass |") || lowered.contains(" pass ")) {
                continue;
            }
            if (line.contains("说明/失败原因")) {
                continue;
            }
            for (String keyword : FAILURE_KEYWORDS) {
                if (lowered.contains(keyword.toLowerCase(Locale.ROOT)) || line.contains(keyword)) {
                    selected.add(line);
                    break;
                }
            }
        }
        if (selected.isEmpty()) {
            selected = lines.subList(0, Math.min(8, lines.size()));
        }
        return truncate(joinLines(selected, 12), maxChars);
    }

    private static String summarizeComment(String text, int maxChars) {
        return truncate(joinLines(meaningfulLines(text), 10), maxChars);
    }

    private static List<String> meaningfulLines(String text) {
        String stripped = HTML_COMMENT.matcher(text).replaceAll("");
        List<String> lines = new ArrayList<>();
        for (String rawLine : stripped.split("\\r?\\n")) {
            String line = cleanText(rawLine);
            if (line.isEmpty()) {
                continue;
            }
            if ("| --- | --- |".equals(line) || "| --- | --- | --- |".equals(line)
                    || "```text".equals(line) || "```".equals(line)) {
                continue;
            }
            lines.add(line);
        }
        return lines;
    }

    private static String cleanText(String text) {
        String value = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&");
        value = HTML_TAG.matcher(value).replaceAll(" ");
        value = MARKDOWN_LINK.matcher(value).replaceAll("$1");
        value = MARKDOWN_HEADING.matcher(value).replaceAll("");
        value = value.replace("**", "").replace("__", "").replace("`", "");
        return value.replaceAll("\\s+", " ").trim();
    }

    private static String normalizeState(JSONObject status) {
        return firstNonEmpty(status.optString("state", ""), status.optString("status", ""))
                .trim()
                .toLowerCase(Locale.ROOT);
    }

    private static Comparator<JSONObject> statusComparator() {
        return (left, right) -> {
            int timestamp = statusTimestamp(left).compareTo(statusTimestamp(right));
            if (timestamp != 0) {
                return timestamp;
            }
            return Integer.compare(left.optInt("id", 0), right.optInt("id", 0));
        };
    }

    private static Comparator<JSONObject> commentComparator() {
        return (left, right) -> {
            String leftTime = firstNonEmpty(left.optString("updated_at", ""), left.optString("created_at", ""));
            String rightTime = firstNonEmpty(right.optString("updated_at", ""), right.optString("created_at", ""));
            int timestamp = leftTime.compareTo(rightTime);
            if (timestamp != 0) {
                return timestamp;
            }
            return Integer.compare(left.optInt("id", 0), right.optInt("id", 0));
        };
    }

    private static String statusTimestamp(JSONObject status) {
        return firstNonEmpty(
                status.optString("updated_at", ""),
                firstNonEmpty(
                        status.optString("created_at", ""),
                        firstNonEmpty(status.optString("submitted_at", ""), status.optString("date", ""))));
    }

    private static List<JSONObject> jsonObjectList(JSONArray array) {
        List<JSONObject> result = new ArrayList<>();
        if (array == null) {
            return result;
        }
        for (int i = 0; i < array.length(); i++) {
            JSONObject object = array.optJSONObject(i);
            if (object != null) {
                result.add(object);
            }
        }
        return result;
    }

    private static String joinDistinct(String first, String second) {
        if (first.isEmpty()) {
            return second;
        }
        if (second.isEmpty() || first.contains(second)) {
            return first;
        }
        return first + "\n\n" + second;
    }

    private static String joinLines(List<String> lines, int maxLines) {
        StringBuilder builder = new StringBuilder();
        int count = Math.min(maxLines, lines.size());
        for (int i = 0; i < count; i++) {
            if (builder.length() > 0) {
                builder.append('\n');
            }
            builder.append(lines.get(i));
        }
        return builder.toString();
    }

    private static String truncate(String text, int maxChars) {
        if (text.length() <= maxChars) {
            return text;
        }
        return text.substring(0, Math.max(0, maxChars - 1)).trim() + "…";
    }

    private static String firstNonEmpty(String first, String second) {
        return first == null || first.isEmpty() ? (second == null ? "" : second) : first;
    }

    private static void putQuietly(JSONObject object, String key, String value) {
        try {
            object.put(key, value);
        } catch (Exception ignored) {
            // JSONObject only throws for invalid numeric values; strings are safe.
        }
    }
}
