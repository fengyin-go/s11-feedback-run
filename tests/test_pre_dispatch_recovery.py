"""Recover only a provably untouched container before the first dispatch."""
import time
from unittest import mock
from fixtures import done, write
from test_run import DockerCase
import fbrlib as lib
import fbr_run
import fbr_runtime as rt


class PreDispatch(DockerCase):
    def prepared(self):
        task = self.make_task()
        record = lib.load_task(task)
        fbr_run.bound_key(task, self.cfg, start=True)
        lib.save_git_config(task)
        self.docker.run(fbr_run.run_command(task, record, self.cfg))
        current = {'state': 'starting', 'attempt': 1, 'started_at': lib.now_iso(),
            'container_id': 'c0ffee', 'owner_pid': 99999999, 'owner_identity': 'absent',
            'generation': lib.content_hash([record['prompt'], record['initial_commit'], record['task_repo_full']])}
        lib.save_json(task / 'run/current.json', current)
        original = self.docker.run
        def run(command, **kwargs):
            if command[:2] == ['docker', 'inspect']:
                return done(0, 'c0ffee' if '{{.Id}}' in command else lib.content_hash(str(task.resolve())))
            if command[:2] == ['docker', 'top']:
                return done(0, 'PID COMMAND\n1 docker-init\n2 sleep\n')
            return original(command, **kwargs)
        patch = mock.patch.object(lib, 'run', side_effect=run)
        patch.start(); self.addCleanup(patch.stop)
        return task, record, current

    def test_untouched_container_resumes_same_attempt_without_recreation(self):
        task, _, _ = self.prepared()
        self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'prepared')
        self.docker.outcomes = [self.good()]
        row = fbr_run.run_one(task, self.cfg)
        self.assertEqual(row['state'], 'ok')
        self.assertEqual(len(self.docker.docker_calls('run', '-i')), 1)
        self.assertEqual(lib.load_json(task / 'run/attempts.json')[0]['n'], 1)
        self.assertEqual(self.docker.outcomes, [])

    def test_request_created_before_worker_identity_blocks_resend(self):
        task, _, _ = self.prepared()
        lib.save_json(task / 'run/exec-ambiguous.json', {'started_epoch': time.time()})
        self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'blocked')

    def test_any_current_output_blocks_resend(self):
        task, _, _ = self.prepared()
        write(task / 'run/stream.jsonl', 'partial')
        self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'blocked')

    def test_session_files_or_failed_session_inspection_blocks_resend(self):
        task, _, _ = self.prepared()
        self.docker.exec_rcs = [1]
        self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'blocked')

    def test_live_controller_cannot_be_taken_over(self):
        task, record, current = self.prepared()
        with mock.patch.object(rt, 'alive', return_value=True):
            self.assertIn('原调度仍活着', fbr_run.prepared_problems(task, record, self.cfg, current)[0])

    def test_dirty_workspace_and_mismatched_attempts_block(self):
        task, record, current = self.prepared()
        write(task / 'workspace/unexpected.py', 'x=1')
        self.assertIn('不干净', fbr_run.prepared_problems(task, record, self.cfg, current)[0])
        current['attempt'] = 2
        self.assertIn('次数不符', fbr_run.prepared_problems(task, record, self.cfg, current)[0])

    def test_non_initial_process_blocks_before_git(self):
        task, record, current = self.prepared()
        with mock.patch.object(lib, 'run', return_value=done(0, 'PID COMMAND\n1 docker-init\n2 sleep\n3 claude\n')), \
             mock.patch.object(fbr_run, 'container_exists', return_value=True), \
             mock.patch.object(lib, 'git', side_effect=AssertionError('no git')):
            self.assertIn('初始等待进程', fbr_run.prepared_problems(task, record, self.cfg, current)[0])

    def test_surviving_worker_counts_once_without_raising_cap(self):
        info = {'pid': 12345, 'identity': 'verified'}
        gateway = rt.Gateway(self.cfg)
        gateway.event('limit')
        with mock.patch.object(rt, 'alive', return_value=True):
            self.assertTrue(gateway.track_existing(info))
            self.assertTrue(gateway.track_existing(info))
        with gateway.transaction() as state:
            self.assertEqual(len(state['slots']), 1)
            self.assertEqual(state['cap'], 4)
        with mock.patch.object(rt, 'alive', return_value=False):
            self.assertFalse(gateway.track_existing(info))

    def test_exhausted_wait_budget_does_not_dispatch_prepared_attempt(self):
        task, _, current = self.prepared()
        budget = {'active_seconds': 12, 'waiting_seconds': self.cfg['run']['gateway_wait_total']}
        lib.save_json(task / 'run/budget.json', budget)
        self.docker.outcomes = [self.good()]
        row = fbr_run.run_one(task, self.cfg)
        self.assertEqual(row['state'], 'deferred')
        self.assertIn('网关等待预算耗尽', row['detail'])
        self.assertEqual(lib.load_json(task / 'run/current.json'), current)
        self.assertEqual(lib.load_json(task / 'run/budget.json'), budget)
        self.assertFalse((task / 'run/attempts.json').exists())
        self.assertEqual(len(self.docker.outcomes), 1)
        self.assertEqual(len(self.docker.docker_calls('run', '-i')), 1)

    def test_exhausted_wait_budget_does_not_create_fresh_attempt(self):
        task = self.make_task()
        lib.save_json(task / 'run/budget.json', {'active_seconds': 12,
            'waiting_seconds': self.cfg['run']['gateway_wait_total'] + 2})
        self.docker.outcomes = [self.good()]
        row = fbr_run.run_one(task, self.cfg)
        self.assertEqual(row['state'], 'deferred')
        self.assertFalse(self.docker.docker_calls('run', '-i'))
        self.assertFalse((task / 'run/attempts.json').exists())
        self.assertEqual(len(self.docker.outcomes), 1)

    def test_setup_failure_before_container_retries_without_counting_attempt(self):
        task = self.make_task()
        record = lib.load_task(task)
        lib.save_git_config(task)
        current = {'state': 'starting', 'attempt': 1,
            'generation': lib.content_hash([record['prompt'], record['initial_commit'], record['task_repo_full']])}
        lib.save_json(task/'run/current.json', current)
        budget = {'active_seconds': 0, 'waiting_seconds': 0}
        lib.save_json(task/'run/budget.json', budget)
        with mock.patch.object(fbr_run, 'container_exists', return_value=False):
            row = fbr_run.recover_one(task, self.cfg)
        self.assertEqual(row['state'], 'idle')
        self.assertEqual(lib.load_json(task/'run/current.json')['state'], 'setup_ready')
        self.assertEqual(lib.load_json(task/'run/budget.json'), budget)
        self.assertFalse((task/'run/attempts.json').exists())

    def test_setup_recovery_refuses_any_ambiguous_dispatch(self):
        for number, (name, value) in enumerate([('request_id', 'request'), ('pid', 123), ('result', '/tmp/result')], 1):
            task = self.make_task(number)
            record = lib.load_task(task)
            current = {'state': 'starting', 'attempt': 1, name:value,
                'generation': lib.content_hash([record['prompt'], record['initial_commit'], record['task_repo_full']])}
            lib.save_json(task/'run/current.json', current)
            with mock.patch.object(fbr_run, 'container_exists', return_value=False):
                self.assertEqual(fbr_run.recover_one(task,self.cfg)['state'],'blocked')
