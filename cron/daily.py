#!/usr/bin/env python3
"""Local, resumable daily writing. State is private and separate from the repo."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

BEIJING = dt.timezone(dt.timedelta(hours=8))
SOURCE = Path(__file__).resolve().parents[1]  # where this code lives; holds the real .git
STATE = Path.home() / 'Library/Application Support/story-daily'
# The task works in its own detached worktree so the user's checkout can never race it.
REPO = Path(os.environ.get('STORY_REPO') or STATE / 'worktree')
STORY = re.compile(r'^\d{3}-.+\.md$')
CELL = re.compile(r'^- \*\*([^*\n]+)\*\*.*$', re.M)  # 「- **好笑**——…」: one cell of CLAUDE.md「情绪的谱」
JOB = re.compile(r'^\d{4}-\d{2}-\d{2}$')  # Only date-named files are jobs; anything else in jobs/ is ignored.
# Alerts run this checkout's sender, next to this file: a broken worktree must still be able to speak.
SENDER = SOURCE / 'cron/wechat_send.py'
STEPS = {'generating': '两篇没写成', 'manual_review': '两篇没写成', 'generated': '写好了没提交',
         'committing': '写好了没提交', 'committed': '提交了没推上去', 'pushed': '推上去了没上站',
         'published': '上站了没发出来'}
DONE = {'published', 'complete'}  # nothing left that a human could hurry along; delivery waits on WeChat


class TaskError(RuntimeError):
    pass


def report(message):
    print(f'[{dt.datetime.now(BEIJING).isoformat(timespec="seconds")}] {message}', file=sys.stderr)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'run.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def rotation(text):
    """Return picks and prospective ledger; dry runs never write the latter."""
    pending = re.findall(r'^- \[ \] (.+)$', text, re.M)
    if len(pending) < 2:
        block = re.search(r'<!-- 轮值顺序 -->(.*?)<!-- 轮值顺序结束 -->', text, re.S)
        if not block:
            raise TaskError('轮值顺序缺失')
        entries = re.findall(r'^- (.+)$', block[1], re.M)
        if len(entries) < 2:
            raise TaskError('轮值顺序不足两条')
        number = len(re.findall(r'^## 第', text, re.M)) + 1
        text += f'\n## 第{number}轮\n\n' + ''.join(f'- [ ] {e}\n' for e in entries)
        pending += entries
    picks = pending[:2]
    categories = [re.search(r'（([^（）]+)）$', item) for item in picks]
    if not all(categories) or categories[0][1] == categories[1][1]:
        raise TaskError('轮值前两条必须属于不同大类，请先调整 ROTATION.md')
    return picks, text


def short_name(emotion):
    """A rotation entry without its category suffix: 「整篇好笑（其他）」 -> 「整篇好笑」."""
    return re.sub(r'（[^（）]+）$', '', emotion).strip()


def cells(guide):
    """Every emotion cell of CLAUDE.md, as (name, start, end) spans of the text given.

    Only 「情绪的谱」 is read, so a bold bullet living elsewhere in CLAUDE.md can never be taken for
    a cell; a CLAUDE.md without that heading falls back to the whole document, as before.
    """
    head = re.search(r'^## 情绪的谱.*$', guide, re.M)
    body, offset = guide, 0
    if head:
        offset = head.end()
        body = guide[offset:]
        tail = re.search(r'^## ', body, re.M)
        if tail:
            body = body[:tail.start()]
    return [(m[1], offset + m.start(), offset + m.end()) for m in CELL.finditer(body)]


def slot(guide, emotion):
    """The one cell a rotation entry registers into, as (name, start, end). Never guesses.

    Both names are hand-written and drift apart: the entry carries its category
    (「整篇好笑（其他）」) and is sometimes a longer way of saying the cell (「整篇好笑」 for the cell
    「好笑」). Three rules are tried in order, and each must hit exactly one cell:
      1. same name                            「安宁／幸福」 -> 「安宁／幸福」
      2. the cell name starts with the entry  「失望」       -> 「失望（等的人没来／…）」
      3. the cell name ends the entry name    「整篇好笑」   -> 「好笑」, cell name at least 2 characters
    Nothing looser: no similarity scores, no substring in the middle, no one-character tails. Two
    hits stops the day exactly like none — a story registered in the wrong cell is worse than a day
    that waits for a human, and the message below has to be enough to fix the right side by hand.
    """
    short = short_name(emotion)
    found = cells(guide)
    for rule, hits in (('同名', [c for c in found if c[0] == short]),
                       ('格子名以条目名开头', [c for c in found if c[0].startswith(short)]),
                       ('格子名是条目名的结尾', [c for c in found if len(c[0]) > 1 and short.endswith(c[0])])):
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise TaskError(f'情绪格子不唯一：ROTATION.md 的「{emotion}」按「{rule}」在 CLAUDE.md'
                            f'「情绪的谱」里命中 {len(hits)} 个格子（{"、".join(h[0] for h in hits)}）；'
                            '请把两边改成一一对应')
    # Nearby means only「名字里有相同的字」, and the parenthetical gloss of a cell does not count:
    # enough to point a human at the line to rename, never enough to rename it for them.
    near = '、'.join([c[0] for c in found if set(c[0].split('（')[0]) & set(short)][:5])
    raise TaskError(f'找不到情绪格子：ROTATION.md 的「{emotion}」去掉大类按「{short}」找，'
                    f'CLAUDE.md「情绪的谱」的 {len(found)} 个格子里没有一个同名、以它开头或作它的结尾'
                    + (f'；字面沾边的有：{near}' if near else '')
                    + '。把格子改成同名，或让条目以格子名结尾')


def audit(ledger, guide):
    """Rotation entries CLAUDE.md cannot answer for, as {entry: reason}; empty means every day can register."""
    block = re.search(r'<!-- 轮值顺序 -->(.*?)<!-- 轮值顺序结束 -->', ledger, re.S)
    problems = {}
    for entry in re.findall(r'^- (.+)$', block[1], re.M) if block else []:
        try:
            slot(guide, entry)
        except TaskError as exc:
            problems[entry] = str(exc)
    return problems


def environment():
    env = dict(os.environ)
    env.pop('CLAUDECODE', None)
    env.pop('CLAUDE_CODE_ENTRYPOINT', None)
    env['PATH'] = str(Path.home() / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:' + env.get('PATH', '/usr/bin:/bin')
    if not env.get('CLAUDE_CODE_OAUTH_TOKEN'):
        profile = Path.home() / '.zshrc'
        if profile.exists():
            for line in profile.read_text(encoding='utf-8').splitlines():
                if not re.match(r'^\s*export\s+CLAUDE_CODE_OAUTH_TOKEN=', line):
                    continue
                # Literal exports only. Never execute shell expansion or other code.
                try:
                    words = shlex.split(line, comments=True)
                except ValueError:
                    continue
                if len(words) == 2 and words[0] == 'export':
                    value = words[1].partition('=')[2]
                    if value and not any(c in value for c in '$`\n;'):
                        env['CLAUDE_CODE_OAUTH_TOKEN'] = value
                        break
    return env


def alerted(state):
    """Text of the last alert sent under each key. Damage here must not silence a new alert."""
    try:
        value = json.loads((state / 'alerts.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def alert(state, key, text, env=None):
    """Say one plain sentence on WeChat, once per fault. Never raises: alerting must not add faults."""
    try:
        if alerted(state).get(key) == text:
            return False  # the same fault every five minutes would bury the one that matters
        if not SENDER.is_file():
            raise TaskError(f'找不到发送脚本 {SENDER}')
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state / 'alert.txt'
        path.write_text(text + '\n', encoding='utf-8')
        path.chmod(0o600)
        code = subprocess.run([sys.executable, str(SENDER), str(path)], cwd=SENDER.parent,
                              env=env or environment(), timeout=180).returncode
        # 75 is WeChat asleep, locked or busy: nothing was sent and nothing is wrong. Retry next tick.
        if code == 75:
            return False
        # Any other code either sent it or left the outcome uncertain, and wechat_send.py never
        # repeats an uncertain send; record it either way so this cannot become a second message.
        ledger = alerted(state)
        ledger[key] = text
        save(state / 'alerts.json', ledger)
        if code:
            report(f'告警未必发出（退出码 {code}），不再重发：{text}')
        return not code
    except (TaskError, OSError, ValueError, subprocess.SubprocessError) as exc:
        report(f'告警发送失败，任务状态不受影响：{type(exc).__name__}：{exc}')
        return False


def overdue(state, now, env=None):
    """Past the morning deadline with today unfinished: the failure no exception would report."""
    now = now.astimezone(BEIJING)
    day = now.date().isoformat()
    if now.hour < 8:  # 06:00 start plus two hours of slack for a Mac that woke up late
        return False
    try:
        job = json.loads((state / 'jobs' / (day + '.json')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        job = None
    status = job.get('status') if isinstance(job, dict) else None
    # Everything but delivery done means WeChat is asleep; that is a wait, not a fault.
    if status in DONE:
        return False
    # This day already spoke for itself: one incident is worth exactly one message.
    if 'job-' + day in alerted(state):
        return False

    def moved(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    # A run in progress keeps touching these. Only a scheduler that stopped moving — a hung run
    # holding the lock, a task that never got going — leaves both untouched for an hour.
    if now.timestamp() - max(moved(state / 'jobs' / (day + '.json')), moved(state / 'heartbeat.json')) < 3600:
        return False
    return alert(state, 'day-' + day, f'{now.month}月{now.day}日到八点还没写成两篇：'
                 f'任务状态「{status or "今天没有任务记录"}」，也没有报错。需要人动手看一眼。', env)


def command(args, repo, env, timeout=300, log=None):
    process = subprocess.Popen(args, cwd=repo, env=env, stdout=log or subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise TaskError(f'{args[0]} 超时，已终止进程组')
    if process.returncode:
        # Do not echo command arguments or authentication-related child output.
        raise TaskError(f'{args[0]} 失败，退出码 {process.returncode}')
    return (output or '').strip()


def bootstrap(repo, env, source=None):
    """Prepare the private worktree and its ignored dependencies; safe to repeat every run."""
    source, repo = Path(source or SOURCE).resolve(), Path(repo).resolve()
    if repo == source:
        return  # A plain checkout is its own workspace; never touch the user's files.
    if not (repo / '.git').exists():
        if repo.exists():
            repo.rmdir()  # An empty leftover blocks `worktree add`; anything else is a scene to keep.
        command(['git', 'fetch', 'origin'], source, env)
        command(['git', 'worktree', 'prune'], source, env)  # forget a worktree the user deleted
        command(['git', 'worktree', 'add', '--detach', str(repo), 'origin/main'], source, env)
    lock = repo / 'package-lock.json'
    stamp = repo / 'node_modules/.story-install'
    if lock.is_file():
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        # `git reset --hard` keeps ignored files: install only when they are missing or stale.
        if not stamp.is_file() or stamp.read_text(encoding='utf-8') != digest:
            command(['npm', 'ci'], repo, env, timeout=900)
            stamp.write_text(digest, encoding='utf-8')


def prompt(picks, day):
    return f'''你在微小说集《照常》的仓库里。先完整读 CLAUDE.md，再读 001–004 四篇范本和序号最大的三篇找语感。
今天写且只写两篇新篇，主情绪由 ROTATION.md 指定，不要换：
- 第一篇：{picks[0]}
- 第二篇：{picks[1]}
要求：
1. 每篇动笔前，按 CLAUDE.md「动笔前三问」用大白话回答（在输出里，不进正文）；答不出就换事，不换情绪。按「情绪的谱」找压强来源和身体从哪漏。主副情绪先选定，交稿时删掉草稿标记。
2. 严格守全部写作规范：正文以200字为准、180–220字含标点（贴着上限写就是塞了第二场戏，先删场不删字）、直角引号、第一人称、一场戏、没人哭也没人笑出声、有「照常」一句、结尾落在画面上、标题是短名词。文件名NNN-标题.md，序号当前最大+1、+2，首行「# 标题」。只新增两篇，不改已有小说。
3. 先列标题，再搜索拟用核心物件，题材、人物关系、核心物件不要和已有篇目撞车。
4. 自查这篇让读者替谁疼（或松一口气）、在哪一句，答不上回炉。复述检验：用一句大白话复述谁、因为什么、结果；每个事实正文必须给实，缺了补白描，不补解释。出过什么事、机制怎么卡住人的前提用一句人话坐实；事件不直写、情绪不点破。赌注必须是人、命、家、一辈子；叙述者也被碾到；身体先于纸面，爱是燃料。注意既有连作共享事实。
5. ROTATION.md把对应两条未写项分别登记为「- [x] 情绪 → {day} NNN《标题》」；CLAUDE.md情绪的谱对应格子补篇名，写过的从待写清单移除。
6. 只允许改两篇新文件、ROTATION.md、CLAUDE.md。不要commit、push、deploy、发微信、调用其他自动化或改定时任务，外层脚本会做。
7. 最后输出每篇主副情绪、哪几笔留白、对比怎么搭。'''


class Runner:
    def __init__(self, repo=REPO, state=STATE, now=None):
        self.repo, self.state = Path(repo).resolve(), Path(state)
        self.now = now or dt.datetime.now(BEIJING)
        self.now = self.now.astimezone(BEIJING)
        self.day = self.now.date().isoformat()
        self.env = environment()

    def run(self, args, timeout=300, log=None):
        return command(args, self.repo, self.env, timeout, log)

    def git(self, *args):
        return self.run(['git', *args])

    def record(self, job):
        save(self.state / 'jobs' / (job['date'] + '.json'), job)

    def files(self):
        return sorted(p.name for p in self.repo.iterdir() if STORY.fullmatch(p.name))

    def changes(self):
        # -z handles names containing spaces without shell parsing.
        changed = self.git('diff', '--name-only', '-z').split('\0')
        staged = self.git('diff', '--cached', '--name-only', '-z').split('\0')
        untracked = self.git('ls-files', '--others', '--exclude-standard', '-z').split('\0')
        return set(changed + staged + untracked) - {''}

    def validate(self, job):
        new = sorted(set(self.files()) - set(job['before']))
        highest = max([int(f[:3]) for f in job['before']] or [0])
        if len(new) != 2 or [int(f[:3]) for f in new] != [highest + 1, highest + 2]:
            raise TaskError('新增小说必须恰好两篇且序号连续；保留现场，请人工检查')
        for name in new:
            title = name[4:-3]
            lines = (self.repo / name).read_text(encoding='utf-8').splitlines()
            body = '\n'.join(lines[1:]).strip()
            count = len(re.sub(r'\s', '', body))
            if (not lines or lines[0] != '# ' + title or not 150 <= count <= 230
                    or '照常' not in body or any(c in body for c in '“”"')
                    or body.count('「') != body.count('」')
                    or re.search(r'^\s*#|^\s*```', body, re.M)):
                raise TaskError(f'{name} 格式、字数或写作硬性规格未通过；请人工检查')
        allowed = set(new) | {'CLAUDE.md', 'ROTATION.md'}
        if self.changes() - allowed:
            raise TaskError('Claude 修改了允许范围外的文件；请人工检查')
        return new

    def register(self, job):
        path = self.repo / 'ROTATION.md'
        text = path.read_text(encoding='utf-8')
        rules = self.repo / 'CLAUDE.md'
        guide = rules.read_text(encoding='utf-8')
        for emotion, name in zip(job['picks'], job['files']):
            title, number = name[4:-3], name[:3]
            mark = f'- [x] {emotion} → {job["date"]} {number}《{title}》'
            if mark not in text:
                text, replaced = re.subn(r'^' + re.escape('- [ ] ' + emotion) + r'$',
                                        lambda _: mark, text, count=1, flags=re.M)
                if not replaced:
                    raise TaskError(f'轮值登记不一致：{emotion}')
            short = short_name(emotion)
            cell, begin, end = slot(guide, emotion)  # start() proved this resolves before Claude wrote
            line = guide[begin:end]
            if f'《{title}》' not in line:
                guide = guide[:begin] + line + f'；《{title}》' + guide[end:]
            # Remove a completed emotion from the selection pool if Claude missed it.
            pool = re.search(r'(### 待写清单[^\n]*\n)(.*?)(?=\n## |\Z)', guide, re.S)
            if pool:
                choices, remaining = pool[2].strip().split(' · '), []
                for choice in choices:
                    label = choice.strip()
                    if label in {short, short + '（正面）'}:
                        continue
                    try:
                        if slot(guide, label)[0] == cell:
                            continue  # the pool spells the same cell another way (「倦怠」 for 「倦怠／厌倦」)
                    except TaskError:
                        pass  # free text belonging to no cell: leave the pool exactly as it is
                    remaining.append(choice)
                if remaining != choices:
                    guide = guide[:pool.start(2)] + '\n' + ' · '.join(remaining) + '\n' + guide[pool.end(2):]
        path.write_text(text, encoding='utf-8')
        rules.write_text(guide, encoding='utf-8')

    def start(self):
        if self.changes():
            raise TaskError('工作区不干净，暂停生成；请先保存本地开发改动')
        self.git('fetch', 'origin')
        # A detached worktree has no branch name; what matters is that nothing here is unpushed.
        if self.git('rev-list', '--count', 'HEAD', '^origin/main') != '0':
            raise TaskError('有未推送的提交，暂停生成；请人工处理后再恢复')
        # Only start() may reset: resume() can meet stories written but not yet committed.
        self.git('reset', '--hard', 'origin/main')
        if self.changes():
            raise TaskError('同步 origin/main 后工作区不干净')
        text = (self.repo / 'ROTATION.md').read_text(encoding='utf-8')
        guide = (self.repo / 'CLAUDE.md').read_text(encoding='utf-8')
        picks, ledger = rotation(text)
        # Registering happens after the writing, so a cell that cannot be found used to cost a whole
        # day: two finished stories, an hour of Claude, and a task that failed at the last step.
        # Resolve today's two cells first, on the same rules register() will use, and stop here.
        for emotion in picks:
            slot(guide, emotion)
        later = {e: why for e, why in audit(text, guide).items() if e not in picks}
        if later:  # the rest of the ledger is a warning, not a stop: those days are not today's
            report(f'轮值表另有 {len(later)} 条在 CLAUDE.md 找不到唯一格子，轮到那天会停下：' + '、'.join(later))
        job = dict(date=self.day, status='generating', delivery='pending', delivery_format='images', picks=picks,
                   repo=str(self.repo), before=self.files(), base_head=self.git('rev-parse', 'HEAD'))
        self.record(job)  # Intent precedes any generation; a crash must never write again.
        (self.repo / 'ROTATION.md').write_text(ledger, encoding='utf-8')
        try:
            executable = shutil.which('claude', path=self.env['PATH'])
            if not executable:
                raise TaskError('找不到 claude')
            with (self.state / (self.day + '-claude.log')).open('a', encoding='utf-8') as log:
                self.run([executable, '--model', 'opus', '--effort', 'xhigh', '-p',
                          prompt(picks, self.day), '--dangerously-skip-permissions',
                          '--output-format', 'text'], timeout=3600, log=log)
            job['files'] = self.validate(job)
            self.register(job)
            job['hashes'] = {name: hashlib.sha256((self.repo / name).read_bytes()).hexdigest()
                             for name in job['files'] + ['CLAUDE.md', 'ROTATION.md']}
            message = f'《照常》每日两篇 · {self.day}\n\n' + '\n\n'.join(
                (self.repo / name).read_text(encoding='utf-8').strip() for name in job['files'])
            message_path = self.state / 'jobs' / (self.day + '.txt')
            message_path.write_text(message + '\n', encoding='utf-8')
            message_path.chmod(0o600)
            job.update(status='generated', message_file=str(message_path))
            self.record(job)
        except Exception as exc:
            job.update(status='manual_review', error=str(exc))
            self.record(job)
            raise
        return job

    def publish(self, job):
        for name, digest in job['hashes'].items():
            if hashlib.sha256((self.repo / name).read_bytes()).hexdigest() != digest:
                raise TaskError('待发布文件已被改动；请人工检查状态与正文')
        allowed = set(job['files']) | {'CLAUDE.md', 'ROTATION.md', 'site/stories.js'}
        if job['status'] == 'generated':
            if self.changes() - allowed or self.git('rev-parse', 'HEAD') != job['base_head']:
                raise TaskError('生成后仓库已被其他工作修改；请人工检查')
            self.run(['node', 'site/build.js'])
            self.git('add', '--', *sorted(allowed))
            job['status'] = 'committing'
            self.record(job)
        if job['status'] == 'committing':
            head = self.git('rev-parse', 'HEAD')
            marker = f'story-daily:{job["date"]}'
            if head == job['base_head']:
                if self.changes() - allowed:
                    raise TaskError('提交前发现其他工作改动')
                self.git('commit', '-m', f'每日两篇：{ " ／ ".join(job["picks"]) }\n\n{marker}')
            elif (self.git('rev-parse', 'HEAD^') != job['base_head']
                  or marker not in self.git('log', '-1', '--format=%B')):
                raise TaskError('提交状态不确定；请人工检查')
            job.update(status='committed', commit=self.git('rev-parse', 'HEAD'))
            self.record(job)
        if job['status'] in {'committed', 'pushed'}:
            if self.changes() or self.git('rev-parse', 'HEAD') != job['commit']:
                raise TaskError('发布前工作区或提交已变化；请人工检查')
        if job['status'] == 'committed':
            self.git('push', 'origin', 'HEAD:main')
            job['status'] = 'pushed'
            self.record(job)
        if job['status'] == 'pushed':
            self.run(['bash', 'site/deploy.sh'], timeout=600)
            job['status'] = 'published'
            self.record(job)

    def prepare_images(self, job):
        output_dir = (self.state / 'jobs' / (job['date'] + '-images')).resolve()

        def valid_images(images):
            if not isinstance(images, list) or len(images) != 2 or not all(isinstance(p, str) for p in images):
                return False
            paths = [Path(p) for p in images]
            if len({p.resolve() for p in paths}) != 2:
                return False
            for path in paths:
                if (not path.is_absolute() or path.resolve().parent != output_dir
                        or path.suffix.lower() != '.png' or not path.is_file()):
                    return False
                with path.open('rb') as stream:
                    if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                        return False
            return True

        if valid_images(job.get('image_files')):
            return job['image_files']
        args = ['node', 'cron/render_share.mjs', '--output-dir', str(output_dir)]
        files = job.get('files', [])
        if len(files) != 2 or not all(STORY.fullmatch(name) for name in files):
            raise TaskError('分享卡片需要任务中的两篇小说文件名；请人工检查')
        for name in files:
            args.extend(['--story', str(int(name[:3]))])
        try:
            result = json.loads(self.run(args, timeout=180))
            images = result.get('images') if isinstance(result, dict) else None
            if not valid_images(images):
                raise TaskError('渲染器未返回两张有效的PNG分享卡片')
        except (TaskError, OSError, ValueError) as exc:
            job['error'] = f'分享卡片生成失败，可重试：{exc}'
            self.record(job)
            raise TaskError(job['error']) from exc
        job['image_files'] = images
        job.pop('error', None)
        self.record(job)
        return images

    def send(self, job):
        if job.get('delivery') == 'sent':
            job['status'] = 'complete'
            self.record(job)
            return
        if job.get('delivery') == 'sending':
            raise TaskError(f'{job["date"]} 微信发送结果不确定，请核实聊天后人工处理；自动重发已停止')
        args = [sys.executable, str(self.repo / 'cron/wechat_send.py')]
        delivery_format = job.get('delivery_format', 'text')
        if delivery_format == 'images':
            args.extend(['--images', *self.prepare_images(job)])
        elif delivery_format == 'text':
            message = Path(job['message_file'])
            if not message.is_file():
                raise TaskError('待发送正文丢失；请人工检查')
            args.append(str(message))
        else:
            raise TaskError(f'未知发送格式：{delivery_format}')
        job['delivery'] = 'sending'
        self.record(job)
        try:
            result = subprocess.run(args, cwd=self.repo, env=self.env, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            job['error'] = type(exc).__name__ + '：发送结果不确定'
            self.record(job)
            raise TaskError(job['error'])
        if result.returncode == 75:
            job['delivery'] = 'pending'
            job['error'] = '微信未就绪，稍后重试'
            self.record(job)
            return
        if result.returncode != 0:
            job['error'] = f'微信退出码 {result.returncode}，结果不确定；请人工核实'
            self.record(job)
            raise TaskError(job['error'])
        job.update(status='complete', delivery='sent')
        job.pop('error', None)
        self.record(job)

    def stale(self):
        """Last error of each unreadable job file. Its own damage must not break this read."""
        try:
            value = json.loads((self.state / 'broken.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def job(self, date):
        """The recorded job for a day, or None when it is missing, damaged or misnamed."""
        try:
            job = json.loads((self.state / 'jobs' / (date + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return job if isinstance(job, dict) and job.get('date') == date else None

    def fault(self, date, exc):
        """Tell a human once: which day, which step it stopped at, and whether anyone must act."""
        exc.alerted = True  # main() must not repeat what this sentence already carries
        job = self.job(date) or {}
        status = job.get('status')
        step = STEPS.get(status) or ('任务记录读不出来' if (self.state / 'jobs' / (date + '.json')).exists()
                                     else '两篇没写成')
        # The job's own recorded error keeps this sentence identical on every path that reports the
        # same day, so a day stuck for a week costs one message, not one message a day.
        reason = job.get('error') or str(exc) or type(exc).__name__
        manual = job.get('delivery') == 'sending' or status in {None, 'generating', 'manual_review'}
        stamp = dt.date.fromisoformat(date)
        alert(self.state, 'job-' + date, f'{stamp.month}月{stamp.day}日{step}：{reason}。'
              + ('现场已保留，需要人动手。' if manual else '下一轮会自动重试，先不用管。'), self.env)

    def resolve(self, key):
        """A day that recovered must be able to speak again if it breaks later."""
        ledger = alerted(self.state)
        if ledger.pop(key, None) is not None:
            save(self.state / 'alerts.json', ledger)

    def beat(self):
        """A finished pass is the only proof this scheduler still runs; a watchdog outside reads it."""
        save(self.state / 'heartbeat.json',
             {'time': dt.datetime.now(BEIJING).isoformat(timespec='seconds'), 'day': self.day,
              'status': (self.job(self.day) or {}).get('status', 'none')})

    def defer(self, job, text):
        """Park a stuck job in its own file; report whether this failure repeats the last one."""
        repeated = job.get('last_error') == text
        # Separate from job['error']; the state machine reads neither of these two fields.
        job['last_error'] = text
        job['failures'] = job.get('failures', 0) + 1 if repeated else 1
        self.record(job)
        return repeated

    def resume(self, job):
        if job['status'] == 'complete':
            return
        # One STATE serves every checkout; only the repo that wrote a job may publish it.
        if job.get('repo', str(self.repo)) != str(self.repo):
            raise TaskError(f'{job["date"]} 由其他仓库生成（{job["repo"]}）；保留现场，请人工处理')
        if job['status'] in {'generating', 'manual_review'}:
            raise TaskError(f'{job["date"]} 生成中断或校验失败；保留现场，需人工处理 {self.state / "jobs"}')
        if job['status'] not in {'generated', 'committing', 'committed', 'pushed', 'published'}:
            raise TaskError('未知任务状态；请人工检查')
        if job['status'] != 'published':
            self.publish(job)
        self.send(job)

    def tick(self, scheduled=False):
        jobs = sorted(p for p in (self.state / 'jobs').glob('*.json') if JOB.match(p.stem))
        stale, broken, told = self.stale(), {}, alerted(self.state)
        today_exists = False
        for path in jobs:
            # JOB already proved the name is a date: identity comes from the file name, not its
            # content, so an unreadable file cannot decide whether today's two stories are written.
            today_exists |= path.stem == self.day
            job = None
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                # record() writes by job['date']; a file whose date is not its own name would
                # overwrite another day. Refuse it instead, and never rewrite the file itself.
                if not isinstance(job, dict) or job.get('date') != path.stem:
                    raise TaskError('任务文件损坏或与文件名不符；原样保留，请人工检查')
                self.resume(job)  # Older unsent jobs are retried even before today's 06:00.
            except (TaskError, OSError, ValueError, KeyError) as exc:
                # Today's own job is this run's output; only older jobs are retries to skip.
                if path.stem == self.day:
                    self.fault(self.day, exc)
                    raise
                text = f'{type(exc).__name__}：{exc}'
                if isinstance(job, dict) and job.get('date') == path.stem:
                    repeated = self.defer(job, text)  # A stuck job must never block today's two.
                else:
                    # No usable job here: keep the damaged file untouched and note it beside them.
                    broken[path.name], repeated = text, stale.get(path.name) == text
                # The same failure every five minutes must not bury the rest of the log.
                if not repeated:
                    report(f'{path.stem} 任务卡住，保留现场并跳过：{exc}')
                    self.fault(path.stem, exc)  # nobody reads the log; say it where a human looks
            else:
                if job.pop('last_error', None) is not None:
                    job.pop('failures', None)  # A recovered job must not silence its next failure.
                    self.record(job)
                # Only a finished day clears its alert, so a half-done one cannot flip-flop into noise.
                if job.get('status') == 'complete' and 'job-' + job['date'] in told:
                    self.resolve('job-' + job['date'])
        if broken != stale:
            save(self.state / 'broken.json', broken)  # Rebuilt each tick: a repaired file drops out.
        if not today_exists and not (scheduled and self.now.hour < 6):
            try:
                self.resume(self.start())  # Failure here must reach the caller and the exit code.
            except (TaskError, OSError, ValueError, KeyError) as exc:
                self.fault(self.day, exc)
                raise
        self.beat()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--scheduled', action='store_true')
    group.add_argument('--repo', action='store_true', help='打印本次任务干活的仓库路径')
    group.add_argument('--bootstrap', action='store_true', help='只准备工作仓库，不写作')
    args = parser.parse_args(argv)
    if args.repo:
        print(REPO)
        return 0
    if args.dry_run:
        # Dry runs create nothing: read the ledger from the source checkout until the worktree exists.
        ledger = REPO if (REPO / 'ROTATION.md').is_file() else SOURCE
        text = (ledger / 'ROTATION.md').read_text(encoding='utf-8')
        picks, _ = rotation(text)
        print('下一次轮到：' + ' ／ '.join(picks))
        # The preview is also the cheapest place to see which entries would stop a morning.
        if (ledger / 'CLAUDE.md').is_file():
            for entry, why in audit(text, (ledger / 'CLAUDE.md').read_text(encoding='utf-8')).items():
                report(f'轮值表「{entry}」还登记不了：{why}')
        return 0
    try:
        with locked(STATE) as acquired:
            if acquired:
                # Repair the workspace before any state machine runs; a deleted worktree heals here.
                bootstrap(REPO, environment())
                if args.bootstrap:
                    print('工作仓库就绪：' + str(REPO))
                    return 0
                Runner(REPO).tick(args.scheduled)
            else:
                print('上一次任务还在运行，跳过')
        # After the run, never before it: a Mac that wakes up late writes today's two right here, and
        # must not be accused first. A run that hangs holding the lock reports nothing else at all.
        if args.scheduled:
            overdue(STATE, dt.datetime.now(BEIJING))
        return 0
    except (TaskError, OSError, ValueError, KeyError) as exc:
        report(exc)
        # A day's own fault already told a human; a failure outside one (bootstrap, lock, state) owes
        # them this one sentence. One key per day: an outage repeats daily, never every five minutes.
        if not getattr(exc, 'alerted', False):
            stamp = dt.datetime.now(BEIJING)
            alert(STATE, 'run-' + stamp.date().isoformat(),
                  f'{stamp.month}月{stamp.day}日的定时写作没能开工：{exc}。需要人动手。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
