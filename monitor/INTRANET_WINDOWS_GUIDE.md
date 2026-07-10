# 内网 Windows 使用指南（零基础一步一步版）

这份指南用于把“全开放 PR WeLink 监控”跑在一台能够访问 TaiChu Gitea、并且已经安装 `welink-cli` 的内网 Windows 电脑上。

不需要改代码。每个命令块都可以整块复制到 PowerShell 执行。遇到错误时先停在当前步骤，不要跳过，也不要删除状态文件后重试。

## 最终会得到什么

- 每 3 分钟检查一次 `SystemAgentDev/TaiChu` 的全部开放 PR；
- 只识别五个关键门禁的当前失败；
- 第一次运行只建立基线，不补发历史问题；
- 新问题通过 WeLink 私聊发送给 PR 提交人；
- Gitea 登录号默认直接作为 WeLink W3 账号；
- 浏览器可在 `http://127.0.0.1:8790` 查看运行状态和发送记录。

## 开始前准备

先确认这台 Windows 电脑满足下面四项：

1. 能打开 `https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls`；
2. 能取得本仓库代码；推荐直接访问 GitHub，也可以使用公司允许的文件摆渡方式；
3. 已安装并登录 WeLink，且内部文档要求的 `welink-cli` 已配置完成；
4. 有一个能读取 `SystemAgentDev/TaiChu` 的 Gitea Personal Access Token（PAT）。

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

然后给自己发一条冒烟测试消息。只把引号里的内容改成自己的 W3 账号：

```powershell
.\monitor\windows\verify_welink.ps1 -Send -Receiver "自己的W3账号"
```

必须同时满足：

1. PowerShell 最后显示 `send-to-user returned exit code 0`；
2. 自己的 WeLink 收到一条 `TaiChu PR Monitor welink-cli smoke test` 消息。

这一步失败时不要启动正式监控。先按内部文档修复 WeLink CLI 登录、账号或网络问题。

## 第 5 步：把 Gitea PAT 放进当前窗口

在 Gitea 的“设置 -> 应用 -> 管理访问令牌”创建 PAT。令牌需要能够读取 `SystemAgentDev/TaiChu`。

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
py -3 -m monitor --once --dry-run --state-db "$env:TEMP\taichu-pr-monitor-dry-run.sqlite3"
```

开放 PR 较多时可能需要几十秒。成功日志类似：

```text
poll complete: open=... scanned=... new_failures=0 sent=0 ... errors=0
```

重点看 `errors=0`。如果是 `401`、`403` 或无法连接 Gitea，先检查 PAT 和内网访问。

## 第 8 步：建立正式基线

只在前面步骤全部通过后执行：

```powershell
py -3 -m monitor --once
```

这是正式状态库的第一次扫描。它会记录当前已有问题作为基线，不会把全部历史失败群发出去。

默认状态库位于：

```text
monitor\.state\monitor.sqlite3
```

不要删除、覆盖或复制别的设备上的这个文件。它负责防止相同问题重复发送。

## 第 9 步：启动持续监控

```powershell
py -3 -m monitor --open-dashboard
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

必须保持 PowerShell 窗口开启。关闭窗口、退出 Python、电脑睡眠或关机都会停止监控。需要停止时回到 PowerShell，按一次 `Ctrl+C`。

## 第 10 步：做一次真实端到端验收

建议用自己的开放 PR，确保 PR 提交人的 Gitea 登录号就是自己的 WeLink W3 账号。

1. 先确认持续监控已经完成至少一轮，工作台显示最近扫描成功；
2. 在自己的 PR 触发新的 `/ci build` 或 `/ci merge`；
3. 让当前 head 上的五个关键门禁之一产生一个新失败；
4. 等待一个轮询周期加扫描时间，通常不超过 4 分钟；
5. 确认自己收到一条包含 PR 编号、标题和失败摘要的 WeLink 消息；
6. 再等待一轮，确认同一问题不会重复发送。

只有基线之后产生的新失败才应该通知。旧 head、旧命令轮次和历史评论不会补发。

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

启动：

```powershell
py -3 -m monitor --open-dashboard
```

更新代码不会删除 `monitor\.state\monitor.sqlite3`，因此已经发送过的问题不会因为正常升级而重新通知。

## 提交人账号不能直接对应时

默认规则是：Gitea 登录号直接作为 WeLink W3 账号。只有少数账号不一致时才需要映射表。

```powershell
Copy-Item .\monitor\recipients.example.json .\monitor\recipients.json
notepad .\monitor\recipients.json
```

按下面格式填写，左右两边都必须保留英文双引号：

```json
{
  "gitea-login": "welink-w3-account"
}
```

然后这样启动：

```powershell
py -3 -m monitor --recipients .\monitor\recipients.json --open-dashboard
```

`monitor\recipients.json` 已被 Git 忽略，不要强制提交。

## 常见故障

| 现象 | 处理方式 |
| --- | --- |
| `py` 无法识别 | 安装 Python 3，确认安装器勾选 PATH，然后重新打开 PowerShell |
| `welink-cli` 无法识别 | 按内部文档安装和登录 CLI，再运行第 4 步 |
| `401 Unauthorized` | PAT 无效、过期或权限不足；重新创建 PAT，再执行第 5 步 |
| `403 Forbidden` | 当前账号无权读取仓库或 PAT 权限不足 |
| `errors` 不为 `0` | 在工作台查看扫描错误；先检查 Gitea、内网和 PAT |
| 端口 `8790` 被占用 | 用 `py -3 -m monitor --dashboard-port 8791 --open-dashboard` |
| 消息状态为 `failed` | CLI 返回非零退出码，程序会在下一轮重试，默认最多 3 次 |
| 消息状态为 `uncertain` | CLI 调用超时，程序为避免重复骚扰不会自动重发；确认 WeLink 是否收到后，再在工作台手工重试 |
| 发给了错误的人 | 立即按 `Ctrl+C` 停止，核对 Gitea 登录号和 W3 账号，必要时使用映射表 |
| 新失败没有通知 | 确认它发生在首次基线之后、属于当前 head 和五个关键门禁，并检查工作台 outbox |
| `git pull` 提示本地修改冲突 | 不要执行 `git reset --hard`；保留现场并联系维护者 |

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
- [ ] 给自己的 WeLink 冒烟消息发送成功；
- [ ] 自动测试最终显示 `OK`；
- [ ] dry-run 扫描显示 `errors=0`；
- [ ] 正式基线已完成；
- [ ] 工作台能打开并持续刷新；
- [ ] 基线后制造的新失败只通知一次；
- [ ] 已记录谁负责保持电脑开机、WeLink 登录和 PowerShell 进程运行。
