"""运行基础回归：真实临时 Git、无害持久子进程、本地 HTTP；Docker/模型全部替换。"""
from __future__ import annotations
import argparse
import concurrent.futures
import copy
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

from fixtures import Sandbox, write, done, stream_text
from test_run import DockerCase, SID
from test_record_facts import FactsCase, RecordCase, mp4, GOOD
import fbrlib as lib
import fbr_run
import fbr_facts
import fbr_record
import fbr_runtime as rt
import fbr_flow
import fbr_preview


class ZeroChanges(DockerCase):
    def test_no_change_uses_three_fresh_containers_and_archives(self):
        task = self.make_task()
        self.docker.outcomes = [self.good(effect=lambda ws: None) for _ in range(3)]
        row = fbr_run.run_one(task, self.cfg)
        self.assertEqual(row['state'], 'deferred')
        attempts = lib.load_json(task / 'run/attempts.json')
        self.assertEqual([a['reason'] for a in attempts], ['零代码改动'] * 3)
        self.assertEqual(len(self.docker.docker_calls('run', '-i')), 3)
        self.assertEqual(len(self.docker.docker_calls('rm')), 3)
        self.assertTrue(all((task / f'run/attempt-{n}/projects').is_dir() for n in (1, 2, 3)))
        self.assertEqual(fbr_run.run_one(task, self.cfg)['state'], 'deferred')
        self.assertEqual(len(self.docker.docker_calls('run', '-i')), 3)

    def test_docs_only_does_not_count(self):
        task = self.make_task()
        self.docker.outcomes = [self.good(effect=lambda ws: write(ws / 'README.md', 'updated')),
                                self.good()]
        self.assertEqual(fbr_run.run_one(task, self.cfg)['state'], 'ok')
        self.assertEqual(lib.load_json(task / 'run/attempts.json')[0]['reason'], '零代码改动')

    def test_wrong_but_changed_code_is_valid(self):
        task = self.make_task()
        self.docker.outcomes = [self.good(effect=lambda ws: write(ws / 'src/broken.py', 'invalid syntax!!!'))]
        self.assertEqual(fbr_run.run_one(task, self.cfg)['state'], 'ok')

    def test_budget_exhaustion_does_not_reset_on_resume(self):
        task = self.make_task()
        lib.save_json(task / 'run/budget.json', {'active_seconds': self.cfg['run']['task_budget'], 'waiting_seconds': 0})
        for _ in range(2):
            self.assertEqual(fbr_run.run_one(task, self.cfg)['state'], 'deferred')
        self.assertEqual(self.docker.docker_calls('run', '-i'), [])

    def test_live_current_blocks_host_git(self):
        task = self.make_task()
        lib.save_json(task / 'run/current.json', {'state': 'running'})
        with mock.patch.object(lib, 'git', side_effect=AssertionError('must not execute git')):
            self.assertIn('recover', fbr_run.ready_problems(task, lib.load_task(task), self.cfg)[0])

    def test_frozen_task_rejects_rerun(self):
        task = self.make_task()
        lib.save_json(task / 'review/upload.json', {'submission_id': 123})
        with self.assertRaisesRegex(lib.FbrError, '冻结'):
            fbr_run.run_one(task, self.cfg)


class CacheIdentity(FactsCase):
    def test_command_change_executes_new_baseline(self):
        task = self.prepared(change=False)
        self.results = [GOOD, GOOD]
        fbr_facts.facts_one(task, self.root, self.cfg)
        self.cfg['facts']['test_cmd'] = 'make alternate-test'
        fbr_facts.facts_one(task, self.root, self.cfg, redo=True)
        self.assertEqual(len(self.step_calls), 2)
        self.assertEqual(self.step_calls[-1]['steps'][-1][1], 'make alternate-test')

    def test_image_drift_is_not_hidden_by_cache(self):
        task = self.prepared(change=False)
        self.results = [GOOD]
        fbr_facts.facts_one(task, self.root, self.cfg)
        self.docker.image_id = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(lib.FbrError, '镜像 ID'):
            fbr_facts.facts_one(task, self.root, self.cfg)

    def test_record_change_invalidates_facts(self):
        task = self.prepared(change=False)
        self.results = [GOOD]
        fbr_facts.facts_one(task, self.root, self.cfg)
        before = lib.load_json(task / 'evidence/facts.json')['source_identity']
        write(task / 'record/recording.mp4', 'new video')
        fbr_facts.facts_one(task, self.root, self.cfg)
        self.assertNotEqual(before, lib.load_json(task / 'evidence/facts.json')['source_identity'])


class ProtectedRecording(RecordCase):
    def test_bad_redo_retains_old_video_metadata_and_stage(self):
        task = self.committed()
        fbr_record.record_one(task, self.cfg)
        old_video = (task / 'record/recording.mp4').read_bytes()
        old_meta = (task / 'record/recording.json').read_bytes()
        self.video = mp4(640, 360, 95000)
        self.assertEqual(fbr_record.record_one(task, self.cfg, redo=True)['state'], 'failed')
        self.assertEqual((task / 'record/recording.mp4').read_bytes(), old_video)
        self.assertEqual((task / 'record/recording.json').read_bytes(), old_meta)
        self.assertTrue(lib.stage_ok(task, 'record'))
        failed = lib.load_json(task / 'record/last-attempt.json')
        self.assertTrue(Path(failed['path'], 'recording.mp4').is_file())

    def test_render_failure_retains_previous_version(self):
        task = self.committed()
        fbr_record.record_one(task, self.cfg)
        old = (task / 'record/recording.mp4').read_bytes()
        self.render_error = lib.FbrError('capture failed')
        with self.assertRaises(lib.FbrError):
            fbr_record.record_one(task, self.cfg, redo=True)
        self.assertEqual((task / 'record/recording.mp4').read_bytes(), old)
        self.assertTrue(lib.stage_ok(task, 'record'))


class SharedRuntime(Sandbox):
    def test_lock_is_reentrant_but_rejects_other_thread(self):
        path = self.tmp / 'lock'
        with lib.file_lock(path):
            with lib.file_lock(path):
                pass
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                future = pool.submit(lambda: lib.file_lock(path).__enter__())
                with self.assertRaises(lib.FbrError):
                    future.result()
        with lib.file_lock(path):
            self.assertTrue(path.exists())

    def test_slots_share_actual_key_across_paths(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['gateway']['machine_parallel'] = 1
        first = rt.Gateway(cfg)
        alternate = self.tmp / 'other.env'
        alternate.write_bytes(self.key_file.read_bytes())
        alternate.chmod(0o600)
        cfg['key_file'] = str(alternate)
        second = rt.Gateway(cfg)
        self.assertEqual(first.path, second.path)
        self.assertTrue(first.acquire(time.time() + 1, once=True))
        self.assertFalse(second.acquire(time.time() + 1, once=True))
        first.release()
        self.assertTrue(second.acquire(time.time() + 1, once=True))
        second.release()

    def test_pressure_dedup_floor_and_recovery(self):
        options = self.cfg['gateway']
        state = {'ceiling': 5, 'cap': 5}
        for now in (1000, 1001, 1002):
            rt.pressure_update(state, 'soft', options, now)
        self.assertEqual(state['cap'], 4)
        rt.pressure_update(state, 'soft', options, 1121)
        rt.pressure_update(state, 'soft', options, 1242)
        self.assertEqual(state['cap'], 3)
        rt.pressure_update(state, 'limit', options, 1363)
        self.assertEqual(state['cap'], 2)
        rt.pressure_update(state, 'success', options, 1664)
        self.assertEqual(state['cap'], 3)

    def test_probe_failure_does_not_downgrade_and_spike_rolls_back(self):
        options = self.cfg['gateway']
        state = {'ceiling': 5, 'cap': 3, 'next_spike': 1000}
        rt.pressure_update(state, 'probe_fail', options, 900)
        self.assertEqual(state['cap'], 3)
        rt.pressure_update(state, 'tick', options, 1000)
        self.assertEqual(state['cap'], 5)
        rt.pressure_update(state, 'limit', options, 1001)
        self.assertEqual(state['cap'], 3)
        self.assertEqual(state['next_spike'], 1001 + options['spike_backoff'])

    def test_watcher_handles_partial_json_and_ignores_project_429(self):
        path = write(self.tmp / 'stream', '{"type":"assistant",')
        watcher = rt.Watcher(10)
        watcher.poll(path, 20)
        with path.open('a') as f:
            f.write('"message":{"content":[{"type":"tool_use"}]}}\n')
            f.write(json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'HTTP 429'}]}}) + '\n')
        watcher.poll(path, 30)
        self.assertEqual(watcher.tools, 1)
        self.assertEqual(watcher.retry_at, 0)
        with path.open('a') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'api_retry', 'error': '429'}) + '\n')
        watcher.poll(path, 40)
        self.assertTrue(watcher.protected(500, self.cfg['run']))
        self.assertFalse(watcher.protected(4000, self.cfg['run']))

    def test_persistent_worker_writes_matching_exit_receipt(self):
        task = self.make_task()
        cfg = copy.deepcopy(self.cfg)
        cfg['run']['poll_seconds'] = .01
        gateway = mock.Mock()
        command = [sys.executable, '-c', 'import json;print(json.dumps({"type":"result","is_error":False,"result":"ok"}))']
        current = {'state': 'starting', 'attempt': 1}
        lib.save_json(task / 'run/current.json', current)
        result = rt.monitored_stream(command, '', task / 'run/out', task / 'run/err', 5,
                    {'task': task, 'cfg': cfg, 'gateway': gateway, 'segment': 0})
        self.assertEqual(result, (0, False))
        current = lib.load_json(task / 'run/current.json')
        self.assertEqual(lib.load_json(Path(current['result']))['id'], current['request_id'])
        self.assertEqual(current['state'], 'exited')
        self.assertIn('"ok"', (task / 'run/out').read_text())


class Flow(Sandbox):
    def test_downstream_starts_before_last_producer_finishes(self):
        first, second = self.make_task(1), self.make_task(2)
        events = {}
        def produce(task, cfg, opts):
            time.sleep(.01 if task == first else .25)
            events[task.name + '-produce'] = time.monotonic()
            return {'state': 'ok', 'task_id': task.name}
        def record(task, cfg):
            events[task.name + '-record'] = time.monotonic()
            return task
        options = argparse.Namespace(batch=first.parent.name, tasks=None, publish=False, auto_submit=False,
                    no_watchdog=True, reviewer_env='unused', execute=True, command='run')
        with mock.patch('fbr_reviewer.credentials', return_value=('fixture', 'fixture')), \
             mock.patch.object(fbr_run, 'preflight', return_value=[]), \
             mock.patch.object(fbr_flow, 'produce', side_effect=produce), \
             mock.patch.object(fbr_flow, 'record_task', side_effect=record), \
             mock.patch.object(fbr_flow, 'facts_task', side_effect=lambda task, root, cfg: task), \
             mock.patch.object(fbr_flow, 'review_task', side_effect=lambda task, *args: {'state': 'ok', 'task_id': task.name}), \
             mock.patch('fbr_sheet.write_sheet', return_value={}):
            result = fbr_flow.run_flow(self.root, options)
        self.assertEqual(result['state'], 'completed')
        self.assertLess(events[first.name + '-record'], events[second.name + '-produce'])

    def test_resumed_ready_task_does_not_wait_for_busy_generation_pool(self):
        first, second = self.make_task(1), self.make_task(2)
        cfg=lib.settings(self.root);cfg['concurrency']=1;cfg['run']['batch_budget']=.5
        events={}
        for stage in ('run','collect','commit','record','facts'):
            lib.set_stage(second,stage,'ok')
        def produce(task,cfg,opts):
            time.sleep(.15);events['produced']=time.monotonic()
            return {'state':'ok','task_id':task.name}
        def review(task,*args):
            if task==second:events['ready_review']=time.monotonic()
            return {'state':'needs_task_agent','task_id':task.name}
        opts=argparse.Namespace(batch=first.parent.name,tasks=None,publish=False,auto_submit=False,
                               no_watchdog=True,reviewer_env='unused',execute=True,command='resume')
        with mock.patch.object(lib,'settings',return_value=cfg), \
             mock.patch.object(fbr_run,'preflight',return_value=[]), \
             mock.patch.object(fbr_flow,'produce',side_effect=produce), \
             mock.patch.object(fbr_flow,'record_task',side_effect=lambda t,*a:t), \
             mock.patch.object(fbr_flow,'facts_task',side_effect=lambda t,*a:t), \
             mock.patch.object(fbr_flow,'review_task',side_effect=review), \
             mock.patch('fbr_sheet.write_sheet'):
            result=fbr_flow.run_flow(self.root,opts)
        self.assertLess(events['ready_review'],events['produced'])
        self.assertEqual(result['state'],'budget')
        waiting=lib.load_json(first.parent/'flow/waiting.json')
        self.assertTrue(waiting['tasks'])
        self.assertEqual(waiting['state'],'waiting_for_task_ai')

    def test_frozen_production_does_not_invoke_runner(self):
        task = self.make_task()
        lib.save_json(task / 'review/upload.json', {'submission_id': 10})
        with mock.patch.object(fbr_run, 'run_one') as run:
            result = fbr_flow.produce(task, self.cfg, argparse.Namespace())
        self.assertEqual(result['state'], 'frozen')
        run.assert_not_called()

    def test_watchdog_honors_absolute_budget(self):
        task = self.make_task()
        lib.save_json(fbr_flow.home(self.root, task.parent.name) / 'plan.json', {'deadline': time.time() - 1})
        with mock.patch.object(fbr_flow, 'spawn') as spawn:
            self.assertEqual(fbr_flow.watchdog(self.root, argparse.Namespace(batch=task.parent.name))['state'], 'stopped')
        spawn.assert_not_called()


class RangeServer(Sandbox):
    def test_video_seek_and_unsatisfiable_range(self):
        write(self.tmp / 'video.mp4', '0123456789')
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(fbr_preview.Handler, directory=str(self.tmp)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/video.mp4'
            for spec, expected in (('bytes=2-5', b'2345'), ('bytes=-3', b'789')):
                with urllib.request.urlopen(urllib.request.Request(url, headers={'Range': spec})) as result:
                    self.assertEqual(result.status, 206)
                    self.assertEqual(result.read(), expected)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url, headers={'Range': 'bytes=20-30'}))
            self.assertEqual(error.exception.code, 416)
            error.exception.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


class Recovery(DockerCase):
    def saved(self, changed=True):
        task = self.make_task()
        record = lib.load_task(task)
        fbr_run.bound_key(task, self.cfg, start=True)
        lib.save_git_config(task)
        if changed:
            write(task / 'workspace/src/new.py', 'print("changed")')
        out = write(task / 'run/stream.jsonl', stream_text(SID))
        err = write(task / 'run/stderr.log', '')
        receipt = task / 'run/exit-test.json'
        current = {'state': 'exited', 'attempt': 1, 'started_at': lib.now_iso(), 'request_id': 'test',
            'generation': lib.content_hash([record['prompt'], record['initial_commit'], record['task_repo_full']]),
            'stdout': str(out), 'stderr': str(err), 'result': str(receipt)}
        lib.save_json(task / 'run/current.json', current)
        lib.save_json(receipt, {'id': 'test', 'rc': 0})
        return task, current

    def test_matching_receipt_accepts_final_tree(self):
        task, _ = self.saved()
        self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'ok')
        self.assertTrue(lib.stage_ok(task, 'run'))
        self.assertEqual(lib.load_json(task / 'run/current.json')['state'], 'finished')

    def test_recovered_success_updates_shared_pressure_once(self):
        task, _ = self.saved()
        cfg = copy.deepcopy(self.cfg)
        cfg['_managed'] = True
        with mock.patch.object(rt, 'Gateway') as gateway:
            self.assertEqual(fbr_run.recover_one(task, cfg)['state'], 'ok')
            self.assertEqual(fbr_run.recover_one(task, cfg)['state'], 'ok')
        gateway.return_value.event.assert_called_once_with('success')

    def test_recovery_zero_change_archives_instead_of_scoring(self):
        task, _ = self.saved(changed=False)
        self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'deferred')
        self.assertEqual(lib.load_json(task / 'run/attempts.json')[0]['reason'], '零代码改动')
        self.assertTrue((task / 'run/attempt-1/stream.jsonl').exists())

    def test_missing_exit_receipt_preserves_workspace(self):
        task, current = self.saved()
        Path(current['result']).unlink()
        with mock.patch.object(lib, 'git', side_effect=AssertionError('no git')):
            self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'blocked')
        self.assertTrue((task / 'workspace/src/new.py').exists())

    def test_live_worker_waits_without_host_git(self):
        task, _ = self.saved()
        with mock.patch.object(rt, 'alive', return_value=True), \
             mock.patch.object(lib, 'git', side_effect=AssertionError('no git')):
            self.assertEqual(fbr_run.recover_one(task, self.cfg)['state'], 'running')

    def test_wrong_container_owner_is_never_stopped(self):
        task, _ = self.saved()
        self.docker.containers.add(task.name)
        original = self.docker.run
        def inspect(command, **kwargs):
            return done(0, 'someone-else') if command[:2] == ['docker', 'inspect'] else original(command, **kwargs)
        with mock.patch.object(lib, 'run', side_effect=inspect), self.assertRaisesRegex(lib.FbrError, '不属于'):
            fbr_run.recover_one(task, self.cfg)
        self.assertEqual(self.docker.docker_calls('stop'), [])
        self.assertEqual(self.docker.docker_calls('rm'), [])

    def test_unobserved_time_is_not_free_on_recovery(self):
        task, current = self.saved()
        lib.save_json(task / 'run/exec-test.json', {'started_epoch': 100, 'base_budget': {'active_seconds': 20}})
        lib.save_json(Path(current['result']), {'id': 'test', 'rc': 0, 'ended_epoch': 140})
        fbr_run.recover_one(task, self.cfg)
        self.assertEqual(lib.load_json(task / 'run/budget.json')['active_seconds'], 60)


class GatewayErrors(Sandbox):
    def test_auth_failure_blocks_all_same_key_slots(self):
        first, second = rt.Gateway(self.cfg), rt.Gateway(self.cfg)
        first.event('auth')
        with self.assertRaisesRegex(lib.FbrError, '鉴权失败'):
            second.acquire(time.time() + 1, once=True)

    def test_soft_error_does_not_raise_a_low_cap(self):
        state = {'ceiling': 5, 'cap': 1}
        rt.pressure_update(state, 'soft', self.cfg['gateway'], 1000)
        self.assertEqual(state['cap'], 1)

    def test_probe_requires_actual_ok_result(self):
        gateway = rt.Gateway(self.cfg)
        for output, expected in [('{"type":"result","is_error":false,"result":"wrong"}', 'probe_fail'),
                                 ('{"type":"result","is_error":false,"result":"OK"}', 'probe_ok')]:
            with mock.patch.object(gateway, 'probe_due', return_value=True), \
                 mock.patch.object(gateway, 'acquire', return_value=True), \
                 mock.patch.object(gateway, 'event') as event, \
                 mock.patch.object(lib, 'run', return_value=done(0, output)):
                gateway.health_probe()
            event.assert_called_once_with(expected)


class BudgetAndLocking(Sandbox):
    def test_all_entrypoints_keep_earliest_batch_deadline(self):
        task = self.make_task()
        earlier = time.time() + 10
        lib.save_json(task.parent / 'runner/budget.json', {'deadline': earlier})
        self.assertEqual(rt.batch_deadline(self.root, task.parent.name, self.cfg), earlier)
        lib.save_json(task.parent / 'runner/budget.json', {'deadline': earlier + 1000})
        self.assertEqual(rt.batch_deadline(self.root, task.parent.name, self.cfg), earlier)

    def test_gate_selected_task_does_not_lock_other_running_task(self):
        import fbr_feedback
        first, second = self.make_task(1), self.make_task(2)
        held, release = threading.Event(), threading.Event()
        def running():
            with lib.file_lock(second / '.operation.lock'):
                held.set()
                release.wait(5)
        thread = threading.Thread(target=running)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            rows, _ = fbr_feedback.gate_batch(self.root, first.parent.name, self.cfg, first.name)
            self.assertEqual(rows, [])
        finally:
            release.set()
            thread.join()

    def test_persistent_worker_survives_parent_exit(self):
        directory = self.tmp / 'orphan'
        directory.mkdir()
        request = {'id':'orphan-test', 'command':[sys.executable, '-c', 'import time;time.sleep(.2);print("finished")'],
                   'stdin':'', 'stdout':str(directory/'out'), 'stderr':str(directory/'err'),
                   'result':str(directory/'result.json')}
        path = directory/'request.json'
        lib.save_json(path,request)
        worker=Path(rt.__file__).with_name('fbr_worker.py')
        parent='import os,subprocess,sys;subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);os._exit(0)'
        subprocess.run([sys.executable,'-c',parent,str(worker),str(path)],check=True,timeout=5)
        until=time.monotonic()+5
        while not Path(request['result']).exists() and time.monotonic()<until:
            time.sleep(.02)
        self.assertEqual(lib.load_json(Path(request['result']))['id'],'orphan-test')
        self.assertEqual((directory/'out').read_text().strip(),'finished')


class ErrorProvenance(DockerCase):
    def test_successful_product_description_of_401_is_not_gateway_auth(self):
        task=self.make_task()
        self.docker.outcomes=[self.good(stdout=stream_text(SID,answer='已修复接口 401 和 unauthorized 错误。'))]
        self.assertEqual(fbr_run.run_one(task,self.cfg)['state'],'ok')

    def test_actual_api_401_stops_without_new_attempt(self):
        task=self.make_task()
        self.docker.outcomes=[self.good(stdout=stream_text(SID,answer='API Error: 401 unauthorized',is_error=True))]
        self.assertEqual(fbr_run.run_one(task,self.cfg)['state'],'failed')
        self.assertEqual(len(lib.load_json(task/'run/attempts.json')),1)
        self.assertEqual(self.docker.docker_calls('rm'),[])


class CompleteLogs(Sandbox):
    def test_full_test_output_supports_earlier_excerpt_and_detects_tamper(self):
        import hashlib, fbr_quality
        task=self.make_task()
        path=write(task/'evidence/command-logs/after/1.log','early error detail\n'+'padding\n'*1000+'final line')
        facts={'tests':{'before':{},'after':{'test_tail':'final line','outputs':[{'name':'test',
            'output_file':str(path.relative_to(task)),'output_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}}}
        fbr_quality.enrich_test_logs(task,facts)
        self.assertIn('early error detail',fbr_quality.source_text({'source':{'kind':'tests_after'}},facts,[]))
        path.write_text('tampered')
        with self.assertRaisesRegex(lib.FbrError,'摘要不符'):
            fbr_quality.enrich_test_logs(task,facts)
