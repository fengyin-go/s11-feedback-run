"""fbr_feedback：素材卡、实跑记录、单题闸门、整批闸门（查重、抽检退回、5 分占比）。

容器是假的（run_steps_in_image 被换掉），git 用临时目录里的真实本地仓库。
"""

from __future__ import annotations

import unittest

from fixtures import BATCH, IMAGE, KEY, PROMPT
from flow import P_LONG, P_ZERO, TEXT_A, TEXT_B, FlowCase, audit, feedback, run_main
from test_gate import facts as gate_facts, task as gate_task
from test_record_facts import BAD, GOOD

import fbrlib as lib
import fbr_feedback


class Card(FlowCase):
    def card(self, task_dir) -> str:
        self.assertEqual(fbr_feedback.card_one(task_dir),
                         {"task_id": task_dir.name, "state": "ok", "detail": "review/card.md"})
        return (task_dir / "review" / "card.md").read_text(encoding="utf-8")

    def test_sections(self):
        text = self.card(self.evidenced())
        for line in (
            "# s11-000001 素材卡", PROMPT,
            "- R1：pack 支持 --segment 分片（怎么验：python3 -m clipcut pack fixtures/short.mp4 --segment 10）",
            "- 命令：`make setup` 然后 `make test`",
            "- 初始提交：setup 退出码 0，test 退出码 0，通过 1，失败 0",
            "- 产物提交：setup 退出码 0，test 退出码 0，通过 1，失败 0",
            "- 新增失败：无；新修好：无",
            "- 结论口径：产物测试通过，跑得起来，不算这次改动造成的失败",
            "1. `python3 -m clipcut pack fixtures/short.mp4 --segment 10` → 退出码 0；要看的：生成 6 个分片",
            "2. `make test` → 退出码 2；要看的：测试通过",
            "- 2 个文件，+3 / -0；模型自己提交 0 次\n",
            "  - M src/clipcut/packer.py (+1 / -0)\n",
            "  - A src/clipcut/segment.py (+2 / -0)\n",
            "- 新增的 TODO 或占位：src/clipcut/packer.py：# TODO: 一小时以上的输入",
            "- 完整 diff 在 review/diff.patch",
            "- 共 4 步，其中报错 0 步，改文件 2 步",
            "- 模型自己跑过测试：是；最后一次改动在第 4 步，之后没有再验证；最后一次检查通过",
            "  - 第 3 步 test：`make test` → 通过",
            "    3 Bash   make test",
            "改了 src/clipcut/packer.py，新增 src/clipcut/segment.py 和 docs/usage.md。",
            "- 回答里提到、产物里没有的文件：docs/usage.md",
            "还没有。验证需求点和试边界用 `fbr_feedback.py probe`",
            '{"type": "missing", "path": "docs/x.md"}',
            "打分口径见 references/scoring.md，正文写法见 references/feedback-writing.md。",
        ):
            self.assertIn(line, text)
        self.assertNotIn("产物上测试输出的末尾", text)
        self.assertNotIn("这次没改的文件", text)
        self.assertNotIn(KEY, text)

    def test_failed_tests_show_the_output(self):
        text = self.card(self.evidenced(tests=[GOOD, BAD]))
        self.assertIn("- 产物提交：setup 退出码 0，test 退出码 1，通过 1，失败 1", text)
        self.assertIn("- 新增失败：test_long (tests.test_packer.T)；新修好：无", text)
        self.assertIn("- 结论口径：产物测试不通过，跑得起来，算在这次改动头上", text)
        self.assertIn("产物上测试输出的末尾：", text)
        self.assertIn("FAILED (failures=1)", text)

    def test_no_change(self):
        text = self.card(self.evidenced(change=False))
        self.assertIn("- 0 个文件，+0 / -0；模型自己提交 0 次；相对初始提交没有任何改动", text)
        self.assertIn("- 回答里提到、产物里没有的文件：src/clipcut/segment.py、docs/usage.md", text)
        self.assertIn("- 回答里提到、这次没改的文件：src/clipcut/packer.py", text)

    def test_demo_states(self):
        recording = {"setup_ok": False, "setup_runs": [{"attempt": 1, "rc": 1, "tail": "pip: no matching distribution"}],
                     "steps": [{"cmd": "make demo", "show": "", "rc": 124, "seconds": 60, "cut": True, "tail": "…",
                                "skipped": False},
                               {"cmd": "make test", "show": "测试通过", "rc": None, "seconds": None, "cut": False,
                                "tail": "", "skipped": True}]}
        text = self.card(self.evidenced(recording=recording))
        self.assertIn("- 准备命令没成功：pip: no matching distribution", text)
        self.assertIn("1. `make demo` → 退出码 124，被截断；要看的：未写", text)
        self.assertIn("2. `make test` → 没跑到；要看的：测试通过", text)

    def test_probes_are_listed(self):
        task_dir = self.evidenced()
        self.probe(task_dir, *P_ZERO)
        self.probe(task_dir, *P_LONG)
        text = self.card(task_dir)
        self.assertIn("- P1：`python3 -m clipcut pack fixtures/short.mp4 --segment 0` → 退出码 2", text)
        self.assertIn("- P2：`python3 -m clipcut pack fixtures/long.mp4 --segment 10` → 退出码 1", text)
        self.assertIn("RuntimeError: input longer than 3600s is not supported", text)
        self.assertNotIn("还没有。", text)

    def test_needs_facts(self):
        task_dir = self.prepared()
        with self.assertRaisesRegex(lib.FbrError, "s11-000001：facts 阶段还没通过，不能往下做"):
            fbr_feedback.card_one(task_dir)
        self.assertFalse((task_dir / "review" / "card.md").exists())

    def test_change_flags_and_step_errors(self):
        facts = gate_facts()
        facts["changes"].update(tests_deleted=["tests/test_packer.py"], tests_weakened=["tests/test_a.py"],
                                skips_added=[{"file": "tests/test_b.py", "line": "@pytest.mark.skip"}],
                                history_rewritten=True, model_commits=2)
        text = fbr_feedback.build_card(gate_task(), facts, [])
        for line in ("- 3 个文件，+136 / -6；模型自己提交 2 次；历史被改写过\n",
                     "  - A tests/test_segment.py (+37 / -0) [测试]",
                     "- 被删的测试：tests/test_packer.py", "- 删得比加得多的测试：tests/test_a.py",
                     "- 新加的跳过标记：tests/test_b.py：@pytest.mark.skip",
                     "    4 Bash   make test  ✗ AssertionError: 4 != 3",
                     "最后一次改动在第 5 步，之后又验证过；最后一次检查通过"):
            self.assertIn(line, text)
        facts["verification"].update(tests_run=False, last_check_passed=False, verified_after_last_edit=False)
        text = fbr_feedback.build_card(gate_task(), facts, [])
        self.assertIn("- 模型自己跑过测试：否；最后一次改动在第 5 步，之后没有再验证；最后一次检查不存在", text)
        facts["verification"]["checks"] = [{"step": 4, "kind": "test", "cmd": "make test", "passed": False, "error": ""}]
        text = fbr_feedback.build_card(gate_task(), facts, [])
        self.assertIn("最后一次检查没通过", text)
        self.assertIn("  - 第 4 步 test：`make test` → 没通过", text)


class Probe(FlowCase):
    def test_runs_on_a_clean_clone_of_the_result(self):
        task_dir = self.evidenced()
        commit = self.facts(task_dir)["commit"]
        self.step_calls.clear()
        row = self.probe(task_dir, *P_ZERO)
        self.assertEqual({k: v for k, v in row.items() if k not in ("at", "container_name", "clone")},
                         {"id": "P1", "cmd": P_ZERO[0], "rc": 2, "seconds": 1, "tail": P_ZERO[2], "setup_rc": 0,
                          "commit": commit})
        call, = self.step_calls
        self.assertEqual((call["image"], call["tail"], call["head"]), (IMAGE, 80, commit))
        self.assertEqual(call["steps"], [("setup", "make setup", 900), ("probe", P_ZERO[0], 180)])
        self.assertEqual((call["segment"], call["env"]), (True, False))      # 有产物的文件，没有被忽略的 .env
        self.assertNotEqual(call["workdir"], task_dir / "workspace")
        self.assertTrue(call["workdir"].parent.name.startswith("s11-probe-s11-000001-"))
        self.assertTrue(call["workdir"].parent.exists())                     # 保留容器的挂载目录供核查
        self.assertEqual(row["clone"], str(call["workdir"]))
        self.assertEqual(fbr_feedback.load_probes(task_dir), [row])

    def test_no_setup(self):
        task_dir = self.evidenced()
        self.step_calls.clear()
        row = self.probe(task_dir, "ls src", 0, "clipcut", with_setup=False)
        self.assertEqual((row["setup_rc"], row["rc"]), (None, 0))
        self.assertEqual(self.step_calls[0]["steps"], [("probe", "ls src", 180)])

    def test_ids_increase(self):
        task_dir = self.evidenced()
        self.assertEqual([self.probe(task_dir, *P_ZERO)["id"], self.probe(task_dir, *P_LONG)["id"]], ["P1", "P2"])
        self.assertEqual([r["id"] for r in fbr_feedback.load_probes(task_dir)], ["P1", "P2"])

    def test_key_is_redacted_and_tail_is_cut(self):
        task_dir = self.evidenced()
        row = self.probe(task_dir, "env", 0, f"apikey={KEY}\nauth {KEY} end")
        self.assertNotIn(KEY, row["tail"])
        self.assertIn("auth *** end", row["tail"])
        self.assertNotIn(KEY, (task_dir / "evidence" / "probes.jsonl").read_text(encoding="utf-8"))
        row = self.probe(task_dir, "yes", 0, "x" * 5000)
        self.assertEqual(len(row["tail"]), 3000)

    def test_empty_command(self):
        task_dir = self.evidenced()
        self.step_calls.clear()
        with self.assertRaisesRegex(lib.FbrError, "--cmd 不能为空"):
            fbr_feedback.probe_one(task_dir, self.cfg, "   ", 180)
        self.assertEqual(self.step_calls, [])
        self.assertEqual(fbr_feedback.load_probes(task_dir), [])

    def test_needs_facts(self):
        task_dir = self.prepared()
        with self.assertRaisesRegex(lib.FbrError, "facts 阶段还没通过"):
            fbr_feedback.probe_one(task_dir, self.cfg, "make test", 180)
        self.assertEqual(self.step_calls, [])

    def test_clone_is_retained_when_the_run_breaks(self):
        task_dir = self.evidenced()
        self.step_calls.clear()
        self.results = []                      # 假容器没有结果可给，run_steps_in_image 会抛异常
        with self.assertRaises(IndexError):
            fbr_feedback.probe_one(task_dir, self.cfg, "make test", 180)
        self.assertTrue(self.step_calls[0]["workdir"].parent.exists())
        self.assertEqual(fbr_feedback.load_probes(task_dir), [])


class GateTask(FlowCase):
    def test_missing_files(self):
        task_dir = self.evidenced()
        result = fbr_feedback.gate_task(task_dir, self.cfg)
        self.assertEqual((result["ready"], result["score"], result["text"]), (False, None, ""))
        self.assertEqual(result["errors"], ["还没写：evidence/audit.json、review/feedback.json"])
        self.put(task_dir, audit_doc=audit())
        self.assertEqual(fbr_feedback.gate_task(task_dir, self.cfg)["errors"], ["还没写：review/feedback.json"])

    def test_not_objects(self):
        task_dir = self.evidenced()
        self.put(task_dir, audit_doc=[1], feedback_doc=feedback())
        result = fbr_feedback.gate_task(task_dir, self.cfg)
        self.assertEqual((result["ready"], result["errors"]), (False, ["audit.json 和 feedback.json 都要是 JSON 对象"]))

    def test_pass(self):
        task_dir = self.written()
        result = fbr_feedback.gate_task(task_dir, self.cfg)
        self.assertEqual(result, {"task_id": "s11-000001", "ready": True, "errors": [], "warnings": [], "score": 3,
                                  "text": TEXT_A, "commit": self.facts(task_dir)["commit"]})

    def test_all_four_checks_run(self):
        task_dir = self.written()
        bad_audit, bad_doc = audit(), feedback(score=5)
        del bad_audit["requirements"][0]
        bad_doc["feedback"] += "我看过了。"
        self.put(task_dir, audit_doc=bad_audit, feedback_doc=bad_doc)
        errors = fbr_feedback.gate_task(task_dir, self.cfg)["errors"]
        self.assertIn("需求点 R1 没有结论", errors)                                    # check_audit
        self.assertTrue(any(e.startswith("分数 5 超过证据允许的上限 3：") for e in errors))   # check_score
        self.assertIn("正文里不写验收人自己（我、我们、本人）", errors)                      # check_text
        self.assertIn("5 分不写缺口：issues 应当为空", errors)                            # check_feedback

    def test_anchors_are_checked_against_this_task(self):
        task_dir = self.written()
        doc = feedback()
        doc["issues"][0]["anchors"] += [{"type": "step", "step": 9}, {"type": "probe", "id": "P3"}]
        self.put(task_dir, feedback_doc=doc)
        errors = fbr_feedback.gate_task(task_dir, self.cfg)["errors"]
        self.assertEqual(errors, ["第 1 个问题句：轨迹里没有第 9 步", "第 1 个问题句：没有编号 P3 的实跑记录"])

    def test_failing_tests_must_be_written(self):
        task_dir = self.evidenced(tests=[GOOD, BAD])
        self.probe(task_dir, *P_ZERO)
        self.probe(task_dir, *P_LONG)
        self.put(task_dir, audit_doc=audit(), feedback_doc=feedback())
        self.assertEqual(fbr_feedback.gate_task(task_dir, self.cfg)["errors"], ["产物上构建或测试不通过，正文必须明写"])
        doc = feedback()
        doc["feedback"] += "产物上再跑 make test，test_long 这条用例失败。"
        self.put(task_dir, feedback_doc=doc)
        self.assertEqual(fbr_feedback.gate_task(task_dir, self.cfg)["errors"], [])


class GateBatch(FlowCase):
    def gate(self, wanted=None):
        return fbr_feedback.gate_batch(self.root, BATCH, self.cfg, wanted)

    def gate_file(self, task_dir) -> dict:
        return lib.load_json(task_dir / "review" / "gate.json")

    def test_pass_writes_gate_and_stage(self):
        task_dir = self.written()
        rows, notes = self.gate()
        self.assertEqual(rows, [{"task_id": "s11-000001", "state": "ok", "score": 3, "errors": [], "warnings": []}])
        self.assertEqual(notes, [])
        saved = self.gate_file(task_dir)
        self.assertEqual(set(saved), {"task_id", "passed", "score", "errors", "warnings", "feedback_sha", "commit",
                                      "checked_at"})
        self.assertEqual((saved["passed"], saved["score"], saved["errors"], saved["feedback_sha"], saved["commit"]),
                         (True, 3, [], fbr_feedback.sha(TEXT_A), self.facts(task_dir)["commit"]))
        stage = self.stage(task_dir, "feedback")
        self.assertEqual((stage["state"], stage["score"], stage["feedback_sha"]), ("ok", 3, fbr_feedback.sha(TEXT_A)))

    def test_not_written_yet(self):
        task_dir = self.evidenced()
        rows, _ = self.gate()
        self.assertEqual([(r["state"], r["score"], r["errors"]) for r in rows],
                         [("todo", None, ["还没写：evidence/audit.json、review/feedback.json"])])
        self.assertFalse(self.gate_file(task_dir)["passed"])
        self.assertEqual(self.stage(task_dir, "feedback"), {})          # 还没开始写，不算失败

    def test_failed(self):
        task_dir = self.written(score=5)
        rows, _ = self.gate()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("5 分不写缺口：issues 应当为空", rows[0]["errors"])
        self.assertEqual(self.gate_file(task_dir)["errors"], rows[0]["errors"])
        stage = self.stage(task_dir, "feedback")
        self.assertEqual((stage["state"], stage["problems"]), ("failed", rows[0]["errors"][:20]))

    def test_passed_then_changed_for_the_worse(self):
        task_dir = self.gated()
        self.put(task_dir, feedback_doc=feedback(score=4))
        self.assertEqual(self.gate()[0][0]["state"], "failed")
        self.assertEqual(self.stage(task_dir, "feedback")["state"], "failed")
        (task_dir / "review" / "feedback.json").unlink()                # 过过闸门之后文件没了，也要记成没过
        self.assertEqual(self.gate()[0][0]["state"], "todo")
        self.assertEqual(self.stage(task_dir, "feedback")["state"], "failed")
        self.put(task_dir, feedback_doc=feedback())
        self.assertEqual(self.gate()[0][0]["state"], "ok")
        self.assertEqual(self.stage(task_dir, "feedback")["state"], "ok")

    def test_only_tasks_with_facts(self):
        self.prepared(1)
        self.written(2)
        self.assertEqual([r["task_id"] for r in self.gate()[0]], ["s11-000002"])

    def test_wanted(self):
        self.written(1, TEXT_A)
        second = self.written(2, TEXT_B)
        self.assertEqual([(r["task_id"], r["state"]) for r in self.gate("s11-000002")[0]], [("s11-000002", "ok")])
        self.assertFalse((second.parent / "s11-000001" / "review" / "gate.json").exists())
        with self.assertRaisesRegex(lib.FbrError, "批次里没有这些任务：s11-000009"):
            self.gate("s11-000009")

    def test_different_wording_passes(self):
        self.written(1, TEXT_A)
        self.written(2, TEXT_B)
        self.assertEqual([(r["state"], r["errors"]) for r in self.gate()[0]], [("ok", []), ("ok", [])])

    def test_same_text_in_the_batch(self):
        self.written(1, TEXT_A)
        self.written(2, TEXT_A)
        first, second = self.gate()[0]
        self.assertEqual((first["state"], second["state"]), ("failed", "failed"))
        self.assertTrue(any(e.startswith("和 s11-000002 有 ") for e in first["errors"]))
        self.assertTrue(any(e.startswith("和 s11-000001 有 ") for e in second["errors"]))
        self.assertTrue(any(e.startswith("整句和 s11-000001 重复：") for e in second["errors"]))

    def test_unselected_tasks_still_count_for_duplicates(self):
        first = self.written(1, TEXT_A)
        self.written(2, TEXT_A)
        row, = self.gate("s11-000002")[0]
        self.assertEqual(row["state"], "failed")
        self.assertTrue(any(e.startswith("和 s11-000001 有 ") for e in row["errors"]))
        self.assertFalse((first / "review" / "gate.json").exists())

    def test_delivered_history(self):
        self.written()
        registry = self.root / "_registry" / "delivered-feedback.jsonl"
        lib.append_jsonl(registry, {"task_id": "s11-000001", "feedback": TEXT_A})      # 自己交过的同一题不算
        lib.append_jsonl(registry, {"task_id": "s11-000800"})                          # 没正文的行跳过
        self.assertEqual(self.gate()[0][0]["state"], "ok")
        lib.append_jsonl(registry, {"task_id": "s11-000900", "feedback": TEXT_A})
        row, = self.gate()[0]
        self.assertEqual(row["state"], "failed")
        self.assertTrue(any(e.startswith("和已交付的 s11-000900 有相同片段：") for e in row["errors"]))

    def test_other_batches(self):
        self.written()
        other = self.root / "batches" / "b1109-01" / "s11-000500"
        lib.save_json(other / "review" / "feedback.json", {"score": 3, "feedback": TEXT_A})
        self.assertEqual(self.gate()[0][0]["state"], "ok")              # 那一题自己还没过闸门，不拿来比
        lib.save_json(other / "state.json", {"stages": {"feedback": {"state": "ok"}}})
        self.assertEqual(fbr_feedback.other_feedback(self.root, BATCH), [{"task_id": "s11-000500", "feedback": TEXT_A}])
        row, = self.gate()[0]
        self.assertTrue(any(e.startswith("和已交付的 s11-000500 有相同片段：") for e in row["errors"]))

    def test_spotcheck_reject_blocks_until_the_text_changes(self):
        task_dir = self.gated()
        lib.save_json(task_dir / "review" / "spotcheck.json", {
            "task_id": task_dir.name, "state": "退回", "by": "测试员", "note": "第 4 步之后的描述不准",
            "feedback_sha": fbr_feedback.sha(TEXT_A)})
        row, = self.gate()[0]
        self.assertEqual((row["state"], row["errors"]), ("failed", ["抽检退回过（第 4 步之后的描述不准），正文还没改"]))
        self.put(task_dir, feedback_doc=feedback(TEXT_B))
        self.assertEqual(self.gate()[0][0]["state"], "ok")

    def test_five_ratio_note(self):
        self.gated()
        for number in (2, 3, 4):
            lib.save_json(self.make_task(number, repo=False) / "state.json",
                          {"stages": {"feedback": {"state": "ok", "score": 5}}})
        self.assertEqual(self.gate()[1], [])                             # 不到 5 条不提示
        lib.save_json(self.make_task(5, repo=False) / "state.json",
                      {"stages": {"feedback": {"state": "ok", "score": 5}}})
        rows, notes = self.gate()
        self.assertEqual([r["task_id"] for r in rows], ["s11-000001"])
        self.assertEqual(notes, ["这一批 5 分占 80%，超过 40%，回头重看 5 分的那几条"])


class Main(FlowCase):
    def run_cli(self, *argv):
        return run_main(fbr_feedback, "--root", str(self.root), *argv)

    def test_gate(self):
        self.written()
        code, out, err = self.run_cli("gate", "--batch", BATCH)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["s11-000001  通过  分数 3", "共 1 题，通过 1 题"])

    def test_gate_lists_problems(self):
        self.evidenced()
        code, out, _ = self.run_cli("gate", "--batch", BATCH)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["s11-000001  待写  分数 None",
                                            "    ✗ 还没写：evidence/audit.json、review/feedback.json", "共 1 题，通过 0 题"])

    def test_card(self):
        task_dir = self.evidenced()
        self.prepared(2)                                                 # 没取证的题不出卡
        code, out, _ = self.run_cli("card", "--batch", BATCH)
        self.assertEqual((code, out.splitlines()), (0, ["s11-000001  ok  review/card.md"]))
        self.assertTrue((task_dir / "review" / "card.md").is_file())

    def test_probe(self):
        task_dir = self.evidenced()
        self.results = [{"probe": (3, "boom")}]
        code, out, _ = self.run_cli("probe", "--batch", BATCH, "--task", task_dir.name, "--cmd", "make demo",
                                    "--no-setup", "--timeout", "30")
        self.assertEqual((code, out.splitlines()), (0, ["P1  退出码 3  1 秒", "boom"]))
        self.assertEqual(self.step_calls[-1]["steps"], [("probe", "make demo", 30)])

    def test_errors_exit_2(self):
        code, out, err = run_main(fbr_feedback, "--root", str(self.tmp / "nowhere"), "gate", "--batch", BATCH)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("错误：找不到工作目录", err)


if __name__ == "__main__":
    unittest.main()
