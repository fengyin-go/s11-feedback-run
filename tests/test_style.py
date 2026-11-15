"""Public Feedback language policy: jargon, density, and review receipts."""
from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import fbrlib as lib
import fbr_style


class LanguagePolicy(unittest.TestCase):
    def test_company_jargon_and_empty_completion_are_blocked(self):
        report = fbr_style.review_text(
            "水位提交和主链路均已落地，未发现其他明确未完成功能。")
        self.assertEqual(report["state"], "failed")
        messages = "\n".join(row["message"] for row in report["errors"])
        self.assertIn("水位", messages)
        self.assertIn("主链路", messages)
        self.assertIn("完成了什么", messages)
        self.assertTrue(any("上次处理位置" in row["suggestion"] for row in report["errors"]))

    def test_concrete_rewrite_passes(self):
        text = ("提交后能看到这次同步处理到了哪条记录；重置时旧记录仍然保留，"
                "不同来源的任务和历史不会混在一起。")
        self.assertEqual(fbr_style.review_text(text)["state"], "passed")

    def test_contextual_terms_are_density_checks_not_blanket_bans(self):
        acceptable = "筛选条件变化后，页面保留旧记录并显示新的处理位置。"
        self.assertEqual(fbr_style.review_text(acceptable)["state"], "passed")
        piled = "能力、方案、策略、机制、流程、维度、层面的处理已经完成。"
        self.assertEqual(fbr_style.review_text(piled)["state"], "failed")
        self.assertTrue(any("抽象词堆在一起" in row["message"]
                            for row in fbr_style.review_text(piled)["errors"]))

    def test_machine_report_density_is_blocked(self):
        text = ("ABC-0001 在 09:00 处理 2400，ABC-0002 在 09:05 处理 2500，"
                "ABC-0003 在 09:10 处理 2600，ABC-0004 返回 422。")
        self.assertTrue(any(row["code"] == "dense_report"
                            for row in fbr_style.review_text(text)["errors"]))
        self.assertTrue(fbr_style.error_messages("16 项后端测试通过。"))

    def test_language_review_requires_exact_criterion_and_reasons(self):
        text = "长素材仍然打不了包。模型检查后把限制写进 packer.py。"
        errors = fbr_style.language_review_errors(text, {
            "reviewed": True,
            "criterion_sentence": "长素材仍然打不了包",
            "translated_terms": [],
            "retained_terms": [],
        })
        self.assertEqual(errors, [])
        wrong = fbr_style.language_review_errors(text, {
            "reviewed": True,
            "criterion_sentence": "短素材可以打包",
            "translated_terms": [],
            "retained_terms": [{"term": "packer.py", "reason": "短"}],
        })
        self.assertTrue(any("逐字出现" in item for item in wrong))
        self.assertTrue(any("保留理由" in item for item in wrong))

    def test_report_binds_feedback_and_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            (task / "review").mkdir()
            text = "超过一小时的输入仍会报错，页面没有给出可读提示。"
            review = {
                "reviewed": True,
                "criterion_sentence": "超过一小时的输入仍会报错",
                "translated_terms": [],
                "retained_terms": [],
                "note": "使用用户看到的输入条件和报错结果",
            }
            lib.save_json(task / "review/feedback.json", {"feedback": text, "score": 3})
            report = fbr_style.write_report(task, text, 3, review, require_review=True)
            self.assertEqual(report["state"], "passed")
            self.assertEqual(fbr_style.provenance_errors(task), [])
            lib.save_json(task / "review/feedback.json", {"feedback": text + "再补一句。", "score": 3})
            self.assertTrue(fbr_style.provenance_errors(task))


if __name__ == "__main__":
    unittest.main()
