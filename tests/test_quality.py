"""反馈证据契约：真实出处、数量口径、评分类别和独立复核完整性。"""
import copy
import unittest

from test_gate import task, facts, audit, probes, TEXT5, S1_PASS
import fbr_quality as quality


class Contract(unittest.TestCase):
    def doc(self):
        return {"score": 5, "feedback": TEXT5,
                "results": [{"sentence": "--segment 0 和负数都会打印 error 并以退出码 2 结束",
                             "requirement_ids": ["R3"], "anchors": [{"type": "probe", "id": "P2"}]}],
                "score_basis": {"lowest_supported_score": 5, "lower_score_rejected": "没有真实交付缺口，不应凭空降低档位", "anchors": [{"type": "probe", "id": "P2"}], "category": "complete", "delivered": "短输入及长输入均能正确分片",
                                "remaining": "当前实跑范围内未发现交付缺口", "why_this_score": "需求和两个边界均有成功运行证据"},
                "test_claims": [{"sentence": S1_PASS.rstrip("。"), "requirement_ids": ["R1"],
                                 "behavior": "运行分片功能用例并确认全部通过",
                                 "source": {"kind": "trace", "step": 6},
                                 "output_excerpt": "Ran 14 tests in 0.52s OK", "count": 14,
                                 "count_quote": "Ran 14 tests in 0.52s OK"}]}

    def errors(self, doc, f=None):
        return quality.check_contract(doc, task(), f or facts(), audit(5), probes())

    def test_valid_evidence_and_count(self):
        self.assertEqual(self.errors(self.doc()), [])

    def test_results_cannot_use_only_requirement_or_process(self):
        for anchors in ([], [{"type": "req", "id": "R3"}], [{"type": "step", "step": 2}]):
            doc = self.doc()
            doc["results"][0]["anchors"] = anchors
            self.assertTrue(any("产物或运行证据" in e for e in self.errors(doc)))

    def test_test_source_must_match_exact_execution(self):
        doc = self.doc()
        doc["test_claims"][0]["source"]["step"] = 4
        self.assertTrue(any("摘录不在" in e for e in self.errors(doc)))
        doc = self.doc()
        doc["test_claims"][0]["requirement_ids"] = ["R404"]
        self.assertTrue(any("真实需求" in e for e in self.errors(doc)))

    def test_no_count_invention_or_boolean(self):
        for number, quote in ((15, "Ran 14 tests in 0.52s OK"), (True, "Ran 14 tests in 0.52s OK"),
                              (14, "all 14 passed")):
            doc = self.doc()
            doc["test_claims"][0].update(count=number, count_quote=quote)
            self.assertTrue(any("通过数量" in e for e in self.errors(doc)))

    def test_full_clause_not_only_command_and_bare_count_detected(self):
        doc = self.doc()
        doc["test_claims"][0]["sentence"] = "第 4 步跑 make test"
        self.assertTrue(any("完整分句" in e for e in self.errors(doc)))
        doc = self.doc()
        doc["feedback"] += "另外 35 条全部通过。"
        self.assertTrue(any("35 条全部通过" in e for e in self.errors(doc)))

    def test_score_category_cannot_conflict(self):
        for score, category in ((5, "delivery_issue"), (4, "complete"), (3, "verification_gap"),
                                (3, "process_issue"), (4, "anything")):
            doc = self.doc()
            doc["score"], doc["score_basis"]["category"] = score, category
            doc["score_basis"]["lowest_supported_score"] = score
            self.assertTrue(self.errors(doc))

    def test_absent_verification_has_evidence_without_fabricated_output(self):
        doc, f = self.doc(), facts()
        sentence = "最后一次改动后没有再跑测试"
        doc["feedback"] += sentence + "。"
        f["verification"]["verified_after_last_edit"] = False
        doc["test_claims"].append({"sentence": sentence, "requirement_ids": ["R1"],
            "behavior": "末次修改之后缺少分片功能的复测", "source": {"kind": "verification"},
            "output_excerpt": '"verified_after_last_edit": false', "count": None, "count_quote": ""})
        self.assertEqual(self.errors(doc, f), [])
        doc["test_claims"][0]["source"] = {"kind": "verification"}
        self.assertTrue(any("仅用于未执行" in e for e in self.errors(doc, f)))

    def test_semantic_check_requires_every_item_and_specific_reasons(self):
        report = {"checks": [{"id": name, "passed": True, "reason": "当前题面行为有对应运行证据"}
                             for name in quality.CHECKS]}
        self.assertEqual(quality.report_errors(report), [])
        for change in ("missing", "duplicate", "short", "reject", "bad_bool"):
            bad = copy.deepcopy(report)
            if change == "missing":
                bad["checks"].pop()
            elif change == "duplicate":
                bad["checks"][-1] = bad["checks"][0]
            else:
                bad["checks"][0].update({"short": {"reason": "符合要求"},
                                         "reject": {"passed": False}, "bad_bool": {"passed": "true"}}[change])
            self.assertTrue(quality.report_errors(bad))


class ClaimReview(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = Path(self.temp.name)
        (self.task / 'trace/projects').mkdir(parents=True)
        self.source = self.task / 'trace/projects/session.jsonl'
        self.source.write_text('first: command failed\nlater: fixed and passed\n')
        self.doc = {'feedback': '第一次启动失败，修正后成功。页面现在可以打开。'}
        self.report = {'claim_reviews': [
            {'sentence': sentence, 'assertions': ['启动结果已经修正'], 'verdict': 'supported',
             'reason': '原始返回同时记录了失败及修复成功', 'later_check': '已回查后续修复并确认最终成功',
             'sources': [{'path': 'trace/projects/session.jsonl', 'excerpt': 'later: fixed and passed'}]}
            for sentence in quality.public_units(self.doc['feedback'])]}

    def errors(self, report=None):
        return quality.claim_report_errors(report or self.report, self.doc, self.task)

    def test_all_public_sentences_require_independent_review(self):
        self.assertEqual(self.errors(), [])
        self.report['claim_reviews'].pop()
        self.assertTrue(self.errors())

    def test_no_acceptance_of_contradiction_or_uncertainty(self):
        for verdict in ('contradicted', 'insufficient', True):
            report = copy.deepcopy(self.report)
            report['claim_reviews'][0]['verdict'] = verdict
            self.assertTrue(self.errors(report))

    def test_actual_original_quote_not_generated_analysis(self):
        for path, quote in [('trace/projects/session.jsonl', 'invented success'),
                            ('evidence/facts.json', 'later: fixed and passed'),
                            ('../outside.jsonl', 'later: fixed and passed')]:
            report = copy.deepcopy(self.report)
            report['claim_reviews'][0]['sources'] = [{'path': path, 'excerpt': quote}]
            self.assertTrue(self.errors(report))

    def test_original_quote_and_later_state_are_required(self):
        for key, value in [('later_check', ''), ('assertions', []), ('sources', []),
                           ('sentence', '第一次启动失败')]:
            report = copy.deepcopy(self.report)
            report['claim_reviews'][0][key] = value
            self.assertTrue(self.errors(report))

    def test_file_names_and_decimal_not_split(self):
        self.assertEqual(quality.public_units('a.py 返回 2.5；页面失败。'), ['a.py 返回 2.5', '页面失败'])

    def test_changed_original_invalidates_quote(self):
        self.source.write_text('first: command failed\n')
        self.assertTrue(self.errors())

    def test_process_sentence_requires_trace_source(self):
        self.doc['process'] = [{
            'sentence': self.doc['feedback'].split('。')[0], 'steps': [1],
            'evidence': [{'kind': 'tool', 'step': 1, 'field': 'output',
                          'excerpt': 'command failed'}],
        }]
        self.report['claim_reviews'][0]['sources'] = [
            {'path': 'evidence/probes.jsonl', 'excerpt': 'first: command failed'}
        ]
        self.assertTrue(any('原始轨迹 JSONL' in error for error in self.errors()))
        trace_source = {'path': 'trace/projects/session.jsonl', 'excerpt': 'later: fixed and passed'}
        self.report['claim_reviews'][0]['sources'] = [trace_source]
        self.assertTrue(any('evidence 对应的原始轨迹摘录' in error for error in self.errors()))
        trace_source['excerpt'] = 'command failed'
        self.assertNotIn('evidence 对应的原始轨迹摘录', '；'.join(self.errors()))


class ProcessAudit(unittest.TestCase):
    def facts(self, *, browser=False):
        steps = [{"step": 1, "tool": "Bash", "target": "npm run typecheck"},
                 {"step": 2, "tool": "Bash", "target": "curl http://127.0.0.1/api/health"}]
        if browser:
            steps.append({"step": 3, "tool": "Playwright", "target": "page.click('权限编排')"})
        return {"trace": {"steps": steps}}

    def doc(self, sentence="模型先运行 typecheck 检查"):
        return {
            "process": [{"sentence": sentence, "steps": [1],
                         "evidence": [{"kind": "tool", "step": 1, "field": "target",
                                       "excerpt": "npm run typecheck"}]}],
            "process_audit": [{
                "sentence": sentence, "subject": "model", "verdict": "supported",
                "checks": {name: True for name in quality.PROCESS_AUDIT_CHECKS},
                "note": "已回查完整轨迹、基线、时间线和后续最终状态",
            }],
        }

    def test_valid_process_attribution(self):
        self.assertEqual(quality.process_audit_errors(self.doc(), self.facts()), [])

    def test_external_probe_cannot_be_model_action(self):
        doc = self.doc("模型修正后两个边界 probe 都通过")
        doc["process_audit"][0]["sentence"] = doc["process"][0]["sentence"]
        self.assertTrue(any("独立 probe" in error for error in
                            quality.process_audit_errors(doc, self.facts())))

    def test_page_action_requires_browser_trace(self):
        doc = self.doc("模型在页面操作里点击权限编排按钮")
        doc["process_audit"][0]["sentence"] = doc["process"][0]["sentence"]
        self.assertTrue(any("浏览器动作" in error for error in
                            quality.process_audit_errors(doc, self.facts())))
        self.assertEqual(quality.process_audit_errors(doc, self.facts(browser=True)), [])

    def test_audit_must_cover_every_process_sentence(self):
        doc = self.doc()
        doc["process_audit"] = []
        self.assertTrue(any("按顺序覆盖" in error for error in
                            quality.process_audit_errors(doc, self.facts())))

    def test_each_process_step_needs_matching_tool_evidence(self):
        doc = self.doc("模型运行 typecheck 检查")
        doc["process_audit"][0]["sentence"] = doc["process"][0]["sentence"]
        self.assertEqual(quality.process_audit_errors(doc, self.facts()), [])
        doc["process"][0].pop("evidence")
        self.assertTrue(any("缺少逐句轨迹证据" in error for error in
                            quality.process_audit_errors(doc, self.facts())))
        doc = self.doc("模型运行 typecheck 检查")
        doc["process_audit"][0]["sentence"] = doc["process"][0]["sentence"]
        doc["process"][0]["evidence"][0]["excerpt"] = "make test"
        self.assertTrue(any("摘录不在第 1 步" in error for error in
                            quality.process_audit_errors(doc, self.facts())))
