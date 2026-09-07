#!/usr/bin/env python3
"""Recovery tests use temporary git repos and never call Claude, SSH or WeChat."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
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
        if args[:2] == ['git', 'pull']:
            return ''
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
        if args[:2] == ['git', 'push']:
            if self.push_failure:
                raise daily.TaskError('offline')
            return ''
        if args[0] == 'bash':
            return ''
        return super().run(args, timeout, log)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.state = Path(self.tmp.name) / 'state'
        self.repo.mkdir()
        self.state.mkdir()
        (self.repo / 'site').mkdir()
        (self.repo / 'ROTATION.md').write_text(LEDGER)
        (self.repo / 'CLAUDE.md').write_text('- **失望（等的人没来）**——待写。\n- **安宁／幸福**——待写。\n'
                                          '### 待写清单（选题池）\n\n失望 · 安宁／幸福 · 骄傲（正面）\n\n## 下节\n')
        (self.repo / '001-旧.md').write_text('# 旧\n旧故事')
        (self.repo / 'site/stories.js').write_text('old')
        for args in [('init', '-b', 'main'), ('config', 'user.name', 'Test'),
                     ('config', 'user.email', 'test@example.invalid'), ('add', '.'),
                     ('commit', '-m', 'initial')]:
            subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True)
        self.now = dt.datetime(2026, 9, 8, 6, tzinfo=daily.BEIJING)
        self.runner = FakeRunner(self.repo, self.state, self.now)
        self.claude = patch.object(daily.shutil, 'which', return_value='fake-claude')
        self.claude.start()
        self.addCleanup(self.claude.stop)

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

    def test_interrupted_generation_blocks_following_day(self):
        job = dict(date='2026-09-07', status='generating')
        self.runner.record(job)
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertEqual(self.runner.calls, [])

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
        self.assertNotIn(['git', 'pull'], self.runner.calls)

    def test_feature_branch_not_pulled(self):
        self.runner.git('checkout', '-b', 'work')
        with self.assertRaises(daily.TaskError):
            self.runner.tick()
        self.assertNotIn(['git', 'pull'], self.runner.calls)

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
