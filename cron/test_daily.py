#!/usr/bin/env python3
"""Recovery tests use temporary git repos and never call Claude, SSH or WeChat."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('daily', Path(__file__).with_name('daily.py'))
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)

LEDGER = '''<!-- 轮值顺序 -->
- 失望（怒）
- 安宁／幸福（喜）
<!-- 轮值顺序结束 -->
## 第1轮
- [ ] 失望（怒）
- [ ] 安宁／幸福（喜）
'''


class FakeRunner(daily.Runner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.generated_count = 2
        self.generation_failure = False
        self.push_failure = False
        self.render_failure = False
        self.render_args = None

    def run(self, args, timeout=300, log=None):
        self.calls.append(args[:2])
        if args[:2] == ['git', 'push'] and self.push_failure:
            raise daily.TaskError('offline')
        if args[0] == 'fake-claude':
            for i in range(self.generated_count):
                title = ['门', '窗'][i]
                body = '我把手放在门上。' * 21 + '公交照常来。'
                (self.repo / f'{i+2:03}-{title}.md').write_text(f'# {title}\n\n{body}\n')
            if self.generation_failure:
                raise daily.TaskError('Claude failed')
            return ''
        if args[:2] == ['node', 'cron/render_share.mjs']:
            self.render_args = args
            if self.render_failure:
                raise daily.TaskError('renderer failed')
            output_dir = Path(args[3])
            output_dir.mkdir(parents=True, exist_ok=True)
            images = [output_dir / (number + '.png') for number in args[5::2]]
            for image in images:
                image.write_bytes(b'\x89PNG\r\n\x1a\nmock image')
            return json.dumps({'images': [str(image) for image in images]})
        if args[0] == 'node':
            (self.repo / 'site/stories.js').write_text('built stories')
            return ''
        if args[0] == 'bash':
            return ''
        return super().run(args, timeout, log)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin, self.source = root / 'origin.git', root / 'source'
        self.repo, self.state = root / 'worktree', root / 'state'
        self.source.mkdir()
        self.state.mkdir()
        (self.source / 'site').mkdir()
        (self.source / 'ROTATION.md').write_text(LEDGER)
        (self.source / 'CLAUDE.md').write_text('- **失望（等的人没来）**——待写。\n- **安宁／幸福**——待写。\n'
                                          '### 待写清单（选题池）\n\n失望 · 安宁／幸福 · 骄傲（正面）\n\n## 下节\n')
        (self.source / '001-旧.md').write_text('# 旧\n旧故事')
        (self.source / 'site/stories.js').write_text('old')
        subprocess.run(['git', 'init', '--bare', str(self.origin)], check=True, capture_output=True)
        # The real thing: a bare origin plus a detached worktree, so no test mocks away detached HEAD.
        for args in [('init', '-b', 'main'), ('config', 'user.name', 'Test'),
                     ('config', 'user.email', 'test@example.invalid'), ('add', '.'),
                     ('commit', '-m', 'initial'), ('remote', 'add', 'origin', str(self.origin)),
                     ('push', '-u', 'origin', 'main'),
                     ('worktree', 'add', '--detach', str(self.repo), 'origin/main')]:
            subprocess.run(['git', *args], cwd=self.source, check=True, capture_output=True)
        self.now = dt.datetime(2026, 9, 8, 6, tzinfo=daily.BEIJING)
        self.runner = FakeRunner(self.repo, self.state, self.now)
        self.claude = patch.object(daily.shutil, 'which', return_value='fake-claude')
        self.claude.start()
        self.addCleanup(self.claude.stop)

    def push_to_origin(self, name='NOTES.md'):
        """Advance origin/main behind the worktree's back, the way a manual push does."""
        (self.source / name).write_text('手写改动')
        for args in [('add', '.'), ('commit', '-m', 'manual'), ('push', 'origin', 'main')]:
            subprocess.run(['git', *args], cwd=self.source, check=True, capture_output=True)
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.source, check=True,
                              capture_output=True, text=True).stdout.strip()

    def head(self, repo=None):
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo or self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def read_job(self, day='2026-09-08'):
        return json.loads((self.state / 'jobs' / (day + '.json')).read_text())

    def sender(self, code):
        return patch.object(daily.subprocess, 'run', return_value=Mock(returncode=code))

    def test_dry_run_rollover_has_no_side_effects(self):
        text = LEDGER.replace('- [ ]', '- [x]')
        path = self.repo / 'ROTATION.md'
        path.write_text(text)
        with patch.object(daily, 'REPO', self.repo), patch.object(daily, 'STATE', self.state / 'absent'):
            self.assertEqual(daily.main(['--dry-run']), 0)
        self.assertEqual(path.read_text(), text)
        self.assertFalse((self.state / 'absent').exists())

    def test_same_category_rejected(self):
        with self.assertRaises(daily.TaskError):
            daily.rotation(LEDGER.replace('（喜）', '（怒）'))

    def test_generation_failure_never_generates_again(self):
        self.runner.generation_failure = True
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertEqual(self.read_job()['status'], 'manual_review')
        self.runner.generation_failure = False
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)

    def test_partial_generation_requires_manual_review(self):
        self.runner.generated_count = 1
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertEqual(self.read_job()['status'], 'manual_review')
        self.assertNotIn(['git', 'push'], self.runner.calls)
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)

    def test_success_idempotent_and_fallback_registered(self):
        with self.sender(0) as send:
            self.runner.tick()
            self.runner.tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['status'], 'complete')
        self.assertEqual(self.read_job()['delivery'], 'sent')
        self.assertEqual(self.read_job()['delivery_format'], 'images')
        self.assertEqual(send.call_args.args[0][2:], ['--images', *self.read_job()['image_files']])
        self.assertEqual(self.runner.render_args[-4:], ['--story', '2', '--story', '3'])
        self.assertEqual(sum(c == ['node', 'cron/render_share.mjs'] for c in self.runner.calls), 1)
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)
        self.assertIn('2026-09-08 002《门》', (self.repo / 'ROTATION.md').read_text())
        self.assertIn('《窗》', (self.repo / 'CLAUDE.md').read_text())
        self.assertIn('清单（选题池）\n\n骄傲（正面）', (self.repo / 'CLAUDE.md').read_text())
        self.assertEqual(self.runner.changes(), set())

    def test_foreign_file_in_jobs_dir_does_not_block_the_day(self):
        jobs = self.state / 'jobs'
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / '2026-09-07-rewrite.json').write_text(json.dumps({'status': 'complete'}))
        with self.sender(0) as send:
            self.runner.tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_previous_day_message_retried_before_six(self):
        with self.sender(75):
            self.runner.tick()
        self.assertEqual(self.read_job()['delivery'], 'pending')
        tomorrow = FakeRunner(self.repo, self.state, self.now + dt.timedelta(hours=23))
        with self.sender(0) as send:
            tomorrow.tick(scheduled=True)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['status'], 'complete')
        self.assertFalse((self.state / 'jobs/2026-09-09.json').exists())
        self.assertNotIn(['node', 'site/build.js'], tomorrow.calls)

    def test_uncertain_delivery_never_retried(self):
        with self.sender(1) as send:
            with self.assertRaises(daily.TaskError):
                self.runner.tick()
            with self.assertRaises(daily.TaskError):
                self.runner.tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['delivery'], 'sending')
        self.assertTrue(Path(self.read_job()['message_file']).is_file())

    def test_renderer_failure_retries_without_new_generation_or_publish(self):
        self.runner.render_failure = True
        with self.sender(0) as send:
            with self.assertRaises(daily.TaskError):
                self.runner.tick()
            self.assertEqual(send.call_count, 0)
            job = self.read_job()
            self.assertEqual(job['status'], 'published')
            self.assertEqual(job['delivery'], 'pending')
            self.runner.render_failure = False
            self.runner.tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)
        self.assertEqual(sum(c == ['bash', 'site/deploy.sh'] for c in self.runner.calls), 1)
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_old_text_job_keeps_text_delivery(self):
        job = self.runner.start()
        self.runner.publish(job)
        job.pop('delivery_format')
        self.runner.record(job)
        with self.sender(0) as send:
            self.runner.tick()
        self.assertEqual(send.call_args.args[0][2:], [job['message_file']])
        self.assertNotIn(['node', 'cron/render_share.mjs'], self.runner.calls)

    def test_published_job_can_switch_to_images_without_hash_check(self):
        job = self.runner.start()
        self.runner.publish(job)
        job['delivery_format'] = 'text'
        with self.sender(0):
            self.runner.send(job)
        job.update(status='published', delivery='pending', delivery_format='images')
        self.runner.record(job)
        # A later code/docs change must not block rerendering a published story.
        with (self.repo / 'CLAUDE.md').open('a') as stream:
            stream.write('Updated delivery instructions\n')
        with self.sender(0) as send:
            self.runner.tick()
        self.assertEqual(send.call_args.args[0][2], '--images')
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)
        self.assertEqual(sum(c == ['bash', 'site/deploy.sh'] for c in self.runner.calls), 1)

    def test_missing_pending_image_is_regenerated(self):
        with self.sender(75):
            self.runner.tick()
        Path(self.read_job()['image_files'][0]).unlink()
        with self.sender(0):
            self.runner.tick()
        self.assertEqual(sum(c == ['node', 'cron/render_share.mjs'] for c in self.runner.calls), 2)
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)

    def test_missing_uncertain_image_does_not_regenerate_or_resend(self):
        with self.sender(1):
            with self.assertRaises(daily.TaskError):
                self.runner.tick()
        Path(self.read_job()['image_files'][0]).unlink()
        with self.sender(0) as send:
            with self.assertRaises(daily.TaskError):
                self.runner.tick()
        self.assertEqual(send.call_count, 0)
        self.assertEqual(sum(c == ['node', 'cron/render_share.mjs'] for c in self.runner.calls), 1)
        self.assertEqual(self.read_job()['delivery'], 'sending')

    def test_push_failure_resumes_without_writing(self):
        self.runner.push_failure = True
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertEqual(self.read_job()['status'], 'committed')
        self.runner.push_failure = False
        with self.sender(0):
            self.runner.tick()
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_interrupted_older_job_does_not_block_the_day(self):
        scene = dict(date='2026-09-07', status='generating')
        self.runner.record(scene)
        log = io.StringIO()
        with self.sender(0) as send, contextlib.redirect_stderr(log):
            self.runner.tick()
        job = self.read_job('2026-09-07')
        # The stuck job keeps its scene; only the two log-quieting fields are added.
        self.assertEqual({k: v for k, v in job.items() if k not in {'last_error', 'failures'}}, scene)
        self.assertEqual(job['failures'], 1)
        self.assertEqual(len(log.getvalue().splitlines()), 1)
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_repeated_failure_is_logged_once(self):
        self.runner.record(dict(date='2026-09-07', status='generating'))
        first, second = io.StringIO(), io.StringIO()
        with self.sender(0):
            with contextlib.redirect_stderr(first):
                self.runner.tick()
            with contextlib.redirect_stderr(second):
                self.runner.tick()
        self.assertEqual(len(first.getvalue().splitlines()), 1)
        self.assertEqual(second.getvalue(), '')
        self.assertEqual(self.read_job('2026-09-07')['failures'], 2)
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)

    def test_broken_job_file_does_not_block_the_day(self):
        jobs = self.state / 'jobs'
        jobs.mkdir(parents=True, exist_ok=True)
        damaged = {'2026-09-05.json': 'not json at all',
                   '2026-09-06.json': json.dumps({'status': 'generated'})}  # the missing-date case
        for name, text in damaged.items():
            (jobs / name).write_text(text)
        log = io.StringIO()
        with self.sender(0) as send, contextlib.redirect_stderr(log):
            self.runner.tick()
        self.assertEqual(len(log.getvalue().splitlines()), 2)
        for name, text in damaged.items():
            self.assertEqual((jobs / name).read_text(), text)  # left untouched as evidence
        self.assertEqual(set(json.loads((self.state / 'broken.json').read_text())), set(damaged))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['status'], 'complete')
        with self.sender(0), contextlib.redirect_stderr(log):
            self.runner.tick()
        self.assertEqual(len(log.getvalue().splitlines()), 2)  # same two lines: nothing reprinted

    def test_lock_excludes_second_runner(self):
        with daily.locked(self.state) as first:
            self.assertTrue(first)
            with daily.locked(self.state) as second:
                self.assertFalse(second)
        with daily.locked(self.state) as third:
            self.assertTrue(third)

    def test_dirty_repo_blocks_generation(self):
        (self.repo / 'unrelated.txt').write_text('work in progress')
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertFalse((self.state / 'jobs').exists())
        self.assertNotIn(['git', 'fetch'], self.runner.calls)

    def test_generation_starts_from_origin_main(self):
        pushed = self.push_to_origin()
        with self.sender(0):
            self.runner.tick()
        # The worktree took the manual push before writing, so today's numbers follow it.
        self.assertTrue((self.repo / 'NOTES.md').is_file())
        self.assertEqual(self.read_job()['base_head'], pushed)
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_unpushed_commit_blocks_generation(self):
        (self.repo / 'NOTES.md').write_text('只在本地')
        for args in [('add', '.'), ('commit', '-m', 'local')]:
            subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True)
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertNotIn(['git', 'reset'], self.runner.calls)
        self.assertFalse((self.state / 'jobs').exists())
        self.assertTrue((self.repo / 'NOTES.md').is_file())

    def test_generated_stories_survive_a_later_resume(self):
        job = self.runner.start()
        self.assertEqual(job['status'], 'generated')
        tomorrow = FakeRunner(self.repo, self.state, self.now + dt.timedelta(hours=23))
        with self.sender(0) as send:
            tomorrow.tick(scheduled=True)  # 05:00: no new day begins; yesterday's job resumes
        # A reset on this path rolls back the ledger, and once committed it deletes both stories.
        self.assertNotIn(['git', 'reset'], tomorrow.calls)
        self.assertNotIn(['git', 'fetch'], tomorrow.calls)
        for name in job['files']:
            self.assertTrue((self.repo / name).is_file())
        self.assertIn('2026-09-08 002《门》', (self.repo / 'ROTATION.md').read_text())
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_rejected_push_keeps_the_scene(self):
        job = self.runner.start()
        self.push_to_origin()  # a manual push landed while Claude was writing
        with self.sender(0) as send:
            with self.assertRaises(daily.TaskError):
                self.runner.tick()  # never force-pushed: the day waits for a human
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.read_job()['status'], 'committed')
        for name in job['files']:
            self.assertTrue((self.repo / name).is_file())

    def test_second_checkout_does_not_publish_the_worktree_job(self):
        self.runner.start()
        other = FakeRunner(self.source, self.state, self.now)  # same STATE, different repo
        with self.sender(0) as send:
            with self.assertRaises(daily.TaskError):
                other.tick()
        self.assertEqual(send.call_count, 0)
        self.assertNotIn(['git', 'commit'], other.calls)
        self.assertEqual(self.read_job()['status'], 'generated')
        self.assertEqual(sum(c[0] == 'fake-claude' for c in other.calls), 0)

    def test_missing_worktree_is_recreated(self):
        shutil.rmtree(self.repo)
        pushed = self.push_to_origin()
        daily.bootstrap(self.repo, self.runner.env, self.source)
        daily.bootstrap(self.repo, self.runner.env, self.source)  # idempotent
        self.assertEqual(self.head(), pushed)
        self.assertTrue((self.repo / 'ROTATION.md').is_file())
        branch = subprocess.run(['git', 'branch', '--show-current'], cwd=self.repo, check=True,
                                capture_output=True, text=True)
        self.assertEqual(branch.stdout.strip(), '')  # detached on purpose
        with self.sender(0):
            self.runner.tick()
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_dependencies_installed_once_per_lockfile(self):
        binaries = Path(self.tmp.name) / 'bin'
        binaries.mkdir()
        calls = binaries / 'npm-calls'
        (binaries / 'npm').write_text(f'#!/bin/sh\n/bin/mkdir -p node_modules\necho "$@" >> {calls}\n')
        (binaries / 'npm').chmod(0o755)
        env = dict(self.runner.env, PATH=str(binaries))
        (self.repo / 'package-lock.json').write_text('{"v":1}')
        daily.bootstrap(self.repo, env, self.source)
        daily.bootstrap(self.repo, env, self.source)
        self.assertEqual(calls.read_text().split(), ['ci'])
        (self.repo / 'package-lock.json').write_text('{"v":2}')
        daily.bootstrap(self.repo, env, self.source)
        self.assertEqual(calls.read_text().split(), ['ci', 'ci'])

    def test_local_edits_after_generation_are_not_committed(self):
        job = self.runner.start()
        with (self.repo / 'CLAUDE.md').open('a') as stream:
            stream.write('User editing while automation is paused\n')
        with self.assertRaises(daily.TaskError):
            self.runner.publish(job)
        self.assertNotIn(['git', 'commit'], self.runner.calls)

    def test_commit_crash_recovery(self):
        job = self.runner.start()
        original_record = self.runner.record
        def fail_record(value):
            if value['status'] == 'committed':
                raise OSError('simulated crash after git commit')
            original_record(value)
        with patch.object(self.runner, 'record', side_effect=fail_record):
            with self.assertRaises(OSError):
                self.runner.publish(job)
        self.assertEqual(self.read_job()['status'], 'committing')
        with self.sender(0):
            self.runner.tick()
        self.assertEqual(self.read_job()['status'], 'complete')
        self.assertEqual(sum(c == ['git', 'commit'] for c in self.runner.calls), 1)


if __name__ == '__main__':
    unittest.main()
