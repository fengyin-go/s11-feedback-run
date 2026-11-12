"""独立验收的隔离、错误处理与证据绑定；不请求真实网关。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from flow import FlowCase, audit, feedback, mock_claim_reviews
import fbr_feedback
import fbr_delivery
import fbr_reviewer as reviewer
import fbrlib as lib
import fbr_quality as quality


class Configuration(unittest.TestCase):
    def test_credentials_require_complete_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reviewer.env"
            path.write_text("base_url=\napikey=\n")
            path.chmod(0o600)
            with self.assertRaises(lib.FbrError):
                reviewer.credentials(path)
            path.write_text("base_url=https://review.example.test/v1/\napikey=test-review-secret\n")
            self.assertEqual(reviewer.credentials(path), ("https://review.example.test/v1", "test-review-secret"))
            path.chmod(0o644)
            with self.assertRaises(lib.FbrError):
                reviewer.credentials(path)

    def test_trace_model_settings_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'reviewer.env'
            path.write_text('model=configured-trace-model\nreasoning_effort=medium\n')
            self.assertEqual(reviewer.trace_configuration(path), ('configured-trace-model', 'medium'))
            path.write_text('base_url=https://example.test\napikey=offline\n')
            self.assertEqual(reviewer.trace_configuration(path), (reviewer.MODEL, reviewer.EFFORT))

    def test_process_uses_fixed_model_and_isolated_environment(self):
        secret = "test-review-secret"
        original_home = os.environ.get("CODEX_HOME")
        def run(args, **kwargs):
            self.assertNotIn(secret, " ".join(args))
            self.assertEqual(args[args.index("--model") + 1], "gpt-6.1-sol")
            self.assertIn('model_reasoning_effort="high"', args)
            self.assertIn('model_provider="solo11_reviewer"', args)
            self.assertIn("--ignore-user-config", args)
            self.assertIn("--ephemeral", args)
            env = kwargs["env"]
            self.assertEqual(env[reviewer.KEY_ENV], secret)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("OPENAI_BASE_URL", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotEqual(env["CODEX_HOME"], "/tmp/existing-codex-home")
            self.assertTrue(Path(env["CODEX_HOME"]).is_dir())
            Path(args[args.index("--output-last-message") + 1]).write_text("OK")
            return subprocess.CompletedProcess(args, 0, "{}", "")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "main-secret", "OPENAI_BASE_URL": "https://main.test",
                                         "ANTHROPIC_AUTH_TOKEN": "other-secret", "CODEX_HOME": "/tmp/existing-codex-home"}), \
             mock.patch.object(reviewer.shutil, "which", return_value="codex"), \
             mock.patch.object(reviewer.subprocess, "run", side_effect=run):
            answer, receipt = reviewer.invoke("https://review.example.test/v1", secret, Path.cwd(), "probe")
        self.assertEqual(answer, "OK")
        self.assertEqual(receipt["reasoning_effort"], "high")
        self.assertEqual(os.environ.get("CODEX_HOME"), original_home)

    def test_failure_timeout_and_secret_output_stop_without_retry(self):
        for kind in ("failed", "timeout", "leak"):
            with self.subTest(kind=kind):
                def run(args, **kwargs):
                    if kind == "timeout":
                        raise subprocess.TimeoutExpired(args, 1)
                    if kind == "leak":
                        Path(args[args.index("--output-last-message") + 1]).write_text("test-review-secret")
                        return subprocess.CompletedProcess(args, 0, "", "")
                    return subprocess.CompletedProcess(args, 1, "", "unsupported model")
                with mock.patch.object(reviewer.shutil, "which", return_value="codex"), \
                     mock.patch.object(reviewer.subprocess, "run", side_effect=run) as called:
                    with self.assertRaises(lib.FbrError) as error:
                        reviewer.invoke("https://review.example.test/v1", "test-review-secret", Path.cwd(), "probe")
                    self.assertNotIn("test-review-secret", str(error.exception))
                    self.assertEqual(called.call_count, 1)


class TraceNavigation(unittest.TestCase):
    def test_indexes_all_sessions_and_pairs_tools_without_copying_large_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            source = task / "trace/projects/main.jsonl"
            source.parent.mkdir(parents=True)
            rows = [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "call1", "name": "Bash", "input": {"command": "make test"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "call1", "is_error": True, "content": "x" * 100000}]}}]
            source.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            child = source.parent / "subagents/child.jsonl"
            child.parent.mkdir()
            child.write_text(json.dumps({"type": "assistant", "message": {"content": "later fixed"}}) + "\n")
            before = source.read_bytes()
            index = reviewer.trace_index(task)
            entries = [json.loads(line) for line in index.read_text().splitlines()]
            self.assertEqual(len(entries), 5)
            self.assertEqual(entries[1]["blocks"][0]["id"], entries[2]["blocks"][0]["tool_use_id"])
            self.assertTrue(entries[2]["blocks"][0]["is_error"])
            self.assertEqual(entries[2]["line"], 2)
            self.assertIn("subagents/child.jsonl", entries[3]["source"])
            self.assertLess(index.stat().st_size, 2000)
            self.assertEqual(source.read_bytes(), before)

    def test_corrupt_trace_is_rejected_instead_of_silently_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            source = task / "trace/projects/main.jsonl"
            source.parent.mkdir(parents=True)
            source.write_text("broken json\n")
            with self.assertRaisesRegex(lib.FbrError, "JSON 损坏"):
                reviewer.trace_index(task)


class Review(FlowCase):
    def setUp(self):
        super().setUp()
        publishing = mock.patch('fbr_flow.finish_review', return_value={'state':'ok'})
        self.finish_delivery = publishing.start()
        self.addCleanup(publishing.stop)

    def semantic(self):
        return json.dumps({"claim_reviews": mock_claim_reviews(feedback()), "checks": [{"id": name, "passed": True,
                          "reason": "已对照当前轨迹和最终产物核对本项具体事实"} for name in quality.CHECKS]})

    def receipt(self):
        return {"model": reviewer.MODEL, "reasoning_effort": reviewer.EFFORT, "provider": reviewer.PROVIDER}

    def candidate(self):
        return json.dumps({"audit": audit(), "feedback": feedback(), "needs_probes": []}), self.receipt()

    def agent_prepared(self, number=1):
        task = self.written(number)
        fbr_feedback.card_one(task)
        # Offline fixture only: refresh the bound synthetic trace report.
        self.review_receipt(task)
        reviewer.prepare_task_agent(task)
        return task

    def verification(self, task, candidate):
        report = json.loads(self.semantic())
        report.update(task_agent='current_task_agent', candidate_sha=lib.content_hash(candidate),
                      binding=lib.load_json(task/'review/task-agent-work.json')['binding'])
        return report

    def test_review_only_analyzes_traces_then_returns_checkpoint(self):
        task = self.agent_prepared()
        import fbr_trace_review
        with mock.patch.object(fbr_trace_review, 'analyze', return_value=lib.load_json(task/'review/trace-analysis.json')) as analysis, \
             mock.patch.object(reviewer, 'invoke', side_effect=AssertionError('product model must never be called')):
            result = reviewer.review(task, 'https://review.example.test/v1', 'test-review-secret')
        self.assertEqual(result['state'], 'needs_task_agent')
        analysis.assert_called_once()

    def test_acceptance_and_process_recheck_use_current_ai_without_model_config(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        self.assertFalse((task/'review/trace-claims.json').exists())
        with mock.patch.object(reviewer, 'invoke', side_effect=AssertionError('no external model')), \
             mock.patch.object(reviewer, 'credentials', side_effect=AssertionError('no reviewer.env')), \
             mock.patch.object(reviewer, 'trace_configuration', side_effect=AssertionError('no model settings')):
            self.assertEqual(reviewer.finalize(task, candidate, verification)['state'], 'ok')
            self.assertEqual(reviewer.provenance_errors(task), [])
        receipt = lib.load_json(task/'review/model-review.json')
        self.assertEqual(receipt['provider'], 'current_task_agent')
        self.assertEqual(receipt['model'], 'current_task_agent')
        self.assertEqual(receipt['reasoning_effort'], 'session')
        self.assertNotIn('trace-claims_sha', receipt)

    def test_semantic_rejection_keeps_old_candidate_and_can_be_repaired(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        before = (task/'review/feedback.json').read_bytes()
        verification = self.verification(task, candidate)
        verification['checks'][3].update(passed=False, reason='扣分依据只引用已修复的过程错误，须重查最终影响')
        with mock.patch.object(reviewer, 'invoke', side_effect=AssertionError('no model')), \
             mock.patch.object(reviewer, 'credentials', side_effect=AssertionError('no env')):
            result = reviewer.finalize(task, candidate, verification)
        self.assertEqual(result['state'], 'repairing')
        self.assertEqual((task/'review/feedback.json').read_bytes(), before)
        verification = self.verification(task, candidate)
        with mock.patch('fbr_sheet.write_sheet') as sheet:
            result = reviewer.finalize(task, candidate, verification)
        self.assertEqual(result['state'], 'ok')
        self.assertGreaterEqual(sheet.call_count, 1)
        self.assertEqual(reviewer.provenance_errors(task), [])

    def test_overall_pass_without_original_recheck_is_rejected(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        verification.pop('claim_reviews')
        result = reviewer.finalize(task, candidate, verification)
        self.assertEqual(result['state'], 'repairing')
        self.assertIn('逐句复查缺失', str(result))

    def test_stale_evidence_and_changed_candidate_are_refused(self):
        for kind in ('candidate', 'evidence'):
            with self.subTest(kind=kind):
                task = self.agent_prepared(1 if kind == 'candidate' else 2)
                candidate = json.loads(self.candidate()[0])
                verification = self.verification(task, candidate)
                if kind == 'candidate': candidate['feedback']['note'] = 'changed'
                else:
                    path = task/'evidence/facts.json'
                    path.write_text(path.read_text()+'\n')
                with self.assertRaises(lib.FbrError): reviewer.finalize(task, candidate, verification)

    def test_missing_rejected_or_stale_quality_blocks_delivery(self):
        task = self.gated()
        path = task / "review/quality.json"
        cert = lib.load_json(path)
        for kind in ("missing", "rejected", "policy", "binding", "check"):
            lib.save_json(path, cert)
            if kind == "missing":
                path.unlink()
            else:
                bad = json.loads(json.dumps(cert))
                if kind == "rejected":
                    bad["state"] = "rejected"
                elif kind == "policy":
                    bad["policy_sha"] = "old rules"
                elif kind == "binding":
                    bad["feedback_sha"] = "old text"
                else:
                    bad["checks"][0]["passed"] = False
                lib.save_json(path, bad)
            self.assertTrue(reviewer.provenance_errors(task), kind)
            self.assertFalse(fbr_delivery.current_feedback(task)[0], kind)

    def test_success_saves_receipt_and_gate_rejects_changed_output(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        with mock.patch('fbr_sheet.write_sheet'):
            result = reviewer.finalize(task, candidate, self.verification(task, candidate))
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(reviewer.provenance_errors(task), [])
        doc = lib.load_json(task/'review/feedback.json'); doc['note'] = '手改评价'
        lib.save_json(task/'review/feedback.json', doc)
        self.assertTrue(reviewer.provenance_errors(task))

    def test_missing_receipt_wrong_model_and_changed_evidence_block_delivery(self):
        task = self.gated()
        path = task / "review/model-review.json"
        receipt = lib.load_json(path)
        for kind in ("missing", "model", "effort", "evidence"):
            with self.subTest(kind=kind):
                lib.save_json(path, receipt)
                if kind == "missing":
                    path.unlink()
                elif kind in ("model", "effort"):
                    bad = {**receipt, "model" if kind == "model" else "reasoning_effort": "other"}
                    lib.save_json(path, bad)
                else:
                    facts = task / "evidence/facts.json"
                    facts.write_text(facts.read_text() + "\n")
                self.assertTrue(reviewer.provenance_errors(task))
                self.assertFalse(fbr_delivery.current_feedback(task)[0])

    def test_probe_request_preserved_for_real_execution(self):
        task = self.agent_prepared()
        candidate = {'audit': None, 'feedback': None, 'needs_probes': [{'cmd': 'make test', 'reason': '补证'}]}
        result = reviewer.finalize(task, candidate, self.verification(task, candidate))
        self.assertEqual(result['state'], 'needs_probes')
        self.assertFalse(lib.stage_ok(task, 'feedback'))
        self.assertTrue((task/'review/probe-requests.json').is_file())
