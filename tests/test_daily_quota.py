"""Account quota admission and late-only trials on simulated dates and platform records."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from fixtures import Sandbox
import fbr_platform
import fbr_quota as q

REAL_SNAPSHOT = q.snapshot


class DailyQuota(Sandbox):
    def setUp(self):
        super().setUp()
        self.account = q.lib.platform_account(self.cfg)
        self.cfg['submit']['daily_limit'] = 1
        self.a, self.b = [self.make_task(n, repo=False) for n in (1, 2)]

    def clock(self, day, hour, minute=0, second=0):
        q.now.return_value = q.datetime(2026, 11, day, hour, minute, second, tzinfo=q.BJ)

    def info(self, task, at=None):
        stamp = at or q.now()
        return {'end': stamp.isoformat(), 'session': task.name, 'due': str(q.due(stamp))}

    def test_one_slot_concurrent_then_abandon_and_resume(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda task: q.reserve(task, self.cfg), (self.a, self.b)))
        self.assertEqual(sum(r is None for r in results), 1)
        first, second = (self.a, self.b) if results[0] is None else (self.b, self.a)
        self.assertIsNone(q.reserve(first, self.cfg))
        self.assertEqual(q.reserve(second, self.cfg)['state'], 'retry')
        q.release(first, self.cfg)
        self.assertIsNone(q.reserve(second, self.cfg))

    def old_run(self, task, state='running'):
        current = {'state': state, 'started_at': '2026-11-09T19:00:00+08:00', 'request_id': 'old'}
        q.lib.save_json(task / 'run/current.json', current)
        return current

    def test_expired_import_releases_today_and_requires_fresh_admission(self):
        old = self.old_run(self.a)
        self.old_run(self.b, 'starting')
        with q.book(self.account) as data:
            for task in (self.a, self.b):
                q.new_hold(data, task, q.now().date())
        for _ in range(2):
            self.assertEqual(q.summary(self.root, rescan=True)['today_reserved'], 0)
        self.assertEqual(q.lib.load_json(self.a / 'run/current.json'), old)
        self.assertIsNone(q.reserve(self.b, self.cfg))
        self.assertEqual(q.reserve(self.a, self.cfg)['state'], 'retry')
        q.release(self.b, self.cfg)
        self.assertIsNone(q.reserve(self.a, self.cfg))
        self.assertEqual(q.summary(self.root, rescan=True)['today_reserved'], 1)

    def test_next_day_scans_inactive_project_and_preserves_late_finish(self):
        self.clock(9, 19)
        for task in (self.a, self.b):
            self.old_run(task)
        q.lib.save_json(self.b / 'run/exit-old.json', {
            'id': 'old', 'ended_epoch': q.now().replace(hour=20, minute=1).timestamp()})
        self.assertEqual(q.summary(self.root)['today_reserved'], 2)
        other = self.tmp / 'other'
        q.lib.save_json(other / '_config/settings.json', self.cfg)
        self.clock(10, 8)
        self.assertEqual(q.summary(other)['today_reserved'], 1)
        with q.book(self.account) as data:
            self.assertEqual(set(data['holds']), {q.key_for(self.b)})
        self.clock(11, 8)
        self.assertEqual(q.summary(other)['today_reserved'], 0)

    def test_expired_timestamps_keep_verified_live_and_uncertain_work(self):
        self.old_run(self.a)
        self.old_run(self.b)
        q.lib.save_json(self.b / 'review/upload.json', {'state': 'uncertain'})
        with mock.patch.object(q.fbr_runtime, 'alive', return_value=True):
            self.assertEqual(q.summary(self.root)['today_reserved'], 2)
        self.assertEqual(q.summary(self.root, rescan=True)['today_reserved'], 1)
        with q.book(self.account) as data:
            self.assertTrue(data['holds'][q.key_for(self.b)]['uncertain'])

    def test_dateless_old_reservation_does_not_roll_forward(self):
        data = {'holds': {'legacy': {'day': '2026-11-08'}}}
        for day in (9, 10):
            self.clock(day, 8)
            q.rollover(data, q.now().date())
            self.assertEqual(q.occupied(data, q.now().date()), 0)

    def test_separate_processes_share_the_last_slot(self):
        program = """import sys
from pathlib import Path
import fbr_quota as q
q.snapshot = lambda *args: (0, {}, 0)
task = Path(sys.argv[1]); cfg = q.lib.settings(q.lib.containing_root(task))
cfg['submit']['daily_limit'] = 1
print('reserved' if q.reserve(task, cfg) is None else 'wait')
"""
        env = dict(os.environ, PYTHONPATH=str(Path(q.__file__).parent))
        children = [subprocess.Popen([sys.executable, '-c', program, str(task)], env=env,
                                     stdout=subprocess.PIPE, text=True) for task in (self.a, self.b)]
        results = [child.communicate(timeout=15)[0].strip() for child in children]
        self.assertCountEqual(results, ['reserved', 'wait'])

    def test_boundary_late_trial_daily_rejection_rollover(self):
        self.clock(10, 20)
        q.snapshot.return_value = (1, {'99': 'existing'}, 0)
        self.assertIsNotNone(q.reserve(self.a, self.cfg))
        with self.assertRaises(q.Wait):
            q.check_create(self.root, self.a, self.cfg, None, self.info(self.a), before_post=True)
        self.clock(10, 20, second=1)
        self.assertIsNone(q.reserve(self.b, self.cfg))
        info = self.info(self.b)
        self.assertTrue(q.check_create(self.root, self.b, self.cfg, None, info, before_post=True)['probe'])
        q.full(self.account, q.now().date())
        with self.assertRaisesRegex(q.Wait, '明确拒绝'):
            q.check_create(self.root, self.b, self.cfg, None, info)
        self.clock(11, 0)
        q.snapshot.return_value = (0, {}, 0)
        self.assertFalse(q.check_create(self.root, self.b, self.cfg, None, info, before_post=True)['probe'])
        q.confirmed(self.b, self.account, 100, q.now().isoformat(), self.b.name, 1)
        self.assertEqual(q.summary(self.root)['today_reserved'], 0)
        self.assertIsNotNone(q.reserve(self.a, self.cfg))

    def test_success_notice_idempotent_and_does_not_raise_baseline(self):
        self.clock(10, 21)
        q.snapshot.return_value = (1, {'1':'old'}, 0)
        self.assertIsNone(q.reserve(self.a, self.cfg))
        q.check_create(self.root, self.a, self.cfg, None, self.info(self.a), before_post=True)
        notice = q.confirmed(self.a, self.account, 2, q.now().isoformat(), self.a.name, 1)
        self.assertIn('新增 2 条', notice)
        q.confirmed(self.a, self.account, 2, q.now().isoformat(), self.a.name, 1)
        self.assertEqual(q.summary(self.root)['confirmed'], 2)
        self.assertIsNone(q.reserve(self.b, self.cfg))
        self.clock(11, 9)
        q.snapshot.return_value = (0, {}, 0)
        self.assertIsNotNone(q.reserve(self.a, self.cfg))  # tomorrow's reservation carried over

    def test_unknown_probe_blocks_next_trial_until_session_reconciled(self):
        self.clock(10, 21)
        self.cfg['submit']['daily_limit'] = 2
        q.snapshot.return_value = (2, {'1': 'old1', '2': 'old2'}, 0)
        for task in (self.a, self.b):
            self.assertIsNone(q.reserve(task, self.cfg))
        q.check_create(self.root, self.a, self.cfg, None, self.info(self.a), before_post=True)
        with self.assertRaisesRegex(q.Wait, '上一条试投'):
            q.check_create(self.root, self.b, self.cfg, None, self.info(self.b), before_post=True)
        q.snapshot.return_value = (3, {'1': 'old1', '2': 'old2', '3': self.a.name}, 0)
        self.assertTrue(q.check_create(self.root, self.b, self.cfg, None, self.info(self.b), before_post=True)['probe'])

    def test_late_task_preserves_slot_for_urgent_task(self):
        self.assertIsNone(q.reserve(self.a, self.cfg))
        self.clock(10, 21)
        self.assertIsNone(q.reserve(self.b, self.cfg))
        with self.assertRaisesRegex(q.Wait, '留给今晚'):
            q.check_create(self.root, self.b, self.cfg, None, self.info(self.b))
        yesterday = self.info(self.a, q.now().replace(day=9, hour=18))
        with self.assertRaisesRegex(q.Wait, '超过提交期限'):
            q.check_create(self.root, self.a, self.cfg, None, yesterday)

    def test_trace_timestamp_and_sha_ignore_unrelated_records(self):
        path = self.tmp / 'main.jsonl'
        rows = [{'type':'assistant', 'timestamp':'2026-11-10T12:00:00Z', 'sessionId':'main'},
                {'type':'user', 'timestamp':'2026-11-10T03:00:00Z', 'sessionId':'main'},
                {'type':'assistant', 'timestamp':'2026-11-11T01:00:00Z', 'sessionId':'child'},
                {'type':'file-history-snapshot', 'timestamp':'2026-11-11T07:00:00Z', 'sessionId':'main'}]
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        self.assertEqual(q.trace_info(path, 'main')['due'], '2026-11-10')
        rows[0]['timestamp'] = '2026-11-10T12:00:00.001Z'
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        self.assertEqual(q.trace_info(path, 'main')['due'], '2026-11-11')
        del rows[0]['timestamp']
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        with self.assertRaises(q.lib.FbrError):
            q.trace_info(path, 'main')

    def test_overview_excludes_yesterdays_repairs_and_scopes_account(self):
        client = mock.Mock()
        client.me.return_value = {'username': self.account, 'id': 7}
        rows = [{'id': 1, 'user_id': 7, 'session_id': 'A', 'current_version': 1,
                 'created_at': '2026-11-10T00:01:03'},
                {'id': 2, 'user_id': 7, 'session_id': 'B', 'current_version': 2}]
        client.list_submissions.return_value = {'meta': {'total_pages': 1, 'total': 2}, 'items': rows}
        client.detail.return_value = {'created_at': '2026-11-09T13:00:01'}
        self.assertEqual(REAL_SNAPSHOT(client, self.account, q.now().date()), (2, {'1': 'A'}, 1))
        client.detail.assert_called_once_with(2)
        self.assertEqual(client.list_submissions.call_args.kwargs['user_id'], 7)

    def test_explicit_limit_only(self):
        for message in ('今日新增提交已达上限', '今天最多提交 70 条', 'daily_submission_limit_exceeded'):
            self.assertTrue(fbr_platform.daily_limit_error(json.dumps({'detail': message}).encode()))
        for message in ('请求太频繁，请稍后重试', '今日提交字段校验失败', 'too many requests'):
            self.assertFalse(fbr_platform.daily_limit_error(json.dumps({'detail': message}).encode()))
