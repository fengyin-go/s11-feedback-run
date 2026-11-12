"""Real harmless workers survive monitor loss; adoption never resends input."""
import copy
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

from fixtures import Sandbox
import fbrlib as lib
import fbr_runtime as rt


class Adoption(Sandbox):
    def test_recovered_thinking_stops_wait_even_when_new_dispatch_is_full(self):
        task = self.make_task()
        cfg = copy.deepcopy(self.cfg)
        cfg['run']['poll_seconds'] = .01
        lib.save_json(task / 'run/current.json', {'state': 'starting', 'attempt': 1})
        gateway = mock.Mock()
        gateway.acquire.return_value = False
        gateway.track_existing.return_value = True
        command = [sys.executable, '-u', '-c',
            'import json,time; '
            'print(json.dumps({"type":"system","subtype":"api_retry","error_status":504})); '
            'time.sleep(.3); '
            'print(json.dumps({"type":"system","subtype":"thinking_tokens",'
            '"estimated_tokens":10,"estimated_tokens_delta":2})); '
            'time.sleep(.3); print(json.dumps({"type":"result","is_error":False}))']
        beats = []
        original_save = lib.save_json
        def save(path, doc):
            if Path(path).name == 'heartbeat.json':
                beats.append(dict(doc))
            return original_save(path, doc)
        with mock.patch.object(lib, 'save_json', side_effect=save):
            result = rt.monitored_stream(command, 'once', task / 'run/out', task / 'run/err', 5,
                {'task': task, 'cfg': cfg, 'gateway': gateway, 'segment': 0})
        self.assertEqual(result, (0, False))
        self.assertTrue(any(b['gateway_wait'] for b in beats))
        self.assertTrue(any(b['events'] == 2 and not b['gateway_wait'] for b in beats))
        self.assertEqual(gateway.track_existing.call_count, 0)
        gateway.release.assert_not_called()
        self.assertEqual(len(list((task / 'run').glob('exec-*.json'))), 1)

    def orphan(self):
        task = self.make_task()
        cfg = copy.deepcopy(self.cfg)
        cfg['run']['poll_seconds'] = .01
        lib.save_json(task / 'run/current.json', {'state': 'starting', 'attempt': 1})
        command = [sys.executable, '-c',
            'import json,time; time.sleep(1); print(json.dumps({"type":"result","is_error":False,"result":"once"}))']
        workers = []
        original_popen = rt.subprocess.Popen
        def keep_worker(*args, **kwargs):
            worker = original_popen(*args, **kwargs)
            workers.append(worker)
            return worker
        self.addCleanup(lambda: [worker.wait(timeout=10) for worker in workers])
        with mock.patch.object(rt.subprocess, 'Popen', side_effect=keep_worker), mock.patch.object(rt, 'monitor_process', side_effect=RuntimeError('monitor disconnected')):
            with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                rt.monitored_stream(command, 'original input', task / 'run/out', task / 'run/err', 5,
                    {'task': task, 'cfg': cfg, 'gateway': mock.Mock(), 'segment': 0})
        current = lib.load_json(task / 'run/current.json')
        self.addCleanup(self.reap, current['pid'])
        return task, cfg, current

    @staticmethod
    def reap(pid):
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_adopts_same_worker_and_receipt_without_new_dispatch(self):
        task, cfg, current = self.orphan()
        self.assertTrue(rt.alive(current))
        result = rt.monitor_existing(task, cfg, current)
        self.assertEqual(result, (0, False))
        saved = lib.load_json(task / 'run/current.json')
        self.assertEqual(saved['pid'], current['pid'])
        self.assertEqual(saved['request_id'], current['request_id'])
        self.assertEqual(saved['state'], 'exited')
        self.assertEqual(len(list((task / 'run').glob('exec-*.json'))), 1)
        self.assertEqual((task / 'run/out').read_text().count('once'), 1)
        self.assertEqual(lib.load_json(task / 'run/heartbeat.json')['pid'], current['pid'])
        with rt.Gateway(cfg).transaction() as pressure:
            self.assertEqual(pressure.get('slots'), {})

    def test_unobserved_time_is_charged_without_clearing_budget(self):
        task, cfg, current = self.orphan()
        path = task / 'run' / ('exec-' + current['request_id'] + '.json')
        request = lib.load_json(path)
        request['started_epoch'] -= 2
        request['base_budget'] = {'active_seconds': 20, 'waiting_seconds': 10}
        lib.save_json(path, request)
        lib.save_json(task / 'run/budget.json', {'active_seconds': 21, 'waiting_seconds': 11})
        rt.monitor_existing(task, cfg, current)
        budget = lib.load_json(task / 'run/budget.json')
        self.assertGreaterEqual(budget['active_seconds'], 22)
        self.assertEqual(budget['waiting_seconds'], 11)

    def test_historical_retry_does_not_emit_new_pressure(self):
        task, cfg, current = self.orphan()
        # The worker has not emitted anything yet; append a historical retry for reconstruction.
        time.sleep(.1)
        with Path(current['stdout']).open('a') as stream:
            stream.write(json.dumps({'type': 'system', 'subtype': 'api_retry', 'error_status': 429}) + '\n')
        gateway = mock.Mock()
        gateway.track_existing.return_value = True
        with mock.patch.object(rt, 'Gateway', return_value=gateway):
            self.assertEqual(rt.monitor_existing(task, cfg, current), (0, False))
        gateway.event.assert_not_called()

    def test_mismatched_request_is_blocked_without_launching_worker(self):
        task, cfg, current = self.orphan()
        current['stdout'] = str(task / 'run/wrong-output')
        with mock.patch.object(rt.subprocess, 'Popen', side_effect=AssertionError('must not launch')):
            with self.assertRaisesRegex(lib.FbrError, '路径不匹配'):
                rt.monitor_existing(task, cfg, current)
