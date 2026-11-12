import copy
import json
import os
from unittest.mock import patch

from fixtures import Sandbox, PROMPT, VERSION, done, trace_rows, write, write_projects
import fbr_completion as completion
import fbr_trace
import fbrlib as lib

SID = '11111111-2222-3333-4444-555555555555'


class CompletionTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.task = self.make_task(repo=False)
        self.path = self.task / 'run/stream.jsonl'
        self.answer = '本轮已完成。'
        self.rows = [
            {'type': 'system', 'subtype': 'init', 'model': 'auto_model/urm',
             'session_id': SID, 'claude_code_version': VERSION},
            {'type': 'assistant', 'session_id': SID, 'message': {
                'content': [{'type': 'text', 'text': self.answer}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': self.answer,
             'terminal_reason': 'completed', 'stop_reason': 'end_turn', 'session_id': SID, 'uuid': 'end-1'}]
        self.save()
        self.current = {'request_id': 'request-1', 'container_id': 'container-1', 'stdout': str(self.path)}
        self.request = {'id': 'request-1', 'stdout': str(self.path)}
        self.calls = []

    def save(self):
        write(self.path, ''.join(json.dumps(r) + '\n' for r in self.rows))
        os.utime(self.path, (1, 1))

    def docker(self, cmd, **kwargs):
        self.calls.append(cmd[1])
        if cmd[1] == 'inspect':
            return done(out=json.dumps({'Id': 'container-1', 'Config': {'Labels': {
                'solo11.owner': lib.content_hash(str(self.task.resolve()))}}}))
        if cmd[1] == 'cp':
            write_projects(self.task / 'run/completed-request-1/projects', SID,
                           trace_rows(SID, answer=self.answer))
            return done()
        if cmd[1] == 'stop':
            return done()
        self.fail(str(cmd))

    def cleanup(self):
        with patch.object(lib, 'run', side_effect=self.docker):
            result = completion.cleanup(self.task, self.cfg, self.current, self.request)
        self.current['completion_cleanup'] = result
        return result

    def test_exports_matching_trace_before_stop_and_keeps_real_rc(self):
        self.assertTrue(self.cleanup())
        self.assertEqual(self.calls, ['inspect', 'cp', 'stop'])
        receipt = {'id': 'request-1', 'rc': 137, 'timed_out': False}
        self.assertTrue(completion.valid(self.task, self.current, receipt))
        stream = fbr_trace.read_stream(self.path)
        self.assertEqual(fbr_trace.classify_attempt(137, False, stream)['verdict'], 'retry')
        self.assertEqual(fbr_trace.classify_attempt(137, False, stream, completed_cleanup=True)['verdict'], 'accept')
        self.assertEqual(receipt['rc'], 137)
        self.assertEqual(fbr_trace.classify_attempt(137, True, stream, completed_cleanup=True)['verdict'], 'retry')

    def test_error_incomplete_or_mismatched_events_never_stop_container(self):
        original = copy.deepcopy(self.rows)
        variants = [{'is_error': True}, {'terminal_reason': 'api_error'}, {'stop_reason': 'max_tokens'},
                    {'session_id': 'other'}, {'result': 'different'}, {'uuid': ''}, {'parent_tool_use_id': 'child'}]
        for change in variants:
            self.rows = copy.deepcopy(original)
            self.rows[-1].update(change)
            self.save()
            with self.subTest(change=change), patch.object(lib, 'run') as run:
                self.assertIsNone(completion.cleanup(self.task, self.cfg, self.current, self.request))
                run.assert_not_called()

    def test_new_output_invalidates_terminal_and_recent_result_waits(self):
        os.utime(self.path, None)
        with patch.object(lib, 'run') as run:
            self.assertIsNone(completion.cleanup(self.task, self.cfg, self.current, self.request))
            run.assert_not_called()
        self.rows.append({'type': 'assistant', 'message': {'content': []}})
        self.save()
        self.assertIsNone(completion.terminal(self.path, 'auto_model/urm'))

    def test_trace_mismatch_preserves_container(self):
        def docker(cmd, **kwargs):
            result = self.docker(cmd, **kwargs)
            if cmd[1] == 'cp':
                write_projects(self.task / 'run/completed-request-1/projects', SID,
                               trace_rows(SID, prompt='different prompt', answer=self.answer))
            return result
        with patch.object(lib, 'run', side_effect=docker):
            with self.assertRaisesRegex(lib.FbrError, '不一致'):
                completion.cleanup(self.task, self.cfg, self.current, self.request)
        self.assertNotIn('stop', self.calls)

    def test_tampering_or_timeout_cannot_salvage_execution(self):
        self.cleanup()
        receipt = {'id': 'request-1', 'rc': 137, 'timed_out': False}
        self.assertFalse(completion.valid(self.task, self.current, {**receipt, 'id': 'other'}))
        self.assertFalse(completion.valid(self.task, self.current, {**receipt, 'timed_out': True}))
        project = next((self.task / 'run/completed-request-1/projects').rglob('*.jsonl'))
        project.write_text(project.read_text() + '{}\n')
        self.assertFalse(completion.valid(self.task, self.current, receipt))
