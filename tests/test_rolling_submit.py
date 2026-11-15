"""Rolling S11 submission: one-task preflight, serialized submit and opt-in flow."""
from __future__ import annotations

import argparse
import time
import unittest
from unittest import mock
from pathlib import Path

from fixtures import BATCH
from test_submit import FakeClient, SubmitCase

import fbrlib as lib
import fbr_flow
import fbr_run
import fbr_submit as submit


class RollingSubmit(SubmitCase):
    def test_preflight_is_read_only_and_persists_binding(self):
        task_dir, client = self.ready(), FakeClient()
        result = submit.preflight_one(self.root, BATCH, task_dir.name, client=client)
        self.assertTrue(result["ready"], result["errors"])
        self.assertEqual((client.uploads, client.creates), ([], []))
        saved = lib.load_json(task_dir / "review/submit-preflight.json")
        self.assertEqual(saved["state"], "ready")
        self.assertEqual(saved["content_digest"], result["content_digest"])
        self.assertTrue(saved["source_digest"])
        self.assertEqual(saved["schema_fingerprint"], client.current_schema["fingerprint"])
        plan = lib.load_json(task_dir / "review/upload-plan.json")
        self.assertEqual(plan["content_digest"], result["content_digest"])
        self.assertNotIn("client", plan)

    def test_submit_ready_runs_preflight_then_submits_once(self):
        task_dir, client = self.ready(), FakeClient()
        result = submit.submit_ready(
            self.root, BATCH, task_dir.name, execute=True, confirm="SUBMIT", client=client, interval=0)
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual((len(client.uploads), len(client.creates)), (2, 1))
        self.assertEqual(lib.load_json(task_dir / "review/upload.json")["state"], "confirmed")
        self.assertEqual(lib.load_json(task_dir / "review/submit-preflight.json")["state"], "ready")

    def test_duplicate_session_blocks_only_this_item(self):
        task_dir, client = self.ready(), FakeClient()
        ids = lib.load_json(task_dir / "trace/ids.json")
        other = self.make_task(2, repo=False)
        lib.save_json(other / "trace/ids.json", ids)
        result = submit.preflight_one(self.root, BATCH, task_dir.name, client=client)
        self.assertFalse(result["ready"])
        self.assertTrue(any("SessionID" in item and "重复" in item for item in result["errors"]))
        self.assertEqual((client.uploads, client.creates), ([], []))

    def test_changed_recording_invalidates_preflight(self):
        task_dir, client = self.ready(), FakeClient()
        first = submit.preflight_one(self.root, BATCH, task_dir.name, client=client)
        self.assertTrue(first["ready"], first["errors"])
        with (task_dir / "record/recording.mp4").open("ab") as stream:
            stream.write(b"changed")
        second = submit.preflight_one(self.root, BATCH, task_dir.name, client=client)
        self.assertFalse(second["ready"])
        self.assertTrue(second["errors"])
        attempted = submit.submit_ready(
            self.root, BATCH, task_dir.name, execute=True, confirm="SUBMIT", client=client, interval=0)
        self.assertFalse(attempted["ready"])
        self.assertEqual((client.uploads, client.creates), ([], []))

    def test_probe_persists_full_schema_and_compact_result_hides_plan(self):
        task_dir, client = self.ready(), FakeClient()
        result = submit.probe(self.root, client=client)
        self.assertTrue(Path(result["schema_report"]).is_file())
        field = submit.schema_field(self.root, result["fingerprint"], "feedback")
        self.assertEqual(field["field_key"], "feedback")
        compact = submit.compact_result({
            "task_id": task_dir.name, "state": "blocked",
            "errors": ["a", "b", "c", "d", "e", "f"],
            "plan": {"large": "hidden"},
        })
        self.assertNotIn("hidden", compact)
        self.assertIn("其余 1 项", compact)


class RollingFlow(SubmitCase):
    def options(self, *, auto_submit: bool, auto_cleanup: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            batch=BATCH, tasks=None, publish=True, auto_submit=auto_submit,
            submit_mode="drain", auto_cleanup=auto_cleanup, confirm_cleanup=(
                "CLEANUP" if auto_cleanup else ""), cleanup_poll_seconds=10,
            no_watchdog=True, reviewer_env="unused",
            execute=True, command="run")

    def test_auto_submit_uses_one_serial_submit_job_after_local_review(self):
        first, second = self.make_task(1), self.make_task(2)
        for task in (first, second):
            for stage in ("run", "collect", "commit", "record", "facts"):
                lib.set_stage(task, stage, "ok")
        events = []

        def review(task, *args):
            time.sleep(.05)
            events.append(("review", task.name))
            return {"task_id": task.name, "state": "ok"}

        def submitted(task, *args):
            events.append(("submit", task.name))
            return {"task_id": task.name, "state": "ok", "detail": "已提交 #1"}

        with mock.patch.object(fbr_run, "preflight", return_value=[]), \
             mock.patch("fbr_dependencies.prewarm_batch",
                        return_value={"prepared": 0, "fallback": 2, "failed": 0}), \
             mock.patch.object(fbr_flow, "review_task", side_effect=review), \
             mock.patch.object(fbr_flow, "submit_task", side_effect=submitted), \
             mock.patch.object(
                 fbr_flow, "cleanup_task",
                 side_effect=lambda task, *args: {
                     "task_id": task.name,
                     "state": "ok",
                     "detail": "离线测试不连接平台",
                 }), \
             mock.patch("fbr_sheet.write_sheet", return_value={}):
            result = fbr_flow.run_flow(self.root, self.options(auto_submit=True))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(sorted(events), sorted([
            ("review", first.name), ("review", second.name),
            ("submit", first.name), ("submit", second.name)]))
        self.assertEqual(sum(1 for kind, _ in events if kind == "submit"), 2)

    def test_without_auto_submit_never_invokes_platform(self):
        task = self.make_task()
        for stage in ("run", "collect", "commit", "record", "facts"):
            lib.set_stage(task, stage, "ok")
        with mock.patch.object(fbr_run, "preflight", return_value=[]), \
             mock.patch("fbr_dependencies.prewarm_batch",
                        return_value={"prepared": 0, "fallback": 1, "failed": 0}), \
             mock.patch.object(fbr_flow, "review_task",
                               return_value={"task_id": task.name, "state": "ok"}), \
             mock.patch.object(fbr_flow, "submit_task") as submit_task, \
             mock.patch("fbr_sheet.write_sheet", return_value={}):
            result = fbr_flow.run_flow(self.root, self.options(auto_submit=False))
        self.assertEqual(result["state"], "completed")
        submit_task.assert_not_called()

    def test_flow_passes_submit_interval_to_single_task_submit(self):
        task = self.make_task()
        options = self.options(auto_submit=True)
        options.submit_interval = 7.5
        options.delivery_lock = mock.MagicMock()
        with mock.patch.object(submit, "submit_ready", return_value={
                "state": "confirmed",
                "task_result": {"submission_id": 1, "status": "pending"},
        }) as submit_ready, mock.patch("fbr_sheet.write_sheet", return_value={}):
            result = fbr_flow.submit_task(task, self.root, self.cfg, options)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(submit_ready.call_args.kwargs["interval"], 7.5)

    def test_auto_cleanup_runs_only_after_successful_submit(self):
        task = self.make_task()
        for stage in ("run", "collect", "commit", "record", "facts"):
            lib.set_stage(task, stage, "ok")
        events = []
        with mock.patch.object(fbr_run, "preflight", return_value=[]), \
             mock.patch("fbr_dependencies.prewarm_batch",
                        return_value={"prepared": 0, "fallback": 1, "failed": 0}), \
             mock.patch.object(
                 fbr_flow, "review_task",
                 return_value={"task_id": task.name, "state": "ok"}), \
             mock.patch.object(
                 fbr_flow, "submit_task",
                 side_effect=lambda *args: events.append("submit") or
                 {"task_id": task.name, "state": "ok", "detail": "已提交 #1"}), \
             mock.patch.object(
                 fbr_flow, "cleanup_task",
                 side_effect=lambda *args: events.append("cleanup") or
                 {"task_id": task.name, "state": "ok", "detail": "已清理"}), \
             mock.patch("fbr_sheet.write_sheet", return_value={}):
            result = fbr_flow.run_flow(
                self.root, self.options(auto_submit=True, auto_cleanup=True))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(events, ["submit", "cleanup"])


if __name__ == "__main__":
    unittest.main()
