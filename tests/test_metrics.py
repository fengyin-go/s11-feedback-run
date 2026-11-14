"""Batch token/cost accounting separates product, reviewer and feedback workflow."""
from __future__ import annotations

import json

from fixtures import Sandbox, write

import fbr_metrics
import fbrlib as lib


class Metrics(Sandbox):
    def test_aggregate_separates_product_reviewer_and_feedback_costs(self):
        task = self.make_task()
        write(task/"run/stream.jsonl", "\n".join([
            json.dumps({"type": "assistant", "message": {"content": "work"}}),
            json.dumps({"type": "result", "num_turns": 4, "duration_ms": 1000,
                        "total_cost_usd": 1.25, "usage": {
                            "input_tokens": 100, "output_tokens": 40,
                            "cache_read_input_tokens": 900}}),
        ])+"\n")
        lib.save_json(task/"review/metrics.json", {"stages": {"trace_analysis": {
            "prompt_bytes": 5000, "response_bytes": 600, "pack_bytes": 3000,
            "raw_trace_bytes": 50000,
            "usage": {"input_tokens": 200, "output_tokens": 50,
                      "reasoning_output_tokens": 10}}}})
        lib.save_json(task/"review/task-agent-history.json", {"attempts": [
            {"candidate_sha": "same", "errors": ["bad"]},
            {"candidate_sha": "same", "errors": []},
        ]})
        lib.save_json(task/"run/attempts.json", [
            {"n": 1, "verdict": "retry", "reason": "网关报错"},
            {"n": 2, "verdict": "accept", "reason": ""},
        ])
        report = fbr_metrics.aggregate(self.root, task.parent.name)
        self.assertEqual(report["product_model"]["input_tokens"], 100)
        self.assertEqual(report["product_model"]["output_tokens"], 40)
        self.assertEqual(report["product_model"]["cache_read_input_tokens"], 900)
        self.assertEqual(report["product_model"]["cost_usd"], 1.25)
        self.assertEqual(report["reviewer_model"]["input_tokens"], 200)
        self.assertEqual(report["reviewer_model"]["output_tokens"], 50)
        self.assertEqual(report["feedback_workflow"]["attempts"], 2)
        self.assertEqual(report["feedback_workflow"]["duplicate_candidate_attempts"], 1)
        self.assertEqual(report["run_workflow"]["attempts"], 2)
        self.assertEqual(report["run_workflow"]["retries"], 1)
        self.assertEqual(report["run_workflow"]["reasons"]["网关报错"], 1)
        self.assertTrue(report["report"].endswith(f"{task.parent.name}.json"))
