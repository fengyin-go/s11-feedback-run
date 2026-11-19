"""QC-gated archive and cleanup of S11 process directories."""
from __future__ import annotations

import hashlib
import shutil
import unittest

from fixtures import BATCH, KEY
from test_submit import FakeClient, SubmitCase

import fbr_cleanup
import fbr_delivery as delivery
import fbr_flow
import fbr_submit as submit
import fbrlib as lib


class Cleanup(SubmitCase):
    def submitted(self, *, status: str = "QC_PASSED"):
        task_dir, client = self.ready(), FakeClient()
        state = lib.load_state(task_dir)
        state["stages"]["record"] = {"state": "ok", "at": lib.now_iso()}
        lib.save_json(task_dir / "state.json", state)
        lib.save_json(task_dir / "run/key-identity.json",
                      {"sha256": hashlib.sha256(KEY.encode()).hexdigest()})
        lib.save_json(task_dir / "run/attempts.json", [{"verdict": "ok"}])
        delivery.export_batch(self.root, BATCH, self.cfg)
        result = submit.submit_ready(
            self.root, BATCH, task_dir.name, execute=True, confirm="SUBMIT", client=client, interval=0)
        self.assertEqual(result["state"], "confirmed")
        client.records[0]["status"] = status
        (task_dir / "origin").mkdir(exist_ok=True)
        (task_dir / "origin/bin.dat").write_bytes(b"x" * 4096)
        (task_dir / "workspace/cache.dat").write_bytes(b"y" * 8192)
        scratch = self.root / "_verification/runtime-scratch" / f"s11-rec-{task_dir.name}-test"
        (scratch / "workspace").mkdir(parents=True)
        (scratch / "workspace/cache.dat").write_bytes(b"z" * 4096)
        return task_dir, client

    def result_dir(self, task_dir):
        return delivery.delivery_dir(self.root, BATCH) / task_dir.name

    def test_audit_is_read_only_and_prune_keeps_delivery_package(self):
        task_dir, client = self.submitted()
        audit = fbr_cleanup.audit_batch(
            self.root, BATCH, task_dir.name, client=client)
        self.assertTrue(audit["tasks"][0]["eligible"], audit["tasks"][0]["reason"])
        self.assertTrue(audit["candidate_bytes"] > 0)
        self.assertTrue((task_dir / "origin").is_dir())
        result = fbr_cleanup.prune_batch(
            self.root, BATCH, task_dir.name, execute=True, confirm="CLEANUP", client=client)
        self.assertEqual(len(result["cleaned"]), 1)
        self.assertFalse((task_dir / "origin").exists())
        self.assertFalse((task_dir / "workspace").exists())
        self.assertTrue((task_dir / "trace/ids.json").is_file())
        self.assertTrue((task_dir / "record/recording.mp4").is_file())
        self.assertTrue((task_dir / "evidence/facts.json").is_file())
        self.assertTrue((task_dir / "review/feedback.json").is_file())
        self.assertTrue((task_dir / "cleanup.json").is_file())
        artifact = self.result_dir(task_dir)
        self.assertTrue((artifact / "retention.json").is_file())
        self.assertTrue((artifact / "platform/upload.json").is_file())
        self.assertEqual(fbr_flow.phase(task_dir), "cleaned")
        verified = fbr_cleanup.verify_batch(self.root, BATCH, task_dir.name)
        self.assertTrue(verified["valid"], verified)
        self.assertTrue((artifact / "cleanup.json").is_file())
        retention = lib.load_json(artifact / "retention.json")
        self.assertEqual(retention["platform"]["status"], "QC_PASSED")
        self.assertIn("trace/ids.json", retention["files"])
        manifest = delivery.export_batch(self.root, BATCH, self.cfg)
        self.assertTrue(delivery._verify_files(self.root, delivery.delivery_dir(self.root, BATCH),
                                               manifest))
        self.assertTrue((artifact / "retention.json").is_file())
        self.assertTrue((artifact / "platform/upload.json").is_file())

    def test_pending_qc_never_deletes(self):
        task_dir, client = self.submitted(status="QC_RUNNING")
        result = fbr_cleanup.prune_batch(
            self.root, BATCH, task_dir.name, execute=True, confirm="CLEANUP", client=client)
        self.assertEqual(result["cleaned"], [])
        self.assertTrue((task_dir / "origin").is_dir())
        self.assertTrue((task_dir / "workspace").is_dir())

    def test_verifying_receipt_is_reconciled_before_cleanup(self):
        task_dir, client = self.submitted()
        receipt = lib.load_json(task_dir / "review/upload.json")
        receipt["state"] = "verifying"
        lib.save_json(task_dir / "review/upload.json", receipt)
        audit = fbr_cleanup.audit_batch(
            self.root, BATCH, task_dir.name, client=client)
        self.assertTrue(audit["tasks"][0]["eligible"], audit["tasks"][0]["reason"])
        self.assertEqual(lib.load_json(task_dir / "review/upload.json")["state"], "confirmed")

    def test_delivery_manifest_drift_blocks_cleanup(self):
        task_dir, client = self.submitted()
        (task_dir / "trace/ids.json").write_text("{}", encoding="utf-8")
        audit = fbr_cleanup.audit_batch(
            self.root, BATCH, task_dir.name, client=client)
        self.assertFalse(audit["tasks"][0]["eligible"])
        self.assertIn("变化", audit["tasks"][0]["reason"])

    def test_restore_frozen_projection_from_delivery_history(self):
        task_dir, _ = self.submitted()
        target = delivery.delivery_dir(self.root, BATCH)
        snapshot = self.root / "_verification/delivery-history" / f"{BATCH}-snapshot"
        shutil.copytree(target, snapshot)
        manifest = lib.load_json(target / "delivery-manifest.json")
        manifest["tasks"] = [row for row in manifest["tasks"] if row["task_id"] != task_dir.name]
        manifest["excluded"].append({"task_id": task_dir.name, "reason": "policy changed"})
        lib.save_json(target / "delivery-manifest.json", manifest)
        shutil.rmtree(target / task_dir.name)
        result = fbr_cleanup.restore_frozen_projection(self.root, BATCH, task_dir.name)
        self.assertEqual([row["task_id"] for row in result["restored"]], [task_dir.name])
        self.assertTrue((target / task_dir.name / "trace/ids.json").is_file())
        restored = lib.load_json(target / "delivery-manifest.json")
        self.assertIn(task_dir.name, [row["task_id"] for row in restored["tasks"]])

    def test_hydrated_clean_task_can_rebuild_missing_projection(self):
        task_dir, client = self.submitted()
        backup = self.root / "_verification" / "workspace-backup"
        shutil.copytree(task_dir / "workspace", backup, symlinks=True)
        fbr_cleanup.prune_batch(
            self.root, BATCH, task_dir.name, execute=True, confirm="CLEANUP", client=client)
        target = delivery.delivery_dir(self.root, BATCH)
        manifest = lib.load_json(target / "delivery-manifest.json")
        manifest["tasks"] = [
            row for row in manifest["tasks"] if row.get("task_id") != task_dir.name
        ]
        lib.save_json(target / "delivery-manifest.json", manifest)
        shutil.copytree(backup, task_dir / "workspace", symlinks=True)
        rebuilt = delivery.export_batch(self.root, BATCH, self.cfg)
        self.assertIn(task_dir.name, [row["task_id"] for row in rebuilt["tasks"]])
        self.assertTrue((target / task_dir.name / "trace/ids.json").is_file())


if __name__ == "__main__":
    unittest.main()
