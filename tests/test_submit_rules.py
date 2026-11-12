"""Submission contract rules without credentials, network or runtime fixtures."""
from __future__ import annotations

import copy
import unittest

from fixtures import BATCH
from test_submit import SCHEMA
from test_submit import SubmitCase, FakeClient
import fbrlib as lib
import fbr_submit as submit


def plan():
    data = {key: "value" for key in submit.FIELD_MAP}
    data.update(domain="数据工程", sub_direction="其他", question_type="功能开发", difficulty="困难",
                harness="Claude Code", harness_version="2.1.319", os_platform="MacOS/Linux",
                repro_level="无外部依赖", score_delivery=4,
                env_snapshot="https://github.com/example/project/commit/" + "a" * 40,
                artifact_snapshot="https://github.com/example/project/commit/" + "b" * 40)
    return {"task_id": "s11-000001", "batch": BATCH, "data": data,
            "files": {"trace_file": {"bytes": 100}, "screencast": {"bytes": 100}}}


class Rules(unittest.TestCase):
    def test_live_contract_has_exactly_19_mapped_fields(self):
        self.assertEqual(len(submit.schema_fields(SCHEMA)), 19)
        self.assertEqual(submit.validate_plan(plan(), SCHEMA), [])

    def test_transport_enum_conversion_preserves_input(self):
        row = {"任务类型": "泛Agent需求"}
        self.assertEqual(submit.scalar_payload(row)["question_type"], "泛 Agent 需求")
        self.assertEqual(row["任务类型"], "泛Agent需求")

    def test_new_changed_and_duplicate_fields_are_blocked(self):
        for change in ("extra", "missing", "type", "duplicate"):
            schema = copy.deepcopy(SCHEMA)
            if change == "extra":
                schema["fields"].append({"field_key": "new_required", "field_type": "text"})
            elif change == "missing":
                schema["fields"].pop()
            elif change == "type":
                schema["fields"][0]["field_type"] = "textarea"
            else:
                schema["fields"].append(copy.deepcopy(schema["fields"][0]))
            with self.subTest(change=change), self.assertRaises(lib.FbrError):
                submit.schema_fields(schema)

    def test_unknown_optional_rule_is_blocked_even_when_value_empty(self):
        schema, current = copy.deepcopy(SCHEMA), plan()
        current["data"]["other_issues"] = ""
        schema["fields"][-1]["validation"] = {"new_rule": True}
        with self.assertRaises(lib.FbrError):
            submit.validate_plan(current, schema)

    def test_dependent_subdirection_and_current_select_options(self):
        current = plan()
        current["data"]["sub_direction"] = "身份认证"
        self.assertIn("子方向与所属领域不匹配", submit.validate_plan(current, SCHEMA))
        current["data"]["domain"] = "安全"
        self.assertEqual(submit.validate_plan(current, SCHEMA), [])

    def test_schema_numeric_limits_and_integer_constraint(self):
        for value in (True, "4", 0, 6, 4.5):
            current = plan()
            current["data"]["score_delivery"] = value
            with self.subTest(value=value):
                self.assertTrue(submit.validate_plan(current, SCHEMA))
        schema = copy.deepcopy(SCHEMA)
        score = next(f for f in schema["fields"] if f["field_key"] == "score_delivery")
        score["validation"]["max"] = 3
        self.assertIn("交付完整性不符合当前平台数值范围", submit.validate_plan(plan(), schema))

    def test_attachment_boundaries_and_frontend_video_fallback(self):
        schema = copy.deepcopy(SCHEMA)
        schema.pop("video_max_mb", None)
        current = plan()
        current["files"]["trace_file"]["bytes"] = 27 * 1024 * 1024
        current["files"]["screencast"]["bytes"] = 500 * 1024 * 1024
        self.assertEqual(submit.validate_plan(current, schema), [])
        for key in submit.MEDIA_TYPES:
            current["files"][key]["bytes"] += 1
        self.assertEqual(len(submit.validate_plan(current, schema)), 2)

    def test_length_counts_utf16_units(self):
        schema, current = copy.deepcopy(SCHEMA), plan()
        languages = next(f for f in schema["fields"] if f["field_key"] == "languages")
        languages["max_length"] = 3
        current["data"]["languages"] = "ab😀"
        self.assertIn("语言/框架超过平台长度限制", submit.validate_plan(current, schema))

    def test_short_commit_or_cross_repo_snapshot_is_blocked(self):
        current = plan()
        current["data"]["artifact_snapshot"] = "https://github.com/example/other/commit/" + "a" * 40
        self.assertIn("初始与产物快照必须属于同一GitHub仓库", submit.validate_plan(current, SCHEMA))
        current["data"]["artifact_snapshot"] = "https://github.com/example/project/commit/abcd123"
        self.assertIn("产物快照格式不符", submit.validate_plan(current, SCHEMA))

    def test_invalid_upload_metadata_or_untrusted_url_is_rejected(self):
        for info in (None, [], {"name": "trace.zip", "path": "/uploads/a.zip", "size": 100},
                     {"name": "trace.jsonl", "path": "http://example.invalid/a.jsonl", "size": 100}):
            with self.subTest(info=info), self.assertRaises(lib.FbrError):
                submit.validate_uploaded("trace_file", info, {"bytes": 100})
        for value in ("//elsewhere.invalid/a", "javascript:alert(1)", "/a\nsecret", "https://user:pass@example.invalid/a"):
            with self.subTest(value=value), self.assertRaises(lib.FbrError):
                submit.media_url(value)


class BindingDrift(SubmitCase):
    def test_source_change_during_upload_blocks_create(self):
        task_dir, client = self.ready(), FakeClient()
        original = client.upload

        def upload_then_change_source(path, kind="attachment", **expected):
            result = original(path, kind=kind, **expected)
            if kind == "video":
                (task_dir / "record/recording.mp4").write_bytes(b"source changed after upload")
            return result

        client.upload = upload_then_change_source
        with self.assertRaisesRegex(lib.FbrError, "原始交付记录已变化"):
            self.execute(client)
        self.assertEqual((len(client.uploads), len(client.creates)), (2, 0))
        self.assertEqual(self.receipt(task_dir)["state"], "uploaded")


if __name__ == "__main__":
    unittest.main()
