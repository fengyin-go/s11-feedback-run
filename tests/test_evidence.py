"""Single-ingest trace index, exact locators and batched raw-evidence access."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from fixtures import Sandbox, write

import fbr_evidence
import fbr_logs
import fbr_quality
import fbr_reviewer as reviewer
import fbr_run
import fbr_trace_review
import fbrlib as lib


class EvidenceStore(Sandbox):
    def task(self) -> tuple[Path, Path]:
        task = self.make_task()
        path = task / "trace/projects/-workspace/session.jsonl"
        rows = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make test"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "FAILED first attempt"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "修正后重新运行通过。"}]}},
        ]
        write(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        return task, path

    def test_ingest_once_builds_manifest_offsets_events_pack_and_coverage(self):
        task, source = self.task()
        before = source.read_bytes()
        check = fbr_evidence.ingest(task)
        self.assertTrue(check["full_scan_complete"])
        self.assertTrue(check["all_source_files_processed"])
        self.assertTrue(check["all_lines_accounted"])
        self.assertEqual(check["counts"]["source_files"], 1)
        self.assertEqual(check["counts"]["lines"], 3)
        self.assertFalse({
            "tool_uses", "tool_results", "paired_tools", "logical_tools",
            "unpaired_tools", "duplicate_tool_uses",
        } & set(check["counts"]))
        self.assertTrue((task / fbr_evidence.MANIFEST).is_file())
        self.assertTrue((task / fbr_evidence.EVENTS).is_file())
        self.assertTrue((task / fbr_evidence.PACK).is_file())
        manifest = lib.load_json(task / fbr_evidence.MANIFEST)
        offsets = lib.load_json(task / manifest["files"][0]["offset_file"])
        self.assertEqual(len(offsets["starts"]), 4)
        pack = lib.load_json(task / fbr_evidence.PACK)
        self.assertEqual(pack["final_answer"]["line"], 3)
        self.assertIn("tool_errors", pack["summary"]["high_risk"])
        prompt = fbr_trace_review.prompt_pack(task)
        self.assertNotIn("tool_events", prompt)
        self.assertIn("不统计、不补算工具调用总数", prompt["process_policy"])
        self.assertEqual(len(prompt["assistant_text"]), 1)
        self.assertEqual(prompt["edits"], [])
        self.assertEqual(prompt["columns"]["edits"][0], "source_id")
        self.assertEqual(source.read_bytes(), before)

    def test_batch_fetch_deduplicates_ranges_and_opens_each_source_once(self):
        task, _ = self.task()
        fbr_evidence.ingest(task)
        refs = [
            {"path": "trace/projects/-workspace/session.jsonl", "start_line": 1, "end_line": 1,
             "block": 0, "excerpt": "make test"},
            {"path": "trace/projects/-workspace/session.jsonl", "start_line": 1, "end_line": 1,
             "block": 0, "excerpt": "make test"},
            {"path": "trace/projects/-workspace/session.jsonl", "start_line": 2, "end_line": 2,
             "block": 0, "excerpt": "FAILED first attempt"},
        ]
        result = fbr_evidence.fetch_refs(task, refs, operation="test")
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["stats"]["requested"], 3)
        self.assertEqual(result["stats"]["deduplicated"], 2)
        self.assertEqual(result["stats"]["file_opens"], 1)
        self.assertEqual(result["stats"]["full_reads"], 0)
        access = fbr_evidence.access_summary(task)
        self.assertEqual(access["full_reads"], 0)
        self.assertEqual(access["file_opens"], 1)

    def test_reviewer_pack_keeps_relevant_process_events_without_tool_counts(self):
        task = self.make_task()
        path = task / "trace/projects/-workspace/session.jsonl"
        rows = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "src/app.py"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1", "content": "source"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "e1", "name": "Edit",
                 "input": {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "e1", "content": "updated"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make test"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "OK"}]}},
        ]
        write(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        fbr_evidence.ingest(task)
        prompt = fbr_trace_review.prompt_pack(task)
        self.assertNotIn("tool_events", prompt)
        self.assertEqual(len(prompt["edits"]), 1)
        self.assertEqual(len(prompt["checks"]), 1)
        self.assertNotIn("logical_tool_uses", prompt["summary"])
        self.assertNotIn("paired_tools", prompt["summary"])

    def test_exact_locator_rejects_quote_from_another_line(self):
        task, _ = self.task()
        fbr_evidence.ingest(task)
        ref = {"path": "trace/projects/-workspace/session.jsonl", "start_line": 1,
               "end_line": 1, "block": 0, "excerpt": "FAILED first attempt"}
        result = fbr_evidence.verify_refs(task, [ref])
        self.assertEqual(result["state"], "failed")
        self.assertIn("不在指定原始区间", result["errors"][0]["error"])

    def test_changed_trace_invalidates_manifest_without_reusing_stale_offsets(self):
        task, source = self.task()
        first = fbr_evidence.ingest(task)
        source.write_text(source.read_text() + json.dumps(
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "later"}]}}) + "\n")
        second = fbr_evidence.ingest(task)
        self.assertNotEqual(first["sources"][0]["sha256"], second["sources"][0]["sha256"])
        self.assertEqual(second["sources"][0]["lines"], 4)

    def test_reviewer_projects_navigation_from_single_ingest(self):
        task, _ = self.task()
        index = reviewer.trace_index(task)
        rows = lib.read_jsonl(index)
        self.assertEqual(rows[0]["source"], "trace/projects/-workspace/session.jsonl")
        self.assertEqual(rows[1]["blocks"][0]["tool"], "Bash")
        self.assertEqual(rows[2]["blocks"][0]["tool_use_id"], "t1")

    def test_run_status_default_summary_stays_compact(self):
        tasks = [self.make_task(number) for number in (1, 2)]
        lib.save_json(tasks[0]/"run/current.json", {
            "state": "running", "attempt": 2, "result": "x"*10000,
            "container_id": "c"*64, "stdout": "/very/long/path"})
        lib.save_json(tasks[0]/"run/heartbeat.json", {
            "elapsed": 12.3, "tools": 4, "events": 1000, "output_tail": "ignored"})
        lib.save_json(tasks[1]/"run/current.json", {
            "state": "finished", "attempt": 1, "verdict": "accept"})
        rows = fbr_run.status_rows(tasks)
        text = fbr_run.status_text(rows, tasks[0].parent.name, Path('/tmp/report.json'))
        self.assertLess(len(text.encode()), 2000)
        self.assertIn("running 1", text)
        self.assertNotIn("x"*100, text)

    def test_log_query_returns_bounded_context_not_whole_stream(self):
        task = self.make_task()
        rows = [f"line {index} " + ("x"*200) for index in range(20)]
        rows[10] = "line 10 failure marker " + ("y"*200)
        write(task/"run/stream.jsonl", "\n".join(rows)+"\n")
        text = fbr_logs.query(task, "stream", grep="failure marker", context=1, max_bytes=1000)
        self.assertIn("failure marker", text)
        self.assertIn("run/stream.jsonl:11", text)
        self.assertNotIn("line 0 ", text)
        self.assertLessEqual(len(text.encode()), 1000)

    def test_replayed_tool_use_id_is_deduplicated_in_review_pack(self):
        task = self.make_task()
        path = task / "trace/projects/-workspace/replay.jsonl"
        rows = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Bash", "input": {"command": "make test"}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Bash", "input": {"command": "make test"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "same", "is_error": False, "content": "OK"}]}},
        ]
        write(path, "".join(json.dumps(row) + "\n" for row in rows))
        check = fbr_evidence.ingest(task)
        self.assertNotIn("logical_tools", check["counts"])
        self.assertEqual(len(lib.load_json(task / fbr_evidence.PACK)["tool_events"]), 1)


class MetricsAndStrictQuality(unittest.TestCase):
    def test_token_usage_extracts_only_counters(self):
        events = "\n".join([
            json.dumps({"type": "item.completed", "item": {"content": "not retained"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 120, "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 12}}}),
        ])
        self.assertEqual(reviewer.token_usage(events),
                         {"input_tokens": 120, "output_tokens": 30, "reasoning_output_tokens": 12})

    def test_quality_rejects_trace_source_without_exact_locator(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp)
            path = task / "trace/projects/session.jsonl"
            write(path, json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "first failed then fixed"}]}}) + "\n")
            doc = {"feedback": "模型先失败后修正。"}
            report = {"claim_reviews": [{
                "sentence": "模型先失败后修正", "assertions": ["首次失败"],
                "verdict": "supported", "reason": "原文记录首次失败和后续修正",
                "later_check": "已查看同一行内的后续修正",
                "sources": [{"path": "trace/projects/session.jsonl",
                             "excerpt": "first failed then fixed"}]}]}
            errors = fbr_quality.claim_report_errors(report, doc, task)
            self.assertTrue(any("start_line" in error for error in errors))
            self.assertEqual(
                fbr_quality.claim_report_errors(report, doc, task, strict_locators=False), [])


if __name__ == "__main__":
    unittest.main()
