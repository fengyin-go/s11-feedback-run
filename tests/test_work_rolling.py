"""Current-AI review work is consumed while the batch is still running."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from fixtures import Sandbox
from flow import run_main

import fbr_work
import fbrlib as lib


class RollingWork(Sandbox):
    def test_next_returns_oldest_packet_with_backlog(self):
        first, second = self.make_task(1, repo=False), self.make_task(2, repo=False)
        lib.save_json(first / "review/task-agent-work.json",
                      {"at": "2026-11-15T10:00:00+0800", "next_steps": []})
        lib.save_json(second / "review/task-agent-work.json",
                      {"at": "2026-11-15T10:01:00+0800", "next_steps": []})
        result = fbr_work.next_task(self.root, first.parent.name)
        self.assertEqual(result["task"]["task_id"], first.name)
        self.assertEqual(result["backlog"], 2)

    def test_next_inline_returns_capsule_once(self):
        task = self.make_task(repo=False)
        lib.save_json(task / "review/task-agent-work.json",
                      {"at": "2026-11-15T10:00:00+0800", "next_steps": []})
        lib.save_json(task / "review/review-capsule.json", {
            "version": 2,
            "task_id": task.name,
            "prompt": "prompt",
            "requirements": [],
        })
        result = fbr_work.next_task(self.root, task.parent.name, inline=True)
        packet = result["task"]
        self.assertEqual(packet["capsule"]["task_id"], task.name)
        self.assertEqual(packet["capsule_path"], str(task / "review/review-capsule.json"))
        self.assertNotIn("brief_text", packet)
        self.assertNotIn("review_contract", packet)

    def test_next_skips_submitted_or_closed_work(self):
        submitted, closed = self.make_task(1, repo=False), self.make_task(2, repo=False)
        for task in (submitted, closed):
            lib.save_json(task / "review/task-agent-work.json",
                          {"at": "2026-11-15T10:00:00+0800", "next_steps": []})
        lib.save_json(submitted / "review/upload.json", {"submission_id": 1})
        lib.save_json(closed / "review/task-closeout.json", {"task_id": closed.name})
        self.assertEqual(fbr_work.pending(self.root, submitted.parent.name), [])

    def test_next_inline_falls_back_to_existing_brief(self):
        task = self.make_task(repo=False)
        lib.save_json(task / "review/task-agent-work.json",
                      {"at": "2026-11-15T10:00:00+0800", "next_steps": []})
        brief = "legacy compact brief"
        (task / "review/task-agent-brief.md").write_text(brief, encoding="utf-8")
        result = fbr_work.next_task(self.root, task.parent.name, inline=True)
        self.assertEqual(result["task"]["brief_text"], brief)
        self.assertNotIn("capsule", result["task"])

    def test_next_inline_cli_is_compact_json(self):
        task = self.make_task(repo=False)
        lib.save_json(task / "review/task-agent-work.json",
                      {"at": "2026-11-15T10:00:00+0800", "next_steps": []})
        lib.save_json(task / "review/review-capsule.json", {
            "version": 2, "task_id": task.name, "prompt": "prompt", "requirements": [],
        })
        code, out, err = run_main(
            fbr_work, "--root", str(self.root), "next", "--batch", task.parent.name,
            "--inline",
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(json.loads(out)["task"]["capsule"]["task_id"], task.name)

    def test_next_wait_wakes_when_review_checkpoint_appears(self):
        task = self.make_task()

        def wake(*args, **kwargs):
            lib.save_json(task / "review/task-agent-work.json",
                          {"at": "2026-11-15T10:00:00+0800", "next_steps": []})
            return {"state": "needs_task_agent"}

        with mock.patch.object(fbr_work, "wait_for_event", side_effect=wake):
            result = fbr_work.next_task(self.root, task.parent.name, wait=True, timeout=10)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["task"]["task_id"], task.name)


if __name__ == "__main__":
    unittest.main()
