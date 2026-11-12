"""Offline platform submit integration: real temporary evidence, portable live schema, fake HTTP peer."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from fixtures import BATCH
from flow import TEXT_B, feedback
from test_sheet_batch import SheetCase
import fbrlib as lib
import fbr_delivery as delivery
import fbr_feedback
import fbr_sheet
import fbr_submit as submit

# Frozen from the 2026-11-11 live form response; no local path or network dependency.
SCHEMA = json.loads(r'''{
  "fingerprint": "5082edfb2f47b1cf",
  "attachment_max_mb": 27,
  "video_max_mb": 500,
  "fields": [
    {
      "field_key": "domain",
      "label": "所属领域",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "安全",
        "游戏、图形与多媒体",
        "数据工程"
      ],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "sub_direction",
      "label": "子方向",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "身份认证",
        "网络与通信安全",
        "系统安全",
        "漏洞检测与攻防",
        "其他",
        "游戏开发",
        "图形渲染",
        "音视频处理",
        "图像处理",
        "数据采集、爬虫与 RPA",
        "数仓与数据湖",
        "数据分析与数据应用"
      ],
      "required_when": {},
      "validation": {
        "options_by": {
          "field": "domain",
          "map": {
            "安全": [
              "身份认证",
              "网络与通信安全",
              "系统安全",
              "漏洞检测与攻防",
              "其他"
            ],
            "游戏、图形与多媒体": [
              "游戏开发",
              "图形渲染",
              "音视频处理",
              "图像处理",
              "其他"
            ],
            "数据工程": [
              "数据采集、爬虫与 RPA",
              "数仓与数据湖",
              "数据分析与数据应用",
              "其他"
            ]
          }
        }
      }
    },
    {
      "field_key": "question_type",
      "label": "任务类型",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "功能开发",
        "Bugfix",
        "代码重构",
        "功能优化",
        "依赖升级与迁移",
        "质量保障",
        "项目维护",
        "技术讨论",
        "问题排查与诊断",
        "部署运维",
        "泛 Agent 需求"
      ],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "difficulty",
      "label": "任务难度",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "困难",
        "地狱"
      ],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "languages",
      "label": "语言/框架",
      "field_type": "text",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 255,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "harness",
      "label": "Harness",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "Claude Code",
        "Codex CLI"
      ],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "harness_version",
      "label": "Harness 版本",
      "field_type": "text",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 64,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "os_platform",
      "label": "操作系统",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "MacOS/Linux",
        "Windows"
      ],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "repro_level",
      "label": "环境可复现等级",
      "field_type": "select",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [
        "无外部依赖",
        "有外部依赖，未容器化",
        "已容器化，可一键起环境"
      ],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "env_snapshot",
      "label": "初始环境快照",
      "field_type": "url",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 512,
      "options": [],
      "required_when": {},
      "validation": {
        "pattern": "^https://github\\.com/[^/\\s]+/[^/\\s]+/commit/[0-9a-fA-F]{40}/?$",
        "pattern_message": "必须是 40 位完整 SHA 的 commit 永久链接，拒绝短 SHA、分支名与 tag"
      }
    },
    {
      "field_key": "user_prompt",
      "label": "User Prompt",
      "field_type": "textarea",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "session_id",
      "label": "SessionID",
      "field_type": "text",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 128,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "turn_id",
      "label": "TurnID/PromptID",
      "field_type": "text",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 128,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "trace_file",
      "label": "轨迹文件",
      "field_type": "attachment",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "artifact_snapshot",
      "label": "产物快照",
      "field_type": "url",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 512,
      "options": [],
      "required_when": {},
      "validation": {
        "pattern": "^https://github\\.com/[^/\\s]+/[^/\\s]+/commit/[0-9a-fA-F]{40}/?$",
        "pattern_message": "必须是 40 位完整 SHA 的 commit 永久链接，拒绝短 SHA、分支名与 tag"
      }
    },
    {
      "field_key": "screencast",
      "label": "运行录屏",
      "field_type": "video",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 1024,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "score_delivery",
      "label": "交付完整性",
      "field_type": "number",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [],
      "required_when": {},
      "validation": {
        "min": 1,
        "max": 5,
        "integer": true
      }
    },
    {
      "field_key": "feedback",
      "label": "Feedback",
      "field_type": "textarea",
      "is_enabled": true,
      "is_required": true,
      "is_auto": false,
      "max_length": 0,
      "options": [],
      "required_when": {},
      "validation": {}
    },
    {
      "field_key": "other_issues",
      "label": "备注",
      "field_type": "textarea",
      "is_enabled": true,
      "is_required": false,
      "is_auto": false,
      "max_length": 0,
      "options": [],
      "required_when": {},
      "validation": {}
    }
  ]
}''')


class FakeClient:
    def __init__(self):
        self.current_schema = copy.deepcopy(SCHEMA)
        self.user = {"username": submit.ACCOUNT, "must_change_password": False}
        self.records, self.uploads, self.creates, self.details = [], [], [], []
        self.logins = self.schema_calls = 0
        self.fail_upload_kind = None
        self.create_mode = "ok"
        self.change_schema_after_upload = False

    def login(self):
        self.logins += 1
        return {"user": copy.deepcopy(self.user)}

    def me(self):
        return copy.deepcopy(self.user)

    def schema(self):
        self.schema_calls += 1
        row = copy.deepcopy(self.current_schema)
        if self.change_schema_after_upload and len(self.uploads) >= 2:
            row["fingerprint"] = "changed-during-upload"
        return row

    def list_submissions(self, page=1, page_size=100):
        rows = copy.deepcopy(self.records)
        return {"items": rows if page == 1 else [], "meta": {"total_pages": 1, "total": len(rows)}}

    def upload(self, path, kind="attachment", **expected):
        path = Path(path)
        self.uploads.append((kind, path.name))
        if "expected_size" in expected and expected["expected_size"] != path.stat().st_size:
            raise lib.FbrError("size mismatch")
        if "expected_sha256" in expected and expected["expected_sha256"] != delivery.digest(path):
            raise lib.FbrError("digest mismatch")
        if self.fail_upload_kind == kind:
            self.fail_upload_kind = None
            raise lib.FbrError("upload disconnected")
        if kind == "attachment":
            return {"name": path.name, "path": "/uploads/traces/" + path.name, "size": path.stat().st_size}
        return {"name": path.name, "url": "/uploads/videos/" + path.name}

    def create(self, data, fingerprint):
        self.creates.append({"data": copy.deepcopy(data), "schema_fingerprint": fingerprint})
        if self.create_mode == "timeout_before_create":
            self.create_mode = "ok"
            raise lib.FbrError("POST timeout, result unknown")
        row = {"id": len(self.records) + 1, "status": "pending", "created_at": "2026-11-11T07:08:09.123456Z",
               "data": copy.deepcopy(data)}
        self.records.append(row)
        if self.create_mode == "timeout_after_create":
            self.create_mode = "ok"
            raise lib.FbrError("POST timeout, result unknown")
        return copy.deepcopy(row)

    def detail(self, identifier):
        self.details.append(identifier)
        rows = [r for r in self.records if str(r["id"]) == str(identifier)]
        if len(rows) != 1:
            raise lib.FbrError("record unavailable")
        return copy.deepcopy(rows[0])


class SubmitCase(SheetCase):
    def ready(self):
        task_dir = self.gated()
        self.complete(task_dir)
        ids = lib.load_json(task_dir / "trace/ids.json")
        ids.update(user_turns=1, cli_version="2.1.319")
        lib.save_json(task_dir / "trace/ids.json", ids)
        self.refresh_source_fixture(task_dir)
        self.review_receipt(task_dir)
        self.cfg["submitter"] = ""
        lib.save_json(self.root / "_config/settings.json", self.cfg)
        return task_dir

    def run_submit(self, client, **kwargs):
        return submit.run_batch(self.root, BATCH, client=client, **kwargs)

    def execute(self, client):
        return self.run_submit(client, execute=True, confirm="SUBMIT")

    def receipt(self, task_dir):
        return lib.load_json(task_dir / "review/upload.json")


class Submission(SubmitCase):
    def test_current_schema_contains_twelve_subdirections_and_legacy_five_still_validate(self):
        self.ready()
        direction = next(f for f in SCHEMA["fields"] if f["field_key"] == "sub_direction")
        self.assertEqual(len(direction["options"]), 12)
        self.assertEqual(direction["options"], fbr_sheet.OPTIONS["子方向"])
        plan = submit.prepare(self.root, BATCH, SCHEMA)[0]
        for domain, value in (("安全", "漏洞检测与攻防"), ("游戏、图形与多媒体", "音视频处理"),
                              ("游戏、图形与多媒体", "图形渲染"), ("数据工程", "数据采集、爬虫与 RPA"),
                              ("数据工程", "其他")):
            with self.subTest(domain=domain, subdirection=value):
                older = copy.deepcopy(plan)
                older["data"].update(domain=domain, sub_direction=value)
                self.assertEqual(submit.validate_plan(older, SCHEMA), [])

    def test_default_dry_run_never_uploads_or_creates(self):
        task_dir, client = self.ready(), FakeClient()
        out = self.run_submit(client, limit=1)
        self.assertFalse(out["execute"])
        self.assertEqual(out["tasks"][0]["state"], "dry")
        self.assertEqual((client.uploads, client.creates), ([], []))
        self.assertFalse((task_dir / "review/upload.json").exists())

    def test_execute_requires_confirmation_before_login(self):
        self.ready()
        client = FakeClient()
        with self.assertRaisesRegex(lib.FbrError, "confirm SUBMIT"):
            self.run_submit(client, execute=True)
        self.assertEqual((client.logins, client.uploads, client.creates), (0, [], []))

    def test_one_submission_uploads_twice_creates_once_then_reads_back(self):
        task_dir, client = self.ready(), FakeClient()
        out = self.execute(client)
        self.assertEqual(out["tasks"][0]["state"], "confirmed")
        self.assertEqual((len(client.uploads), len(client.creates), len(client.details)), (2, 1, 1))
        self.assertEqual(set(client.creates[0]["data"]), set(submit.FIELD_MAP) | set(submit.MEDIA_TYPES))
        self.assertEqual(self.receipt(task_dir)["state"], "confirmed")
        row, _ = self.row(task_dir)
        self.assertTrue(row["上传状态"].startswith("已提交"))
        self.assertEqual(row["提交人"], submit.ACCOUNT)
        self.assertEqual(row["提交时间"], "2026-11-11 07:08")
        self.assertTrue(row["轨迹文件"].startswith(submit.BASE_URL + "/uploads/"))
        self.assertTrue(row["运行录屏"].startswith(submit.BASE_URL + "/uploads/"))

    def test_confirmed_receipt_prevents_duplicate_submission(self):
        self.ready()
        client = FakeClient()
        self.execute(client)
        self.execute(client)
        self.assertEqual((len(client.uploads), len(client.creates), len(client.records)), (2, 1, 1))
        self.assertEqual(len(client.details), 2)

    def test_post_timeout_after_create_recovers_without_resending(self):
        task_dir, client = self.ready(), FakeClient()
        client.create_mode = "timeout_after_create"
        with self.assertRaisesRegex(lib.FbrError, "POST timeout"):
            self.execute(client)
        self.assertEqual(self.receipt(task_dir)["state"], "uncertain")
        self.execute(client)
        self.assertEqual((len(client.uploads), len(client.creates), len(client.records)), (2, 1, 1))
        self.assertEqual(self.receipt(task_dir)["state"], "confirmed")

    def test_post_timeout_without_visible_record_refuses_resending(self):
        task_dir, client = self.ready(), FakeClient()
        client.create_mode = "timeout_before_create"
        with self.assertRaisesRegex(lib.FbrError, "POST timeout"):
            self.execute(client)
        with self.assertRaisesRegex(lib.FbrError, "重复建单"):
            self.execute(client)
        self.assertEqual((len(client.uploads), len(client.creates), len(client.records)), (2, 1, 0))
        self.assertEqual(self.receipt(task_dir)["state"], "uncertain")

    def test_upload_failure_retries_only_missing_attachment(self):
        task_dir, client = self.ready(), FakeClient()
        client.fail_upload_kind = "video"
        with self.assertRaisesRegex(lib.FbrError, "upload disconnected"):
            self.execute(client)
        self.assertEqual(set(self.receipt(task_dir)["uploads"]), {"trace_file"})
        self.execute(client)
        self.assertEqual([kind for kind, _ in client.uploads], ["attachment", "video", "video"])
        self.assertEqual(len(client.creates), 1)

    def test_schema_change_blocks_create_after_upload(self):
        task_dir, client = self.ready(), FakeClient()
        client.change_schema_after_upload = True
        with self.assertRaisesRegex(lib.FbrError, "平台表单已变化"):
            self.execute(client)
        self.assertEqual((len(client.uploads), len(client.creates)), (2, 0))
        self.assertEqual(self.receipt(task_dir)["state"], "uploaded")

    def test_unknown_schema_or_locked_domain_blocks_all_uploads(self):
        self.ready()
        for reason in ("schema", "domain"):
            with self.subTest(reason=reason):
                client = FakeClient()
                if reason == "schema":
                    client.current_schema["fields"].pop()
                else:
                    client.user["locked_domain"] = "安全"
                with self.assertRaises(lib.FbrError):
                    self.execute(client)
                self.assertEqual((client.uploads, client.creates), ([], []))

    def test_same_session_without_bound_receipt_refuses_duplicates(self):
        task_dir, client = self.ready(), FakeClient()
        ids = lib.load_json(task_dir / "trace/ids.json")
        client.records = [{"id": 99, "data": {"session_id": ids["session_id"], "domain": "游戏、图形与多媒体"}}]
        with self.assertRaisesRegex(lib.FbrError, "SessionID已存在"):
            self.execute(client)
        self.assertEqual((client.uploads, client.creates), ([], []))


class ReceiptValidity(SubmitCase):
    def submitted(self):
        task_dir, client = self.ready(), FakeClient()
        self.execute(client)
        return task_dir, client

    def test_new_feedback_invalidates_receipt_and_does_not_create_again(self):
        task_dir, client = self.submitted()
        self.put(task_dir, feedback_doc=feedback(TEXT_B))
        self.assertEqual(fbr_feedback.gate_batch(self.root, BATCH, self.cfg)[0][0]["state"], "ok")
        row, _ = self.row(task_dir)
        self.assertEqual(row["上传状态"], "回执已失效")
        with self.assertRaises(lib.FbrError):
            self.execute(client)
        self.assertEqual(len(client.creates), 1)

    def test_source_attachment_change_invalidates_receipt_before_reexport(self):
        task_dir, _ = self.submitted()
        (task_dir / "record/recording.mp4").write_bytes(b"replaced source media")
        row, reason = self.row(task_dir)
        self.assertIsNone(row)
        self.assertIn("证据变化", reason)

    def test_reexported_attachment_change_invalidates_receipt_without_creating_again(self):
        task_dir, client = self.submitted()
        (task_dir / "record/recording.mp4").write_bytes(b"replaced source media")
        delivery.export_batch(self.root, BATCH, self.cfg)
        row, reason = self.row(task_dir)
        self.assertIsNone(row)
        self.assertIn("证据变化", reason)
        with self.assertRaises(lib.FbrError):
            self.execute(client)
        self.assertEqual(len(client.creates), 1)

    def test_exported_attachment_change_invalidates_receipt(self):
        task_dir, _ = self.submitted()
        (self.root / "results" / BATCH / task_dir.name / "record/recording.mp4").write_bytes(b"tampered projection")
        row, _ = self.row(task_dir)
        self.assertEqual(row["上传状态"], "回执已失效")

    def test_legacy_unbound_receipt_is_not_accepted(self):
        task_dir = self.ready()
        delivery.export_batch(self.root, BATCH, self.cfg)
        lib.save_json(task_dir / "review/upload.json", {"state": "已上传", "trace": "https://old.invalid/trace.jsonl"})
        row, _ = self.row(task_dir)
        self.assertEqual(row["上传状态"], "回执已失效")

    def test_failed_get_invalidates_previous_confirmation_without_resending(self):
        task_dir, client = self.submitted()
        with mock.patch.object(client, "detail", side_effect=lib.FbrError("GET disconnected")):
            with self.assertRaisesRegex(lib.FbrError, "GET disconnected"):
                self.execute(client)
        self.assertEqual(self.receipt(task_dir)["state"], "verifying")
        row, _ = self.row(task_dir)
        self.assertEqual(row["上传状态"], "提交待核对")
        self.assertFalse(row["轨迹文件"].startswith("https://"))
        self.assertEqual((len(client.uploads), len(client.creates)), (2, 1))

    def test_remote_field_drift_invalidates_previous_confirmation(self):
        task_dir, client = self.submitted()
        client.records[0]["data"]["feedback"] += " remote edit"
        with self.assertRaisesRegex(lib.FbrError, "平台回读字段与本次提交不一致"):
            self.execute(client)
        self.assertEqual(self.receipt(task_dir)["state"], "verifying")
        row, _ = self.row(task_dir)
        self.assertEqual(row["上传状态"], "提交待核对")
        self.assertEqual((len(client.uploads), len(client.creates)), (2, 1))

    def test_remote_time_and_status_are_required_without_local_fallback(self):
        task_dir, client = self.submitted()
        saved = copy.deepcopy(client.records[0])
        for field, value in (("created_at", None), ("created_at", "yesterday"),
                             ("status", None), ("status", "")):
            with self.subTest(field=field, value=value):
                client.records[0] = {**copy.deepcopy(saved), field: value}
                with self.assertRaisesRegex(lib.FbrError, "created_at|状态"):
                    self.execute(client)
                self.assertEqual(self.receipt(task_dir)["state"], "verifying")
                self.assertEqual(self.row(task_dir)[0]["上传状态"], "提交待核对")
        self.assertEqual((len(client.uploads), len(client.creates)), (2, 1))

    def test_first_confirmation_without_remote_time_stays_unconfirmed(self):
        task_dir, client = self.ready(), FakeClient()
        original = client.detail

        def no_created_at(identifier):
            row = original(identifier)
            row.pop("created_at")
            return row

        with mock.patch.object(client, "detail", side_effect=no_created_at):
            with self.assertRaisesRegex(lib.FbrError, "created_at"):
                self.execute(client)
        receipt = self.receipt(task_dir)
        self.assertEqual(receipt["state"], "uncertain")
        self.assertFalse(receipt.get("submitted_at"))
        self.assertFalse(receipt.get("verified_at"))
        self.assertEqual(self.row(task_dir)[0]["上传状态"], "提交待核对")

    def test_tls_before_create_preserves_uploads_and_allows_later_submit(self):
        import fbr_platform
        task,client=self.ready(),FakeClient()
        create=client.create
        with mock.patch.object(client,'create',side_effect=fbr_platform.RequestNotSent('TLS before HTTP')):
            with self.assertRaises(fbr_platform.RequestNotSent):self.execute(client)
        receipt=self.receipt(task)
        self.assertEqual(receipt['state'],'uploaded')
        self.assertFalse(receipt.get('submission_id'))
        self.assertEqual(len(receipt['uploads']),2)
        self.execute(client)
        self.assertEqual(len(client.uploads),2)
        self.assertEqual(len(client.records),1)

    def test_tls_readback_failure_keeps_id_and_does_not_recreate(self):
        import fbr_platform
        task,client=self.ready(),FakeClient()
        detail=client.detail
        with mock.patch.object(client,'detail',side_effect=fbr_platform.RequestNotSent('TLS before GET')):
            with self.assertRaises(fbr_platform.RequestNotSent):self.execute(client)
        receipt=self.receipt(task)
        self.assertEqual(receipt['state'],'uncertain')
        self.assertEqual(receipt['submission_id'],1)
        self.execute(client)
        self.assertEqual(len(client.creates),1)

    def test_flat_detail_language_separator_and_remote_trace_are_verified(self):
        task,client=self.ready(),FakeClient()
        original_detail=client.detail
        def flat(identifier):
            nested=original_detail(identifier)
            data=nested.pop('data')
            data['languages']=data['languages'].replace('；',';')
            data['trace_file']=[{'name':data['trace_file'][0]['name'],'size':data['trace_file'][0]['size'],'url':'https://media.example/trace'}]
            return {**nested,**data}
        with mock.patch.object(client,'detail',side_effect=flat),mock.patch.object(submit,'verify_remote_trace',return_value={'url':'https://media.example/trace'}) as verify:
            self.execute(client)
            verify.assert_called_once()
        receipt=self.receipt(task)
        self.assertEqual(receipt['state'],'confirmed')
        self.assertEqual(receipt['remote_trace']['url'],'https://media.example/trace')


if __name__ == "__main__":
    unittest.main()
