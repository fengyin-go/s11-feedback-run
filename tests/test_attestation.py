"""Frozen review attestations and the lightweight submission closeout."""
from __future__ import annotations

import unittest
from unittest import mock

from fixtures import BATCH
from flow import TEXT_B
from test_submit import FakeClient, SubmitCase

import fbr_attestation
import fbr_delivery
import fbr_feedback
import fbr_submit
import fbrlib as lib


class Attestation(SubmitCase):
    def closed(self, task):
        manifest = fbr_delivery.export_batch(self.root, BATCH, self.cfg)
        result = fbr_attestation.batch_closeout(
            self.root, BATCH, self.cfg, task.name, manifest=manifest)
        self.assertEqual(result["state"], "ready", result["errors"])
        return manifest

    def test_batch_closeout_materializes_once_and_preflight_skips_feedback_gate(self):
        task = self.ready()
        self.closed(task)
        attestation = lib.load_json(task / "review/attestation.json")
        self.assertEqual(attestation["state"], "accepted")
        self.assertTrue(attestation["source_digest"])
        self.assertTrue(attestation["content_digest"])
        with mock.patch.object(
                fbr_feedback, "gate_batch",
                side_effect=AssertionError("提交预检不应重新评审")):
            result = fbr_submit.preflight_one(
                self.root, BATCH, task.name, client=FakeClient())
        self.assertTrue(result["ready"], result["errors"])

    def test_review_input_change_requests_review(self):
        task = self.ready()
        self.closed(task)
        lib.save_json(task / "task-seed.json",
                      {"task_id": task.name, "review_note": "changed after review"})
        result = fbr_attestation.validate(task, self.root, self.cfg)
        self.assertFalse(result["valid"])
        self.assertEqual(result["state"], "review_changed")

    def test_delivery_change_only_requires_a_delivery_refresh(self):
        task = self.ready()
        self.closed(task)
        snapshot = lib.load_json(task / "review/snapshot.json")
        snapshot["verification_note"] = "delivery-only refresh"
        lib.save_json(task / "review/snapshot.json", snapshot)
        result = fbr_attestation.validate(task, self.root, self.cfg)
        self.assertFalse(result["valid"])
        self.assertEqual(result["state"], "delivery_changed")
        manifest = fbr_delivery.export_batch(self.root, BATCH, self.cfg)
        refreshed = fbr_attestation.refresh_delivery(
            task, self.root, self.cfg, manifest=manifest)
        self.assertNotEqual(refreshed["delivery_digest"], result["attestation"]["delivery_digest"])
        self.assertTrue(fbr_attestation.validate(
            task, self.root, self.cfg, manifest=manifest)["valid"])

    def test_rolling_submit_context_reuses_login_and_history_read(self):
        first = self.ready(1)
        second = self.ready(2, TEXT_B)
        ids_path = second / "trace/ids.json"
        ids = lib.load_json(ids_path)
        ids["session_id"] = "s11-test-session-context-2"
        lib.save_json(ids_path, ids)
        self.refresh_source_fixture(second)
        self.review_receipt(second)
        client = FakeClient()
        context = fbr_submit.SubmitContext()
        for task in (first, second):
            result = fbr_submit.submit_ready(
                self.root, BATCH, task.name, execute=True, confirm="SUBMIT",
                client=client, interval=0, context=context)
            self.assertEqual(result["state"], "confirmed", result.get("errors"))
        self.assertEqual(client.logins, 1)


if __name__ == "__main__":
    unittest.main()
