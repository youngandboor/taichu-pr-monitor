package fun.taichu.prmonitor;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class GateHeadMatcher {
    private static final Pattern HTML_HEAD_MARKER = Pattern.compile(
            "<!--\\s*taichu-(?:protected-file-approval|"
                    + "codex-pr(?:-test)?-review)-head\\s*:\\s*"
                    + "`?([0-9a-f]{7,64})`?\\s*-->",
            Pattern.CASE_INSENSITIVE);

    private GateHeadMatcher() {
    }

    static boolean referencesDifferentHead(String body, String currentHeadSha) {
        if (currentHeadSha == null || currentHeadSha.length() < 7) {
            return false;
        }
        String lower = body == null ? "" : body.toLowerCase(Locale.ROOT);
        String current = currentHeadSha.toLowerCase(Locale.ROOT);
        Matcher matcher = HTML_HEAD_MARKER.matcher(lower);
        boolean foundMarker = false;
        while (matcher.find()) {
            foundMarker = true;
            String markedHead = matcher.group(1).toLowerCase(Locale.ROOT);
            if (!current.startsWith(markedHead) && !markedHead.startsWith(current)) {
                return true;
            }
        }
        if (foundMarker) {
            return false;
        }

        String short7 = current.substring(0, 7);
        String short12 = current.substring(0, Math.min(12, current.length()));
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
}
