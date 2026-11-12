"""Retry quotas follow terminal evidence and never forgive legacy counters."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import fixtures  # Installs this isolated runner's scripts directory on sys.path.
import fbr_retry as retry


class RetryPolicy(unittest.TestCase):
    def test_old_timeout_with_retry_history_still_consumes_model(self):
        record = {"n": 1, "timed_out": True, "reason": "进程超时",
                  "retry_events": [{"error_status": 504}], "api_retry_seen": True}
        original = copy.deepcopy(record)
        result = retry.ledger([record])
        self.assertEqual(result["physical"], 1)
        self.assertEqual(result["model"], 1)
        self.assertEqual(result["infra"], 0)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(record, original)

    def test_terminal_payload_classifies_real_error_not_numbers_or_tool_logs(self):
        self.assertEqual(retry.classify_attempt({"detail": "estimated_tokens=504"})["category"], "unknown")
        self.assertEqual(retry.classify_attempt({"tool_output": "API Error: 504"})["category"], "unknown")
        self.assertEqual(retry.classify_attempt({"detail": "API Error: 504 Gateway Time-out"})["category"], "throttle")
        self.assertEqual(retry.classify_attempt({"terminal_error": {"status_code": 429, "message": "limited"}})["category"], "throttle")
        for status in (500, 502, 503, 504, 529):
            self.assertEqual(retry.classify_attempt({"detail": f"API Error: {status}"})["category"], "throttle")

    def test_protocol_400_cannot_be_blindly_retried(self):
        a = {"detail": 'API Error: 400 unsupported Anthropic Messages input: field "context_management" is not supported by the Responses API'}
        self.assertEqual(retry.classify_attempt(a)["action"], "stop")
        self.assertFalse(retry.admission([a], {})["allowed"])
        self.assertEqual(retry.admission([a], {})["reason"], "api_protocol_context_management")

    def test_success_after_recovered_retry_is_model_not_infra(self):
        a = {"verdict": "accept", "retry_events": [{"error_status": 504}], "detail": "API Error: 504"}
        self.assertEqual(retry.classify_attempt(a)["category"], "model")

    def test_explicit_monitor_reason_beats_recovered_gateway_error(self):
        for reason in ("timeout", "stalled", "task_budget"):
            d = retry.classify_attempt({"termination_reason": reason, "detail": "API Error: 504"})
            self.assertEqual(d["category"], "model")
        self.assertEqual(retry.classify_attempt({"termination_reason": "gateway_wait_exhausted"})["category"], "infra")
        self.assertEqual(retry.classify_attempt({"termination_reason": "batch_budget"})["action"], "defer")

    def test_explicit_reconciliation_and_budget_stops_do_not_consume_model(self):
        old = {"timed_out": True, "reason": "进程超时", "failure_category": "infra"}
        self.assertEqual(retry.classify_attempt(old)["category"], "infra")
        for reason in ("batch_budget", "worker_guard"):
            record = {"termination_reason": reason}
            counts = retry.ledger([record])
            self.assertEqual((counts["physical"], counts["model"], counts["infra"], counts["budget"]), (1, 0, 0, 1))
            self.assertTrue(retry.admission([record], {})["allowed"])
        self.assertEqual(retry.classify_attempt({"failure_category": "budget"})["action"], "defer")

    def test_model_and_physical_authorization_caps_are_independent(self):
        infra = {"termination_reason": "worker_crash"}
        model = {"termination_reason": "timeout"}
        self.assertTrue(retry.admission([infra, infra, model], {"max_attempts": 3})["allowed"])
        self.assertEqual(retry.admission([infra, infra, model], {"max_physical_attempts": 3})["reason"], "max_physical_attempts")
        self.assertEqual(retry.admission([model] * 3, {})["reason"], "max_attempts")
        self.assertEqual(retry.admission([{"reason": "进程超时"}] * 3, {})["reason"], "max_attempts")

    def test_infra_throttle_default_and_configured_caps(self):
        infra = {"termination_reason": "worker_crash"}
        throttle = {"api_error": {"status": 429}}
        self.assertTrue(retry.admission([infra] * 2, {})["allowed"])
        self.assertEqual(retry.admission([infra] * 3, {})["reason"], "max_infra_attempts")
        self.assertTrue(retry.admission([throttle] * 7, {})["allowed"])
        self.assertEqual(retry.admission([throttle] * 8, {})["reason"], "max_throttle_attempts")
        self.assertTrue(retry.admission([infra] * 3, {"run": {"max_infra_attempts": 4}})["allowed"])

    def test_old_protocol_stop_does_not_block_new_authorized_attempt(self):
        protocol = {"detail": "API Error: 400 context_management not supported"}
        newer = {"termination_reason": "worker_crash"}
        self.assertTrue(retry.admission([protocol, newer], {})["allowed"])
        self.assertEqual(retry.admission([newer, protocol], {})["reason"], "api_protocol_context_management")

    def test_physical_cap_still_applies_after_budget_defer(self):
        records = [{"termination_reason": "batch_budget"}] * 4
        self.assertEqual(retry.admission(records, {"max_physical_attempts": 4})["reason"], "max_physical_attempts")

    def test_push_and_preparation_do_not_consume_generation_quota(self):
        counts = retry.ledger([{"stage": "push", "state": "failed"}, {"dispatched": False},
                               {"termination_reason": "timeout"}])
        self.assertEqual((counts["physical"], counts["model"], counts["push"]), (1, 1, 1))

    def test_invalid_caps_do_not_silently_grant_attempts(self):
        for value in (-1, True, 1.5, "3"):
            with self.assertRaises(ValueError):
                retry.admission([], {"max_attempts": value})
        self.assertFalse(retry.admission([], {"max_attempts": 0})["allowed"])

    def test_waiting_across_segments_and_monitor_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            current = {"attempt": 2, "state": "starting"}
            retry.ensure_attempt_waiting_base(task, current, {"waiting_seconds": 1200})
            self.assertEqual(retry.attempt_waiting(current, 1250), 50)
            continuation = {"attempt": 2, "request_id": "next-segment", "segment": 1}
            retry.ensure_attempt_waiting_base(task, continuation, {"waiting_seconds": 1250})
            self.assertEqual(continuation["attempt_waiting_base"], 1200)
            self.assertEqual(retry.attempt_waiting(continuation, {"waiting_seconds": 1375}), 175)
            self.assertFalse((task / "run/budget.json").exists())
            self.assertEqual(json.loads((task / "run/current.json").read_text())["attempt_waiting_base"], 1200)

    def test_legacy_dispatched_baseline_must_not_reset_elapsed_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            with self.assertRaisesRegex(ValueError, "recovered"):
                retry.ensure_attempt_waiting_base(task, {"attempt": 1, "pid": 123}, {"waiting_seconds": 2700})
            current = {"attempt": 1, "pid": 123, "attempt_waiting_base": 0}
            retry.ensure_attempt_waiting_base(task, current, {"waiting_seconds": 2700})
            self.assertEqual(retry.attempt_waiting(current, 2700), 2700)
        with self.assertRaises(ValueError):
            retry.attempt_waiting({}, 20)
        with self.assertRaises(ValueError):
            retry.attempt_waiting({"attempt_waiting_base": 30}, 20)


if __name__ == "__main__":
    unittest.main()
