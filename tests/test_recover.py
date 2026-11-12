import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fbr_recover as recovery

class RecoveryTest(unittest.TestCase):
    def setUp(self):
        loader = patch.object(recovery, 'load_engine', return_value=recovery.reviewer)
        loader.start()
        self.addCleanup(loader.stop)

    def test_rejected_round_continues_and_keeps_model_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            calls = []
            def invoke(*args, **kwargs):
                calls.append(args[3])
                return '{"audit":{},"feedback":{},"needs_probes":[]}', {'model':'gpt-6.1-sol'}
            rounds = []
            def review(task, base, secret):
                rounds.append(True)
                recovery.reviewer.invoke(base, secret, task, 'generation')
                if len(rounds) == 1:
                    recovery.lib.save_json(task/'review/quality.json', {'history':[{'errors':['bad excerpt']}]})
                    raise recovery.lib.FbrError('Feedback 连续 3 版未通过，停止；原因见 review/quality.json，不采用失败候选')
                return {'state':'ok'}
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'invoke', side_effect=invoke) as original, \
                 patch.object(recovery.reviewer, 'review', side_effect=review):
                result = recovery.recover(task, 'https://example.test', 'secret')
                self.assertIs(recovery.reviewer.invoke, original)
            self.assertEqual(result['state'], 'ok')
            self.assertIn('bad excerpt', calls[1])
            files = sorted((task/'review/recovery').glob('call-*.json'))
            self.assertEqual(len(files), 2)
            self.assertEqual(json.loads(files[0].read_text())['state'], 'completed')

    def test_transport_failure_propagates_without_retry_or_fake_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'review', side_effect=recovery.lib.FbrError('gateway down')) as run:
                with self.assertRaisesRegex(recovery.lib.FbrError, 'gateway down'):
                    recovery.recover(task, 'base', 'secret')
                self.assertEqual(run.call_count, 1)
            self.assertFalse((task/'review/model-review.json').exists())

    def test_valid_task_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=[]), \
                 patch.object(recovery.reviewer, 'review') as run:
                result = recovery.recover(Path(temp), 'base', 'secret')
                self.assertEqual(result['state'], 'already_valid')
                run.assert_not_called()

    def test_resume_does_not_overwrite_existing_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            directory = task/'review/recovery'
            directory.mkdir(parents=True)
            existing = directory/'call-0004.json'
            existing.write_text('original')
            before = directory/'cycle-0003-before.json'
            before.write_text('original cycle')
            def review(task, base, secret):
                recovery.reviewer.invoke(base, secret, task, 'generation')
                return {'state':'ok'}
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'invoke', return_value=('{}', {})), \
                 patch.object(recovery.reviewer, 'review', side_effect=review):
                recovery.recover(task, 'base', 'secret')
            self.assertEqual(existing.read_text(), 'original')
            self.assertEqual(before.read_text(), 'original cycle')
            self.assertTrue((directory/'call-0005.json').exists())
            self.assertTrue((directory/'cycle-0004-before.json').exists())

    def test_flow_uses_recovery_before_gate_and_sheet(self):
        import argparse
        import threading
        import fbr_flow, fbr_feedback, fbr_sheet
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)/'task'
            options = argparse.Namespace(reviewer_env='unused', batch='batch',
                                         delivery_lock=threading.Lock(), auto_submit=False)
            with patch.object(recovery.lib, 'stage_ok', return_value=False), \
                 patch.object(recovery.reviewer, 'credentials', return_value=('base','secret')), \
                 patch.object(fbr_feedback, 'card_one'), \
                 patch.object(recovery, 'recover', return_value={'state':'ok'}) as run, \
                 patch.object(fbr_feedback, 'gate_batch', return_value=([{'task_id':'task','state':'ok'}],{})), \
                 patch.object(fbr_sheet, 'write_sheet') as sheet:
                self.assertEqual(fbr_flow.review_task(task, Path(temp), {}, options)['state'], 'ok')
                run.assert_called_once_with(task, 'base', 'secret')
                sheet.assert_called_once()

class EngineIsolationTest(unittest.TestCase):
    def test_loaded_reviewers_have_independent_invoke_globals(self):
        left,right=recovery.load_engine(),recovery.load_engine()
        original=right.invoke
        left.invoke=lambda *args:None
        self.assertIs(right.invoke,original)
        self.assertIsNot(left.review.__wrapped__.__globals__,right.review.__wrapped__.__globals__)
