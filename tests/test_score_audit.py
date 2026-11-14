"""4 -> 3 evidence audit: only reproduced result/process failures can downgrade."""

from flow import FlowCase, TEXT_A, feedback
import fbrlib as lib
import fbr_score_audit as audit
import fbr_reviewer as reviewer


class DowngradeEvidence(FlowCase):
    def prepare_task(self):
        task = self.written()
        self.put(task, feedback_doc=feedback(TEXT_A, score=4))
        return task

    def probes(self, task, artifact_rc=1):
        lib.save_json(task / "review/downgrade-review.json", {
            "from_score": 4,
            "proposed_score": 3,
            "downgrade_class": "compatibility_regression",
            "requirement_ids": ["R2"],
            "baseline_probe": "D1",
            "artifact_probe": "P3",
            "expected": "同一条确认动作只生成一条记录",
            "actual": "产物重复生成记录",
            "user_impact": "用户会得到重复业务数据",
            "workaround": "无",
            "anchors": [{"type": "file", "path": "src/clipcut/packer.py"}],
        })
        lib.append_jsonl(task / "evidence/downgrade-probes.jsonl",
                         {"id": "D1", "side": "baseline", "setup_rc": 0,
                          "rc": 0, "tail": "one receipt"})
        lib.append_jsonl(task / "evidence/probes.jsonl",
                         {"id": "P3", "side": "artifact", "setup_rc": 0,
                          "rc": artifact_rc, "tail": "two receipts"})
        return task

    def test_compatibility_regression_requires_baseline_pass_and_artifact_failure(self):
        task = self.probes(self.prepare_task())
        result = audit.validate_evidence(task, self.cfg)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["suggested_score"], 3)
        self.assertEqual(result["suggested_audit_level"], "明显")

    def test_infrastructure_failure_cannot_support_downgrade(self):
        task = self.probes(self.prepare_task(), artifact_rc=137)
        result = audit.validate_evidence(task, self.cfg)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("基础设施" in error for error in result["errors"]))
        self.assertFalse(any("audit 结论" in error for error in result["errors"]))

    def test_scan_marks_unverified_concurrency_as_candidate(self):
        task = self.prepare_task()
        doc = lib.load_json(task / "review/feedback.json")
        doc["process"][0]["sentence"] = "模型声称完成并发校验，但真实并发未实测，也没有真正验证争抢"
        lib.save_json(task / "review/feedback.json", doc)
        row = audit.scan_task(task, self.cfg)
        self.assertIn("explicit_unverified", row["classes"])

    def test_finalize_requires_current_downgrade_validation(self):
        task = self.probes(self.prepare_task())
        self.assertTrue(audit.validate_evidence(task, self.cfg)["eligible"])
        doc = lib.load_json(task / "review/feedback.json")
        doc["score"] = 3
        doc["score_basis"]["downgrade_from"] = 4
        lib.save_json(task / "review/feedback.json", doc)
        self.assertEqual(reviewer.downgrade_errors(task, doc), [])
        lib.append_jsonl(task / "evidence/probes.jsonl", {"id": "P99", "rc": 0})
        self.assertTrue(any("证据已变化" in error for error in reviewer.downgrade_errors(task, doc)))
