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
REPO = Path(__file__).resolve().parents[1]
STATE = Path.home() / 'Library/Application Support/story-daily'
STORY = re.compile(r'^\d{3}-.+\.md$')
JOB = re.compile(r'^\d{4}-\d{2}-\d{2}$')  # Only date-named files are jobs; anything else in jobs/ is ignored.


class TaskError(RuntimeError):
    pass


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


def prompt(picks, day):
    return f'''你在微小说集《照常》的仓库里。先完整读 CLAUDE.md，再读 001–004 四篇范本和序号最大的三篇找语感。
今天写且只写两篇新篇，主情绪由 ROTATION.md 指定，不要换：
- 第一篇：{picks[0]}
- 第二篇：{picks[1]}
要求：
1. 每篇动笔前，按 CLAUDE.md「动笔前三问」用大白话回答（在输出里，不进正文）；答不出就换事，不换情绪。按「情绪的谱」找压强来源和身体从哪漏。主副情绪先选定，交稿时删掉草稿标记。
2. 严格守全部写作规范：正文150–250字含标点、直角引号、第一人称、一场戏、没人哭也没人笑出声、有「照常」一句、结尾落在画面上、标题是短名词。文件名NNN-标题.md，序号当前最大+1、+2，首行「# 标题」。只新增两篇，不改已有小说。
3. 先列标题，再搜索拟用核心物件，题材、人物关系、核心物件不要和已有篇目撞车。
4. 自查这篇让读者替谁疼（或松一口气）、在哪一句，答不上回炉。复述检验：用一句大白话复述谁、因为什么、结果；每个事实正文必须给实，缺了补白描，不补解释。出过什么事、机制怎么卡住人的前提用一句人话坐实；事件不直写、情绪不点破。赌注必须是人、命、家、一辈子；叙述者也被碾到；身体先于纸面，爱是燃料。注意既有连作共享事实。
5. ROTATION.md把对应两条未写项分别登记为「- [x] 情绪 → {day} NNN《标题》」；CLAUDE.md情绪的谱对应格子补篇名，写过的从待写清单移除。
6. 只允许改两篇新文件、ROTATION.md、CLAUDE.md。不要commit、push、deploy、发微信、调用其他自动化或改定时任务，外层脚本会做。
7. 最后输出每篇主副情绪、哪几笔留白、对比怎么搭。'''


class Runner:
    def __init__(self, repo=REPO, state=STATE, now=None):
        self.repo, self.state = Path(repo), Path(state)
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
            if (not lines or lines[0] != '# ' + title or not 150 <= count <= 250
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
            short = re.sub(r'（[^（）]+）$', '', emotion)
            pattern = r'^- \*\*' + re.escape(short) + r'.*$'
            entry = re.search(pattern, guide, re.M)
            if not entry:
                raise TaskError(f'找不到情绪格子：{emotion}')
            if f'《{title}》' not in entry[0]:
                guide = guide[:entry.start()] + entry[0] + f'；《{title}》' + guide[entry.end():]
            # Remove a completed emotion from the selection pool if Claude missed it.
            pool = re.search(r'(### 待写清单[^\n]*\n)(.*?)(?=\n## |\Z)', guide, re.S)
            if pool:
                choices = pool[2].strip().split(' · ')
                remaining = [choice for choice in choices
                             if choice.strip() not in {short, short + '（正面）'}]
                if remaining != choices:
                    guide = guide[:pool.start(2)] + '\n' + ' · '.join(remaining) + '\n' + guide[pool.end(2):]
        path.write_text(text, encoding='utf-8')
        rules.write_text(guide, encoding='utf-8')

    def start(self):
        if self.changes():
            raise TaskError('工作区不干净，暂停生成；请先保存本地开发改动')
        if self.git('branch', '--show-current') != 'main':
            raise TaskError('自动写作要求 main 分支')
        self.git('pull', '--ff-only', 'origin', 'main')
        if self.changes():
            raise TaskError('拉取后工作区不干净')
        picks, ledger = rotation((self.repo / 'ROTATION.md').read_text(encoding='utf-8'))
        job = dict(date=self.day, status='generating', delivery='pending', delivery_format='images', picks=picks,
                   before=self.files(), base_head=self.git('rev-parse', 'HEAD'))
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

    def resume(self, job):
        if job['status'] == 'complete':
            return
        if job['status'] in {'generating', 'manual_review'}:
            raise TaskError(f'{job["date"]} 生成中断或校验失败；保留现场，需人工处理 {self.state / "jobs"}')
        if job['status'] not in {'generated', 'committing', 'committed', 'pushed', 'published'}:
            raise TaskError('未知任务状态；请人工检查')
        if job['status'] != 'published':
            self.publish(job)
        self.send(job)

    def tick(self, scheduled=False):
        jobs = sorted(p for p in (self.state / 'jobs').glob('*.json') if JOB.match(p.stem))
        today_exists = False
        for path in jobs:
            job = json.loads(path.read_text(encoding='utf-8'))
            today_exists |= job['date'] == self.day
            self.resume(job)  # Older unsent jobs are retried even before today's 06:00.
        if today_exists or (scheduled and self.now.hour < 6):
            return
        self.resume(self.start())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--scheduled', action='store_true')
    args = parser.parse_args(argv)
    if args.dry_run:
        picks, _ = rotation((REPO / 'ROTATION.md').read_text(encoding='utf-8'))
        print('下一次轮到：' + ' ／ '.join(picks))
        return 0
    try:
        with locked(STATE) as acquired:
            if not acquired:
                print('上一次任务还在运行，跳过')
                return 0
            Runner().tick(args.scheduled)
        return 0
    except (TaskError, OSError, ValueError, KeyError) as exc:
        print(f'[{dt.datetime.now(BEIJING).isoformat(timespec="seconds")}] {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
