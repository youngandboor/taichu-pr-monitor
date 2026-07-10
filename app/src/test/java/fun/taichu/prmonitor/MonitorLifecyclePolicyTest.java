package fun.taichu.prmonitor;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class MonitorLifecyclePolicyTest {
    @Test
    public void launchDefaultsMonitoringOnWhenTokenExistsAndNoStoredChoice() {
        assertTrue(MonitorLifecyclePolicy.enabledOnLaunch(true, false, false));
    }

    @Test
    public void launchHonorsUserPausedMonitoring() {
        assertFalse(MonitorLifecyclePolicy.enabledOnLaunch(true, true, false));
    }

    @Test
    public void serviceRunsOnlyWhenMonitoringAndTokenExist() {
        assertTrue(MonitorLifecyclePolicy.shouldRunService(true, "token"));
        assertFalse(MonitorLifecyclePolicy.shouldRunService(true, ""));
        assertFalse(MonitorLifecyclePolicy.shouldRunService(false, "token"));
    }
}
