# 内网 Windows 使用指南（零基础一步一步版）

这份指南用于把“全开放 PR WeLink 监控”跑在一台能够访问 TaiChu Gitea、并且已经安装 `welink-cli` 的内网 Windows 电脑上。

不需要改代码。每个命令块都可以整块复制到 PowerShell 执行。遇到错误时先停在当前步骤，不要跳过，也不要删除状态文件后重试。

## 最终会得到什么

- 每 3 分钟检查一次 `SystemAgentDev/TaiChu` 的全部开放 PR；
- 把五个关键门禁分成 Build 三项和 Merge 两项；
- 第一次运行只建立基线，不补发历史问题；
- 每轮 Build 或 Merge 失败最多私聊一次；
- Build 三门禁成功后自动评论 `/ci merge`，不发成功私聊；
- Merge 成功后按 PR Diff 变更量与持续时间选择祝贺消息，并向提交人发送一次；
- 从 Gitea `full_name` 自动生成“姓氏拼音首字母 + 8 位工号”的 WeLink W3 账号；
- 浏览器可在 `http://127.0.0.1:8790` 查看完整发送记录、磁盘占用并动态设置免打扰工号。

## 开始前准备

先确认这台 Windows 电脑满足下面五项：

1. 能打开 `https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls`；
2. 能取得本仓库代码；推荐直接访问 GitHub，也可以使用公司允许的文件摆渡方式；
3. 已安装并登录 WeLink，且内部文档要求的 `welink-cli` 已配置完成；
4. 有一个能读取 `SystemAgentDev/TaiChu`、并能在 PR 下发表评论的 Gitea Personal Access Token（PAT）。
5. 有一位知情同事可接收 WeLink 冒烟消息；`welink-cli` 登录账号不能给自己发消息。

不要把 PAT、真实 W3 账号、`recipients.json` 或本地 SQLite 文件发到 GitHub。

## 第 1 步：打开 PowerShell

按 Windows 键，输入 `PowerShell`，打开“Windows PowerShell”。正常情况下不需要管理员权限。

后续所有命令都在这个窗口里执行。命令运行中不要关闭窗口。

## 第 2 步：检查必需工具

逐行执行：

```powershell
git --version
py -3 --version
Get-Command welink-cli
```

三个命令都必须有正常输出：

| 命令 | 正常现象 | 如果失败 |
| --- | --- | --- |
| `git --version` | 显示 Git 版本 | 安装 Git for Windows 后重新打开 PowerShell |
| `py -3 --version` | 显示 Python 3 版本 | 安装 Python 3，并勾选加入 PATH |
| `Get-Command welink-cli` | 显示 `.exe`、`.cmd` 或脚本路径 | 按内部 WeLink CLI 文档安装并登录；只有 WeLink 桌面客户端不一定够 |

任何一项提示“无法识别”时都不要继续。

## 第 3 步：取得或更新代码

### 第一次安装

```powershell
cd $HOME
git clone https://github.com/youngandboor/taichu-pr-monitor.git
cd .\taichu-pr-monitor
git switch main
git pull --ff-only origin main
```

### 已经安装过

```powershell
cd $HOME\taichu-pr-monitor
git switch main
git pull --ff-only origin main
```

看到 `Already up to date.` 或正常的文件更新列表即可。再执行：

```powershell
git status -sb
git log -1 --oneline
```

第一行应该以 `## main...origin/main` 开头，并且不应列出被修改的代码文件。

如果内网电脑不能直接访问 GitHub，请在可访问 GitHub 的电脑取得 `main` 最新代码，再通过公司批准的方式传入。不要使用来源不明的压缩包。

## 第 4 步：验证 WeLink CLI

先允许当前 PowerShell 窗口运行仓库脚本。这个设置只对当前窗口有效：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

检查 `welink-cli` 是否能被监控程序找到：

```powershell
.\monitor\windows\verify_welink.ps1
```

应看到类似下面的结果：

```text
welink-cli: C:\...\welink-cli.cmd
Command discovery passed.
```

然后给一位知情同事发冒烟测试消息。收件人必须与当前 `welink-cli` 登录账号不同：

```powershell
.\monitor\windows\verify_welink.ps1 -Send -Receiver "另一位知情同事的W3账号"
```

必须同时满足：

1. PowerShell 最后显示 `send-to-user returned exit code 0`；
2. 该同事的 WeLink 收到一条 `TaiChu PR Monitor welink-cli smoke test` 消息。

这一步失败时不要启动正式监控。先按内部文档修复 WeLink CLI 登录、账号或网络问题。

### 可选：测试 WeLink 支持的消息格式

独立格式测试脚本默认只预览，不会发送：

```powershell
.\monitor\windows\test_welink_messages.ps1 -TestCase url-last
```

确认预览内容后，向一位知情同意且不是当前 WeLink 登录账号的收件人发送：

```powershell
.\monitor\windows\test_welink_messages.ps1 -TestCase url-last -Receiver "对方W3账号" -Send
```

支持 `single-line`、`merge-success`、`url-last`、`url-followed-by-text`、`long-single-line`、`multi-line` 和 `all`。`merge-success` 用于确认庆祝表情和成功文案在 WeLink 客户端中的显示；`url-last` 与 `url-followed-by-text` 使用同类正文，可直接对比 URL 后有无文字对链接识别的影响；`all` 会连续发送六条测试消息。每条都会显示字符数、UTF-8 字节数、行数、URL 是否位于最后、CLI 返回码和耗时。`multi-line` 只用于复现和对照，正式 PRbot 消息仍保持单行。

用于选择成功文案的“变更行数”是 Gitea 的 `additions + deletions`，包含本次 Diff 中的代码、配置、文档及生成文件，不代表净新增代码。实际行数会以不带千分位的纯数字自然嵌入成功正文；持续时间从 PR 的 `created_at` 计算到成功消息生成时刻，不足 24 小时按 1 天参与分档，但不会单独展示。只有生成 Merge 成功消息时才额外读取该 PR 详情，普通三分钟扫描不会为所有开放 PR 批量读取行数。

## 第 5 步：把 Gitea PAT 放进当前窗口

在 Gitea 的“设置 -> 应用 -> 管理访问令牌”创建 PAT。令牌既要能够读取 `SystemAgentDev/TaiChu`，也要能够在 PR/Issue 下发表评论；如果页面提供权限范围，请同时选择仓库读取和 Issue/评论写入权限。

回到 PowerShell，执行：

```powershell
$gitea = Get-Credential -UserName "token" -Message "把 Gitea PAT 粘贴到密码框"
$env:TAICHU_GITEA_TOKEN = $gitea.GetNetworkCredential().Password
Remove-Variable gitea
```

弹窗里的“密码”框粘贴 PAT，用户名保持 `token` 即可。检查变量是否已经设置，但不要打印令牌本身：

```powershell
if ([string]::IsNullOrWhiteSpace($env:TAICHU_GITEA_TOKEN)) { throw "PAT 未设置" } else { "PAT 已装入当前 PowerShell" }
```

PAT 只存在于当前 PowerShell 进程内存中。关闭窗口或重启电脑后，需要重新执行本步骤。

## 第 6 步：运行自动测试

确认当前位置仍是仓库根目录：

```powershell
cd $HOME\taichu-pr-monitor
py -3 -m unittest discover -s monitor/tests -v
```

等待测试结束。测试数量可能随版本增加，以最后一行出现 `OK` 为通过标准。出现 `FAILED` 或 `ERROR` 时不要继续正式运行。

## 第 7 步：做一次完全不发消息的扫描

下面的命令会读取真实 Gitea，但绝不会调用真实 WeLink 发送：

```powershell
Remove-Item "$env:TEMP\taichu-pr-monitor-dry-run.sqlite3" -ErrorAction SilentlyContinue
py -3 -m monitor --once --dry-run --strict-recipients --state-db "$env:TEMP\taichu-pr-monitor-dry-run.sqlite3"
```

开放 PR 较多时可能需要几十秒。成功日志类似：

```text
poll complete: open=... scanned=... new_notifications=0 sent=0 ... errors=0
```

重点看 `errors=0`。如果是 `401`、`403` 或无法连接 Gitea，先检查 PAT 和内网访问。

## 第 8 步：配置自发兜底

正常情况下不需要人工收件人表。监控会按下面顺序从 Gitea `full_name` 自动得到 W3：

1. 末尾已有“字母 + 8 位工号”时直接使用；
2. 否则取中文姓氏的拼音首字母，加末尾 8 位工号；
3. 无法解析时报告错误并停止正式启动，不会把 Gitea 昵称当成 W3。

WeLink 不支持当前登录账号给自己发私聊，因此还要设置当前发送账号和备用接收人：

```powershell
$env:TAICHU_WELINK_SENDER = "y发送账号"
$env:TAICHU_WELINK_SELF_FALLBACK = "y备用接收账号"
```

如果映射后的目标等于发送账号，监控会自动改发给备用接收人。两者必须同时设置且不能相同。

只有 dry-run 报告某位作者无法解析时，才需要复制 `monitor\recipients.example.json` 为 `monitor\recipients.json` 并添加例外覆盖。该文件已被 Git 忽略，不要强制提交。

## 第 9 步：建立正式基线

只在前面步骤全部通过后执行：

```powershell
py -3 -m monitor --once --strict-recipients
```

这是正式状态库的第一次扫描。它会记录当前已有问题和已经完成的命令轮次作为基线，不会群发历史结果，也不会为历史 Build 自动评论 `/ci merge`。

默认状态库位于：

```text
monitor\.state\monitor.sqlite3
```

不要删除、覆盖或复制别的设备上的这个文件。它负责防止相同问题重复发送。

## 第 10 步：启动持续监控

```powershell
py -3 -m monitor --strict-recipients --open-dashboard
```

正常情况下浏览器会自动打开：

```text
http://127.0.0.1:8790
```

浏览器没自动打开时，手工复制这个地址到浏览器即可。PowerShell 中应看到：

```text
dashboard available at http://127.0.0.1:8790
```

此后程序每 180 秒开始一轮扫描。扫描本身可能再花几十秒。

必须保持 PowerShell 窗口开启。关闭窗口、退出 Python、电脑睡眠或关机都会停止监控。工作台的“暂停监控”只停止后续扫描和发送，页面仍然可用并可随时恢复；彻底退出仍需回到 PowerShell 按一次 `Ctrl+C`。

默认只有这台 Windows 电脑自己能修改免打扰名单。如果确实需要从另一台内网电脑访问，停止当前进程，先生成一个本次部署专用的随机密码：

```powershell
$env:TAICHU_DASHBOARD_TOKEN = [guid]::NewGuid().ToString("N")
"工作台用户名: monitor"
"工作台密码: $env:TAICHU_DASHBOARD_TOKEN"
```

然后启动：

```powershell
py -3 -m monitor --strict-recipients --dashboard-host 0.0.0.0 --allow-remote-dashboard-actions
```

在受信任的内网电脑打开 `http://<监控电脑内网IP>:8790`，浏览器登录框的用户名填 `monitor`，密码填刚生成的值。HTTP Basic 只能用于可信内网中的访问控制，仍需受控防火墙，绝不能映射到公共网络。

## 第 11 步：做一次真实端到端验收

建议使用一位知情同事的开放 PR，或先配置下方“发送给自己时的备用接收人”。最终收件人必须与 `welink-cli` 登录账号不同。

1. 先确认持续监控已经完成至少一轮，工作台显示最近扫描成功；
2. 在约定的测试 PR 触发新的 `/ci build`；
3. 让三个 Build 门禁之一产生一个新失败；
4. 等待一个轮询周期加扫描时间，通常不超过 4 分钟；
5. 确认对应提交人或备用接收人收到单行 WeLink 消息，PR URL 位于消息最后且可正确点击；
6. 让同一 Build 轮次出现第二个失败，确认不会再发第二条；
7. 再触发一个新的 `/ci build` 并让三个 Build 门禁全部成功，确认 PR 中自动出现一次 `/ci merge`，且没有 Build 成功私聊；若提交人已先在最新评论中发出 `/ci merge`，确认机器人识别到最新 CI 命令并且不重复评论；
8. 让 Merge 门禁失败，确认提交人收到一次失败消息；
9. 重新触发 `/ci merge` 并让两个 Merge 门禁成功，确认提交人收到一次“恭喜，Merge 已成功”；
10. 在工作台“消息发送”中分别查看已发送、需人工处理和完整消息详情。

只有基线之后产生的新阶段结果才会触发动作。旧 head、旧命令轮次和历史评论不会补发。自动评论 `/ci merge` 失败时程序只写 warning，不重试也不通知。

消息中的“回复 TD 退订”目前是人工流程。`welink-cli` 不能读取回复；维护者在 WeLink 看到 TD 后，需要打开工作台“免打扰”，输入对方 8 位工号或完整 W3。加入后待发消息会标为“已跳过”，移出名单只恢复未来消息，不补发历史消息。

## 以后每天怎么启动

打开一个新的 PowerShell，依次执行下面几块。

更新代码：

```powershell
cd $HOME\taichu-pr-monitor
git switch main
git pull --ff-only origin main
Set-ExecutionPolicy -Scope Process Bypass
```

装入 PAT：

```powershell
$gitea = Get-Credential -UserName "token" -Message "把 Gitea PAT 粘贴到密码框"
$env:TAICHU_GITEA_TOKEN = $gitea.GetNetworkCredential().Password
Remove-Variable gitea
```

设置自发兜底路由：

```powershell
$env:TAICHU_WELINK_SENDER = "y发送账号"
$env:TAICHU_WELINK_SELF_FALLBACK = "y备用接收账号"
```

启动：

```powershell
py -3 -m monitor --strict-recipients --open-dashboard
```

更新代码不会删除 `monitor\.state\monitor.sqlite3`，因此已经发送过的问题不会因为正常升级而重新通知。

持续监控已经启动时，也可以在工作台点击“更新程序”。只有本地处于干净的 `main` 且能安全快进到 `origin/main` 时才会更新并自动重启；检测到本地改动、分支不对、网络失败或历史分叉时会拒绝操作，不覆盖任何文件。

## 常见故障

| 现象 | 处理方式 |
| --- | --- |
| `py` 无法识别 | 安装 Python 3，确认安装器勾选 PATH，然后重新打开 PowerShell |
| `welink-cli` 无法识别 | 按内部文档安装和登录 CLI，再运行第 4 步 |
| `401 Unauthorized` | PAT 无效、过期或权限不足；重新创建 PAT，再执行第 5 步 |
| `403 Forbidden` | 当前账号无权读取仓库、无权在 PR 下评论，或 PAT 权限不足 |
| `failed to list open pull requests`、`urlopen error timed out` | 先执行下方 Gitea API 连通性检查；更新到最新 `main` 后可增大超时并重试 |
| `errors` 不为 `0` | 在工作台查看扫描错误；先检查 Gitea、内网和 PAT |
| 端口 `8790` 被占用 | 用 `py -3 -m monitor --dashboard-port 8791 --open-dashboard` |
| 消息状态为 `failed` | CLI 返回非零退出码，程序会在下一轮重试，默认最多 3 次 |
| 消息状态为 `uncertain` | CLI 调用超时，程序为避免重复骚扰不会自动重发；确认 WeLink 是否收到后，再在工作台手工重试 |
| 从 `ba9f5c3` 升级后有旧 `pending` | 不要删除 SQLite；最新版本会从仍开放的 PR 回填 W3。先停止旧进程、拉取 `main`，再按第 8 至 10 步启动 |
| 给自己发送失败 | WeLink 不支持自发消息；配置发送账号和备用接收人，或改用不提交 PR 的专用发送账号 |
| 发给了错误的人 | 立即按 `Ctrl+C` 停止，核对 Gitea 登录号和 W3 账号，必要时使用映射表 |
| 新失败没有通知 | 确认它发生在首次基线之后、属于当前命令阶段，并检查工作台 outbox 与免打扰名单 |
| Build 成功后没有 `/ci merge` 评论 | 三项 Build 门禁都必须是当前 head 的成功结果，且 `taichu/pr-build` 不早于最新 `/ci build`；PAT 没有评论权限时按设计只记录 warning，不重试 |
| Merge 成功没有祝贺消息 | 两个 Merge 门禁必须全部成功且时间不早于最新 `/ci merge`，同时检查收件人工号是否在免打扰名单 |
| 工作台显示磁盘空间偏低 | 查看“本地存储”的数据库占用和磁盘剩余；先清理其他无关大文件，不要直接删除 `monitor.sqlite3` |
| `git pull` 提示本地修改冲突 | 不要执行 `git reset --hard`；保留现场并联系维护者 |

Gitea API 超时时，可以先检查直连 443 端口：

```powershell
Test-NetConnection taichu.fun -Port 443
```

这个命令不经过浏览器代理。它失败但浏览器能打开 Gitea 时，通常是浏览器使用了公司代理或 PAC，而 Python 正在直连。查看 Windows 为 Gitea 解析出的代理：

```powershell
$target = [Uri]"https://taichu.fun/gitea/api/v1/version"
$systemProxy = [System.Net.WebRequest]::DefaultWebProxy
$proxyUrl = $systemProxy.GetProxy($target).AbsoluteUri
"Proxy: $proxyUrl"
"Bypassed: $($systemProxy.IsBypassed($target))"
```

如果 `Proxy` 与目标地址不同，把该代理交给当前 PowerShell 中的 Python：

```powershell
$env:HTTPS_PROXY = $proxyUrl
$env:HTTP_PROXY = $proxyUrl
```

再用当前 PAT 直接读取一个开放 PR：

```powershell
$headers = @{ Authorization = "token $env:TAICHU_GITEA_TOKEN" }
Invoke-RestMethod -Uri "https://taichu.fun/gitea/api/v1/repos/SystemAgentDev/TaiChu/pulls?state=open&limit=1" -Headers $headers -TimeoutSec 120
```

如果这条命令也超时，问题在内网、代理或 Gitea 服务端，需要找网络/Gitea 管理员；如果它能成功，再用更宽松的监控参数：

```powershell
Remove-Item "$env:TEMP\taichu-pr-monitor-dry-run.sqlite3" -ErrorAction SilentlyContinue
py -3 -m monitor --once --dry-run --gitea-timeout 120 --gitea-retries 3 --state-db "$env:TEMP\taichu-pr-monitor-dry-run.sqlite3"
```

查看发送记录：

```powershell
py -3 -m monitor --list-outbox
```

需要导出排障信息时：

```powershell
py -3 -m monitor --list-outbox > "$HOME\Desktop\taichu-monitor-outbox.json"
```

导出的 outbox 可能包含内部 PR 摘要和收件人账号，只能通过批准的内部渠道传递，排障结束后删除。

## 上线验收清单

- [ ] Gitea PR 页面可以访问；
- [ ] `git`、Python 3、`welink-cli` 三项检查通过；
- [ ] 给另一位知情同事的 WeLink 冒烟消息发送成功；
- [ ] 自动测试最终显示 `OK`；
- [ ] dry-run 扫描显示 `errors=0`；
- [ ] 正式基线已完成；
- [ ] 工作台能打开并持续刷新；
- [ ] Build 同一轮多个失败最多通知一次；
- [ ] Build 三门禁成功后自动评论一次 `/ci merge`，不发 Build 成功私聊；
- [ ] Merge 失败通知一次，成功发送一次祝贺消息；
- [ ] 工作台能查看已发送消息和完整错误；
- [ ] 按 WeLink 工号加入免打扰后，新消息显示为“已跳过”；
- [ ] 工作台能暂停、恢复并显示数据库与磁盘容量；
- [ ] 已记录谁负责保持电脑开机、WeLink 登录和 PowerShell 进程运行。
