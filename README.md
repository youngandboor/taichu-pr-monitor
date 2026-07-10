# TaiChu PR Monitor

一套专注于 TaiChu Gitea PR 门禁的轻量工具，提供 Android、HarmonyOS 和本地 Web 三种使用方式。目标很直接：隐藏历史噪音，让最新失败、排队状态和下一步操作一眼可见。

## 能做什么

- 聚合并归一化五个关键门禁：
  - `protected-file-approval`
  - `taichu/codex-pr-review`
  - `taichu/pr-build`
  - `taichu/dev-cloud-preflight`
  - `ci/merge-gate`
- 只展示最新且有价值的失败摘要，避免旧评论反复干扰。
- 显示最近的 `/ci build`、`/ci merge` 和队列状态。
- 支持任意 `SystemAgentDev/TaiChu` PR 编号。
- 保留 PR body、标题和分支信息。

## 三种客户端

| 目录 | 适用场景 | 主要能力 |
| --- | --- | --- |
| [`android/`](android/) | 商用 Android 手机随时监控 | PAT 授权、后台轮询、失败通知、rebuild/remerge |
| [`harmony/`](harmony/) | HarmonyOS 原生手机 | ArkTS 原生页面、OAuth2 PKCE、rebuild/remerge |
| [`web/`](web/) | Mac/PC 本地快速查看 | 单文件 Python bridge、紧凑 Web 页面、只读访问 |

## 快速开始

### Android

从 [GitHub Releases](https://github.com/youngandboor/taichu-pr-monitor/releases/latest) 下载 `taichu_pr_monitor-release.apk`，在手机上通过系统安装器安装。详细说明见 [`android/README.md`](android/README.md)。

### HarmonyOS

使用 DevEco Studio 打开 `harmony/`，同步 HarmonyOS 6.1.1 / API 24 SDK 后运行 `entry` 模块。详细说明见 [`harmony/README.md`](harmony/README.md)。

### 本地 Web

无需第三方 Python 依赖：

```bash
python3 web/gitea_pr_brief.py \
  https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1222 \
  --serve
```

然后打开 `http://127.0.0.1:8787/pr/1222`。详细说明见 [`web/README.md`](web/README.md)。

## 安全边界

- 仓库不包含 Gitea token、账号密码或 OAuth client secret。
- Android/HarmonyOS 签名材料、设备 profile、本机 SDK/JDK 和构建产物不会进入 Git。
- Web bridge 的凭据只从环境变量或 `git credential fill` 读取，并仅保存在进程内存中。
- Android token 仅保存在应用私有本地偏好中。

## 验证

```bash
cd android
./gradlew :app:testReleaseUnitTest --no-daemon --max-workers=1

cd ../web
python3 -m unittest -v test_gitea_pr_brief.py
```

HarmonyOS 工程通过 DevEco Studio / Hvigor 构建验证。
