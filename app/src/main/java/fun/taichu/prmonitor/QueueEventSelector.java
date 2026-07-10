package fun.taichu.prmonitor;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

final class QueueEventSelector {
    interface ItemView<T> {
        String kind(T item);

        String updatedAt(T item);
    }

    private QueueEventSelector() {
    }

    static <T> List<T> latestRelevantPerKind(
            List<T> items,
            Map<String, String> latestCommandAtByKind,
            ItemView<T> view,
            int limit
    ) {
        Map<String, T> latestByKind = new HashMap<>();
        for (T item : items) {
            String updatedAt = valueOrEmpty(view.updatedAt(item));
            String kind = normalizeKind(view.kind(item));
            if (!isRelevantToLatestCommand(updatedAt, latestCommandAtByKind.get(kind))) {
                continue;
            }
            T current = latestByKind.get(kind);
            if (current == null || updatedAt.compareTo(valueOrEmpty(view.updatedAt(current))) >= 0) {
                latestByKind.put(kind, item);
            }
        }

        List<T> selected = new ArrayList<>(latestByKind.values());
        selected.sort((left, right) -> valueOrEmpty(view.updatedAt(right))
                .compareTo(valueOrEmpty(view.updatedAt(left))));
        if (limit > 0 && selected.size() > limit) {
            return new ArrayList<>(selected.subList(0, limit));
        }
        return selected;
    }

    private static boolean isRelevantToLatestCommand(String updatedAt, String latestCommandAt) {
        String commandAt = valueOrEmpty(latestCommandAt);
        return commandAt.isEmpty() || valueOrEmpty(updatedAt).compareTo(commandAt) >= 0;
    }

    private static String normalizeKind(String kind) {
        String normalized = valueOrEmpty(kind).trim().toLowerCase(Locale.ROOT);
        return normalized.isEmpty() ? "queue" : normalized;
    }

    private static String valueOrEmpty(String value) {
        return value == null ? "" : value;
    }
}
