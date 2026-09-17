# 本地每日两篇

Mac 的 `com.friday.story-daily` 每 5 分钟检查一次，北京时间 06:00 后每天生成两篇。2026-09-07 服务器已生成《板凳》《拖把》，本地从 2026-09-08 接手。Mac 关机或注销时不能执行，登录后补跑当天；不会一口气补写过去多天。

```bash
cron/daily.sh --dry-run             # 只看下一组情绪，不改文件
python3 cron/install_launchd.py     # 安装/更新并加载 LaunchAgent
cron/daily.sh                      # 执行当天任务；已完成则跳过
launchctl print gui/$(id -u)/com.friday.story-daily
launchctl bootout gui/$(id -u)/com.friday.story-daily  # 暂停
```

依赖本机 Claude 登录、Node、Python 3、Google Chrome、Git SSH、`v1` SSH 别名（站点发布）与 `/Users/friday/.local/bin/wxmac`。Git 和站点发布都不再经过 V4。脚本使用当前账号，不修改 HOME 或系统锁屏设置。运行期间 `caffeinate -i` 防止闲置睡眠，不会解锁电脑。渲染器使用独立无头 Chrome，不占用日常浏览器会话，可通过 `STORY_CHROME` 指定可执行文件路径。

`node_modules` 不进版本库，而 worktree 是新目录，所以 `--bootstrap` 会在 worktree 里跑 `npm ci` 安装锁定版本的 `playwright-core`，并把 `package-lock.json` 的摘要写进 `node_modules/.story-install`；`git reset --hard` 不动被忽略的文件，因此只在依赖缺失或锁文件变化时重装。主工作区的 `node_modules` 与它互不相干。

本机默认 GitHub 密钥属于其他仓库（`inaitex_ios` 的 deploy key），不能推送 story。story 有自己的 deploy key `~/.ssh/story_deploy`，经 `~/.ssh/config` 的 `github-story` 别名直连 GitHub——2026-09-14 从 V4 挪到本机，不再中转，`core.sshCommand` 已清空。重新克隆或迁移机器时需重新设置：

```bash
git remote set-url origin git@github-story:wangshipei/story.git
```

并在 `~/.ssh/config` 里补上别名（密钥要 `chmod 600`）：

```
Host github-story
  HostName github.com
  User git
  IdentityFile ~/.ssh/story_deploy
  IdentitiesOnly yes
```

自检：`ssh -T git@github-story` 回 `Hi wangshipei/story!` 即可。密钥无 passphrase，launchd 后台无 ssh-agent 也能用。

## 独立 worktree

定时任务在自己的 detached worktree `~/Library/Application Support/story-daily/worktree` 里干活，主工作区怎么改都影响不到它。`STORY_REPO` 指定干活的仓库，默认就是这个 worktree；`cron/daily.sh --repo` 打印实际路径，`cron/daily.sh --bootstrap` 只准备仓库和依赖、不写作。LaunchAgent 跑的是**主工作区**的 `cron/daily.py`（改完立刻生效，不必先 push），`WorkingDirectory` 也留在主工作区——指向不存在的目录时 launchd 在 spawn 阶段就失败，Python 一行不跑、日志里没有线索。worktree 被误删或清空，下一轮自动重建。

开始写作前要求 worktree 干净、没有未推送的提交，`git fetch origin` 成功；随后 `git reset --hard origin/main`，所以当天两篇一定长在最新的 `origin/main` 上。**只有这一步会 reset**：`generated`（两篇已写、尚未提交）之后的任何恢复路径都不同步、不 reset，否则跨天恢复会把稿子和登记一起抹掉。分支名不再检查——worktree 是 detached，`git branch --show-current` 返回空。推送用 `git push origin HEAD:main` 显式 refspec。

worktree 看不见主工作区未推送的稿子，序号可能撞。若远端已前进，push 会被拒，任务停在 `committed` 保留现场等人工处理，不会强推。Claude 用 Opus / xhigh 按轮值写作，校验通过后构建、提交、推送，再将静态站同步到 V1，最后发送微信。只暂存本次两篇与登记/构建文件。

任务状态里记着生成它的仓库（`repo` 字段）。状态目录是全局的，若某天的任务由别的仓库生成，当前仓库只会报错保留现场，绝不拿自己的工作区去发布别人的稿子。

## 微信

每天发送两张 PNG，每篇一张，收件人默认「王士沛Ronald」。`cron/render_share.mjs` 打开本地站点，操作原生「分享 → 保存图片」生成 1200px 宽的浅色纸纹卡片，保留项目排版，默认不显示网址。调用 `/wechat` 的同一 `wxmac` CLI，发送前搜索并精确核对聊天标题。若实际联系人显示名不同，核实后修改 `~/Library/LaunchAgents/com.friday.story-daily.plist` 的 `STORY_WECHAT_CHAT`，重新加载；安装器会保留该值。

必须保持 Mac 解锁、微信已登录且主窗口可见。调用进程需要辅助功能权限和屏幕录制权限，后者用于核对收件人。可运行 `wxmac doctor --prompt` 按系统提示授权，随后完全重启调用它的 App；后台 Python 的权限也需在 launchd 环境下验证。只读检查：

```bash
python3 cron/wechat_send.py --check
```

微信在跑、只是主窗口被关掉时（macOS 上关窗口不退程序），发送前会用 AppleScript 自动把主窗口唤回来，每次尝试只唤一次，唤不回才按未就绪退出等下一轮；微信没在跑、缺辅助功能或只缺屏幕录制时都不会去唤。

锁屏、缺权限、缺窗口时保留待发图片，稍后重试；图片生成失败或待发图片丢失会重新生成卡片，不会重新写作。发送启动后的失败或超时不能确定是否已发，会停止自动重发。图片通过一次 `wxmac send-file` 粘贴发送。CLI 返回成功只代表发送动作完成，不代表对方已读或服务器回执。旧任务未指定发送格式时仍支持原文字流程。

只生成指定篇目的分享卡片（不发送）：

```bash
node cron/render_share.mjs --output-dir /tmp/story-share --story 57 --story 58
```

## 出事会主动发微信

状态 json 没人会去翻，所以任务卡住时自己来找人：用同一个聊天、同一套 `cron/wechat_send.py`，发一句人话——哪天、卡在哪一步、要不要人动手，例如「9月15日两篇没写成：新增小说必须恰好两篇且序号连续；保留现场，请人工检查。现场已保留，需要人动手。」

- **每个故障只发一次。** 已发文案按键记在 `alerts.json`（`job-<日期>` 故障、`day-<日期>` 兜底、`run-<日期>` 开工失败）；同一句不再重发。文案优先取任务自己记下的 `error`，所以同一天卡一个星期只说一次，不是一天一次。那天最终完成后清掉它的键，将来再坏还能再说。
- **微信未就绪不是故障。** `wechat_send.py` 返回 75（没登录、锁屏、缺权限、别的发送占着锁）表示一个字都没发出去，既不告警也不记账，下一轮照常重试；每日分享图遇到 75 同样只是等待，不会被当成故障。
- **不确定就不再重发。** 75 以外的退出码一律记账并在日志写明退出码，交给人核实，绝不自动重发。
- **告警失败不牵连任务。** 发告警出的任何错只写日志，任务状态机一个字段都不动。
- **兜底。** `--scheduled` 每轮干完自己的活之后再看一眼当天：北京时间 08:00 后当天既没完成、也没报过故障，并且任务记录和心跳都一小时没动过，才发一条「到八点还没写成两篇」。放在这一轮之后，睡醒补跑的 Mac 不会先被冤枉一句再把两篇写出来；上一轮挂住、锁一直被别的进程占着时，这条检查照样跑，是那种时候唯一还会说话的人。

## 心跳与看门狗

每轮 tick 完整跑完写一次 `~/Library/Application Support/story-daily/heartbeat.json`：`time`（带时区）、`day`、当天任务 `status`。`daily.py` 压根没被执行的那类故障（plist 坏了、LaunchAgent 没加载、Mac 关机）它没法给自己告警，只能由外部看门狗读这个文件判断陈旧：Mac 睡眠时不跑，所以阈值别按 5 分钟算，建议「北京时间 08:00 后 `day` 不是今天、或 `time` 超过 3 小时」才报。

## 状态与恢复

- `~/Library/Application Support/story-daily/jobs/YYYY-MM-DD.json`：逐阶段状态。
- 同目录 `YYYY-MM-DD.txt`：留存的审阅正文；新任务不发送此文本。
- 同目录 `YYYY-MM-DD-images/`：原生分享 PNG；路径记录在任务的 `image_files`，新任务 `delivery_format=images`。
- 状态目录 `YYYY-MM-DD-claude.log`：生成日志。
- `~/Library/Logs/story-daily/launchd.log`：调度、校验、发布和微信结果。
- 状态目录 `alerts.json`：已经发过的告警文案，按键去重；`alert.txt` 是最后一条告警的原文；`heartbeat.json` 是最后一次完整跑完的时间。

同一天已有状态时只恢复后续步骤，跨天待发也会重试。网络故障修复后会恢复推送/发布；尚未开始生成时工作区脏或拉取失败会在下一轮检查重试。

`generating` / `manual_review` 表示生成中断或校验失败：先暂停调度，检查小说和轮值，不要直接删状态重跑。确认如何保留/修复已生成内容后再处理状态。

`delivery: sending` 表示发送可能已发生：先人工看微信；确认已发后设 `delivery: sent`，确认没发才改为 `pending`，再恢复调度。不要凭退出码盲目重发。

测试均使用临时仓库或 mock，不生成真实小说、不部署、不发微信：

```bash
python3 -m unittest discover -s cron -p 'test_*.py'
```
