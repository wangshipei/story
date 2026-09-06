#!/bin/bash
# 每日两篇：从 ROTATION.md 取接下来两个未写的情绪，交给 claude -p 按 CLAUDE.md 写，然后登记、部署、提交、推送。
# crontab：0 22 * * * /home/ubuntu/story/cron/daily.sh   （北京时间 06:00）
# 手动：cron/daily.sh            真跑一次
#       cron/daily.sh --dry-run  只看今天会轮到哪两个情绪
set -uo pipefail
export LANG=C.UTF-8 LC_ALL=C.UTF-8
export HOME=/home/ubuntu
export PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
# cron 不加载 ~/.profile，登录凭据（claude setup-token 生成的长期 token）在那里；只取这一行，不把 token 抄进仓库
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  eval "$(grep -m1 '^export CLAUDE_CODE_OAUTH_TOKEN=' "$HOME/.profile")"
fi
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || { echo "没有 CLAUDE_CODE_OAUTH_TOKEN，请在 ~/.profile 里 export（claude setup-token 生成）" >&2; exit 1; }

REPO=/home/ubuntu/story
LEDGER=$REPO/ROTATION.md
LOG=/home/ubuntu/log/story-daily.log
TODAY=$(TZ=Asia/Shanghai date +%F)

cd "$REPO" || exit 1
log(){ local m="[$(TZ=Asia/Shanghai date '+%F %T')] $*"; echo "$m" >> "$LOG"; [ -t 1 ] && echo "$m"; }

exec 9>"$REPO/.daily.lock"
flock -n 9 || { log "上一次还没跑完，跳过"; exit 0; }

# 取最前面两条未写的；本轮写完就照「轮值顺序」开下一轮
pick(){ grep -m2 '^- \[ \] ' "$LEDGER" | sed 's/^- \[ \] //'; }
mapfile -t PICKS < <(pick)
if [ "${#PICKS[@]}" -lt 2 ]; then
  ROUND=$(( $(grep -c '^## 第' "$LEDGER") + 1 ))
  { echo; echo "## 第${ROUND}轮"; echo
    sed -n '/^<!-- 轮值顺序 -->/,/^<!-- 轮值顺序结束 -->/p' "$LEDGER" | grep '^- ' | sed 's/^- /- [ ] /'
  } >> "$LEDGER"
  mapfile -t PICKS < <(pick)
  log "上一轮写完，开第${ROUND}轮"
fi
E1="${PICKS[0]}"; E2="${PICKS[1]}"

if [ "${1:-}" = "--dry-run" ]; then echo "今天轮到：$E1 ／ $E2"; exit 0; fi

git pull --rebase -q origin main 2>>"$LOG" || log "git pull 失败，用本地继续"

before=$(ls [0-9][0-9][0-9]-*.md | sort)
log "开始：$E1 ／ $E2"

read -r -d '' PROMPT <<PEOF
你在微小说集《照常》的仓库里。先完整读 CLAUDE.md，再读 001–004 四篇范本和序号最大的三篇找语感。

今天写两篇新篇，主情绪由轮值表 ROTATION.md 指定，不要换：
- 第一篇：$E1
- 第二篇：$E2

要求：
1. 每篇动笔前，先按 CLAUDE.md「动笔前三问」用大白话答一遍（写在输出里，不进正文）。答不出就换事，不换情绪。再按「情绪的谱」里这种情绪那一条找压强来源和身体从哪漏。
2. 严格守 CLAUDE.md 全部规范：正文 150–250 字含标点、直角引号、一场戏、没人哭也没人笑出声、有「照常」一句、结尾落在画面上、标题是短名词。文件名 NNN-标题.md，序号取当前最大 +1、+2，正文以「# 标题」开头。
3. 题材、人物关系、核心物件不要和已有各篇撞车：先 ls 看一遍标题，grep 一下你打算用的核心物件。
4. 写完自查：这篇让读者替谁疼（或替谁松一口气）？在哪一句？答不上就回炉。再做复述检验：用一句大白话复述发生了什么（谁、因为什么、结果），复述里的每个事实正文都要给实；给不出就补一笔白描，不补解释。前提（出过什么事、机制怎么卡住人）必须有一句人话坐实，不能只靠暗示。
5. 登记：在 ROTATION.md 里把「- [ ] $E1」改成「- [x] $E1 → $TODAY NNN《标题》」，第二篇同样；在 CLAUDE.md「情绪的谱」里把篇名补进对应格子，若该情绪在「待写清单」里就从清单删掉。
6. 不要 commit、push、deploy，脚本会做。
7. 最后输出交付说明：每篇的主副情绪、哪几笔是留白、对比怎么搭。
PEOF

timeout 3600 claude -p "$PROMPT" --dangerously-skip-permissions --output-format text >> "$LOG" 2>&1
rc=$?
[ $rc -ne 0 ] && log "claude 退出码 $rc"

after=$(ls [0-9][0-9][0-9]-*.md | sort)
mapfile -t NEW < <(comm -13 <(echo "$before") <(echo "$after"))
if [ "${#NEW[@]}" -eq 0 ]; then log "没有新篇，不登记，明天重试同两个情绪"; exit 1; fi
[ "${#NEW[@]}" -ne 2 ] && log "注意：新篇数是 ${#NEW[@]}，不是 2"

# 兜底登记：模型没改 ROTATION.md 的话，脚本按顺序补上
i=0
for E in "$E1" "$E2"; do
  f="${NEW[$i]:-}"; [ -z "$f" ] && break
  title="${f:4}"; title="${title%.md}"; num="${f:0:3}"
  if grep -qF -- "- [ ] $E" "$LEDGER"; then
    python3 - "$LEDGER" "$E" "$TODAY $num《$title》" <<'PY'
import sys
p,e,mark=sys.argv[1:]
s=open(p,encoding='utf8').read()
s=s.replace(f"- [ ] {e}\n", f"- [x] {e} → {mark}\n",1)
open(p,'w',encoding='utf8').write(s)
PY
    log "兜底登记 $E → $num《$title》"
  fi
  i=$((i+1))
done

bash site/deploy.sh >>"$LOG" 2>&1 || log "deploy 失败"
git add -- [0-9][0-9][0-9]-*.md CLAUDE.md ROTATION.md site/stories.js
if ! git diff --cached --quiet; then
  titles=$(printf '《%s》' "${NEW[@]%.md}" | sed 's/《[0-9]*-/《/g')
  git commit -q -m "每日两篇（$E1 ／ $E2）：$titles

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
  git push -q origin main 2>>"$LOG" || log "push 失败"
fi
log "完成：${NEW[*]}"
