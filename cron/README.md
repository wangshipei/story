# 本地每日两篇

Mac 的 `com.friday.story-daily` 每 5 分钟检查一次，北京时间 06:00 后每天生成两篇。2026-09-07 服务器已生成《板凳》《拖把》，本地从 2026-09-08 接手。Mac 关机或注销时不能执行，登录后补跑当天；不会一口气补写过去多天。

```bash
cron/daily.sh --dry-run             # 只看下一组情绪，不改文件
python3 cron/install_launchd.py     # 安装/更新并加载 LaunchAgent
cron/daily.sh                      # 执行当天任务；已完成则跳过
launchctl print gui/$(id -u)/com.friday.story-daily
launchctl bootout gui/$(id -u)/com.friday.story-daily  # 暂停
```

依赖本机 Claude 登录、Node、Python 3、Git SSH、`apps2-server` SSH 别名与 `/Users/friday/.local/bin/wxmac`。不需要复制服务器凭据。脚本使用当前账号，不修改 HOME 或系统锁屏设置。运行期间 `caffeinate -i` 防止闲置睡眠，不会解锁电脑。

本机默认 GitHub 密钥属于其他仓库，不能推送 story。本地仓库使用 V4 已有的 `github-story` 专用密钥中转 Git 连接，配置只存在当前仓库的 `.git/config`；重新克隆或迁移机器时需重新设置：

```bash
git remote set-url origin git@github-story:wangshipei/story.git
git config core.sshCommand 'ssh -o BatchMode=yes -o ConnectTimeout=15 apps2-server ssh -o BatchMode=yes -o ConnectTimeout=15'
```

拉取和推送均依赖 V4 的 SSH 连接；密钥留在 V4，不复制到本地。

开始写作前要求 `main` 分支、工作区干净、`git pull --ff-only` 成功。本地开发中的改动先提交或暂存。Claude 用 Opus / xhigh 按轮值写作，校验通过后构建、提交、推送，再将静态站同步到 V4，最后发送微信。只暂存本次两篇与登记/构建文件。

## 微信

两篇合为一条文字，含日期、标题和完整正文，收件人默认「王士沛」。调用 `/wechat` 的同一 `wxmac` CLI，发送前搜索并精确核对聊天标题。若实际联系人显示名不同，核实后修改 `~/Library/LaunchAgents/com.friday.story-daily.plist` 的 `STORY_WECHAT_CHAT`，重新加载；安装器会保留该值。

必须保持 Mac 解锁、微信已登录且主窗口可见。调用进程需要辅助功能权限和屏幕录制权限，后者用于核对收件人。可运行 `wxmac doctor --prompt` 按系统提示授权，随后完全重启调用它的 App；后台 Python 的权限也需在 launchd 环境下验证。只读检查：

```bash
python3 cron/wechat_send.py --check
```

锁屏、缺权限、缺窗口时只保留待发正文，稍后重试。发送启动后的失败或超时不能确定是否已发，会停止自动重发。CLI 返回成功只代表发送动作完成；日志会记录是否在当前屏幕读到全文，不代表对方已读或服务器回执。

## 状态与恢复

- `~/Library/Application Support/story-daily/jobs/YYYY-MM-DD.json`：逐阶段状态。
- 同目录 `YYYY-MM-DD.txt`：固定的待发正文。
- 状态目录 `YYYY-MM-DD-claude.log`：生成日志。
- `~/Library/Logs/story-daily/launchd.log`：调度、校验、发布和微信结果。

同一天已有状态时只恢复后续步骤，跨天待发也会重试。网络故障修复后会恢复推送/发布；尚未开始生成时工作区脏或拉取失败会在下一轮检查重试。

`generating` / `manual_review` 表示生成中断或校验失败：先暂停调度，检查小说和轮值，不要直接删状态重跑。确认如何保留/修复已生成内容后再处理状态。

`delivery: sending` 表示发送可能已发生：先人工看微信；确认已发后设 `delivery: sent`，确认没发才改为 `pending`，再恢复调度。不要凭退出码盲目重发。

测试均使用临时仓库或 mock，不生成真实小说、不部署、不发微信：

```bash
python3 -m unittest discover -s cron -p 'test_*.py'
```
