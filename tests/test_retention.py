"""Bounded retention, stage compaction and workspace recovery."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from unittest import mock

from fixtures import BATCH, KEY, Sandbox, sh, write
from test_submit import SubmitCase

import fbr_cleanup
import fbr_delivery as delivery
import fbr_retention as retention
import fbrlib as lib


class LocalCompaction(SubmitCase):
    def sealed(self):
        task = self.ready()
        manifest = delivery.export_batch(self.root, BATCH, self.cfg)
        self.assertIn(task.name, {row["task_id"] for row in manifest["tasks"]})
        lib.save_json(task / "run/attempts.json", [{"verdict": "ok"}])
        lib.save_json(task / "run/key-identity.json",
                      {"sha256": hashlib.sha256(KEY.encode()).hexdigest()})
        (task / "origin").mkdir(exist_ok=True)
        write(task / "origin/cache.bin", "origin")
        write(task / "run/debug.log", "temporary run log")
        write(task / "record/attempts/old/raw.webm", "old candidate")
        return task

    def test_local_seal_compacts_rebuildable_data_and_keeps_evidence(self):
        task = self.sealed()
        result = fbr_cleanup.compact_local_task(
            self.root, BATCH, task, self.cfg)
        self.assertEqual(result["state"], "ok", result)
        self.assertFalse((task / "workspace").exists())
        self.assertFalse((task / "origin").exists())
        self.assertFalse((task / "run/debug.log").exists())
        self.assertFalse((task / "record/attempts").exists())
        self.assertTrue((task / "trace/ids.json").is_file())
        self.assertTrue((task / "record/recording.mp4").is_file())
        self.assertTrue((task / "evidence/facts.json").is_file())
        self.assertTrue((task / "review/feedback.json").is_file())
        cleanup = lib.load_json(task / "cleanup.json")
        self.assertEqual(cleanup["state"], "local_compacted")
        artifact = delivery.delivery_dir(self.root, BATCH) / task.name
        self.assertTrue((artifact / "retention.json").is_file())
        self.assertTrue((artifact / "cleanup.json").is_file())
        verified = fbr_cleanup.verify_batch(self.root, BATCH, task.name)
        self.assertTrue(verified["valid"], verified)

    def test_unpublished_snapshot_blocks_code_deletion(self):
        task = self.sealed()
        snapshot = lib.load_json(task / "review/snapshot.json")
        snapshot.update(pushed=False, artifact_snapshot="")
        lib.save_json(task / "review/snapshot.json", snapshot)
        result = fbr_cleanup.compact_local_task(
            self.root, BATCH, task, self.cfg)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue((task / "workspace").is_dir())
        self.assertTrue((task / "origin").is_dir())

    def test_compaction_never_re_exports_a_stale_manifest(self):
        task = self.sealed()
        manifest_path = delivery.delivery_dir(self.root, BATCH) / "delivery-manifest.json"
        manifest = lib.load_json(manifest_path)
        manifest["tasks"] = []
        lib.save_json(manifest_path, manifest)
        with mock.patch.object(delivery, "export_batch") as exported:
            result = fbr_cleanup.compact_local_task(
                self.root, BATCH, task, self.cfg)
        exported.assert_not_called()
        self.assertEqual(result["state"], "blocked")
        self.assertIn("拒绝为清理重新导出", result["reason"])
        self.assertTrue((task / "workspace").is_dir())

    def test_local_compacted_projection_restores_without_upload_receipt(self):
        task = self.sealed()
        self.assertEqual(
            fbr_cleanup.compact_local_task(self.root, BATCH, task, self.cfg)["state"],
            "ok",
        )
        target = delivery.delivery_dir(self.root, BATCH)
        snapshot = self.root / "_verification/delivery-history" / f"{BATCH}-local"
        shutil.copytree(target, snapshot)
        manifest = lib.load_json(target / "delivery-manifest.json")
        manifest["tasks"] = []
        lib.save_json(target / "delivery-manifest.json", manifest)
        shutil.rmtree(target / task.name)
        self.assertFalse((task / "review/upload.json").exists())
        result = fbr_cleanup.restore_frozen_projection(
            self.root, BATCH, task.name)
        self.assertEqual(len(result["restored"]), 1)
        self.assertTrue((target / task.name / "retention.json").is_file())

    def test_disabled_policy_never_deletes(self):
        task = self.sealed()
        cfg = {**self.cfg, "cleanup": {
            **self.cfg["cleanup"], "policy": "safe",
        }}
        result = fbr_cleanup.compact_local_task(self.root, BATCH, task, cfg)
        self.assertEqual(result["state"], "disabled")
        self.assertTrue((task / "workspace").is_dir())

    def test_missing_workspace_is_restored_from_snapshot_commit(self):
        task = self.sealed()
        snapshot = lib.load_json(task / "review/snapshot.json")
        self.assertTrue(snapshot["pushed"])
        remote = self.tmp / "remote.git"
        sh(self.tmp, "init", "--bare", "-q", str(remote))
        sh(task / "workspace", "remote", "add", "snapshot", str(remote))
        sh(task / "workspace", "push", "-q", "snapshot", f"{snapshot['commit']}:refs/heads/main")
        record = lib.load_task(task)
        record["publish"]["repo_url"] = remote.as_uri()
        lib.save_json(task / "task.json", record)
        shutil.rmtree(task / "workspace")
        lib.restore_git_config(task)
        self.assertEqual(sh(task / "workspace", "rev-parse", "HEAD"), snapshot["commit"])
        self.assertTrue((task / "workspace/.git").is_dir())


class StageCompaction(Sandbox):
    def test_successful_record_drops_candidates_but_keeps_formal_video(self):
        task = self.make_task()
        self.mark(task, "record")
        write(task / "record/recording.mp4", "video")
        write(task / "record/recording.json", '{"task_id":"s11-000001"}')
        write(task / "record/attempts/old/raw.webm", "candidate")
        write(task / "record/containers.json", '{"containers":[]}')
        result = retention.compact_record_attempts(self.root, task, self.cfg)
        self.assertEqual(result["state"], "ok", result)
        self.assertFalse((task / "record/attempts").exists())
        self.assertFalse((task / "record/containers.json").exists())
        self.assertEqual((task / "record/recording.mp4").read_text(), "video")

    def test_task_scratch_is_bounded_by_task_id(self):
        task = self.make_task()
        mine = self.root / "_verification/runtime-scratch" / f"s11-facts-{task.name}-one"
        other = self.root / "_verification/runtime-scratch" / "s11-facts-s11-000002-one"
        write(mine / "workspace/file", "mine")
        write(other / "workspace/file", "other")
        result = retention.compact_task_scratch(self.root, task, self.cfg)
        self.assertEqual(result["state"], "ok", result)
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())


class HistoryAndBatch(Sandbox):
    def test_history_keeps_requested_number_and_latest_snapshot(self):
        history = self.root / "_verification/delivery-history"
        old = history / f"{BATCH}-old"
        new = history / f"{BATCH}-new"
        write(old / "delivery-manifest.json", "{}")
        write(new / "delivery-manifest.json", "{}")
        os.utime(old, (time.time() - 10, time.time() - 10))
        os.utime(new, None)
        self.cfg["cleanup"]["history_keep"] = 1
        preview = fbr_cleanup.maintain_batch(
            self.root, BATCH, self.cfg, execute=False)
        self.assertEqual(preview["history"]["candidate_snapshots"], 1)
        self.assertGreater(preview["history"]["candidate_bytes"], 0)
        result = retention.trim_delivery_history(self.root, BATCH, self.cfg)
        self.assertEqual(result["state"], "compacted")
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue((self.root / "_verification/retention" / f"{BATCH}-history.json").is_file())

    def test_batch_terminal_purges_dependency_cache(self):
        task = self.make_task()
        lib.save_json(task / "cleanup.json", {"state": "local_compacted"})
        write(self.root / "_registry/dependencies/cache/tree/file", "deps")
        result = fbr_cleanup.maintain_batch(
            self.root, BATCH, self.cfg, execute=True)
        self.assertFalse((self.root / "_registry/dependencies").exists())
        self.assertIsNotNone(result["dependencies"])
        self.assertEqual(result["dependencies"]["state"], "purged")

    def test_selected_task_does_not_purge_cache_while_batch_is_unfinished(self):
        task = self.make_task(1)
        self.make_task(2)
        lib.save_json(task / "cleanup.json", {"state": "local_compacted"})
        write(self.root / "_registry/dependencies/cache/tree/file", "deps")
        result = fbr_cleanup.maintain_batch(
            self.root, BATCH, self.cfg, execute=True, tasks=task.name)
        self.assertTrue((self.root / "_registry/dependencies").is_dir())
        self.assertIsNone(result["dependencies"])
