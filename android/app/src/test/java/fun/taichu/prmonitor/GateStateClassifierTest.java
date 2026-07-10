package fun.taichu.prmonitor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class GateStateClassifierTest {
    @Test
    public void failureTextOverridesSuccessState() {
        String summary = "2026-07-09 20:54:24 | TaiChu merge gate: 执行结果：失败，Cloud Preflight 未通过";

        assertEquals("failure", GateStateClassifier.effectiveState("success", summary));
        assertFalse(GateStateClassifier.isSuccessful("success", summary));
        assertTrue(GateStateClassifier.isActionableFailure("success", summary));
    }

    @Test
    public void plainSuccessRemainsSuccessful() {
        assertEquals("success", GateStateClassifier.effectiveState("success", "当前 head 该门禁已通过。"));
        assertTrue(GateStateClassifier.isSuccessful("success", "当前 head 该门禁已通过。"));
        assertFalse(GateStateClassifier.isActionableFailure("success", "当前 head 该门禁已通过。"));
    }

    @Test
    public void explicitSuccessIgnoresArtifactErrorFilename() {
        String summary = "TaiChu PR build：执行结果：成功\n构建成功\ntestreport/error.txt：merge-gate 状态摘要";

        assertEquals("success", GateStateClassifier.effectiveState("success", summary));
        assertTrue(GateStateClassifier.isSuccessful("success", summary));
        assertFalse(GateStateClassifier.isActionableFailure("success", summary));
    }
}
