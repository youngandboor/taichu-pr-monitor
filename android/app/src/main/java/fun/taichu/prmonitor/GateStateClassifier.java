package fun.taichu.prmonitor;

import java.util.Locale;

final class GateStateClassifier {
    private GateStateClassifier() {
    }

    static String effectiveState(String state, String summary) {
        if (hasDefinitiveFailureSignal(state, summary)) {
            return "failure";
        }
        if (hasDefinitiveSuccessSignal(summary)) {
            return "success";
        }
        String lowerSummary = valueOrEmpty(summary).toLowerCase(Locale.ROOT);
        if (lowerSummary.contains("failed") || lowerSummary.contains("failure") || lowerSummary.contains("error")) {
            return "failure";
        }
        String normalized = valueOrEmpty(state).trim().toLowerCase(Locale.ROOT);
        if (normalized.equals("successful") || normalized.equals("passed") || normalized.equals("passing") || normalized.equals("ok")) {
            return "success";
        }
        if (!normalized.isEmpty()) {
            return normalized;
        }
        if (hasSuccessSignal(summary)) {
            return "success";
        }
        return "unknown";
    }

    static boolean isSuccessful(String state, String summary) {
        return "success".equals(effectiveState(state, summary));
    }

    static boolean isActionableFailure(String state, String summary) {
        return "failure".equals(effectiveState(state, summary));
    }

    private static boolean hasDefinitiveFailureSignal(String state, String summary) {
        String lowerState = valueOrEmpty(state).toLowerCase(Locale.ROOT);
        String lowerSummary = valueOrEmpty(summary).toLowerCase(Locale.ROOT);
        return lowerState.equals("failure")
                || lowerState.equals("failed")
                || lowerState.equals("error")
                || lowerSummary.contains("暂不能入队")
                || lowerSummary.contains("执行结果：失败")
                || lowerSummary.contains("执行结果: 失败")
                || lowerSummary.contains("失败摘要")
                || lowerSummary.contains("未通过")
                || lowerSummary.contains("result: failure")
                || lowerSummary.contains("result：failure")
                || lowerSummary.contains("status: failure")
                || lowerSummary.contains("status：failure")
                || lowerSummary.contains("= `failure`")
                || lowerSummary.contains("build failed")
                || lowerSummary.contains("merge gate failed")
                || lowerSummary.contains("preflight failed")
                || lowerSummary.contains("not passed")
                || lowerSummary.contains("did not pass")
                || lowerSummary.contains("unsatisfied")
                || lowerSummary.contains("not satisfied");
    }

    private static boolean hasDefinitiveSuccessSignal(String summary) {
        String lowerSummary = valueOrEmpty(summary).toLowerCase(Locale.ROOT);
        return lowerSummary.contains("执行结果：成功")
                || lowerSummary.contains("执行结果: 成功")
                || lowerSummary.contains("build success")
                || lowerSummary.contains("merge gate success")
                || lowerSummary.contains("preflight: 通过")
                || lowerSummary.contains("preflight：通过")
                || lowerSummary.contains("found no p0/p1")
                || lowerSummary.contains("no p0/p1 principle issues")
                || lowerSummary.contains("当前 head 该门禁已通过");
    }

    private static boolean hasSuccessSignal(String summary) {
        String lowerSummary = valueOrEmpty(summary).toLowerCase(Locale.ROOT);
        return lowerSummary.contains("执行结果：成功")
                || lowerSummary.contains("执行结果: 成功")
                || lowerSummary.contains("build success")
                || lowerSummary.contains("merge gate success")
                || lowerSummary.contains("preflight: 通过")
                || lowerSummary.contains("preflight：通过")
                || lowerSummary.contains("passed")
                || lowerSummary.contains("satisfied")
                || lowerSummary.contains("found no p0/p1")
                || lowerSummary.contains("no p0/p1 principle issues")
                || lowerSummary.contains("当前 head 该门禁已通过")
                || lowerSummary.contains("通过")
                || lowerSummary.contains("success");
    }

    private static String valueOrEmpty(String value) {
        return value == null ? "" : value;
    }
}
