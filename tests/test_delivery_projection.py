"""独立交付投影：当前有效闸门、文件白名单、任务隔离与可用媒体链接。

复用离线 FlowCase；不调用真实 Docker、网络或推送。
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from openpyxl import load_workbook

from fixtures import BATCH, write
from flow import TEXT_A, TEXT_B, feedback, run_main
from test_sheet_batch import SheetCase

import fbrlib as lib
import fbr_batch
import fbr_delivery
import fbr_sheet


class MediaSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag in ("video", "source"):
            self.sources.extend(value for key, value in attrs if key == "src")


class DeliveryProjection(SheetCase):
    def export(self):
        return fbr_delivery.export_batch(self.root, BATCH, self.cfg)

    def task_result(self, task):
        return self.root / "results" / BATCH / task.name

    def listed(self, manifest):
        return {item["task_id"]: item for item in manifest["tasks"]}

    def assert_excluded(self, manifest, task):
        self.assertNotIn(task.name, self.listed(manifest))
        reasons = {item["task_id"]: item["reason"] for item in manifest["excluded"]}
        self.assertTrue(reasons.get(task.name))
        self.assertFalse(self.task_result(task).exists())

    def test_current_feedback_requires_current_gate_text_score_and_state(self):
        task = self.gated()
        self.assertEqual(fbr_delivery.current_feedback(task), (True, ""))
        self.put(task, feedback_doc=feedback(TEXT_B))
        self.assertFalse(fbr_delivery.current_feedback(task)[0])
        self.put(task, feedback_doc=feedback(TEXT_A, score=2))
        self.assertFalse(fbr_delivery.current_feedback(task)[0])
        self.put(task, feedback_doc=feedback(TEXT_A))
        self.patch_json(task / "review/gate.json", passed=False)
        self.assertFalse(fbr_delivery.current_feedback(task)[0])
        self.patch_json(task / "review/gate.json", passed=True)
        lib.set_stage(task, "feedback", "failed", problems=["人为退回夹具"])
        self.assertFalse(fbr_delivery.current_feedback(task)[0])

    def test_export_only_valid_tasks_and_rebuild_drops_stale_task(self):
        good = self.gated(1, TEXT_A)
        pending = self.written(2, TEXT_B)
        self.complete(good)
        first = self.export()
        self.assertEqual(set(self.listed(first)), {good.name})
        self.assert_excluded(first, pending)
        self.assertTrue((self.task_result(good) / "review/feedback.json").is_file())
        self.put(good, feedback_doc=feedback(TEXT_B))
        second = self.export()
        self.assert_excluded(second, good)
        self.assertTrue((good / "workspace/.git").is_dir())
        self.assertTrue((good / "review/feedback.json").is_file())

    def test_manifest_records_real_bytes_and_hashes_without_source_mutation(self):
        task = self.gated()
        self.complete(task)
        before = {str(p.relative_to(task)): p.read_bytes() for p in task.rglob("*") if p.is_file()}
        manifest = self.export()
        self.assertEqual(manifest["batch_id"], BATCH)
        self.assertTrue(manifest["schema_version"])
        row = self.listed(manifest)[task.name]
        self.assertEqual(row["feedback_sha"], hashlib.sha256(TEXT_A.encode()).hexdigest())
        self.assertTrue(row["source_digest"])
        self.assertTrue(row["files"])
        seen = set()
        for item in row["files"]:
            path = self.root / item["path"]
            path.relative_to(self.task_result(task))
            self.assertNotIn(item["path"], seen)
            seen.add(item["path"])
            raw = path.read_bytes()
            self.assertEqual(item["bytes"], len(raw))
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(before, {str(p.relative_to(task)): p.read_bytes() for p in task.rglob("*") if p.is_file()})
        saved = lib.load_json(self.root / "results" / BATCH / "delivery-manifest.json")
        self.assertEqual(saved, manifest)

    def test_modified_export_is_rebuilt_from_original_whitelist(self):
        task = self.gated()
        self.complete(task)
        first = self.export()
        video = self.task_result(task) / "record/recording.mp4"
        video.write_bytes(b"modified delivery copy")
        self.export()
        self.assertEqual(video.read_bytes(), (task / "record/recording.mp4").read_bytes())
        self.assertNotEqual(video.read_bytes(), b"modified delivery copy")
        current = lib.load_json(self.root / "results" / BATCH / "delivery-manifest.json")
        self.assertEqual(self.listed(first)[task.name]["files"], self.listed(current)[task.name]["files"])

    def test_export_whitelist_omits_code_git_config_and_author_solution(self):
        task = self.gated()
        self.complete(task)
        self.patch_json(task / "task.json", repro={"fixed_commit": "f" * 40, "cmd": "private oracle"},
                        why_hard="author explanation", recording={"private": "author recording"})
        self.review_receipt(task)
        write(task / "run/git-config.before", "private credential helper\n")
        write(task / "review/secret-waivers.json", '[{"path":"private.pem","reason":"fixture"}]')
        write(task / "extra.json", '{"secret":"must not copy"}')
        write(task / "evidence/unlisted.txt", "not public evidence\n")
        manifest = self.export()
        self.assertIn(task.name, self.listed(manifest))
        target = self.task_result(task)
        relative = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        for item in relative:
            self.assertFalse(item.startswith(("workspace/", ".git/", "run/")), item)
        for unwanted in ("task.json", "extra.json", "review/secret-waivers.json", "evidence/unlisted.txt"):
            self.assertNotIn(unwanted, relative)
        summary = lib.load_json(target / "task-summary.json")
        self.assertEqual(summary["task_id"], task.name)
        self.assertEqual(summary["prompt"], lib.load_task(task)["prompt"])
        for hidden in ("repro", "why_hard", "recording", "fixed_commit"):
            self.assertNotIn(hidden, summary)

    def test_two_tasks_have_disjoint_files_and_no_cross_task_payload(self):
        first = self.gated(1, TEXT_A)
        second = self.gated(2, TEXT_B)
        self.complete(first)
        self.complete(second)
        manifest = self.export()
        rows = self.listed(manifest)
        self.assertEqual(set(rows), {first.name, second.name})
        sets = []
        for task in (first, second):
            files = {item["path"] for item in rows[task.name]["files"]}
            self.assertTrue(files)
            self.assertTrue(all((self.root / name).is_relative_to(self.task_result(task)) for name in files))
            sets.append(files)
        self.assertFalse(sets[0] & sets[1])
        self.assertEqual(lib.load_json(self.task_result(first) / "review/feedback.json")["feedback"], TEXT_A)
        self.assertEqual(lib.load_json(self.task_result(second) / "review/feedback.json")["feedback"], TEXT_B)

    def test_cross_task_session_file_does_not_export_other_task_trace(self):
        first = self.gated(1, TEXT_A)
        second = self.gated(2, TEXT_B)
        other_ids = lib.load_json(second / "trace/ids.json")
        self.patch_json(first / "trace/ids.json",
                        session_file=f"../../{second.name}/trace/{other_ids['session_file']}")
        manifest = self.export()
        self.assert_excluded(manifest, first)
        self.assertIn(second.name, self.listed(manifest))

    def test_symlinked_whitelist_file_cannot_escape_task(self):
        task = self.gated()
        source = task / "evidence/facts.json"
        outside = self.root / "outside-facts.json"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        manifest = self.export()
        self.assert_excluded(manifest, task)
        self.assertTrue(outside.is_file())

    def test_symlinked_trace_is_not_followed_or_exported(self):
        task = self.gated()
        write(self.root / "outside.jsonl", '{"private":"outside trace"}\n')
        (task / "trace/projects/outside.jsonl").symlink_to(self.root / "outside.jsonl")
        manifest = self.export()
        self.assert_excluded(manifest, task)

    def test_spotcheck_exports_only_the_current_feedback_version(self):
        task = self.gated()
        self.patch_json(task / "review/spotcheck.json", state="通过", by="测试员", feedback_sha="0" * 64)
        self.export()
        target = self.task_result(task) / "review/spotcheck.json"
        self.assertFalse(target.exists())
        self.patch_json(task / "review/spotcheck.json", feedback_sha=hashlib.sha256(TEXT_A.encode()).hexdigest())
        self.export()
        self.assertEqual(lib.load_json(target)["by"], "测试员")

    def test_preview_media_resolves_inside_delivery_directory(self):
        task = self.gated()
        self.complete(task)
        target = fbr_batch.preview(self.root, BATCH)
        self.assertEqual(target, self.root / "results" / BATCH / "preview.html")
        text = target.read_text()
        parser = MediaSources()
        parser.feed(text)
        self.assertNotIn("diff --git ", text)
        self.assertNotIn("+++ b/src/clipcut/segment.py", text)
        self.assertTrue(parser.sources)
        for src in parser.sources:
            self.assertFalse(urlsplit(src).scheme)
            local = (target.parent / unquote(urlsplit(src).path)).resolve()
            self.assertTrue(local.is_relative_to(target.parent.resolve()))
            self.assertTrue(local.is_file())
            self.assertEqual(local, (self.task_result(task) / "record/recording.mp4").resolve())

    def test_sheet_and_upload_paths_refer_to_exported_files(self):
        task = self.gated()
        self.complete(task)
        out = fbr_sheet.write_sheet(self.root, BATCH, self.cfg)
        self.assertEqual(out["path"], self.root / "results" / BATCH / "result.xlsx")
        book = load_workbook(out["path"])
        sheet = book["结果"]
        columns = [cell.value for cell in sheet[1]]
        row = dict(zip(columns, (cell.value for cell in sheet[2])))
        for field in ("轨迹文件", "运行录屏", "证据路径"):
            path = self.root / row[field]
            self.assertTrue(path.is_relative_to(self.task_result(task)))
            self.assertTrue(path.exists())
        plan, = fbr_batch.upload_plan(self.root, BATCH)
        self.assertEqual(plan["task_id"], task.name)
        self.assertEqual(plan["trace"], row["轨迹文件"])
        self.assertEqual(plan["video"], row["运行录屏"])

    def test_batch_export_command_is_local_projection_entry(self):
        task = self.gated()
        code, out, err = run_main(fbr_batch, "--root", str(self.root), "export", "--batch", BATCH)
        self.assertEqual((code, err), (0, ""))
        manifest = lib.load_json(self.root / "results" / BATCH / "delivery-manifest.json")
        self.assertIn(task.name, self.listed(manifest))
        self.assertFalse((task / "review/upload.json").exists())

    def test_missing_key_blocks_batch_and_preserves_existing_projection_bytes(self):
        task = self.gated()
        self.complete(task)
        self.export()
        target = self.root / "results" / BATCH
        before = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        source_before = {p.relative_to(task).as_posix(): p.read_bytes() for p in task.rglob("*") if p.is_file()}
        self.key_file.unlink()
        with self.assertRaises(lib.FbrError):
            self.export()
        self.assertEqual(before, {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()})
        self.assertEqual(source_before, {p.relative_to(task).as_posix(): p.read_bytes() for p in task.rglob("*") if p.is_file()})

    def test_replaced_key_blocks_whole_batch_export(self):
        task = self.gated()
        self.assertTrue((task / "run/key-identity.json").is_file())
        replacement = "replacement-delivery-key-for-selftest"
        write(self.key_file, f"apikey={replacement}\n")
        with self.assertRaises(lib.FbrError) as caught:
            self.export()
        self.assertNotIn(replacement, str(caught.exception))
        self.assertFalse((self.root / "results" / BATCH).exists())
        self.assertTrue((task / "evidence/facts.json").is_file())

    def test_missing_start_key_fingerprint_blocks_whole_batch_export(self):
        task = self.gated()
        identity = task / "run/key-identity.json"
        self.assertTrue(identity.is_file())
        identity.unlink()
        with self.assertRaises(lib.FbrError):
            self.export()
        self.assertFalse((self.root / "results" / BATCH).exists())
        self.assertTrue((task / "trace/ids.json").is_file())

    def test_no_ready_task_can_export_empty_manifest_without_key(self):
        pending = self.written()
        self.key_file.unlink()
        manifest = self.export()
        self.assertEqual(manifest["tasks"], [])
        self.assert_excluded(manifest, pending)
        self.assertTrue((self.root / "results" / BATCH / "delivery-manifest.json").is_file())

