package fun.taichu.prmonitor;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class CiCommandQueueEventsTest {
    @Test
    public void buildCommandCreatesPrBuildQueueCardText() {
        assertEquals("/ci build", CiCommandQueueEvents.exactCommand("  /ci build\n"));
        assertEquals("PR build", CiCommandQueueEvents.kindForCommand("/ci build"));
        assertEquals("命令：/ci build\n已发送，等待队列状态。", CiCommandQueueEvents.summaryForCommand("/ci build"));
    }

    @Test
    public void mergeCommandCreatesMergeGateQueueCardText() {
        assertEquals("/ci merge", CiCommandQueueEvents.exactCommand("/CI MERGE"));
        assertEquals("merge gate", CiCommandQueueEvents.kindForCommand("/ci merge"));
        assertEquals("命令：/ci merge\n已发送，等待队列状态。", CiCommandQueueEvents.summaryForCommand("/ci merge"));
    }

    @Test
    public void ignoresNonExactCommandComment() {
        assertEquals("", CiCommandQueueEvents.exactCommand("please run /ci build"));
    }
}
