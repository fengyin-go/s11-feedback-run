"""Current-AI review work is consumed while the batch is still running."""
from __future__ import annotations

import unittest
from unittest import mock

from fixtures import Sandbox

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
