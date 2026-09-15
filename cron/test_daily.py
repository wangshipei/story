#!/usr/bin/env python3
"""Recovery tests use temporary git repos and never call Claude, SSH or WeChat."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
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
        (self.source / 'CLAUDE.md').write_text(
            self.guide(['失望（等的人没来）', '安宁／幸福'], '失望 · 安宁／幸福 · 骄傲（正面）'))
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
        # Alerts are mocked away by default so no test can reach WeChat by accident; the tests that
        # exercise alerting put the real function back with patch.object(daily, 'alert', self.real_alert).
        self.alerts, self.sent_alerts, self.real_alert = [], [], daily.alert
        for patcher in (patch.object(daily, 'alert', side_effect=lambda *args: self.alerts.append(args[1:3])),
                        patch.object(daily.subprocess, 'run', side_effect=self.guard(daily.subprocess.run))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def guide(self, cells, pool='骄傲（正面）'):
        """A CLAUDE.md holding only the emotion cells a test cares about, plus the selection pool."""
        return (''.join(f'- **{name}**——待写。\n' for name in cells)
                + f'### 待写清单（选题池）\n\n{pool}\n\n## 下节\n')

    def rewrite(self, ledger=None, guide=None):
        """Hand-edit ROTATION.md / CLAUDE.md on origin/main, where the worktree will reset onto them."""
        for name, text in (('ROTATION.md', ledger), ('CLAUDE.md', guide)):
            if text is not None:
                (self.source / name).write_text(text)
        for args in [('add', '.'), ('commit', '-m', 'edit'), ('push', 'origin', 'main')]:
            subprocess.run(['git', *args], cwd=self.source, check=True, capture_output=True)

    def guard(self, real):
        """Let the tests' own git calls through, but never the sender itself."""
        def run(args, **kwargs):
            self.assertNotIn('wechat_send.py', ' '.join(map(str, args)))
            return real(args, **kwargs)
        return run

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

    def age(self, path, now, hours=2):
        """Backdate a file so the deadline check sees a scheduler that stopped moving."""
        os.utime(path, ((now.timestamp() - hours * 3600),) * 2)

    def sender(self, code=0, alert_code=0, alert_error=None):
        """Stand in for every wechat_send.py call; an alert is told apart by the file it sends."""
        alert_file = str(self.state / 'alert.txt')

        def fake(args, **kwargs):
            self.assertEqual(Path(args[1]).name, 'wechat_send.py')  # nothing else may be run here
            if args[-1] != alert_file:
                return Mock(returncode=code)
            if alert_error:
                raise alert_error
            self.sent_alerts.append(Path(alert_file).read_text(encoding='utf-8').strip())
            return Mock(returncode=alert_code)
        return patch.object(daily.subprocess, 'run', side_effect=fake)

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

    # The ledger names an emotion its own way; CLAUDE.md keeps the cell. The two must still meet.

    def test_a_longer_entry_name_registers_in_its_cell(self):
        self.rewrite(LEDGER.replace('失望（怒）', '整篇好笑（其他）'),
                     self.guide(['好笑', '好奇／天真', '安宁／幸福'], '整篇好笑 · 安宁／幸福 · 骄傲（正面）'))
        with self.sender(0):
            self.runner.tick()
        guide = (self.repo / 'CLAUDE.md').read_text()
        self.assertIn('- **好笑**——待写。；《门》', guide)  # 「整篇好笑」 is the cell 「好笑」 spelled long
        self.assertNotIn('《门》', guide.split('- **好奇／天真**')[1])
        self.assertIn('- [x] 整篇好笑（其他） → 2026-09-08 002《门》', (self.repo / 'ROTATION.md').read_text())
        self.assertIn('清单（选题池）\n\n骄傲（正面）', guide)  # the pool spelled it long too
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_two_possible_cells_stop_the_day_instead_of_guessing(self):
        self.rewrite(guide=self.guide(['失望（等的人没来）', '失望（正面）', '安宁／幸福']))
        with self.assertRaises(daily.TaskError) as caught:
            self.runner.tick()
        self.assertIn('不唯一', str(caught.exception))
        self.assertIn('失望（正面）', str(caught.exception))
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 0)
        # A tail that two cells answer to is just as ambiguous as two cells that share a prefix.
        with self.assertRaises(daily.TaskError):
            daily.slot(self.guide(['好笑', '篇好笑']), '整篇好笑（其他）')
        # One-character cells are never tails: 「失望」 must not land in a cell called 「望」.
        with self.assertRaises(daily.TaskError):
            daily.slot(self.guide(['望']), '失望（怒）')

    def test_a_missing_cell_stops_before_claude_writes(self):
        self.rewrite(LEDGER.replace('失望（怒）', '整篇好笑（其他）'),
                     self.guide(['好奇／天真', '安宁／幸福']))
        with self.assertRaises(daily.TaskError) as caught:
            self.runner.tick()
        self.assertIn('找不到情绪格子', str(caught.exception))
        self.assertIn('整篇好笑', str(caught.exception))
        self.assertIn('好奇／天真', str(caught.exception))  # the nearest thing a human could rename
        # Nothing was spent and nothing was touched: no generation, no job, no ledger rollover.
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 0)
        self.assertFalse((self.state / 'jobs/2026-09-08.json').exists())
        self.assertIn('- [ ] 整篇好笑（其他）', (self.repo / 'ROTATION.md').read_text())
        self.assertEqual(self.runner.changes(), set())
        self.assertEqual([key for key, _ in self.alerts], ['job-2026-09-08'])  # a human hears about it

    def test_audit_names_every_entry_that_could_not_register(self):
        guide = self.guide(['失望（等的人没来）', '安宁／幸福'])
        self.assertEqual(daily.audit(LEDGER, guide), {})
        broken = daily.audit(LEDGER.replace('安宁／幸福（喜）', '整篇好笑（其他）'), guide)
        self.assertEqual(list(broken), ['整篇好笑（其他）'])

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

    # The ledger nobody reads must come to a human by itself, and exactly once.

    def test_failure_tells_a_human_on_wechat(self):
        self.runner.generated_count = 1
        with patch.object(daily, 'alert', self.real_alert), self.sender(0):
            with self.assertRaises(daily.TaskError) as caught:
                self.runner.tick()
        self.assertTrue(getattr(caught.exception, 'alerted', False))  # main() must not repeat it
        self.assertEqual(len(self.sent_alerts), 1)
        self.assertTrue(self.sent_alerts[0].startswith('9月8日两篇没写成：'))
        self.assertIn('序号连续', self.sent_alerts[0])
        self.assertIn('需要人动手', self.sent_alerts[0])
        self.assertEqual(self.read_job()['status'], 'manual_review')

    def test_the_same_fault_is_told_once(self):
        self.runner.generated_count = 1
        log = io.StringIO()
        with patch.object(daily, 'alert', self.real_alert), self.sender(0), contextlib.redirect_stderr(log):
            for _ in range(3):  # the first tick generates, the next two resume the stuck day
                with self.assertRaises(daily.TaskError):
                    self.runner.tick()
        self.assertEqual(len(self.sent_alerts), 1)
        self.assertEqual(list(daily.alerted(self.state)), ['job-2026-09-08'])
        self.assertEqual(sum(c[0] == 'fake-claude' for c in self.runner.calls), 1)

    def test_wechat_not_ready_is_never_a_fault(self):
        with patch.object(daily, 'alert', self.real_alert), self.sender(75):
            self.runner.tick()
        self.assertEqual(self.sent_alerts, [])
        self.assertEqual(self.read_job()['delivery'], 'pending')
        self.assertFalse((self.state / 'alerts.json').exists())

    def test_an_alert_waits_for_wechat_instead_of_giving_up(self):
        self.runner.generated_count = 1
        with patch.object(daily, 'alert', self.real_alert), self.sender(0, alert_code=75):
            for _ in range(2):
                with self.assertRaises(daily.TaskError):
                    self.runner.tick()
        self.assertEqual(len(self.sent_alerts), 2)  # 75 sent nothing, so it is tried again
        self.assertFalse((self.state / 'alerts.json').exists())

    def test_an_uncertain_alert_is_never_repeated(self):
        self.runner.generated_count = 1
        log = io.StringIO()
        with patch.object(daily, 'alert', self.real_alert), self.sender(0, alert_code=70), \
                contextlib.redirect_stderr(log):
            for _ in range(2):
                with self.assertRaises(daily.TaskError):
                    self.runner.tick()
        self.assertEqual(len(self.sent_alerts), 1)
        self.assertIn('退出码 70', log.getvalue())
        self.assertEqual(list(daily.alerted(self.state)), ['job-2026-09-08'])

    def test_a_failing_alert_leaves_the_task_alone(self):
        self.runner.generated_count = 1
        log = io.StringIO()
        with patch.object(daily, 'alert', self.real_alert), \
                self.sender(0, alert_error=OSError('wxmac 不见了')), contextlib.redirect_stderr(log):
            with self.assertRaises(daily.TaskError) as caught:
                self.runner.tick()
        self.assertIn('序号连续', str(caught.exception))  # the task's own error, not the alert's
        self.assertEqual(self.read_job()['status'], 'manual_review')
        self.assertIn('序号连续', self.read_job()['error'])
        self.assertIn('告警发送失败', log.getvalue())
        self.assertFalse((self.state / 'alerts.json').exists())

    def test_a_stuck_older_day_is_told_once_and_never_blocks_today(self):
        self.runner.record(dict(date='2026-09-07', status='manual_review', error='页数不对'))
        log = io.StringIO()
        with patch.object(daily, 'alert', self.real_alert), self.sender(0), contextlib.redirect_stderr(log):
            self.runner.tick()
            self.runner.tick()
        self.assertEqual(self.sent_alerts, ['9月7日两篇没写成：页数不对。现场已保留，需要人动手。'])
        self.assertEqual(self.read_job()['status'], 'complete')

    def test_a_recovered_day_can_speak_again(self):
        with patch.object(daily, 'alert', self.real_alert), self.sender(0, alert_code=75):
            self.runner.render_failure = True
            with self.assertRaises(daily.TaskError):
                self.runner.tick()
            self.runner.render_failure = False
            self.runner.tick()
        self.assertEqual(self.read_job()['status'], 'complete')
        self.assertEqual(daily.alerted(self.state), {})

    def test_unfinished_day_is_told_after_eight(self):
        with patch.object(daily, 'alert', self.real_alert), self.sender(0):
            self.assertFalse(daily.overdue(self.state, self.now.replace(hour=7, minute=55)))
            self.assertTrue(daily.overdue(self.state, self.now.replace(hour=8, minute=5)))
            self.assertFalse(daily.overdue(self.state, self.now.replace(hour=8, minute=10)))
        self.assertEqual(len(self.sent_alerts), 1)
        self.assertIn('9月8日到八点还没写成两篇', self.sent_alerts[0])
        self.assertIn('今天没有任务记录', self.sent_alerts[0])

    def test_a_finished_or_already_told_day_stays_quiet(self):
        with patch.object(daily, 'alert', self.real_alert), self.sender(0):
            self.runner.tick()
            self.assertFalse(daily.overdue(self.state, self.now.replace(hour=9)))
            # A day whose own fault already reached a human must not be reported a second time.
            later = self.now + dt.timedelta(days=1, hours=3)
            day = later.date().isoformat()
            self.runner.record(dict(date=day, status='manual_review'))
            self.age(self.state / 'jobs' / (day + '.json'), later)
            self.age(self.state / 'heartbeat.json', later)
            daily.save(self.state / 'alerts.json', {'job-' + day: '已经说过了'})
            self.assertFalse(daily.overdue(self.state, later))
        self.assertEqual(self.sent_alerts, [])

    def test_a_run_still_moving_is_not_reported(self):
        later = self.now.replace(hour=9)
        path = self.state / 'jobs' / '2026-09-08.json'
        self.runner.record(dict(date='2026-09-08', status='generating'))
        with patch.object(daily, 'alert', self.real_alert), self.sender(0):
            # A Mac that woke up late is writing today's two right now: that is work, not a fault.
            os.utime(path, (later.timestamp() - 600,) * 2)
            self.assertFalse(daily.overdue(self.state, later))
            os.utime(path, (later.timestamp() - 7200,) * 2)
            self.assertTrue(daily.overdue(self.state, later))
        self.assertEqual(len(self.sent_alerts), 1)
        self.assertIn('generating', self.sent_alerts[0])

    def test_pending_delivery_alone_is_not_reported(self):
        with patch.object(daily, 'alert', self.real_alert), self.sender(75):
            self.runner.tick()  # published, waiting for WeChat to wake up
            self.assertFalse(daily.overdue(self.state, self.now.replace(hour=10)))
        self.assertEqual(self.sent_alerts, [])

    def test_heartbeat_marks_every_finished_pass(self):
        with self.sender(0):
            self.runner.tick()
        beat = json.loads((self.state / 'heartbeat.json').read_text())
        self.assertEqual((beat['day'], beat['status']), ('2026-09-08', 'complete'))
        self.assertEqual(dt.datetime.fromisoformat(beat['time']).utcoffset(), dt.timedelta(hours=8))
        (self.state / 'heartbeat.json').unlink()
        tomorrow = FakeRunner(self.repo, self.state, self.now + dt.timedelta(days=1))
        tomorrow.generated_count = 1
        with self.sender(0):
            with self.assertRaises(daily.TaskError):
                tomorrow.tick()
        self.assertFalse((self.state / 'heartbeat.json').exists())  # an unfinished pass is no proof

    def test_scheduled_run_checks_the_day_after_its_own_work(self):
        log = io.StringIO()
        with patch.object(daily, 'STATE', self.state), patch.object(daily, 'REPO', self.repo), \
                patch.object(daily, 'bootstrap'), patch.object(daily, 'Runner') as runner, \
                patch.object(daily, 'overdue') as check, contextlib.redirect_stderr(log):
            self.assertEqual(daily.main(['--scheduled']), 0)
            self.assertEqual(daily.main([]), 0)  # a hand run must not nag anyone
            with daily.locked(self.state) as held:  # a run already going is still a day to check
                self.assertTrue(held)
                self.assertEqual(daily.main(['--scheduled']), 0)
            self.assertEqual(check.call_count, 2)
            runner.return_value.tick.side_effect = daily.TaskError('炸了')
            self.assertEqual(daily.main(['--scheduled']), 1)
        self.assertEqual(check.call_count, 2)  # a run that reported itself is not reported twice
        self.assertEqual(runner.return_value.tick.call_count, 3)

    def test_a_run_that_cannot_start_still_speaks(self):
        log = io.StringIO()
        with patch.object(daily, 'STATE', self.state), patch.object(daily, 'REPO', self.repo), \
                patch.object(daily, 'overdue'), patch.object(daily, 'alert', self.real_alert), \
                patch.object(daily, 'bootstrap', side_effect=daily.TaskError('worktree 建不起来')), \
                self.sender(0), contextlib.redirect_stderr(log):
            self.assertEqual(daily.main(['--scheduled']), 1)
            self.assertEqual(daily.main(['--scheduled']), 1)
        self.assertEqual(len(self.sent_alerts), 1)  # the same outage says it once a day
        self.assertIn('没能开工', self.sent_alerts[0])
        self.assertIn('worktree 建不起来', self.sent_alerts[0])


if __name__ == '__main__':
    unittest.main()
