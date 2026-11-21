import argparse
import concurrent.futures
import threading
from pathlib import Path
from unittest import mock

from fixtures import BATCH, Sandbox
from flow import run_main
import fbr_flow as flow
import fbr_followup as follow
import fbr_platform as platform
import fbr_projects as projects
import fbr_quota as quota
import fbr_work as work
import fbrlib as lib


class Followup(Sandbox):
    def setUp(self):
        super().setUp()
        self.task = self.make_task(repo=False)
        follow.enroll(self.task, self.cfg)
        self.account = lib.platform_account(self.cfg)
        self.created = '2026-11-10T18:00:00+08:00'
        self.start = quota.moment(self.created).timestamp()
        self.receipt = {'state': 'confirmed', 'submission_id': 7, 'task_id': self.task.name,
                        'submitted_at': self.created, 'account': self.account, 'platform': platform.BASE_URL,
                        'binding': {'data': {'session_id': 'session', 'user_prompt': lib.load_task(self.task)['prompt']}}}
        self.path = self.task / 'review/upload.json'
        lib.save_json(self.path, self.receipt)
        self.detail = {'id': 7, 'user_id': 9, 'session_id': 'session', 'status': 'SUBMITTED', 'qc_running': False}
        self.client = mock.Mock(spec=platform.Client)
        self.client.me.return_value = {'id': 9, 'username': self.account}
        self.client.detail.return_value = self.detail
        self.options = argparse.Namespace(cleanup_poll_seconds=60)

    def poll(self, seconds=0):
        return follow.poll(self.task, self.root, self.cfg, self.options,
                           client=self.client, clock=lambda: self.start + seconds)

    def journal(self):
        return lib.load_json(self.task / follow.JOURNAL)

    def test_timeout_is_per_submission_and_survives_restart(self):
        self.assertEqual(self.poll(599)['retry_after'], self.start + 600)
        self.assertEqual(self.poll(600)['state'], 'ok')
        self.assertEqual(self.journal()['state'], 'timed_out')
        self.client.reset_mock()
        self.poll(720)
        self.client.me.assert_not_called()
        self.receipt['last_repair_at'] = '2026-11-10T18:12:00+08:00'
        lib.save_json(self.path, self.receipt)
        self.assertEqual(self.poll(721)['state'], 'retry')

    def test_running_qc_keeps_following_after_ten_minutes(self):
        self.detail.update(status='SUBMITTED', stage='QC_RUNNING', qc_running=True)
        self.assertEqual(self.poll(700)['state'], 'retry')
        self.assertEqual(self.journal()['state'], 'running_qc')
        self.detail.update(status='QC_PASSED', stage='QC_PASSED', qc_running=False)
        self.assertEqual(self.poll(710)['state'], 'ok')

    def test_pending_fix_after_ten_minutes_is_repaired_on_original(self):
        self.detail.update(status='PENDING_FIX', editable=True, qc_summary='修正 Feedback')
        self.assertEqual(self.poll(601)['state'], 'retry')
        packet = work.next_task(self.root, BATCH)['task']
        self.assertEqual(packet['kind'], 'platform_repair')
        self.assertEqual(packet['summary'], self.detail['qc_summary'])
        self.client.request.assert_not_called()
        self.receipt['last_repair_at'] = '2026-11-10T18:12:00+08:00'
        lib.save_json(self.path, self.receipt)
        self.assertIsNone(work.next_task(self.root, BATCH)['task'])

    def test_finalized_revision_resubmits_original_once(self):
        self.detail.update(status='PENDING_FIX', editable=True, current_version=2)
        self.poll()
        revision = self.journal()['revision']
        lib.save_json(self.task / 'review/feedback.json', {'feedback': '修正后的原单字段'})
        follow.mark_ready(self.task, revision)
        with mock.patch.object(follow.submit, 'repair_batch', return_value={
                'tasks': [{'state': 'confirmed'}]}) as repair:
            self.assertEqual(self.poll()['state'], 'retry')
            self.assertEqual(repair.call_args.kwargs['tasks'], self.task.name)
            self.assertEqual(repair.call_args.kwargs['expected_revision'], revision)
            self.assertEqual(self.poll()['state'], 'needs_attention')
            repair.assert_called_once()
        self.client.create.assert_not_called()

    def test_guard_handles_changed_version_without_booking_quota(self):
        self.detail['status'] = 'DISCARDED'
        self.client.conditional_delete_supported.return_value = True
        def request(method, path, **kwargs):
            if method == 'GET':
                kwargs['response_headers']['ETag'] = '"v1"'
                return self.detail
            raise platform.PlatformHTTPError(412)
        self.client.request.side_effect = request
        self.assertEqual(self.poll()['state'], 'retry')
        self.detail.update(status='PENDING_FIX', editable=True)
        self.assertEqual(self.poll()['state'], 'retry')
        self.assertEqual(self.journal()['state'], 'needs_platform_repair')
        self.assertNotIn('overview', self.journal())

    def test_locked_fix_does_not_delete_or_rerun(self):
        self.detail.update(status='PENDING_FIX', editable=False)
        self.poll()
        self.assertTrue(lib.platform_deprecated(self.task))
        self.assertIsNone(work.next_task(self.root, BATCH)['task'])
        self.client.request.assert_not_called()

    def test_current_platform_cannot_atomically_delete_only_discarded(self):
        self.detail['status'] = 'DISCARDED'
        self.client.conditional_delete_supported.return_value = False
        self.assertEqual(self.poll()['state'], 'needs_attention')
        self.client.request.assert_not_called()
        self.assertEqual(self.journal()['state'], 'delete_guard_unavailable')

    def test_identity_and_state_required(self):
        for key, wrong in [('session_id', 'other'), ('user_id', 3), ('id', 8)]:
            with self.subTest(key=key), mock.patch.dict(self.detail, {key: wrong, 'status': 'DISCARDED'}):
                with self.assertRaises(lib.FbrError):
                    self.poll()
        self.client.request.assert_not_called()
        self.client.conditional_delete_supported.assert_not_called()

    def test_owner_missing_in_detail_is_checked_against_platform_list(self):
        self.detail.pop('user_id')
        self.client.list_submissions.return_value = {
            'items': [{'session_id': 'session', 'id': 7, 'user_id': 9}],
            'meta': {'total_pages': 1, 'total': 1}}
        self.assertEqual(self.poll()['state'], 'retry')
        self.client.list_submissions.return_value['items'][0]['user_id'] = 2
        with self.assertRaises(lib.FbrError):
            self.poll()
        self.client.request.assert_not_called()

    def test_discard_changed_to_fix_before_delete(self):
        self.detail['status'] = 'DISCARDED'
        self.client.conditional_delete_supported.return_value = True
        self.client.request.return_value = {**self.detail, 'status': 'PENDING_FIX'}
        self.assertEqual(self.poll()['state'], 'retry')
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET'])

    def test_conditional_delete_and_authoritative_quota_readback(self):
        self.detail['status'] = 'DISCARDED'
        self.client.conditional_delete_supported.return_value = True
        def request(method, path, **kwargs):
            self.assertEqual(path, '/submissions/7')
            if method == 'GET':
                kwargs['response_headers']['ETag'] = '"version1"'
                return self.detail
            self.assertEqual((method, kwargs['headers']), ('DELETE', {'If-Match': '"version1"'}))
            return {}
        self.client.request.side_effect = request
        with quota.book(self.account) as book:
            row = quota.day_row(book, quota.now().date())
            row.update(overview=10, ids={'7': 'session'}, unlisted={})
        self.poll()
        self.assertEqual(self.journal()['state'], 'delete_acknowledged')
        self.client.detail.side_effect = platform.PlatformHTTPError(404)
        with mock.patch.object(quota, 'snapshot', return_value=(9, {'8': 'other'}, 8)):
            self.assertEqual(self.poll()['state'], 'ok')
        self.assertEqual(self.journal()['overview'], 9)
        self.poll(30)
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET', 'DELETE'])
        self.assertTrue(lib.platform_deprecated(self.task))


class ProjectQueue(Sandbox):
    def test_only_immediate_predecessor_can_overlap(self):
        first, second = self.tmp / 'p1', self.tmp / 'p2'
        def report(folder, remaining, started=True, limit=4):
            lib.save_json(folder / 'handoff.json', dict(queue_id='q', capacity=limit,
                          started=started, remaining=remaining))
        self.assertEqual(projects.capacity([first], 'q'), 0)
        report(first, 2, started=False)
        self.assertEqual(projects.capacity([first], 'q'), 0)
        for remaining, slots in [(8, 0), (4, 0), (3, 1), (2, 2), (1, 3), (0, 4)]:
            report(first, remaining)
            self.assertEqual(projects.capacity([first], 'q'), slots)
        report(first, 2, limit=2)
        self.assertEqual(projects.capacity([first], 'q'), 0)
        report(second, 1)
        self.assertEqual(projects.capacity([first, second], 'q'), 0)
        report(first, 0)
        self.assertEqual(projects.capacity([first, second], 'q'), 3)
        self.assertEqual(projects.capacity([first], 'unrelated'), 0)

    def test_flow_fills_only_free_slots_then_expands(self):
        path = self.root / '_config/settings.json'
        cfg = lib.load_json(path)
        cfg['concurrency'] = 4
        lib.save_json(path, cfg)
        tasks = [self.make_task(i, repo=False) for i in range(1, 7)]
        earlier = self.tmp / 'previous' / 'flow'
        progress = dict(queue_id='q', started=True, capacity=4, remaining=3)
        lib.save_json(earlier / 'handoff.json', progress)
        options = argparse.Namespace(batch=BATCH, tasks=None, publish=False, auto_submit=False,
                  execute=True, no_watchdog=True, command='pre-submit', queue_id='q',
                  after_project=[str(earlier)], reviewer_env='unused')
        active = maximum = 0
        lock, first, four, release = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
        def produce(task, *_):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                first.set()
                if active == 4:
                    four.set()
            release.wait(8)
            with lock:
                active -= 1
            return {'state': 'abandoned', 'task_id': task.name}
        with mock.patch.object(flow.fbr_run, 'preflight', return_value=[]), \
             mock.patch('fbr_dependencies.prewarm_batch', return_value=dict(prepared=0,fallback=6,failed=0)), \
             mock.patch('fbr_sheet.write_sheet'), mock.patch('fbr_cleanup.maintain_batch', return_value={}), \
             mock.patch.object(flow, 'produce', side_effect=produce), concurrent.futures.ThreadPoolExecutor(1) as pool:
            fut = pool.submit(flow.run_flow, self.root, options)
            try:
                self.assertTrue(first.wait(5))
                self.assertFalse(four.wait(.2))
                with lock:
                    self.assertEqual(active, 1)
                lib.save_json(earlier / 'handoff.json', dict(progress, remaining=0))
                self.assertTrue(four.wait(5))
            finally:
                release.set()
            self.assertEqual(fut.result(timeout=8)['state'], 'completed')
        self.assertEqual(maximum, 4)
        self.assertEqual(len(tasks), 6)

    def test_coordinator_launches_only_first_until_real_tail(self):
        second = self.tmp / 'second'
        lib.save_json(second / '_config/settings.json', lib.load_json(self.root / '_config/settings.json'))
        (second / 'batches' / BATCH).mkdir(parents=True)
        roots = [self.root, second]
        children = []
        def start(argv):
            root = Path(argv[argv.index('--root')+1])
            directory = flow.home(root, BATCH)
            key = argv[argv.index('--queue-id')+1]
            child = type('Child', (), {'poll': lambda self: self.code, 'code': None})()
            children.append((child, directory, key))
            lib.save_json(directory / 'plan.json', dict(queue_id=key))
            if len(children) == 2:
                first_report = lib.load_json(children[0][1] / 'handoff.json')
                self.assertTrue(first_report['started'])
                self.assertEqual(first_report['remaining'], 2)
                self.assertIn(str(children[0][1]), argv)
            return child
        ticks = 0
        def advance(_):
            nonlocal ticks
            ticks += 1
            self.assertLess(ticks, 8)
            if len(children) == 1:
                child, directory, key = children[0]
                count = 4 if ticks == 2 else 2
                lib.save_json(directory / 'handoff.json', dict(queue_id=key, started=ticks>1,
                              remaining=count, capacity=4))
            else:
                for child, directory, _ in children:
                    child.code = 0
                    lib.save_json(directory / 'stop.json', {'reason':'completed'})
        with mock.patch.object(projects.subprocess, 'Popen', side_effect=start), \
                mock.patch.object(projects.time, 'sleep', side_effect=advance):
            self.assertEqual(projects.run([(str(r), BATCH) for r in roots], execute=True), 0)
        self.assertEqual([p.parent.parent.parent for _,p,_ in children], roots)

    def test_default_entry_and_resume_preserve_mode(self):
        self.make_task(repo=False)
        for command, args, saved, expected in [
            ('rolling', [], {}, True), ('run', ['--execute', '--confirm', 'FLOW'], {}, True),
            ('pre-submit', [], {}, False),
            ('resume', ['--execute', '--confirm', 'FLOW'], {'auto_submit': False}, False),
            ('resume', ['--execute', '--confirm', 'FLOW'], {'auto_submit': True}, True),
        ]:
            lib.save_json(flow.home(self.root, BATCH) / 'plan.json', saved)
            with self.subTest(command=command, saved=saved), mock.patch.object(lib, 'active_github_account'), \
                    mock.patch.object(flow, 'run_flow', return_value={'state': 'ok'}) as run:
                code, _, err = run_main(flow, '--root', str(self.root), command, '--batch', BATCH, *args)
                self.assertEqual((code, err), (0, ''))
                self.assertIs(run.call_args.args[1].auto_submit, expected)
