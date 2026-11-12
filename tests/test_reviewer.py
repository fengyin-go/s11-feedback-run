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
    def semantic(self):
        return json.dumps({"claim_reviews": mock_claim_reviews(feedback()), "checks": [{"id": name, "passed": True,
                          "reason": "已对照当前轨迹和最终产物核对本项具体事实"} for name in quality.CHECKS]})

    def receipt(self):
        return {"model": reviewer.MODEL, "reasoning_effort": reviewer.EFFORT, "provider": reviewer.PROVIDER}

    def candidate(self):
        return json.dumps({"audit": audit(), "feedback": feedback(), "needs_probes": []}), self.receipt()

    def test_semantic_rejection_rewrites_then_adopts(self):
        task = self.written()
        fbr_feedback.card_one(task)
        rejected = json.loads(self.semantic())
        rejected["checks"][3].update(passed=False, reason="扣分依据只引用已修复的过程错误，须重查最终影响")
        with mock.patch.object(reviewer, "invoke", side_effect=[self.candidate(),
                (json.dumps(rejected), self.receipt()), self.candidate(), (self.semantic(), self.receipt())]) as called:
            result = reviewer.review(task, "https://review.example.test/v1", "test-review-secret")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("已修复的过程错误", called.call_args_list[2].args[3])
        self.assertEqual(len(lib.load_json(task / "review/quality.json")["history"]), 2)
        self.assertEqual(reviewer.provenance_errors(task), [])

    def test_overall_pass_without_original_recheck_is_rejected(self):
        task = self.written()
        fbr_feedback.card_one(task)
        before = (task / 'review/feedback.json').read_bytes()
        missing = json.loads(self.semantic())
        missing.pop('claim_reviews')
        calls = [self.candidate(), (json.dumps(missing), self.receipt())] * 3
        with mock.patch.object(reviewer, 'invoke', side_effect=calls):
            with self.assertRaisesRegex(lib.FbrError, '连续 3 版'):
                reviewer.review(task, 'https://review.example.test/v1', 'test-review-secret')
        self.assertEqual((task / 'review/feedback.json').read_bytes(), before)
        self.assertIn('逐句复查缺失', str(lib.load_json(task / 'review/quality.json')))

    def test_three_bad_drafts_stop_without_adopting(self):
        task = self.written()
        fbr_feedback.card_one(task)
        before = (task / "review/feedback.json").read_bytes()
        bad = json.loads(self.candidate()[0])
        bad["feedback"]["results"] = []
        with mock.patch.object(reviewer, "invoke", return_value=(json.dumps(bad), self.receipt())) as called:
            with self.assertRaisesRegex(lib.FbrError, "连续 3 版"):
                reviewer.review(task, "https://review.example.test/v1", "test-review-secret")
        self.assertEqual(called.call_count, 3)
        self.assertEqual((task / "review/feedback.json").read_bytes(), before)
        self.assertTrue(reviewer.provenance_errors(task))

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
        task = self.written()
        fbr_feedback.card_one(task)
        with mock.patch.object(reviewer, "invoke", side_effect=[(
                json.dumps({"audit": audit(), "feedback": feedback(), "needs_probes": []}), self.receipt()),
                (self.semantic(), self.receipt())]) as called:
            result = reviewer.review(task, "https://review.example.test/v1", "test-review-secret")
        self.assertEqual(called.call_count, 2)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(reviewer.provenance_errors(task), [])
        self.assertEqual(fbr_feedback.gate_task(task, self.cfg)["errors"], [])
        doc = lib.load_json(task / "review/feedback.json")
        doc["note"] = "手改评价"
        lib.save_json(task / "review/feedback.json", doc)
        self.assertTrue(reviewer.provenance_errors(task))
        self.assertTrue(fbr_feedback.gate_task(task, self.cfg)["errors"])

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

    def test_probe_request_invalidates_previous_feedback(self):
        task = self.gated()
        fbr_feedback.card_one(task)
        before = (task / "review/feedback.json").read_bytes()
        with mock.patch.object(reviewer, "invoke", return_value=(
                json.dumps({"audit": None, "feedback": None,
                            "needs_probes": [{"cmd": "make test", "reason": "补充证据"}]}),
                {"model": reviewer.MODEL, "reasoning_effort": reviewer.EFFORT, "provider": reviewer.PROVIDER})):
            result = reviewer.review(task, "https://review.example.test/v1", "test-review-secret")
        self.assertEqual(result["state"], "needs_probes")
        self.assertEqual((task / "review/feedback.json").read_bytes(), before)
        self.assertFalse(lib.stage_ok(task, "feedback"))
        self.assertTrue((task / "review/probe-requests.json").is_file())
        self.assertTrue(fbr_feedback.gate_task(task, self.cfg)["errors"])
