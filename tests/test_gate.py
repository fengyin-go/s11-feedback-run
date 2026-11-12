"""fbr_gate：锚点核对、audit 检查、分数上下限、正文禁用项、结构与证据对照、查重。全是纯函数，用手写的假证据测。"""

from __future__ import annotations

import copy
import unittest

from fixtures import PROMPT, REQUIREMENTS

import fbrlib as lib
import fbr_gate as gate

GATE = copy.deepcopy(lib.DEFAULTS["feedback_gate"])
S1 = ("模型先读了 src/clipcut/packer.py 和现有测试，再把切分逻辑拆到 src/clipcut/segment.py，"
      "第 4 步跑 make test 时有一条用例没过，改完索引计算后第 6 步重跑，14 条全部通过。")
S1_PASS = "模型先读现有实现和测试，再写分片逻辑，随后运行完整测试，14 条全部通过。"
S2 = ("实际执行 pack fixtures/short.mp4 --segment 10，输出目录里有 3 个分片和 index.json，清单里的条目数和分片数一致；"
      "拿 3700 秒的长输入跑了一遍，生成 370 个分片，没有报错。")
S3 = "--segment 0 和负数都会打印 error 并以退出码 2 结束。"
S4 = ("有一处没处理：--segment abc 这种非数字参数直接抛出 ValueError 的调用栈，退出码是 1，没有给出可读的提示，"
      "使用的人得自己从调用栈里看出是参数写错了。")
TEXT4, TEXT5 = S1 + S2 + S3 + S4, S1_PASS + S2 + S3


def task() -> dict:
    return {"task_id": "s11-000001", "prompt": PROMPT, "requirements": copy.deepcopy(REQUIREMENTS)}


def side(**extra) -> dict:
    row = {"setup_rc": 0, "setup_tail": "", "test_rc": 0, "test_seconds": 1.2,
           "test_tail": "Ran 14 tests in 0.52s\n\nOK", "passed": 14, "failed": 0, "failed_names": []}
    row.update(extra)
    return row


def step(number, tool, target, kind, **extra) -> dict:
    row = {"step": number, "tool": tool, "target": target, "answered": True, "is_error": False, "error": "",
           "ts": f"2026-11-10T10:0{number}:00+0800", "kind": kind}
    row.update(extra)
    return row


def facts() -> dict:
    files = [
        {"path": "src/clipcut/packer.py", "status": "M", "added": 41, "deleted": 6, "binary": False, "is_test": False},
        {"path": "src/clipcut/segment.py", "status": "A", "added": 58, "deleted": 0, "binary": False, "is_test": False},
        {"path": "tests/test_segment.py", "status": "A", "added": 37, "deleted": 0, "binary": False, "is_test": True},
    ]
    return {
        "task_id": "s11-000001", "initial_commit": "a" * 40, "commit": "b" * 40,
        "demo": {"setup_ok": True, "setup_tail": "", "steps": [
            {"cmd": "python3 -m clipcut pack fixtures/short.mp4 --segment 10", "show": "分片和清单", "rc": 0,
             "cut": False, "skipped": False, "tail": "wrote 3 segments\nindex.json: 3 entries"},
            {"cmd": "python3 -m clipcut pack fixtures/short.mp4 --segment 0", "show": "报错退出", "rc": 2,
             "cut": False, "skipped": False, "tail": "error: --segment must be a positive number"}]},
        "tests": {"setup_cmd": "make setup", "test_cmd": "make test", "before": side(), "after": side(),
                  "before_ok": True, "after_ok": True, "after_runnable": True, "new_failures": [], "fixed": [],
                  "regression": False},
        "changes": {"no_change": False, "files": files, "totals": {"files": 3, "added": 136, "deleted": 6},
                    "tests_deleted": [], "tests_weakened": [], "tests_added": ["tests/test_segment.py"],
                    "skips_added": [], "placeholders": [], "model_commits": 0, "host_commit": True,
                    "history_rewritten": False},
        "trace": {"steps": [
            step(1, "Read", "src/clipcut/packer.py", "read"),
            step(2, "Edit", "src/clipcut/packer.py", "edit"),
            step(3, "Write", "src/clipcut/segment.py", "edit"),
            step(4, "Bash", "make test", "test", is_error=True, mutates=False,
                 error="AssertionError: 4 != 3", output_tail="FAILED (failures=1)"),
            step(5, "Edit", "src/clipcut/segment.py", "edit"),
            step(6, "Bash", "make test", "test", mutates=False, output_tail="Ran 14 tests in 0.52s OK"),
        ], "counts": {"tools": 6, "errors": 1, "edits": 3, "unanswered": 0},
            "started_at": "2026-11-10T10:00:00+0800", "ended_at": "2026-11-10T10:07:00+0800", "api_errors": []},
        "verification": {"checks": [], "last_edit_step": 5, "verified_after_last_edit": True,
                         "last_check_passed": True, "tests_run": True, "tests_failed": 1},
        "claims": {"final_answer": "改了 src/clipcut/packer.py，新增 src/clipcut/segment.py。",
                   "mentioned": ["src/clipcut/packer.py", "src/clipcut/segment.py"], "mentioned_missing": [],
                   "mentioned_unchanged": []},
        "tree_files": ["Makefile", "README.md", ".gitignore", "src/clipcut/packer.py", "src/clipcut/segment.py",
                       "tests/test_packer.py", "tests/test_segment.py"],
    }


def probes(count: int = 3) -> list[dict]:
    rows = [
        {"id": "P1", "cmd": "python3 -m clipcut pack fixtures/long.mp4 --segment 10", "setup_rc": 0, "rc": 0,
         "tail": "input 3700s\nwrote 370 segments"},
        {"id": "P2", "cmd": "python3 -m clipcut pack fixtures/short.mp4 --segment -5", "setup_rc": 0, "rc": 2,
         "tail": "error: --segment must be a positive number"},
        {"id": "P3", "cmd": "python3 -m clipcut pack fixtures/short.mp4 --segment abc", "setup_rc": 0, "rc": 1,
         "tail": "Traceback (most recent call last):\nValueError: invalid literal for int() with base 10: 'abc'"},
    ]
    return rows[:count]


def audit(score: int = 4) -> dict:
    doc = {
        "requirements": [
            {"id": "R1", "verdict": "做到", "anchors": [{"type": "demo", "index": 1},
                                                      {"type": "file", "path": "src/clipcut/segment.py"}]},
            {"id": "R2", "verdict": "做到", "anchors": [{"type": "probe", "id": "P1"}]},
            {"id": "R3", "verdict": "做到", "anchors": [{"type": "demo", "index": 2}, {"type": "probe", "id": "P2"}]},
        ],
        "counterexamples": [{"what": "一小时以上的输入", "probe": "P1", "found_problem": False},
                            {"what": "负数的分片时长", "probe": "P2", "found_problem": False}],
        "false_success": "无", "false_success_note": "", "false_success_anchors": [],
    }
    if score < 5:
        doc["requirements"][2] = {"id": "R3", "verdict": "有问题", "level": "细节", "note": "非数字参数抛调用栈",
                                  "anchors": [{"type": "probe", "id": "P3"}, {"type": "demo", "index": 2}]}
        doc["counterexamples"].append({"what": "非数字的分片时长", "probe": "P3", "found_problem": True})
    return doc


def feedback(score: int = 4) -> dict:
    doc = {"score": score, "feedback": TEXT4 if score < 5 else TEXT5, "issues": [], "note": "",
           "process": [{"sentence": "模型先读现有实现和测试，再写分片逻辑，随后运行完整测试，14 条全部通过。",
                        "steps": [1, 2, 3, 6]}] if score == 5 else
                      [{"sentence": "第 4 步跑 make test 时有一条用例没过", "steps": [4]},
                       {"sentence": "改完索引计算后第 6 步重跑", "steps": [5, 6]}]}
    if score < 5:
        doc["issues"] = [{
            "sentence": "--segment abc 这种非数字参数直接抛出 ValueError 的调用栈",
            "trigger": "传入非数字的 --segment 参数", "behavior": "抛出 ValueError 调用栈，退出码 1",
            "impact": "使用的人看不到可读的参数错误提示",
            "anchors": [{"type": "req", "id": "R3"}, {"type": "probe", "id": "P3"},
                        {"type": "log", "text": "ValueError: invalid literal"}]}]
    return doc


def evidence(f=None, a=None, p=None, t=None) -> dict:
    return gate.build_evidence(t or task(), f or facts(), a if a is not None else audit(), probes() if p is None else p)


def problem(level: str, rid: str = "R2") -> dict:
    """把某个需求点改判成有问题的 audit。"""
    doc = audit(5)
    index = int(rid[1:]) - 1
    doc["requirements"][index] = {"id": rid, "verdict": "有问题", "level": level, "note": "长输入处理不了",
                                  "anchors": [{"type": "probe", "id": "P1"}]}
    return doc


class Case(unittest.TestCase):
    def has(self, lines, fragment):
        self.assertTrue(any(fragment in line for line in lines), f"没有包含「{fragment}」的条目：{lines}")

    def hasnt(self, lines, fragment):
        self.assertFalse(any(fragment in line for line in lines), f"不该有包含「{fragment}」的条目：{lines}")


class Helpers(Case):
    def test_squash_and_sentences(self):
        self.assertEqual(gate.squash("  a \n\n b\tc "), "a b c")
        self.assertEqual(gate.sentences("第一句。第二句；第三句！\n第四句？"), ["第一句", "第二句", "第三句", "第四句"])

    def test_longest_common(self):
        self.assertEqual(gate.longest_common("abc清单条目数xyz", "12清单条目数34"), "清单条目数")
        self.assertEqual(gate.longest_common("abc", "xyz"), "")


class Evidence(Case):
    def test_index(self):
        f = facts()
        f["tests"]["after"]["failed_names"] = ["tests/test_a.py::test_x"]
        f["tests"]["new_failures"] = ["tests/test_b.py::test_y"]
        f["claims"]["mentioned_missing"] = ["docs/usage.md"]
        ev = evidence(f)
        self.assertEqual(ev["diff_files"], {"src/clipcut/packer.py", "src/clipcut/segment.py", "tests/test_segment.py"})
        self.assertIn("Makefile", ev["tree"])
        self.assertEqual(ev["steps"], {1, 2, 3, 4, 5, 6})
        self.assertEqual(ev["req_ids"], {"R1", "R2", "R3"})
        self.assertEqual(ev["failed_tests"], {"tests/test_a.py::test_x", "tests/test_b.py::test_y"})
        self.assertEqual(set(ev["probes"]), {"P1", "P2", "P3"})
        self.assertEqual(ev["demo_steps"], 2)
        self.assertEqual(ev["claims_missing"], {"docs/usage.md"})
        for text in ("AssertionError: 4 != 3", "FAILED (failures=1)", "wrote 370 segments", "--segment abc",
                     "index.json: 3 entries", "Ran 14 tests in 0.52s OK"):
            self.assertIn(text, ev["logs"])
        self.assertIn("按 --segment 指定的秒数", ev["blob"])
        self.assertIn("index.json", ev["task_text"])
        self.assertIn("非法参数报错退出", ev["task_text"])


class Anchors(Case):
    def setUp(self):
        f = facts()
        f["tests"]["after"]["failed_names"] = ["tests/test_a.py::test_x"]
        self.ev = evidence(f)

    def test_real_anchors_pass(self):
        for anchor in (
            {"type": "file", "path": "./src/clipcut/packer.py"}, {"type": "tree", "path": "Makefile"},
            {"type": "missing", "path": "docs/usage.md"}, {"type": "step", "step": 4},
            {"type": "log", "text": "AssertionError:  4 != 3"}, {"type": "value", "text": "370"},
            {"type": "req", "id": "R2"}, {"type": "test", "name": "tests/test_a.py::test_x"},
            {"type": "probe", "id": "P3"}, {"type": "demo", "index": 2},
        ):
            self.assertIsNone(gate.verify_anchor(anchor, self.ev), anchor)

    def test_fake_anchors_are_caught(self):
        for anchor, fragment in (
            ("src/a.py", "锚点格式不对"), ({"type": "line", "path": "a.py"}, "锚点格式不对"),
            ({"type": "file"}, "文件锚点没写 path"),
            ({"type": "file", "path": "Makefile"}, "Makefile 不在改动清单里"),
            ({"type": "tree", "path": "docs/usage.md"}, "docs/usage.md 不在产物里"),
            ({"type": "missing", "path": "Makefile"}, "Makefile 在产物里是存在的"),
            ({"type": "step", "step": 7}, "轨迹里没有第 7 步"), ({"type": "step", "step": "4"}, "轨迹里没有第 4 步"),
            ({"type": "log", "text": "OK"}, "报错原文太短"),
            ({"type": "log", "text": "KeyError: 'segment'"}, "报错原文在日志里找不到"),
            ({"type": "value", "text": "9999"}, "数值在证据里找不到"), ({"type": "value", "text": ""}, "数值在证据里找不到"),
            ({"type": "req", "id": "R9"}, "没有需求点 R9"),
            ({"type": "test", "name": "tests/test_a.py::test_z"}, "失败用例里没有"),
            ({"type": "probe", "id": "P9"}, "没有编号 P9 的实跑记录"),
            ({"type": "demo", "index": 3}, "演示里没有第 3 条命令"), ({"type": "demo", "index": 0}, "演示里没有第 0 条命令"),
            ({"type": "demo", "index": "1"}, "演示里没有第 1 条命令"),
        ):
            self.assertIn(fragment, gate.verify_anchor(anchor, self.ev) or "", anchor)


class Audit(Case):
    def check(self, doc, p=None):
        return gate.check_audit(doc, task(), evidence(a=doc, p=p))[0]

    def test_good_samples(self):
        self.assertEqual(self.check(audit(4)), [])
        self.assertEqual(self.check(audit(5)), [])

    def test_shape(self):
        self.assertEqual(self.check({}), ["audit.json 缺 requirements 列表"])
        self.assertEqual(gate.check_audit([], task(), evidence())[0], ["audit.json 缺 requirements 列表"])

    def test_requirement_rules(self):
        doc = audit(5)
        doc["requirements"].pop()
        self.has(self.check(doc), "需求点 R3 没有结论")
        doc = audit(5)
        doc["requirements"][0]["verdict"] = "基本做到"
        self.has(self.check(doc), "需求点 R1 的结论要写 做到 / 有问题 / 看不出来")
        doc = audit(5)
        doc["requirements"][1]["anchors"] = []
        self.has(self.check(doc), "需求点 R2 判了做到，但没有锚点")
        doc = audit(5)
        doc["requirements"][1]["anchors"] = [{"type": "probe", "id": "P8"}]
        self.has(self.check(doc), "需求点 R2：没有编号 P8 的实跑记录")
        doc = audit(5)
        doc["requirements"][1]["anchors"] = {"type": "probe", "id": "P1"}
        self.has(self.check(doc), "需求点 R2：anchors 要是列表")
        doc = audit(5)
        doc["requirements"].append({"id": "R7", "verdict": "做到", "anchors": [{"type": "demo", "index": 1}]})
        self.has(self.check(doc), "audit.json 里有任务里没有的需求点：R7")

    def test_not_done_needs_note_and_level(self):
        doc = problem("明显")
        del doc["requirements"][1]["note"]
        self.has(self.check(doc), "需求点 R2 判了有问题，要在 note 里写清楚是什么情况")
        doc = problem("严重")
        self.has(self.check(doc), "需求点 R2 判了有问题，level 要写 未实现 / 明显 / 细节")
        doc = audit(5)
        doc["requirements"][1] = {"id": "R2", "verdict": "看不出来", "note": "没有一小时以上的素材"}
        self.assertEqual(self.check(doc), [])           # 看不出来可以没有锚点，但要说明
        doc["requirements"][1]["note"] = " "
        self.has(self.check(doc), "需求点 R2 判了看不出来，要在 note 里写清楚")

    def test_counterexamples(self):
        doc = audit(5)
        doc["counterexamples"] = [{"probe": "P1", "found_problem": False},
                                  {"what": "负数", "probe": "P7", "found_problem": False},
                                  {"what": "负数", "probe": "P2", "found_problem": "否"}, "随便写的"]
        errors = self.check(doc)
        self.has(errors, "第 1 个反例没写 what")
        self.has(errors, "第 2 个反例没有对应的实跑记录（probe）")
        self.has(errors, "第 3 个反例要写 found_problem: true / false")
        self.has(errors, "第 4 个反例没写 what")
        self.assertEqual(len(gate.valid_counterexamples(doc, evidence())), 0)

    def test_false_success(self):
        doc = audit(5)
        doc["false_success"] = "有一点"
        self.has(self.check(doc), "false_success 要写 无 / 轻微 / 较大 / 严重")
        doc["false_success"] = "轻微"
        errors = self.check(doc)
        self.has(errors, "要在 false_success_note 里写清楚")
        self.has(errors, "要给锚点（false_success_anchors）")
        doc.update(false_success_note="回答里说写了 docs/usage.md，产物里没有",
                   false_success_anchors=[{"type": "missing", "path": "Makefile"}])
        self.assertEqual(self.check(doc), ["虚假成功：Makefile 在产物里是存在的"])
        doc["false_success_anchors"] = [{"type": "missing", "path": "docs/usage.md"}]
        self.assertEqual(self.check(doc), [])


class Score(Case):
    def cap(self, a, f=None, p=None):
        f = f or facts()
        return gate.score_cap(f, a, evidence(f, a, p), GATE)

    def check(self, score, a, f=None, p=None):
        f = f or facts()
        return gate.check_score(score, f, a, evidence(f, a, p), GATE)

    def test_caps(self):
        self.assertEqual(self.cap(audit(5)), (5, []))
        cap, why = self.cap(audit(4))
        self.assertEqual(cap, 4)
        self.assertEqual(why, ["有细节遗漏，最高 4 分", "反例找出了问题，最高 4 分"])
        f = facts()
        f["tests"]["regression"] = True
        self.assertEqual(self.cap(audit(5), f), (3, ["产物上构建或测试不通过，最高 3 分"]))
        self.assertEqual(self.cap(problem("未实现")), (3, ["有明确要求没实现，最高 3 分"]))
        self.assertEqual(self.cap(problem("明显")), (3, ["有明显问题，最高 3 分"]))
        doc = audit(5)
        doc["requirements"][1] = {"id": "R2", "verdict": "看不出来", "note": "没有素材"}
        self.assertEqual(self.cap(doc), (4, ["有需求点验证不了，最高 4 分"]))

    def test_false_success_caps(self):
        for level, top in (("轻微", 3), ("较大", 2), ("严重", 1)):
            doc = audit(5)
            doc["false_success"] = level
            self.assertEqual(self.cap(doc), (top, [f"虚假成功（{level}），最高 {top} 分"]))

    def test_five_needs_two_real_counterexamples(self):
        doc = audit(5)
        doc["counterexamples"].pop()
        self.assertEqual(self.cap(doc), (4, ["实跑过的反例不到 2 个，最高 4 分"]))
        doc = audit(5)                                   # 写了两个，但其中一个没有实跑记录
        self.assertEqual(self.cap(doc, p=probes(1))[0], 4)
        self.has(self.check(5, doc, p=probes(1))[0], "分数 5 超过证据允许的上限 4：实跑过的反例不到 2 个")

    def test_five_requires_distinct_executed_counterexamples(self):
        doc = audit(5)
        doc["counterexamples"][1]["probe"] = "P1"
        self.assertEqual(len(gate.valid_counterexamples(doc, evidence(a=doc))), 1)
        self.assertEqual(self.cap(doc)[0], 4)
        for field, value in (("rc", None), ("rc", 124), ("rc", 126), ("rc", 127), ("rc", 137),
                             ("rc", True), ("setup_rc", 1), ("setup_rc", None)):
            with self.subTest(field=field, value=value):
                rows = probes()
                rows[0][field] = value
                self.assertEqual(len(gate.valid_counterexamples(audit(5), evidence(p=rows))), 1)
                self.assertEqual(self.cap(audit(5), p=rows)[0], 4)

    def test_inherited_test_failures_prevent_five_without_regression_penalty(self):
        f = facts()
        f["tests"].update(before_ok=False, after_ok=False, regression=False)
        f["tests"]["after"].update(test_rc=1, failed=1)
        self.assertEqual(self.cap(audit(5), f), (4, ["产物测试尚未通过，不能给 5 分，最高 4 分"]))
        self.has(self.check(5, audit(5), f)[0], "分数 5 超过证据允许的上限 4")
        self.assertEqual(self.check(4, audit(5), f), ([], []))

    def test_score_shape(self):
        for value in (True, 0, 6, "4", 4.0, None):
            self.assertEqual(self.check(value, audit(4))[0], ["score 要是 1 到 5 的整数"], value)

    def test_upper_bound(self):
        self.assertEqual(self.check(4, audit(4)), ([], []))
        self.assertEqual(self.check(5, audit(5)), ([], []))
        self.assertEqual(self.check(4, audit(5)), ([], []))          # 证据够 5 分，写 4 分不拦
        self.has(self.check(5, audit(4))[0], "分数 5 超过证据允许的上限 4：有细节遗漏，最高 4 分；反例找出了问题，最高 4 分")
        doc = audit(5)
        doc["false_success"] = "严重"
        errors = self.check(2, doc)[0]
        self.has(errors, "分数 2 超过证据允许的上限 1")
        self.has(errors, "判了严重的虚假成功，只能是 1 分")
        self.assertEqual(self.check(1, doc)[0], [])

    def test_lower_bound(self):
        self.has(self.check(3, audit(4))[0], "分数 3 偏低：证据里没有测试不过、未实现、明显问题或虚假成功中的任何一项")
        self.assertEqual(self.check(3, problem("明显"))[0], [])
        self.assertTrue(any("分数 2 偏低" in error for error in self.check(2, problem("明显"))[0]))
        self.assertEqual(self.check(2, problem("未实现"))[0], [])
        f = facts()
        f["tests"].update(regression=True, after_ok=False, after_runnable=False)
        self.assertEqual(self.check(2, audit(5), f)[0], [])
        doc = audit(5)
        doc["false_success"] = "轻微"
        self.assertEqual(self.check(3, doc)[0], [])
        self.has(self.check(2, doc)[0], "分数 2 偏低")

    def test_warnings(self):
        f = facts()
        f["tests"].update(regression=True, after_ok=False, after_runnable=False)
        self.assertEqual(self.check(3, audit(5), f)[1], [])         # 测试跑不起来，但演示有一条是通的
        for row in f["demo"]["steps"]:
            row["rc"] = 1
        self.assertEqual(self.check(3, audit(5), f)[1], ["测试和演示都跑不起来，没有可用结果，一般是 2 分"])
        self.assertEqual(self.check(2, audit(5), f)[1], [])
        doc = problem("未实现", "R1")
        doc["requirements"][1] = {"id": "R2", "verdict": "有问题", "level": "未实现", "note": "没写",
                                  "anchors": [{"type": "probe", "id": "P1"}]}
        self.assertEqual(self.check(3, doc)[1], ["过半的需求点没实现，一般不超过 2 分"])
        self.assertEqual(self.check(3, problem("未实现"))[1], [])    # 三条里一条没实现，不到一半
        f = facts()
        f["changes"]["no_change"] = True
        self.assertEqual(self.check(3, problem("未实现"), f)[1], ["产物相对初始提交没有任何改动，确认这个分数"])


class Text(Case):
    def check(self, text, prompt=PROMPT):
        return gate.check_text(text, prompt, GATE)

    def test_good_text(self):
        self.assertEqual(self.check(TEXT4), ([], []))
        self.assertEqual(self.check(TEXT5), ([], []))

    def test_length(self):
        self.assertEqual(self.check("  \n "), (["Feedback 是空的"], []))
        self.has(self.check(S3)[0], "字，至少 120 字")
        self.has(self.check(TEXT4 + "分片的起止时间和清单里记的对得上。" * 40)[0], "字，最多 700 字")
        spaced = "  ".join(TEXT5)                        # 空白不算字数
        self.hasnt(self.check(spaced)[0], "最多 700 字")

    def test_banned_words(self):
        for extra, label in (
            ("总体而言还补了测试。", "出现套话或 AI 腔的词：总体而言"), ("充分体现了价值。", "出现套话或 AI 腔的词：充分体现"),
            ("整体不错。", "出现没法核验的评价：整体不错"), ("反例都跑过。", "出现内部口径词：反例"),
            ("对照 facts.json 看过。", "出现内部口径词：facts.json"), ("本题要求分片。", "出现指代题目的说法：本题"),
            ("Docker 里跑过一遍。", "出现运行环境或录屏现场的词（任务本身没提到）：docker"),
            ("录屏里能看到。", "出现运行环境或录屏现场的词（任务本身没提到）：录屏"),
            ("轨迹还执行了完整回归。", "出现取证来源口吻：轨迹"),
            ("素材卡里记录了边界。", "出现取证来源口吻：素材卡"),
            ("中途遇到限流。", "出现网络、额度、耗时这类不该算在模型头上的因素：限流"),
            ("换一台机器未必能跑。", "出现跨机器的说法：换一台机器"),
            ("输出里有“done”字样。", "出现禁用的标点或标记：“ ”"), ("跑了 `make test`。", "出现禁用的标点或标记：`"),
            ("后面没验证——直接结束。", "出现禁用的标点或标记：—— —"), ("改动**很大**。", "出现禁用的标点或标记：**"),
        ):
            self.has(self.check(TEXT4 + extra)[0], label)

    def test_requires_model_process(self):
        self.has(self.check(S2 + S3 + S4)[0], "正文缺少模型实现或验证过程的具体行为句")

    def test_model_subject_appears_only_once(self):
        self.has(self.check(TEXT4 + "模型随后又重跑了一次。")[0], "“模型”最多出现一次")

    def test_rejects_coverage_only_process_recap(self):
        text = S2 + S3 + S4 + "模型先实现主链路并完成构建，但并发到达顺序仍未覆盖。"
        self.has(self.check(text)[0], "模型过程只写了未覆盖或未验证")

    def test_accepts_real_process_failure_and_repair(self):
        text = S2 + S3 + S4 + (
            "模型先用并发 curl 校验重复提交，却把多个响应拼在一起按单个 JSON 解析，"
            "报出 JSONDecodeError；它随后改成五个独立线程分别读取，确认只新增一个批次。"
        )
        self.assertEqual(self.check(text), ([], []))

    def test_environment_words_from_the_task_are_allowed(self):
        prompt = "把 Docker 镜像的构建脚本拆成两段，并且在网关超时时重试。"
        errors = self.check(TEXT4 + "镜像构建脚本拆成了两段，网关超时会重试，录屏没提到。", prompt)[0]
        self.assertEqual(errors, ["出现运行环境或录屏现场的词（任务本身没提到）：录屏"])

    def test_voice(self):
        self.has(self.check("这份交付改了两个文件。" + TEXT4)[0], "用了「这条 / 这一版 / 这份交付」当主语")
        self.has(self.check(TEXT4 + "这一版没有再验证。")[0], "用了「这条 / 这一版 / 这份交付」当主语")
        self.hasnt(self.check(TEXT4 + "对比这一版和上一版的清单。")[0], "当主语")
        for extra in ("所以给 4 分。", "综合下来打了三分。", "因此扣分。", "档位落在中间。"):
            self.has(self.check(TEXT4 + extra)[0], "正文里不写给分结论")
        for extra in ("我跑了一遍长输入。", "本人看过清单。", "笔者重跑了测试。"):
            self.has(self.check(TEXT4 + extra)[0], "正文里不写验收人自己")
        self.has(self.check(TEXT4 + "R2 没有覆盖。")[0], "正文里不写需求点编号")
        self.hasnt(self.check(TEXT4 + "换成 HDR10 的素材也能切。")[0], "需求点编号")

    def test_copying(self):
        self.has(self.check(TEXT4 + "代码可运行，但有极少量的细节遗漏。")[0], "照抄了分数档位的描述：代码可运行但有极少量的细节遗漏")
        errors = self.check(TEXT4 + "要求是按 --segment 指定的秒数切分输入，在输出目录里生成 index.json 清单。")[0]
        self.has(errors, "复述了任务原文：")

    def test_repeated_heads_is_a_warning(self):
        errors, warnings = self.check(TEXT5 + "后续检查了代码。后续检查了测试。后续检查了文档。")
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["多句用同一个开头：后续检查"])


class Feedback(Case):
    def check(self, doc, a=None, f=None, p=None, t=None):
        f, a, t = f or facts(), a or audit(doc["score"] if isinstance(doc.get("score"), int) else 4), t or task()
        return gate.check_feedback(doc, t, f, a, evidence(f, a, p, t), GATE)

    def test_good_samples(self):
        self.assertEqual(self.check(feedback(4)), ([], []))
        self.assertEqual(self.check(feedback(5), p=probes(2)), ([], []))

    def test_verification_gap_does_not_force_invented_delivery_issue(self):
        doc = feedback(5)
        doc.update(score=4, score_basis={"category": "verification_gap"})
        self.hasnt(self.check(doc, a=audit(5), p=probes(2))[0], "4 分至少")
        doc["score_basis"]["category"] = "process_issue"
        self.hasnt(self.check(doc, a=audit(5), p=probes(2))[0], "4 分至少")
        unknown = audit(5)
        unknown["requirements"][1].update(verdict="看不出来", note="现有材料不足以确认该边界")
        self.hasnt(self.check(doc, a=unknown, p=probes(2))[0], "4 分至少")
        self.has(self.check(doc, a=audit(4))[0], "4 分至少")

    def test_process_without_tools_must_reference_conversation(self):
        f, doc = facts(), feedback(4)
        f["trace"]["steps"] = []
        sentence = "回答里只解释了修改思路，随后就结束了对话"
        doc["feedback"] += sentence + "。"
        doc["process"] = [{"sentence": sentence, "steps": [], "source": "conversation"}]
        self.hasnt(self.check(doc, f=f)[0], "哪几步")
        self.has(self.check(doc)[0], "哪几步")

    def test_issue_rules(self):
        doc = feedback(4)
        doc["issues"][0]["sentence"] = "非数字参数会直接抛出调用栈"          # 改写过，不是原句
        self.has(self.check(doc)[0], "第 1 个问题句：sentence 要是正文里的原句")
        doc = feedback(4)
        doc["issues"][0].update(trigger="", behavior="抛栈", impact=None)
        errors = self.check(doc)[0]
        for fragment in ("缺触发节点（trigger）", "缺模型实际行为（behavior）", "缺业务影响（impact）"):
            self.has(errors, "第 1 个问题句：" + fragment)
        doc = feedback(4)
        doc["issues"][0]["anchors"] = [{"type": "req", "id": "R3"}, {"type": "value", "text": "370"}]
        self.has(self.check(doc)[0], "第 1 个问题句：至少要有一个能定位的锚点")
        doc = feedback(4)
        doc["issues"][0]["anchors"].append({"type": "log", "text": "KeyError: 'segment'"})
        self.has(self.check(doc)[0], "第 1 个问题句：报错原文在日志里找不到")
        doc = feedback(4)
        doc["issues"].append("随便写的")
        self.has(self.check(doc)[0], "第 2 个问题句格式不对")

    def test_process_rules(self):
        doc = feedback(4)
        doc["process"][0]["sentence"] = "跑测试时有用例没过"
        self.has(self.check(doc)[0], "第 1 个过程句：sentence 要是正文里的原句")
        doc = feedback(4)
        doc["process"][1]["steps"] = []
        self.has(self.check(doc)[0], "第 2 个过程句：要写它对应轨迹里的哪几步（steps）")
        doc = feedback(4)
        doc["process"][1]["steps"] = [5, 9]
        self.has(self.check(doc)[0], "第 2 个过程句：轨迹里没有第 9 步")
        doc = feedback(4)
        doc["process"] = []
        self.has(self.check(doc)[0], "过程和产物都要写")
        f = facts()                                       # 没有工具步骤也必须反馈实际对话过程
        f["trace"]["steps"] = []
        self.has(self.check(doc, f=f)[0], "过程和产物都要写")
        doc = feedback(4)
        doc["process"] = doc["process"] + doc["process"][:2]
        self.has(self.check(doc)[0], "过程段最多写 3 句")

    def test_score_and_issues_must_agree(self):
        doc = feedback(4)
        doc["score"] = 5
        self.has(self.check(doc)[0], "5 分不写缺口：issues 应当为空")
        doc = feedback(5)
        doc["score"] = 4
        self.has(self.check(doc)[0], "4 分至少要有一处问题句")
        doc = feedback(5)
        doc["feedback"] = "分片和清单都生成了，测试全部通过，长输入也能处理，非法参数会被拒绝并退出。" * 3
        self.has(self.check(doc)[0], "4 到 5 分也要写出核对过的要求和实际验证结果")
        doc["score"] = 3
        self.hasnt(self.check(doc)[0], "4 到 5 分也要写出")

    def test_five_rejects_visible_process_rework(self):
        doc = feedback(5)
        doc["feedback"] = S1 + S2 + S3
        doc["process"] = [
            {"sentence": "第 4 步跑 make test 时有一条用例没过", "steps": [4]},
            {"sentence": "改完索引计算后第 6 步重跑", "steps": [5, 6]},
        ]
        self.has(self.check(doc)[0], "5 分要求过程接近一次性完美跑通")
        doc["score"] = 4
        doc["score_basis"] = {"category": "process_issue"}
        self.hasnt(self.check(doc)[0], "5 分要求过程接近一次性完美跑通")

    def test_regression_must_be_written(self):
        f = facts()
        f["tests"].update(regression=True, after_ok=False)
        doc = feedback(5)                                 # 「没有报错」不算写了失败
        doc["feedback"] = S2 + S3
        self.has(self.check(doc, f=f)[0], "产物上构建或测试不通过，正文必须明写")
        doc["feedback"] = S2 + S3 + "make test 在产物上不通过。"
        self.hasnt(self.check(doc, f=f)[0], "正文必须明写")
        doc["feedback"] = S2 + S3 + "产物上跑 make test 会失败。"
        self.hasnt(self.check(doc, f=f)[0], "正文必须明写")

    def test_serious_requirement_needs_an_issue(self):
        for level in ("未实现", "明显"):
            errors = self.check(feedback(4), a=problem(level))[0]
            self.has(errors, f"需求点 R2 判了{level}，正文里要有对应的问题句（anchors 里带 req）")
        doc = feedback(4)
        doc["issues"][0]["anchors"].append({"type": "req", "id": "R2"})
        self.hasnt(self.check(doc, a=problem("明显"))[0], "正文里要有对应的问题句")
        self.hasnt(self.check(feedback(4), a=problem("细节"))[0], "正文里要有对应的问题句")

    def test_files_in_text_must_have_a_source(self):
        doc = feedback(4)
        doc["feedback"] = TEXT4.replace("先读了 src/clipcut/packer.py 和现有测试", "先读了packer.py文件和test_packer.py")
        self.hasnt(self.check(doc)[0], "正文提到的文件")            # 紧挨着中文、只写文件名，都认得出来
        doc["feedback"] = TEXT4 + "还改了helper.py文件和 docs/usage.md。"
        errors = self.check(doc)[0]
        self.has(errors, "正文提到的文件 helper.py 在产物、改动清单、任务原文和日志里都找不到")
        self.has(errors, "正文提到的文件 docs/usage.md 在产物")
        f = facts()                                       # 模型声称写了、实际不存在的文件，可以点名
        f["claims"]["mentioned_missing"] = ["docs/usage.md"]
        doc["feedback"] = TEXT4 + "回答里说写了 docs/usage.md，产物里没有。"
        self.hasnt(self.check(doc, f=f)[0], "正文提到的文件")
        doc["feedback"] = TEXT4 + "版本号 2.1.319 和 v1.2 不算文件。"
        self.hasnt(self.check(doc)[0], "正文提到的文件")

    def test_runtime_outputs_named_by_task_or_logs(self):
        f = facts()
        for row in f["demo"]["steps"]:
            row["tail"] = ""
        self.hasnt(self.check(feedback(4), f=f)[0], "正文提到的文件")        # index.json 是任务原文点了名的输出
        other = {"task_id": "s11-000001", "prompt": "给 pack 加上分片。", "requirements": [
            {"id": rid, "text": "分片", "check": ""} for rid in ("R1", "R2", "R3")]}
        self.has(self.check(feedback(4), f=f, t=other)[0], "正文提到的文件 index.json 在产物")
        self.hasnt(self.check(feedback(4), t=other)[0], "正文提到的文件")    # 演示输出里打出来过

    def test_step_references(self):
        doc = feedback(4)
        doc["feedback"] = TEXT4 + "第 12 步又改了一次。"
        self.has(self.check(doc)[0], "正文提到第 12 步，轨迹里没有这一步")

    def test_unknown_numbers_are_warnings(self):
        doc = feedback(4)
        doc["feedback"] = TEXT4 + "清单里一共 512 条。"
        errors, warnings = self.check(doc)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["数值 512 在证据里找不到，确认不是凭印象写的"])

    def test_missing_lists_do_not_crash(self):
        errors = self.check({"score": 4, "feedback": TEXT4})[0]
        self.has(errors, "过程和产物都要写")
        self.has(errors, "4 分至少要有一处问题句")


class Duplicates(Case):
    SHARED = "输出目录里的清单条目数和分片数量对不上"

    def check(self, records, delivered=()):
        return gate.check_duplicates(records, list(delivered), GATE)

    def test_clean_batch(self):
        problems, notes = self.check({"a": "分片时长按参数切开，清单写在同一层。", "b": "非法参数会被拒绝，并以退出码 2 结束。"})
        self.assertEqual(problems, {"a": [], "b": []})
        self.assertEqual(notes, [])

    def test_slot_substitution_is_still_a_template(self):
        text = ("先阅读了 {file} 的现有实现，随后补齐了对应处理，并在命令 {cmd} 上核对输出。"
                "主流程拿到了 {num} 个结果，输入为空时也能及时返回。最后一次改动以后重新检查了边界，"
                "没有遗留需要手工修复的路径，收尾说明和当前产物相符，使用时可以直接按现有入口完成操作。")
        problems, _ = self.check({"a": text.format(file="first.py", cmd="run_first", num=7),
                                  "b": text.format(file="second.py", cmd="run_second", num=19)})
        self.has(problems["a"], "句式骨架")
        self.has(problems["b"], "句式骨架")

    def test_shared_block_in_batch(self):
        problems, _ = self.check({"a": f"长输入跑完后{self.SHARED}，少了两条。", "b": f"实际跑下来{self.SHARED}。",
                                  "c": "测试全部通过，演示命令的退出码也对。"})
        self.has(problems["a"], "和 b 有 19 字的相同片段：" + self.SHARED)
        self.has(problems["b"], "和 a 有 19 字的相同片段：")
        self.assertEqual(problems["c"], [])

    def test_ascii_overlap_is_not_a_duplicate(self):
        command = "python3 -m clipcut pack fixtures/short.mp4 --segment 10"
        problems, _ = self.check({"a": f"跑了 {command}，分片齐全。", "b": f"执行 {command} 之后清单为空。"})
        self.assertEqual(problems, {"a": [], "b": []})

    def test_delivered_history(self):
        old = [{"task_id": "s11-000900", "feedback": f"上一批里{self.SHARED}。"},
               {"task_id": "a", "feedback": "分片时长按参数切开，清单写在同一层。"},        # 同一题自己的旧版本不算
               {"feedback": "非法参数会被拒绝，并以退出码二结束，没有残留文件。"}]
        problems, _ = self.check({"a": "分片时长按参数切开，清单写在同一层。", "b": f"实际跑下来{self.SHARED}。",
                                  "c": "非法参数会被拒绝，并以退出码二结束，没有残留文件。"}, old)
        self.assertEqual(problems["a"], [])
        self.has(problems["b"], "和已交付的 s11-000900 有相同片段：")
        self.has(problems["c"], "和已交付的 历史数据 有相同片段：")

    def test_whole_sentence(self):
        sentence = "make test 14 passed; exit code 0; index.json has 3 entries"
        problems, _ = self.check({"a": f"分片齐全。{sentence}。", "b": f"{sentence}。清单为空。"})
        self.assertEqual(problems["a"], [])
        self.assertEqual(len(problems["b"]), 1)
        self.has(problems["b"], "整句和 a 重复：")

    def test_same_opening(self):
        records = {"a": "模型先读了打包模块，再动手改。", "b": "模型先读了打包的测试，然后补了用例。",
                   "c": "分片时长按参数切开。", "d": "非法参数会被拒绝。", "e": "清单写在输出目录的同一层。"}
        problems, _ = self.check(records)
        self.has(problems["a"], "开头 6 个字和另外 1 条一样：模型先读了打")
        self.has(problems["b"], "开头 6 个字和另外 1 条一样")
        self.assertEqual(problems["c"], [])
        del records["e"]                                 # 不到 5 条不看句首
        self.assertEqual(self.check(records)[0]["a"], [])

    def test_five_ratio_note(self):
        self.assertEqual(gate.five_ratio_note([5, 5, 5, 4, 4], GATE), "这一批 5 分占 60%，超过 40%，回头重看 5 分的那几条")
        self.assertEqual(gate.five_ratio_note([5, 5, 4, 4, 3], GATE), "")
        self.assertEqual(gate.five_ratio_note([5, 5, 5, 5], GATE), "")


if __name__ == "__main__":
    unittest.main()
