package fun.taichu.prmonitor;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class GateHeadMatcherTest {
    @Test
    public void acceptsCurrentCodexTestReviewMarkers() {
        String body = "<!-- taichu-codex-pr-test-review-head:abcdef1234567890 -->\n"
                + "<!-- taichu-codex-pr-review-head:abcdef1234567890 -->";

        assertFalse(GateHeadMatcher.referencesDifferentHead(body, "abcdef1234567890"));
    }

    @Test
    public void rejectsAnAsyncCodexTestReviewForAnOldHead() {
        String body = "<!-- taichu-codex-pr-test-review-head:aaaaaa1234567890 -->\n"
                + "diagnostic also mentioned current bbbbbb1234567890";

        assertTrue(GateHeadMatcher.referencesDifferentHead(body, "bbbbbb1234567890"));
    }

    @Test
    public void keepsVisibleHeadFallbackForLegacyComments() {
        assertTrue(
                GateHeadMatcher.referencesDifferentHead(
                        "| Head | `aaaaaa123456` |",
                        "bbbbbb1234567890"));
        assertFalse(
                GateHeadMatcher.referencesDifferentHead(
                        "| Head | `bbbbbb123456` |",
                        "bbbbbb1234567890"));
    }
}
