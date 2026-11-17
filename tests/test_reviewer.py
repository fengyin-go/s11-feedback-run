"""独立验收的隔离、错误处理与证据绑定；不请求真实网关。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from flow import FlowCase, audit, feedback, mock_claim_reviews, run_main
import fbr_feedback
import fbr_delivery
import fbr_reviewer as reviewer
import fbrlib as lib
import fbr_quality as quality
import fbr_work


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

    def semantic(self, task=None):
        return json.dumps({"claim_reviews": mock_claim_reviews(feedback(), task_dir=task), "checks": [{"id": name, "passed": True,
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
        report = json.loads(self.semantic(task))
        text = str((candidate.get("feedback") or {}).get("feedback", ""))
        criterion = next((item for item in ("超过一小时的输入没有做", "长时长这一项落空了")
                          if item in text), "")
        report.update(task_agent='current_task_agent', candidate_sha=lib.content_hash(candidate),
                      binding=lib.load_json(task/'review/task-agent-work.json')['binding'],
                      language_review={
                          "reviewed": True,
                          "criterion_sentence": criterion,
                          "translated_terms": [],
                          "retained_terms": [],
                          "note": "正文使用用户可观察的切分、报错和缺失文档结果",
                      })
        return report

    def test_review_only_analyzes_traces_then_returns_checkpoint(self):
        task = self.agent_prepared()
        import fbr_trace_review
        with mock.patch.object(fbr_trace_review, 'analyze', return_value=lib.load_json(task/'review/trace-analysis.json')) as analysis, \
             mock.patch.object(reviewer, 'invoke', side_effect=AssertionError('product model must never be called')):
            result = reviewer.review(task, 'https://review.example.test/v1', 'test-review-secret')
        self.assertEqual(result['state'], 'needs_task_agent')
        analysis.assert_called_once()

    def test_platform_repair_checkpoint_rejects_prompt_changes(self):
        task = self.agent_prepared()
        original = lib.load_json(task / 'task.json')['prompt']
        lib.save_json(task / 'review/upload.json', {
            'submission_id': 1,
            'binding': {'data': {'user_prompt': original}},
        })
        changed = original + '\n追加要求后重跑'
        record = lib.load_json(task / 'task.json')
        record['prompt'] = changed
        lib.save_json(task / 'task.json', record)
        (task / 'prompt.txt').write_text(changed, encoding='utf-8')
        with self.assertRaisesRegex(lib.FbrError, '绝对禁止修改提示词'):
            reviewer.prepare_task_agent(task, allow_platform_repair=True)

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

    def test_lint_catches_requirement_gaps_and_platform_style_before_finalize(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        self.assertEqual(reviewer.lint_candidate(task, candidate)['state'], 'ok')
        broken = json.loads(json.dumps(candidate))
        broken['feedback'].pop('requirements_status')
        result = reviewer.lint_candidate(task, broken)
        self.assertEqual(result['state'], 'repairing')
        self.assertTrue(any('requirements_status' in error for error in result['errors']))
        styled = json.loads(json.dumps(candidate))
        styled['feedback']['feedback'] += '16 项后端测试通过。'
        errors = reviewer.lint_candidate(task, styled)['errors']
        self.assertTrue(any('测试总数' in error for error in errors))
        styled = json.loads(json.dumps(candidate))
        styled['feedback']['feedback'] += '主链路和水位均已落地。'
        errors = reviewer.lint_candidate(task, styled)['errors']
        self.assertTrue(any('企业黑话' in error for error in errors))
        styled = json.loads(json.dumps(candidate))
        verification = self.verification(task, styled)
        verification.pop('language_review')
        errors = reviewer.lint_candidate(task, styled, verification)['errors']
        self.assertTrue(any('缺少 language_review' in error for error in errors))

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
        style_report = lib.load_json(task/'review/style-report.json')
        self.assertEqual(style_report['state'], 'passed')
        quality = lib.load_json(task/'review/quality.json')
        self.assertEqual(quality['language_policy_version'], 1)
        self.assertEqual(quality['language_review']['criterion_sentence'],
                         '超过一小时的输入没有做')
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

    def test_identical_failed_attempt_is_content_addressed_and_not_duplicated(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        verification['checks'][0]['passed'] = False
        verification['checks'][0]['reason'] = '本句缺少原始轨迹中的直接支持'
        first = reviewer.finalize(task, candidate, verification)
        with mock.patch.object(reviewer.lib, 'now_iso', return_value='2099-01-01T00:00:00+0800'):
            second = reviewer.finalize(task, candidate, verification)
        self.assertEqual(first['state'], 'repairing')
        self.assertEqual(second['state'], 'repairing')
        self.assertTrue(second['idempotent_reuse'])
        self.assertIn('不要重复 finalize', second['detail'])
        history = lib.load_json(task/'review/task-agent-history.json')
        self.assertEqual(len(history['attempts']), 1)
        attempt = history['attempts'][0]
        self.assertEqual(attempt['reuse_count'], 1)
        self.assertTrue((task/attempt['candidate_ref']).is_file())
        self.assertTrue((task/attempt['verification_ref']).is_file())
        self.assertNotIn('candidate', attempt)
        self.assertNotIn('verification', attempt)

    def test_accepted_finalize_is_idempotent_and_does_not_republish(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        first = reviewer.finalize(task, candidate, verification, defer_delivery=True)
        with mock.patch('fbr_sheet.write_sheet') as sheet, \
             mock.patch('fbr_feedback.gate_batch',
                        side_effect=AssertionError('idempotent finalize must not re-gate')):
            second = reviewer.finalize(task, candidate, verification, defer_delivery=True)
        self.assertEqual(first['state'], 'ok')
        self.assertEqual(second['state'], 'ok')
        self.assertTrue(second['idempotent_reuse'])
        sheet.assert_not_called()
        history = lib.load_json(task/'review/task-agent-history.json')
        self.assertEqual(len(history['attempts']), 1)
        self.assertEqual(len(history['accepted_versions']), 1)
        self.assertEqual(history['attempts'][0]['reuse_count'], 1)
        self.assertEqual(history['attempts'][0]['result_state'], 'ok')

    def test_spotcheck_rejection_invalidates_accepted_idempotency(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        reviewer.finalize(task, candidate, verification, defer_delivery=True)
        feedback_sha = lib.load_state(task)['stages']['feedback']['feedback_sha']
        lib.save_json(task/'review/spotcheck.json', {
            'task_id': task.name, 'state': '退回', 'feedback_sha': feedback_sha,
            'note': '正文第 2 句需要重查', 'by': '复核人',
        })
        lib.set_stage(task, 'feedback', 'failed', problems=['抽检退回'])
        second = reviewer.finalize(task, candidate, verification, defer_delivery=True)
        self.assertEqual(second['state'], 'repairing')
        self.assertFalse(second.get('idempotent_reuse'))

    def test_prompt_file_change_invalidates_deferred_idempotency(self):
        task = self.agent_prepared()
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        reviewer.finalize(task, candidate, verification, defer_delivery=True)
        prompt = task / 'prompt.txt'
        prompt.write_text(prompt.read_text(encoding='utf-8') + '\nchanged', encoding='utf-8')
        with self.assertRaisesRegex(lib.FbrError, 'prompt.txt'):
            reviewer.finalize(task, candidate, verification, defer_delivery=True)

    def test_legacy_array_history_is_normalized_before_idempotent_append(self):
        task = self.agent_prepared()
        lib.save_json(task/'review/task-agent-history.json', [{
            'at': '2026-11-01T00:00:00+0800', 'attempt_sha': 'legacy',
            'candidate_sha': 'old', 'verification_sha': 'old', 'errors': [],
        }])
        candidate = json.loads(self.candidate()[0])
        verification = self.verification(task, candidate)
        verification['checks'][0]['passed'] = False
        result = reviewer.finalize(task, candidate, verification)
        self.assertEqual(result['state'], 'repairing')
        history = lib.load_json(task/'review/task-agent-history.json')
        self.assertIsInstance(history, dict)
        self.assertEqual(history['version'], 2)
        self.assertEqual(len(history['attempts']), 2)
        self.assertEqual(history['attempts'][0]['attempt_sha'], 'legacy')
        self.assertIn('attempt_key', history['attempts'][1])

    def test_success_output_is_one_line_without_generated_paths(self):
        task = self.agent_prepared()
        text = reviewer.compact_result(task, 'build', {
            'task_id': task.name, 'state': 'ok', 'detail': 'Feedback 候选本地预检通过',
            'candidate': '/tmp/candidate.json', 'verification': '/tmp/verification.json',
        })
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn('Feedback 候选本地预检通过', text)
        self.assertNotIn('/tmp/candidate.json', text)

    def test_work_queue_returns_one_compact_packet_not_full_materials(self):
        task = self.agent_prepared()
        rows = fbr_work.pending(self.root, task.parent.name)
        self.assertEqual([row['task_id'] for row in rows], [task.name])
        packet = rows[0]
        self.assertTrue(Path(packet['brief']).is_file())
        self.assertTrue(Path(packet['trace_pack']).is_file())
        self.assertTrue(Path(packet['candidate_schema']).is_file())
        self.assertNotIsInstance(lib.load_json(Path(packet['checkpoint'])).get('candidate_schema'), dict)
        self.assertNotIn('card', packet)
        self.assertNotIn('facts', packet)

    def test_compact_decision_expands_schema_and_resolves_unique_locator(self):
        task = self.agent_prepared()
        doc = feedback()
        audit_doc = audit()
        status = {row["requirement_id"]: row for row in doc["requirements_status"]}
        claims = mock_claim_reviews(doc, task_dir=task)
        process_sentence = doc["process"][0]["sentence"]
        process_source = next(row for row in claims if row["sentence"] == process_sentence)["sources"][0]
        for key in ("start_line", "end_line", "block"):
            process_source.pop(key)
        decision = {
            "requirements": [
                {**row, "user_text": status[row["id"]]["user_text"],
                 "anchors": status[row["id"]]["anchors"]}
                for row in audit_doc["requirements"]
            ],
            "counterexamples": audit_doc["counterexamples"],
            "false_success": audit_doc["false_success"],
            "false_success_note": audit_doc["false_success_note"],
            "false_success_anchors": audit_doc["false_success_anchors"],
            "score": doc["score"], "feedback": doc["feedback"],
            "results": doc["results"], "issues": doc["issues"],
            "test_claims": doc["test_claims"], "score_basis": doc["score_basis"],
            "process": [{**row, "audit": doc["process_audit"][index]}
                        for index, row in enumerate(doc["process"])],
            "claims": claims,
            "language_review": {
                "reviewed": True,
                "criterion_sentence": "超过一小时的输入没有做",
                "translated_terms": [],
                "retained_terms": [],
                "note": "正文写清了长输入失败、短输入切分和缺失文档",
            },
            "quality_checks": {name: "已按当前原始证据和最终产物核对该项"
                               for name in quality.CHECKS},
            "note": "", "needs_probes": [],
        }
        candidate, verification = fbr_feedback.expand_decision(task, decision)
        self.assertEqual(candidate["feedback"]["requirements_status"],
                         doc["requirements_status"])
        self.assertEqual(verification["candidate_sha"], lib.content_hash(candidate))
        self.assertEqual(len(verification["claim_reviews"]),
                         len(quality.public_units(doc["feedback"])))
        expanded_source = next(row for row in verification["claim_reviews"]
                               if row["sentence"] == process_sentence)["sources"][0]
        self.assertNotIn("rebound_from", expanded_source)
        self.assertIn("start_line", expanded_source)
        self.assertEqual(verification["language_review"]["criterion_sentence"],
                         "超过一小时的输入没有做")
        self.assertEqual(reviewer.lint_candidate(task, candidate, verification)["state"], "ok")

    def test_process_evidence_automatically_merges_into_matching_claim(self):
        task = self.agent_prepared()
        doc = feedback()
        claims = mock_claim_reviews(doc, task_dir=task)
        process = [doc["process"][0]]
        sentence = process[0]["sentence"]
        claim = next(row for row in claims if row["sentence"] == sentence)
        claim["sources"] = []

        merged, derived = fbr_feedback._merge_process_evidence_into_claims(
            task, claims, process)

        source = next(row for row in merged if row["sentence"] == sentence)["sources"][0]
        self.assertTrue(source["path"].startswith("trace/projects/"))
        self.assertIn("start_line", source)
        self.assertTrue(derived)

    def test_partial_process_sentence_expands_only_when_unique(self):
        text = "模型先运行构建并发现类型错误。它随后修正类型并重新构建。"
        self.assertEqual(
            fbr_feedback._canonical_sentence(text, "模型先运行构建"),
            "模型先运行构建并发现类型错误",
        )
        self.assertEqual(
            fbr_feedback._canonical_sentence("模型先构建。模型先构建并验证。", "模型先构建"),
            "模型先构建",
        )

    def test_repair_actions_name_the_structured_patch_location(self):
        actions = reviewer.repair_actions([
            "第 1 句复查没有逐条引用过程句 evidence 对应的原始轨迹摘录",
            "第 2 句复查摘录不在原始文件中",
        ])
        self.assertEqual(actions[0]["path"], "process[].evidence")
        self.assertEqual(actions[1]["path"], "claims[].sources")

    def test_compact_build_derives_internal_status_and_score_fields(self):
        task = self.agent_prepared()
        doc = feedback()
        decision = {
            "requirements": [
                {**row, "user_text": ""}
                for row in audit()["requirements"]
            ],
            "counterexamples": [],
            "false_success": "无",
            "score": doc["score"],
            "feedback": doc["feedback"],
            "results": doc["results"],
            "issues": doc["issues"],
            "test_claims": doc["test_claims"],
            "process": [],
            "claims": [],
            "score_basis": {
                "category": "process_issue",
                "delivered": "",
                "remaining": "",
                "why_this_score": "",
                "lowest_supported_score": None,
                "lower_score_rejected": "",
                "anchors": [],
            },
            "language_review": {
                "reviewed": True,
                "criterion_sentence": "超过一小时的输入没有做",
                "translated_terms": [],
                "retained_terms": [],
                "note": "fixture",
            },
            "quality_checks": {},
            "needs_probes": [],
        }

        candidate, verification = fbr_feedback.expand_decision(task, decision)
        basis = candidate["feedback"]["score_basis"]
        self.assertTrue(all(candidate["feedback"]["requirements_status"]))
        self.assertTrue(all(row["user_text"] for row in candidate["feedback"]["requirements_status"]))
        self.assertTrue(basis["delivered"])
        self.assertTrue(basis["remaining"])
        self.assertTrue(basis["why_this_score"])
        self.assertEqual(basis["lowest_supported_score"], doc["score"])
        self.assertTrue(basis["lower_score_rejected"])
        self.assertTrue(basis["anchors"])
        self.assertTrue(verification["build_derivations"]["fields"])

    def test_work_wait_and_closeout_use_files_not_conversation_history(self):
        task = self.agent_prepared()
        flow = lib.batch_dir(self.root, task.parent.name)/'flow'
        lib.save_json(flow/'waiting.json', {'tasks': [{
            'task_id': task.name, 'checkpoint': str(task/'review/task-agent-work.json')}]})
        now = [0.0]
        result = fbr_work.wait_for_event(
            self.root, task.parent.name, {'needs_task_agent'}, timeout=5,
            clock=lambda: now[0], sleeper=lambda value: now.__setitem__(0, now[0]+value))
        self.assertEqual(result['state'], 'needs_task_agent')
        close = fbr_work.closeout(self.root, task.parent.name, task.name)
        saved = lib.load_json(task/'review/task-closeout.json')
        self.assertEqual(close['task_id'], task.name)
        self.assertEqual(saved['version'], 1)
        self.assertTrue(Path(saved['path']).is_file())
        self.assertTrue(Path(fbr_work.stage_brief(task)).is_file())
        code, stdout, stderr = run_main(
            fbr_work, '--root', str(self.root), 'closeout',
            '--batch', task.parent.name, '--task', task.name,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertIn(f"{task.name} closed gate=", stdout)
        code, stdout, _ = run_main(
            fbr_work, '--root', str(self.root), 'closeout',
            '--batch', task.parent.name, '--task', task.name, '--json',
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["task_id"], task.name)
