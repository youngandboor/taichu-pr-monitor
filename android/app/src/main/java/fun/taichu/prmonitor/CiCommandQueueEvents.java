package fun.taichu.prmonitor;

import java.util.Locale;

final class CiCommandQueueEvents {
    private CiCommandQueueEvents() {
    }

    static String exactCommand(String text) {
        String command = valueOrEmpty(text).trim().toLowerCase(Locale.ROOT);
        if ("/ci build".equals(command) || "/ci merge".equals(command)) {
            return command;
        }
        return "";
    }

    static String kindForCommand(String command) {
        if ("/ci merge".equals(command)) {
            return "merge gate";
        }
        if ("/ci build".equals(command)) {
            return "PR build";
        }
        return "队列状态";
    }

    static String summaryForCommand(String command) {
        return "命令：" + command + "\n已发送，等待队列状态。";
    }

    private static String valueOrEmpty(String value) {
        return value == null ? "" : value;
    }
}
