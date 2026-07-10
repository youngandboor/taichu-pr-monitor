package fun.taichu.prmonitor;

final class MonitorLifecyclePolicy {
    private MonitorLifecyclePolicy() {
    }

    static boolean enabledOnLaunch(boolean hasAccessToken, boolean hasStoredPreference, boolean storedEnabled) {
        if (!hasAccessToken) {
            return false;
        }
        return hasStoredPreference ? storedEnabled : true;
    }

    static boolean shouldRunService(boolean monitoringEnabled, String accessToken) {
        return monitoringEnabled && accessToken != null && !accessToken.trim().isEmpty();
    }
}
