package fun.taichu.prmonitor;

import static org.junit.Assert.assertEquals;

import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import org.junit.Test;

public class QueueEventSelectorTest {
    private static final QueueEventSelector.ItemView<Item> ITEM_VIEW = new QueueEventSelector.ItemView<Item>() {
        @Override
        public String kind(Item item) {
            return item.kind;
        }

        @Override
        public String updatedAt(Item item) {
            return item.updatedAt;
        }
    };

    @Test
    public void keepsLatestBuildAndMergeAgainstTheirOwnLatestCommands() {
        Map<String, String> commandAtByKind = commandTimes(
                "merge gate", "2026-07-07T16:50:00+08:00",
                "PR build", "2026-07-09T22:05:00+08:00");

        List<Item> selected = QueueEventSelector.latestRelevantPerKind(Arrays.asList(
                new Item("merge gate", "2026-07-07T16:51:37+08:00"),
                new Item("PR build", "2026-07-09T22:06:00+08:00")
        ), commandAtByKind, ITEM_VIEW, 8);

        assertEquals(2, selected.size());
        assertEquals("PR build", selected.get(0).kind);
        assertEquals("merge gate", selected.get(1).kind);
    }

    @Test
    public void dropsQueueEventsBeforeSameKindLatestCommand() {
        Map<String, String> commandAtByKind = commandTimes(
                "merge gate", "2026-07-09T22:05:00+08:00");

        List<Item> selected = QueueEventSelector.latestRelevantPerKind(Arrays.asList(
                new Item("merge gate", "2026-07-07T16:51:37+08:00"),
                new Item("PR build", "2026-07-09T22:06:00+08:00")
        ), commandAtByKind, ITEM_VIEW, 8);

        assertEquals(1, selected.size());
        assertEquals("PR build", selected.get(0).kind);
    }

    @Test
    public void keepsOnlyLatestQueueEventPerKind() {
        List<Item> selected = QueueEventSelector.latestRelevantPerKind(Arrays.asList(
                new Item("merge gate", "2026-07-09T22:06:00+08:00"),
                new Item("merge gate", "2026-07-09T22:11:00+08:00"),
                new Item("PR build", "2026-07-09T22:08:00+08:00")
        ), commandTimes(
                "merge gate", "2026-07-09T22:05:00+08:00",
                "PR build", "2026-07-09T22:05:00+08:00"), ITEM_VIEW, 8);

        assertEquals(2, selected.size());
        assertEquals("merge gate", selected.get(0).kind);
        assertEquals("2026-07-09T22:11:00+08:00", selected.get(0).updatedAt);
        assertEquals("PR build", selected.get(1).kind);
    }

    @Test
    public void keepsLatestPerKindWhenNoCiCommandIsVisible() {
        List<Item> selected = QueueEventSelector.latestRelevantPerKind(Arrays.asList(
                new Item("merge gate", "2026-07-07T16:51:37+08:00"),
                new Item("merge gate", "2026-07-09T22:11:00+08:00")
        ), new HashMap<>(), ITEM_VIEW, 8);

        assertEquals(1, selected.size());
        assertEquals("2026-07-09T22:11:00+08:00", selected.get(0).updatedAt);
    }

    private static Map<String, String> commandTimes(String... values) {
        Map<String, String> commandAtByKind = new HashMap<>();
        for (int index = 0; index + 1 < values.length; index += 2) {
            commandAtByKind.put(values[index].toLowerCase(Locale.ROOT), values[index + 1]);
        }
        return commandAtByKind;
    }

    private static final class Item {
        final String kind;
        final String updatedAt;

        Item(String kind, String updatedAt) {
            this.kind = kind;
            this.updatedAt = updatedAt;
        }
    }
}
