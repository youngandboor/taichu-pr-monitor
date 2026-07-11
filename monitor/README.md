# 全开放 PR WeLink 监控

这个服务每 3 分钟读取 `SystemAgentDev/TaiChu` 的全部开放 PR，按 Build 和 Merge 两个阶段判断门禁，将必要结果通过 WeLink 发送给 PR 提交人。

第一次在内网 Windows 部署时，请直接按照 [`INTRANET_WINDOWS_GUIDE.md`](INTRANET_WINDOWS_GUIDE.md) 的零基础步骤执行，不需要先读完本文。

## 当前行为

- 只监控 `https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls` 下的开放 PR。
- Build 阶段只检查 `protected-file-approval`、`taichu/codex-pr-review`、`taichu/pr-build`。
- Merge 阶段只检查 `taichu/dev-cloud-preflight`、`ci/merge-gate`。
- 忽略旧 head、队列状态、构建耗时评论和旧命令之前的失败。
- 第一次看到某个 PR 时只建立基线，不发送已有历史失败或历史成功。
- 每个 `/ci build` 轮次最多发送一条失败消息；多个失败项合并在同一行。
- Build 三门禁全部成功后不发成功消息，而是在 PR 中自动评论一次 `/ci merge`；提交前会重新读取最新评论中的最新 CI 命令，若已经是 `/ci merge` 就跳过；读取或评论失败只记录 warning，不重试、不额外通知；`--dry-run` 不会发送 WeLink，也不会写入 Gitea 评论。
- 每个 `/ci merge` 轮次最多发送一条失败消息；Merge 两门禁成功后发送一次祝贺消息。
- 所有 WeLink 消息都是单行，PR URL 始终位于最后，避免 WeLink 错误识别链接边界。
- SQLite 同时保存判定水位和发送 outbox，进程重启后不会重新群发。
- 持续运行时提供运维工作台，集中查看失败 PR、完整发送记录、发送错误、磁盘容量和免打扰名单。

当前 CLI 无法读取 WeLink 回复。消息里的“回复 TD 退订”是人工流程提示：维护者收到 TD 后，需要在工作台按对方的 8 位工号加入免打扰名单。

监控优先从 Gitea PR 的 `user.full_name` 自动得到 W3：末尾已含“字母 + 8 位工号”时直接使用，否则使用中文姓氏的拼音首字母加末尾 8 位工号。无法确定时失败关闭，不会猜测发送；JSON 映射只用于例外覆盖。生产运行建议加 `--strict-recipients`，禁止把 Gitea 昵称当作 W3。

## 本地测试

不依赖 Gitea、内网或真实 WeLink：

```bash
cd /path/to/taichu-pr-monitor
python3 -m unittest discover -s monitor/tests -v
```

测试使用 `fake_welink_cli.py` 分别模拟：

- 退出码 `0`：发送成功；
- 非零退出码：发送失败，下一轮重试，默认最多 3 次；
- 超时：结果记为 `uncertain`，不自动重试，避免实际已送达时重复骚扰。

适配器调用严格采用文档中的参数结构：

```text
welink-cli im send-to-user --receiver <W3 account> --text <message>
```

## Mac 开发运行

Gitea 凭据按以下顺序读取，均不会写入仓库：

1. `TAICHU_GITEA_TOKEN` 或 `GITEA_TOKEN`；
2. `TAICHU_GITEA_USERNAME` + `TAICHU_GITEA_PASSWORD`；
3. `git credential fill`。

先用临时状态库做一次只读扫描：

```bash
python3 -m monitor --once --dry-run --state-db /tmp/taichu-pr-monitor-dry-run.sqlite3
```

真实守护运行只应放在能够访问内网并安装了 WeLink PC/CLI 的设备：

```bash
python3 -m monitor --welink-cli welink-cli
```

启动后打开 `http://127.0.0.1:8790`。工作台支持：

- 失败优先的开放 PR 列表、搜索和状态筛选；
- 最近扫描耗时、错误和停滞提示；
- 待发送、需人工处理、已发送和已跳过消息的精确计数与完整详情；
- WeLink outbox 显式重试；
- 按 8 位工号或完整 W3 动态增删免打扰名单；
- 暂停和恢复监控，暂停后工作台仍可使用；
- 在工作区干净时安全快进到 `origin/main` 并自动重启；
- SQLite 占用、可回收空间和磁盘剩余容量；
- “立即扫描”，不会打断正在执行的一轮扫描。

默认按轮询开始时间每 180 秒启动一轮，并以 6 个并发任务读取 PR，避免开放 PR 较多时扫描本身占满整个周期。可用 `--poll-interval`、`--fetch-workers` 覆盖；`--once` 只执行一轮。常用仪表盘参数：

```bash
python3 -m monitor --open-dashboard
python3 -m monitor --dashboard-port 8791
python3 -m monitor --no-dashboard
```

每个 Gitea API 请求默认等待 60 秒，网络超时后自动重试 2 次。内网链路较慢时可以临时放宽：

```bash
python3 -m monitor --once --dry-run --gitea-timeout 120 --gitea-retries 3
```

HTTP `401`、`403` 等明确响应不会重试，需要直接修复 PAT 或仓库权限。

仪表盘默认只监听 `127.0.0.1`，管理操作也默认只接受本机请求。确实需要从另一台内网电脑动态维护免打扰名单时，必须同时显式启用远程访问：

```bash
export TAICHU_DASHBOARD_TOKEN="use-a-long-random-secret"
python3 -m monitor --dashboard-host 0.0.0.0 --allow-remote-dashboard-actions
```

浏览器会要求登录，用户名固定为 `monitor`，密码是 `TAICHU_DASHBOARD_TOKEN`。HTTP Basic 只能作为可信内网中的访问控制，仍需受控防火墙，绝不能暴露到公共网络。

## 提交人 W3 解析与覆盖

正常情况下不需要人工映射：

```bash
python3 -m monitor --strict-recipients
```

解析顺序为：

1. `full_name` 末尾已有完整 W3 时直接使用；
2. 否则提取中文姓氏首字和 8 位工号，生成“姓氏拼音首字母 + 工号”；
3. 无法解析的姓氏或缺失工号标记为错误，不发送；
4. `recipients.json` 可以覆盖特例，且优先级最高。

覆盖表的键是 Gitea `user.login`，值是完整 W3。示例见 `recipients.example.json`：

```json
{
  "gitea-login-example": "y00000000"
}
```

本地真实文件建议命名为 `monitor/recipients.json`，该文件已被 Git 忽略：

```bash
python3 -m monitor --recipients monitor/recipients.json --strict-recipients
```

账号必须用 JSON 字符串，避免工号前导零丢失。

WeLink 不支持发送账号给自己发私聊。设置发送账号和备用接收人后，凡是原目标等于发送账号的消息都会改发给备用接收人：

```bash
export TAICHU_WELINK_SENDER="y00000001"
export TAICHU_WELINK_SELF_FALLBACK="y00000002"
python3 -m monitor --strict-recipients
```

也可用 `--welink-sender` 和 `--self-fallback-receiver` 传入。两个账号必须同时配置且不能相同；仓库不提供真实默认值。

## 内网 Windows 验证

完整的首次安装、PAT 设置、基线、持续运行、验收和故障排查流程见 [`内网 Windows 使用指南（零基础一步一步版）`](INTRANET_WINDOWS_GUIDE.md)。下面只保留熟悉环境后的命令速查。

在安装并按内部文档登录好 `welink-cli` 后：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./monitor/windows/verify_welink.ps1
./monitor/windows/verify_welink.ps1 -Send -Receiver <another-consenting-W3-account>
python -m unittest discover -s monitor/tests -v
python -m monitor --once
python -m monitor --open-dashboard
```

如果 npm 安装得到的是 `welink-cli.cmd`，Python 适配器会自动通过 `invoke_welink.ps1` 调用，并以 Base64 传递消息正文；不会把 PR 评论拼接成 shell 命令。

真实设备验收时重点确认：

1. `send-to-user` 成功时退出码是否稳定为 `0`；
2. 登录过期、收件人不存在和网络失败时是否返回非零退出码；
3. CLI 卡住时进程能否在超时后被终止；
4. WeLink 退出、锁屏和长时间运行后的登录刷新行为。

## 状态与排障

默认数据库为 `monitor/.state/monitor.sqlite3`。查看发送结果：

```bash
python3 -m monitor --list-outbox
```

状态含义：`sent` 已送达调用成功，`pending` 等待发送，`failed` 等待下一轮重试，`dead` 已耗尽重试，`uncertain` 调用超时且不自动重发，`unmapped` 找不到收件人，`suppressed` 因免打扰设置跳过。

从 `ba9f5c3` 等旧版本升级时不要删除状态库。旧 outbox 记录可能没有收件人字段；新版本会从仍然开放的当前 PR 作者信息回填 W3 后继续发送。若对应 PR 已关闭，则需要用 `recipients.json` 为该 Gitea 登录名提供一次覆盖。

工作台“待发送”包含 `pending` 和会自动重试的 `failed`；“需人工处理”包含 `dead`、`uncertain` 和 `unmapped`。已发送与已跳过消息可独立筛选，并可打开查看完整正文与 `last_error`。

每次扫描主要覆盖现有状态，不会按轮询次数累积历史。持续增长的是 outbox；工作台会显示数据库大小和磁盘余量，但当前不会自动删除历史消息。

仓库不会提交 token、真实账号映射、SQLite 状态库、WeLink 登录信息或内部安装文档。
