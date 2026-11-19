"""Submitted tasks reuse frozen facts instead of being invalidated by a later pass."""
from __future__ import annotations

import hashlib
from unittest import mock

from fixtures import Sandbox, write
import fbr_facts
import fbrlib as lib

SID = "aaaaaaaa-0000-4000-8000-000000000001"


class FrozenFactsRecovery(Sandbox):
    def receipts(self):
        task = self.make_task()
        commit = "b" * 40
        feedback_text = "实现已经完成，但页面操作缺少浏览器证据。"
        feedback_sha = hashlib.sha256(feedback_text.encode()).hexdigest()
        lib.save_json(task / "review/snapshot.json", {"commit": commit})
        lib.save_json(task / "record/recording.json", {"task_id": task.name, "commit": commit})
        lib.save_json(task / "trace/ids.json", {"session_id": SID})
        write(task / "trace/projects/fake.jsonl", "{}\n")
        write(task / "record/recording.mp4", "video")
        lib.save_json(task / "evidence/facts.json", {
            "task_id": task.name,
            "commit": commit,
            "source_identity": fbr_facts.source_identity(task),
        })
        lib.save_json(task / "review/feedback.json", {
            "score": 4,
            "feedback": feedback_text,
        })
        lib.save_json(task / "review/gate.json", {
            "task_id": task.name, "passed": True, "score": 4,
            "feedback_sha": feedback_sha, "commit": commit,
        })
        lib.save_json(task / "review/upload.json", {
            "submission_id": 123, "state": "confirmed",
        })
        lib.save_json(task / "review/task-closeout.json", {
            "task_id": task.name,
            "gate_state": "passed",
            "stages": {
                name: "ok"
                for name in ("run", "collect", "commit", "record", "facts", "feedback")
            },
            "submission": {"state": "confirmed", "submission_id": 123},
        })
        lib.save_json(task / "state.json", {"stages": {
            "run": {"state": "ok"},
            "collect": {"state": "ok", "session_id": SID},
            "commit": {"state": "ok", "commit": commit},
            "record": {"state": "ok"},
            "facts": {"state": "failed", "reason": "已有平台单"},
        }})
        return task, feedback_sha

    def test_restores_facts_and_feedback_from_frozen_receipts(self):
        task, feedback_sha = self.receipts()
        row = fbr_facts.reconcile_frozen_facts(task)
        self.assertEqual(row["state"], "skip")
        stages = lib.load_state(task)["stages"]
        self.assertEqual(stages["facts"]["state"], "ok")
        self.assertEqual(stages["feedback"], {
            "state": "ok", "at": stages["feedback"]["at"],
            "score": 4, "feedback_sha": feedback_sha,
            "reconciled_from": "platform-receipt",
        })

    def test_rejects_mismatched_gate_without_rewriting_state(self):
        task, _ = self.receipts()
        gate = lib.load_json(task / "review/gate.json")
        gate["commit"] = "c" * 40
        lib.save_json(task / "review/gate.json", gate)
        with self.assertRaisesRegex(lib.FbrError, "Feedback gate"):
            fbr_facts.reconcile_frozen_facts(task)
        self.assertEqual(lib.load_state(task)["stages"]["facts"]["state"], "failed")

    def test_local_closeout_can_reuse_frozen_facts_before_platform_write(self):
        task, _ = self.receipts()
        (task / "review/upload.json").unlink()
        closeout = lib.load_json(task / "review/task-closeout.json")
        closeout["submission"] = {"state": "not_submitted", "submission_id": None}
        lib.save_json(task / "review/task-closeout.json", closeout)
        self.assertEqual(fbr_facts.reconcile_frozen_facts(task)["state"], "skip")
        self.assertEqual(lib.load_state(task)["stages"]["feedback"]["state"], "ok")

    def test_existing_facts_skip_does_not_probe_docker_again(self):
        task = self.make_task()
        lib.save_json(task / "review/snapshot.json", {"commit": "b" * 40})
        lib.save_json(task / "record/recording.json", {"task_id": task.name, "commit": "b" * 40})
        lib.save_json(task / "trace/ids.json", {"session_id": SID})
        write(task / "trace/projects/fake.jsonl", "{}\n")
        write(task / "record/recording.mp4", "video")
        lib.save_json(task / "evidence/facts.json", {
            "source_identity": fbr_facts.source_identity(task),
        })
        lib.save_json(task / "state.json", {"stages": {
            "run": {"state": "ok"}, "collect": {"state": "ok"},
            "commit": {"state": "ok"}, "record": {"state": "ok"},
            "facts": {"state": "ok"},
        }})
        with mock.patch.object(fbr_facts, "test_identity", side_effect=AssertionError("must not probe")):
            row = fbr_facts.facts_one(task, self.root, self.cfg)
        self.assertEqual(row["detail"], "已经取证过")
