# TaiChu PR Monitor

一款给 TaiChu 开发者使用的 Android PR 门禁监控工具。输入 PR 编号，即刻看到真正影响合入的信息，不必再从冗长的 Gitea 评论里寻找失败原因。

## 为什么值得用

- **一眼定位阻塞项**：只关注六个关键门禁的最新状态与有效失败摘要。
- **过滤历史噪音**：旧状态、旧评论和已经成功的失败记录不会反复干扰判断。
- **看清排队进度**：分别展示最近一次 `/ci build` 与 `/ci merge` 的时间和队列状态。
- **手机直接操作**：通过 `rebuild`、`remerge` 快速发送 CI 命令。
- **后台失败提醒**：监控开启后定时刷新，新门禁失败按门禁单独通知且只提醒一次。
- **支持任意 TaiChu PR**：可输入 `SystemAgentDev/TaiChu` 仓库中的任意 PR 编号，也会自动定位当前用户最近更新的 PR。

## 关注的门禁

- `protected-file-approval`
- `taichu/codex-pr-review`
- `taichu/codex-pr-test-review`
- `taichu/pr-build`
- `taichu/dev-cloud-preflight`
- `ci/merge-gate`

页面还会保留 PR 标题、分支信息、PR body，以及最近的 build/merge 队列信息。

## 登录与授权

应用复用 `https://taichu.fun/gitea/user/login` 的网页登录流程。登录后，“一键授权并验证”会在 Gitea 设置页创建 Personal Access Token，并立即调用 Gitea API 校验。Token 仅保存在 Android 应用自己的本地偏好中，不写入源码或 Git 仓库。

退出应用授权会清除本地 token；重新使用时可再次一键创建并验证。

## 安装

从 GitHub Releases 下载：

```text
taichu_pr_monitor-release.apk
```

将 APK 发送到 Android 手机并通过系统安装器安装即可。首次安装时，系统可能要求允许“安装未知应用”和通知权限。

通过 ADB 安装：

```bash
adb install -r taichu_pr_monitor-release.apk
```

## 本地开发

要求：

- JDK 17 或更高版本
- Android SDK 35
- Android Build Tools 35

运行单元测试：

```bash
cd android
./gradlew :app:testReleaseUnitTest --no-daemon --max-workers=1
```

构建 debug APK：

```bash
cd android
./gradlew :app:assembleDebug
```

构建签名 release APK：

```bash
PRMONITOR_STORE_FILE="$PWD/local-signing/prmonitor-release.jks" \
PRMONITOR_STORE_PASSWORD='<store-password>' \
PRMONITOR_KEY_ALIAS='prmonitor' \
PRMONITOR_KEY_PASSWORD='<key-password>' \
./gradlew :app:assembleRelease --no-daemon --max-workers=1
```

产物路径：

```text
app/build/outputs/apk/release/taichu_pr_monitor-release.apk
```

签名文件、密码、`local.properties`、本地 Android SDK/JDK、构建目录和 `dist/` 均已被 `.gitignore` 排除。

## 当前状态

- Android 8.0 及以上（minSdk 26）
- 前台与 foreground service 后台轮询
- 六门禁最新状态归一化
- 新失败去重通知
- PR 切换隔离通知状态
- 已在商用 vivo Android 手机验证安装与运行
