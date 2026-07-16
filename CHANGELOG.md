# Changelog

## Unreleased

- 新增 `taichu/codex-pr-test-review`，在普通目标分支上归入 `/ci build` 阶段。
- 兼容尚未产出该门禁的旧 PR；缺失不会被视为失败，旧 PR 后续重跑产生结果时会正常识别。
- Android、HarmonyOS、本地 Web 与全开放 PR WeLink 监控同步支持新门禁。

## 0.1.0

- 整理为 Android、HarmonyOS 和本地 Web 三端 monorepo。
- 聚合 TaiChu PR 的五个关键门禁状态。
- 展示最近的 `/ci build`、`/ci merge` 和队列信息。
- 支持手机端 `rebuild` 与 `remerge`。
- 支持网页登录后创建并验证 Personal Access Token。
- 支持前台自动刷新和 foreground service 后台监控。
- 新门禁失败按门禁通知，并避免重复提醒旧失败。
