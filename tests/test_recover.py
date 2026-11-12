"""Recovery hands product work to the current task AI; never runs a product reviewer."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fbr_recover as recovery

class RecoveryTest(unittest.TestCase):
    def test_missing_review_returns_task_agent_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'review', return_value={'state':'needs_task_agent'}) as run:
                result = recovery.recover(task, 'base', 'secret', model='configured-trace', effort='medium')
            self.assertEqual(result['state'], 'needs_task_agent')
            run.assert_called_once_with(task, 'base', 'secret', model='configured-trace', effort='medium')

    def test_transport_failure_keeps_evidence_without_fake_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'review', side_effect=recovery.lib.FbrError('gateway down')) as run:
                with self.assertRaisesRegex(recovery.lib.FbrError, 'gateway down'):
                    recovery.recover(task, 'base', 'secret')
                self.assertEqual(run.call_count, 1)
            self.assertFalse((task/'review/model-review.json').exists())

    def test_valid_task_preserved_without_trace_call(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=[]), \
                 patch.object(recovery.reviewer, 'review') as run:
                self.assertEqual(recovery.recover(Path(temp), 'base', 'secret')['state'], 'already_valid')
                run.assert_not_called()

    def test_repair_reuses_trace_analysis_without_reading_reviewer_env(self):
        for errors, repair_gate in ((['acceptance missing'], False), ([], True)):
            with self.subTest(errors=errors, repair_gate=repair_gate), tempfile.TemporaryDirectory() as temp:
                task = Path(temp)
                recovery.lib.save_json(task/'review/gate.json', {'passed':False,'errors':['duplicate']})
                with patch.object(recovery.reviewer, 'provenance_errors', return_value=errors), \
                     patch.object(recovery.reviewer, 'analysis_errors', return_value=[]), \
                     patch.object(recovery.reviewer, 'prepare_task_agent', return_value={'state':'needs_task_agent'}) as prepare, \
                     patch.object(recovery.reviewer, 'credentials', side_effect=AssertionError('no env')), \
                     patch.object(recovery.reviewer, 'trace_configuration', side_effect=AssertionError('no model settings')), \
                     patch.object(recovery.reviewer, 'review', side_effect=AssertionError('no repeated trace call')):
                    result = recovery.recover(task, repair_gate=repair_gate, env_file='/does/not/exist')
                self.assertEqual(result['state'], 'needs_task_agent')
                prepare.assert_called_once_with(task)

    def test_missing_trace_analysis_loads_only_its_config_when_needed(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            config = Path(temp)/'trace.env'
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'analysis_errors', return_value=['missing trace']), \
                 patch.object(recovery.reviewer, 'credentials', return_value=('base','secret')) as credentials, \
                 patch.object(recovery.reviewer, 'trace_configuration', return_value=('trace-model','high')) as settings, \
                 patch.object(recovery.reviewer, 'review', return_value={'state':'needs_task_agent'}) as analyze:
                result = recovery.recover(task, env_file=str(config))
            self.assertEqual(result['state'], 'needs_task_agent')
            credentials.assert_called_once_with(config)
            settings.assert_called_once_with(config)
            analyze.assert_called_once_with(task,'base','secret',model='trace-model',effort='high')

    def test_gate_failure_requires_current_ai_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            recovery.lib.save_json(task/'review/gate.json', {'passed':False,'errors':['duplicate']})
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=[]), \
                 patch.object(recovery.reviewer, 'review', return_value={'state':'needs_task_agent'}):
                self.assertEqual(recovery.recover(task, 'base', 'secret', repair_gate=True)['state'], 'needs_task_agent')

    def test_recovery_preserves_original_files(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            path = task/'review/recovery/call-0004.json'
            path.parent.mkdir(parents=True); path.write_text('original history')
            with patch.object(recovery.reviewer, 'provenance_errors', return_value=['missing']), \
                 patch.object(recovery.reviewer, 'review', return_value={'state':'needs_task_agent'}):
                recovery.recover(task, 'base', 'secret')
            self.assertEqual(path.read_text(), 'original history')

    def test_flow_does_not_gate_or_finish_while_agent_work_pending(self):
        import argparse,threading
        import fbr_flow,fbr_feedback
        with tempfile.TemporaryDirectory() as temp:
            task=Path(temp)/'task'
            options=argparse.Namespace(reviewer_env='unused',batch='batch',delivery_lock=threading.Lock(),auto_submit=False)
            with patch.object(recovery.lib,'stage_ok',return_value=False), \
                 patch.object(recovery.reviewer,'credentials',return_value=('base','secret')), \
                 patch.object(recovery.reviewer,'trace_configuration',return_value=('trace','high')), \
                 patch.object(fbr_feedback,'card_one'), \
                 patch.object(recovery,'recover',return_value={'state':'needs_task_agent'}), \
                 patch.object(fbr_feedback,'gate_batch') as gate, \
                 patch.object(fbr_flow,'finish_review') as finish:
                self.assertEqual(fbr_flow.review_task(task,Path(temp),{},options)['state'],'needs_task_agent')
                gate.assert_not_called();finish.assert_not_called()
