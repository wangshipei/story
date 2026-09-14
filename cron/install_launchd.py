#!/usr/bin/env python3
"""安装当前用户的每日写作调度；不修改系统睡眠或锁屏设置。"""
import os
from pathlib import Path
import plistlib
import subprocess
import sys


def main():
    if sys.platform != "darwin":
        raise SystemExit("此安装器仅用于 macOS")
    repo = Path(__file__).resolve().parent.parent
    state = Path.home() / "Library/Application Support/story-daily"
    label = "com.friday.story-daily"
    logs = Path.home() / "Library/Logs/story-daily"
    logs.mkdir(parents=True, exist_ok=True)
    target = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    target.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", sys.executable,
                             str(repo / "cron/daily.py"), "--scheduled"],
        # 必须指向必然存在的目录：目录缺失时 launchd 在 spawn 阶段静默失败，日志里没有线索。
        "WorkingDirectory": str(repo),
        "StartCalendarInterval": {"Hour": 6, "Minute": 0},
        "StartInterval": 300,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": str(Path.home() / ".local/bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "TZ": "Asia/Shanghai",
            "STORY_WECHAT_CHAT": "王士沛Ronald",
            # 代码用主工作区的（改了立刻生效），干活用私有 worktree（用户改稿碰不到）。
            "STORY_REPO": str(state / "worktree"),
        },
        "StandardOutPath": str(logs / "launchd.log"),
        "StandardErrorPath": str(logs / "launchd.log"),
    }
    # 保留用户已核实、调整过的精确微信聊天名。
    if target.exists():
        old = plistlib.loads(target.read_bytes())
        chat = old.get("EnvironmentVariables", {}).get("STORY_WECHAT_CHAT")
        if chat:
            config["EnvironmentVariables"]["STORY_WECHAT_CHAT"] = chat
    domain = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
    # 先备好 worktree 与依赖，首次调度不必等第一轮自愈。失败不算致命（例如网络不通）：
    # 下一轮 tick 会重试，绝不能因此把调度器留在卸载状态。
    if subprocess.run([sys.executable, str(repo / "cron/daily.py"), "--bootstrap"]).returncode:
        print("警告：工作仓库未能就绪，稍后由定时任务自行重建")
    target.write_bytes(plistlib.dumps(config))
    subprocess.run(["plutil", "-lint", str(target)], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print("已安装：", target)
    print("工作仓库：", config["EnvironmentVariables"]["STORY_REPO"])
    print("北京时间06:00起每天两篇，每5分钟检查待发；运行状态：")
    subprocess.run(["launchctl", "print", domain + "/" + label], check=True)


if __name__ == "__main__":
    main()
