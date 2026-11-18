from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fbr_platform_rules


class PlatformRulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="platform-rules-")
        self.addCleanup(self.temp.cleanup)
        self.batch = Path(self.temp.name) / "batch"
        self.task = self.batch / "repo-05-bugfix"
        (self.task / "trace").mkdir(parents=True)
        (self.task / "review").mkdir()

    def write(self, relative: str, value: dict) -> None:
        path = (self.task if not relative.startswith("batch:") else self.batch) / (
            relative.removeprefix("batch:")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_contract(self, *, expected_calls: int | None = None) -> None:
        workload = {
            "work_slices": [{}, {}, {}, {}],
            "verification_paths": ["path-a", "path-b"],
        }
        if expected_calls is not None:
            workload["expected_effective_tool_calls"] = expected_calls
        self.write("batch:repo-topic-map.json", {
            "difficulty_contract_version": 3,
            "task_plan": [{
                "sequence": 5,
                "difficulty_design": {
                    "version": 3,
                    "workload_design": workload,
                    "design_tradeoff": {
                        "false_h3_guards": {
                            name: False
                            for name in fbr_platform_rules.H3_FALSE_GUARDS
                        },
                    },
                },
            }],
        })
        self.write("batch:prompt-generation-manifest.json", {
            "gate_ok": True,
            "raw_a5_ok": True,
            "cross_project": {"platform_dedup_ok": True},
        })

    def write_pack(self, *, memory_target: str = "") -> None:
        events = []
        if memory_target:
            events.append({
                "tool": "Read",
                "target": memory_target,
                "result_preview": '{"sessionId":"x","type":"queue-operation"}',
            })
        self.write("trace/review-pack.json", {
            "summary": {"high_risk": []},
            "tool_events": events,
        })

    def test_tool_call_counts_are_neither_required_nor_reported(self):
        self.write_contract(expected_calls=1)
        self.write_pack()
        pack = json.loads((self.task / "trace/review-pack.json").read_text())
        pack["summary"] = {"paired_tools": 1, "logical_tool_uses": 1}
        (self.task / "trace/review-pack.json").write_text(
            json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        report = fbr_platform_rules.evaluate(self.task)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["errors"], [])
        self.assertNotIn("trace", report)

    def test_a5_failure_is_not_skipped_when_topic_map_is_missing(self):
        self.write(
            "batch:prompt-generation-manifest.json",
            {
                "gate_ok": False,
                "raw_a5_ok": False,
                "cross_project": {"platform_dedup_ok": False},
            },
        )
        self.write_pack()

        report = fbr_platform_rules.evaluate(self.task)

        self.assertEqual(report["state"], "blocked")
        codes = {item["code"] for item in report["errors"]}
        self.assertIn("prompt_topic_map_missing", codes)
        self.assertIn("raw_a5_failed", codes)
        self.assertIn("platform_dedup_failed", codes)
        self.assertIn("prompt_gate_failed", codes)

    def test_legacy_batch_may_omit_generation_sources(self):
        self.write_pack()

        report = fbr_platform_rules.evaluate(self.task, strict=False)

        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["errors"], [])
        warnings = {item["code"] for item in report["warnings"]}
        self.assertEqual(
            warnings,
            {"prompt_topic_map_missing", "prompt_generation_manifest_missing"},
        )

    def test_new_batch_requires_generation_sources(self):
        self.write_pack()

        report = fbr_platform_rules.evaluate(self.task, strict=True)

        self.assertEqual(report["state"], "blocked")
        codes = {item["code"] for item in report["errors"]}
        self.assertEqual(
            codes,
            {"prompt_topic_map_missing", "prompt_generation_manifest_missing"},
        )

    def test_private_runtime_paths_do_not_block_platform_rules(self):
        targets = (
            "/home/node/.claude/projects/-workspace/session.jsonl",
            "/home/node/.claude/projects",
            "/home/node/.claude/todos",
            "/home/node/.claude/sessions",
            "/home/node/.claude/history.jsonl",
            "/home/node/.claude/memory",
        )
        for target in targets:
            with self.subTest(target=target):
                self.write_contract()
                self.write_pack(memory_target=target)
                report = fbr_platform_rules.evaluate(self.task)
                self.assertEqual(report["state"], "ready")
                self.assertEqual(report["errors"], [])

    def test_review_pack_is_not_required_for_platform_rules(self):
        self.write_contract()

        report = fbr_platform_rules.evaluate(self.task)

        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])

    def test_legacy_h3_contract_blocks_new_runs(self):
        self.write("batch:repo-topic-map.json", {
            "difficulty_contract_version": 2,
            "task_plan": [{"sequence": 5, "difficulty_design": {"version": 2}}],
        })
        self.write_pack()
        report = fbr_platform_rules.evaluate(self.task)
        codes = {item["code"] for item in report["errors"]}
        self.assertIn("p5_contract_legacy", codes)
        self.assertIn("p5_h3_design_missing", codes)

    def test_clean_contract_and_trace_are_ready(self):
        self.write_contract()
        self.write_pack()
        report = fbr_platform_rules.evaluate(self.task)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["errors"], [])

    def test_opportunity_map_binding_is_not_a_runtime_prerequisite(self):
        self.write_contract()
        self.write_pack()
        manifest = json.loads(
            (self.batch / "prompt-generation-manifest.json").read_text(encoding="utf-8")
        )
        manifest.pop("prompt_opportunities", None)
        (self.batch / "prompt-generation-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(
            fbr_platform_rules.evaluate(self.task)["state"],
            "ready",
        )

        manifest["prompt_opportunities"] = {
            "path": "../prompt-opportunities/legacy.json",
            "sha256": "a" * 64,
        }
        (self.batch / "prompt-generation-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(
            fbr_platform_rules.evaluate(self.task)["state"],
            "ready",
        )

if __name__ == "__main__":
    unittest.main()
