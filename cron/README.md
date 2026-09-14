# 本地每日两篇

Mac 的 `com.friday.story-daily` 每 5 分钟检查一次，北京时间 06:00 后每天生成两篇。2026-09-07 服务器已生成《板凳》《拖把》，本地从 2026-09-08 接手。Mac 关机或注销时不能执行，登录后补跑当天；不会一口气补写过去多天。

```bash
cron/daily.sh --dry-run             # 只看下一组情绪，不改文件
python3 cron/install_launchd.py     # 安装/更新并加载 LaunchAgent
cron/daily.sh                      # 执行当天任务；已完成则跳过
launchctl print gui/$(id -u)/com.friday.story-daily
launchctl bootout gui/$(id -u)/com.friday.story-daily  # 暂停
```

依赖本机 Claude 登录、Node、Python 3、Google Chrome、Git SSH、`apps2-server` SSH 别名与 `/Users/friday/.local/bin/wxmac`。不需要复制服务器凭据。脚本使用当前账号，不修改 HOME 或系统锁屏设置。运行期间 `caffeinate -i` 防止闲置睡眠，不会解锁电脑。渲染器使用独立无头 Chrome，不占用日常浏览器会话，可通过 `STORY_CHROME` 指定可执行文件路径。

`node_modules` 不进版本库，而 worktree 是新目录，所以 `--bootstrap` 会在 worktree 里跑 `npm ci` 安装锁定版本的 `playwright-core`，并把 `package-lock.json` 的摘要写进 `node_modules/.story-install`；`git reset --hard` 不动被忽略的文件，因此只在依赖缺失或锁文件变化时重装。主工作区的 `node_modules` 与它互不相干。

本机默认 GitHub 密钥属于其他仓库，不能推送 story。本地仓库使用 V4 已有的 `github-story` 专用密钥中转 Git 连接，配置只存在当前仓库的 `.git/config`；重新克隆或迁移机器时需重新设置：

```bash
git remote set-url origin git@github-story:wangshipei/story.git
git config core.sshCommand 'ssh -o BatchMode=yes -o ConnectTimeout=15 apps2-server ssh -o BatchMode=yes -o ConnectTimeout=15'
```

拉取和推送均依赖 V4 的 SSH 连接；密钥留在 V4，不复制到本地。

## 独立 worktree

定时任务在自己的 detached worktree `~/Library/Application Support/story-daily/worktree` 里干活，主工作区怎么改都影响不到它。`STORY_REPO` 指定干活的仓库，默认就是这个 worktree；`cron/daily.sh --repo` 打印实际路径，`cron/daily.sh --bootstrap` 只准备仓库和依赖、不写作。LaunchAgent 跑的是**主工作区**的 `cron/daily.py`（改完立刻生效，不必先 push），`WorkingDirectory` 也留在主工作区——指向不存在的目录时 launchd 在 spawn 阶段就失败，Python 一行不跑、日志里没有线索。worktree 被误删或清空，下一轮自动重建。

开始写作前要求 worktree 干净、没有未推送的提交，`git fetch origin` 成功；随后 `git reset --hard origin/main`，所以当天两篇一定长在最新的 `origin/main` 上。**只有这一步会 reset**：`generated`（两篇已写、尚未提交）之后的任何恢复路径都不同步、不 reset，否则跨天恢复会把稿子和登记一起抹掉。分支名不再检查——worktree 是 detached，`git branch --show-current` 返回空。推送用 `git push origin HEAD:main` 显式 refspec。

worktree 看不见主工作区未推送的稿子，序号可能撞。若远端已前进，push 会被拒，任务停在 `committed` 保留现场等人工处理，不会强推。Claude 用 Opus / xhigh 按轮值写作，校验通过后构建、提交、推送，再将静态站同步到 V4，最后发送微信。只暂存本次两篇与登记/构建文件。

任务状态里记着生成它的仓库（`repo` 字段）。状态目录是全局的，若某天的任务由别的仓库生成，当前仓库只会报错保留现场，绝不拿自己的工作区去发布别人的稿子。

## 微信

每天发送两张 PNG，每篇一张，收件人默认「王士沛Ronald」。`cron/render_share.mjs` 打开本地站点，操作原生「分享 → 保存图片」生成 1200px 宽的浅色纸纹卡片，保留项目排版，默认不显示网址。调用 `/wechat` 的同一 `wxmac` CLI，发送前搜索并精确核对聊天标题。若实际联系人显示名不同，核实后修改 `~/Library/LaunchAgents/com.friday.story-daily.plist` 的 `STORY_WECHAT_CHAT`，重新加载；安装器会保留该值。

必须保持 Mac 解锁、微信已登录且主窗口可见。调用进程需要辅助功能权限和屏幕录制权限，后者用于核对收件人。可运行 `wxmac doctor --prompt` 按系统提示授权，随后完全重启调用它的 App；后台 Python 的权限也需在 launchd 环境下验证。只读检查：

```bash
python3 cron/wechat_send.py --check
```

锁屏、缺权限、缺窗口时保留待发图片，稍后重试；图片生成失败或待发图片丢失会重新生成卡片，不会重新写作。发送启动后的失败或超时不能确定是否已发，会停止自动重发。图片通过一次 `wxmac send-file` 粘贴发送。CLI 返回成功只代表发送动作完成，不代表对方已读或服务器回执。旧任务未指定发送格式时仍支持原文字流程。

只生成指定篇目的分享卡片（不发送）：

```bash
node cron/render_share.mjs --output-dir /tmp/story-share --story 57 --story 58
```

## 状态与恢复

- `~/Library/Application Support/story-daily/jobs/YYYY-MM-DD.json`：逐阶段状态。
- 同目录 `YYYY-MM-DD.txt`：留存的审阅正文；新任务不发送此文本。
- 同目录 `YYYY-MM-DD-images/`：原生分享 PNG；路径记录在任务的 `image_files`，新任务 `delivery_format=images`。
- 状态目录 `YYYY-MM-DD-claude.log`：生成日志。
- `~/Library/Logs/story-daily/launchd.log`：调度、校验、发布和微信结果。

同一天已有状态时只恢复后续步骤，跨天待发也会重试。网络故障修复后会恢复推送/发布；尚未开始生成时工作区脏或拉取失败会在下一轮检查重试。

`generating` / `manual_review` 表示生成中断或校验失败：先暂停调度，检查小说和轮值，不要直接删状态重跑。确认如何保留/修复已生成内容后再处理状态。

`delivery: sending` 表示发送可能已发生：先人工看微信；确认已发后设 `delivery: sent`，确认没发才改为 `pending`，再恢复调度。不要凭退出码盲目重发。

测试均使用临时仓库或 mock，不生成真实小说、不部署、不发微信：

```bash
python3 -m unittest discover -s cron -p 'test_*.py'
```
