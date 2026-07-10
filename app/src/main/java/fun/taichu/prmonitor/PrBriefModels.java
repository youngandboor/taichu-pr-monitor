package fun.taichu.prmonitor;

import java.util.ArrayList;
import java.util.List;

final class PrBriefModels {
    static final class Summary {
        String fetchedAt = "";
        PrInfo pr = new PrInfo();
        List<GateItem> gates = new ArrayList<>();
        List<QueueEvent> queue = new ArrayList<>();
        List<String> hiddenSuccessContexts = new ArrayList<>();
    }

    static final class PrInfo {
        int number;
        String title = "";
        String state = "";
        String body = "";
        String htmlUrl = "";
        String headSha = "";
        String headRef = "";
        String baseRef = "";
        String updatedAt = "";
        String author = "";
    }

    static final class GateItem {
        String context = "";
        String state = "";
        String summary = "";
        String targetUrl = "";
        String updatedAt = "";
        String commentUrl = "";
    }

    static final class QueueEvent {
        String author = "";
        String createdAt = "";
        String updatedAt = "";
        String htmlUrl = "";
        String summary = "";
    }

    private PrBriefModels() {
    }
}
