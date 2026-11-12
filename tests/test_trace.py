"""fbr_trace：SessionID / PromptID、单轮校验、步骤索引、验证情况、运行结果归类。"""

from __future__ import annotations

import json
import unittest

from fixtures import PROMPT, TOOLS, VERSION, Sandbox, stream_text, trace_rows, write, write_projects

import fbr_trace as trace

SID = "11111111-2222-3333-4444-555555555555"


class ReadIds(Sandbox):
    def projects(self, rows, session_id=SID):
        projects = self.tmp / "trace" / "projects"
        write_projects(projects, session_id, rows)
        return projects

    def test_single_turn(self):
        ids = trace.read_ids(self.projects(trace_rows(SID, tools=TOOLS)), PROMPT)
        self.assertEqual(ids["problems"], [])
        self.assertEqual(ids["session_id"], SID)
        self.assertEqual(ids["prompt_id"], "prm-0001")
        self.assertEqual(ids["user_turns"], 1)
        self.assertEqual(ids["cli_version"], VERSION)
        self.assertEqual(ids["session_file"], f"projects/-workspace/{SID}.jsonl")

    def test_tool_results_and_sidechains_are_not_turns(self):
        rows = trace_rows(SID, tools=TOOLS)
        side = dict(rows[0], isSidechain=True, promptId="prm-side", uuid="side")
        meta = dict(rows[0], isMeta=True, uuid="meta")
        ids = trace.read_ids(self.projects(rows + [side, meta]), PROMPT)
        self.assertEqual(ids["user_turns"], 1)
        self.assertEqual(ids["problems"], [])

    def test_two_turns(self):
        rows = trace_rows(SID, tools=TOOLS)
        extra = dict(rows[0], uuid="again", promptId="prm-0002", message={"role": "user", "content": "继续"})
        ids = trace.read_ids(self.projects(rows + [extra]), PROMPT)
        self.assertTrue(any("不是单轮" in p for p in ids["problems"]), ids["problems"])

    def test_system_background_notifications_are_not_user_turns(self):
        rows = trace_rows(SID, tools=TOOLS)
        for content in ("<task-notification>background stopped</task-notification>",
                        [{"type": "text", "text": "<task-notification>done</task-notification>"}]):
            rows.append(dict(rows[0], uuid=str(len(rows)), promptId=str(len(rows)),
                             promptSource="system", turnOrigin="task_notification",
                             origin={"kind": "task-notification", "producer": "session-task"},
                             message={"role": "user", "content": content}))
        ids = trace.read_ids(self.projects(rows), PROMPT)
        self.assertEqual(ids["problems"], [])
        self.assertEqual(ids["user_turns"], 1)
        self.assertEqual(ids["prompt_id"], "prm-0001")

    def test_notification_text_or_partial_provenance_does_not_hide_user_input(self):
        content = "<task-notification>human input</task-notification>"
        cases = [{}, {"promptSource": "system"},
                 {"turnOrigin": "task_notification"},
                 {"origin": {"kind": "task-notification", "producer": "session-task"}},
                 {"promptSource": "user", "turnOrigin": "task_notification",
                  "origin": {"kind": "task-notification", "producer": "session-task"}}]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                rows = trace_rows(SID, tools=TOOLS)
                extra = dict(rows[0], uuid="extra", promptId="extra",
                             message={"role": "user", "content": content}, **metadata)
                self.assertTrue(trace.is_user_turn(extra))
                ids = trace.read_ids(self.projects(rows + [extra]), PROMPT)
                self.assertTrue(any("不是单轮" in p for p in ids["problems"]))

    def test_prompt_mismatch(self):
        ids = trace.read_ids(self.projects(trace_rows(SID, prompt="另一段输入")), PROMPT)
        self.assertIn("轨迹里的用户输入和题面不一致", ids["problems"])

    def test_missing_prompt_id(self):
        rows = trace_rows(SID)
        rows[0].pop("promptId")
        ids = trace.read_ids(self.projects(rows), PROMPT)
        self.assertIn("用户消息上没有 promptId", ids["problems"])

    def test_no_files_and_many_files(self):
        empty = self.tmp / "empty" / "projects"
        empty.mkdir(parents=True)
        self.assertIn("导出的 projects 目录里没有会话文件", trace.read_ids(empty, PROMPT)["problems"])
        projects = self.projects(trace_rows(SID))
        write_projects(projects, "other-session", trace_rows("other-session"))
        self.assertTrue(any("应当只有 1 个" in p for p in trace.read_ids(projects, PROMPT)["problems"]))

    def test_filename_mismatch(self):
        projects = self.tmp / "t2" / "projects"
        write_projects(projects, "renamed", trace_rows(SID))
        self.assertIn("会话文件名和 sessionId 对不上", trace.read_ids(projects, PROMPT)["problems"])

    def test_no_assistant(self):
        ids = trace.read_ids(self.projects(trace_rows(SID)[:1]), PROMPT)
        self.assertIn("轨迹里没有助手消息", ids["problems"])


class Steps(Sandbox):
    def summary(self, tools, answer="分片打包已实现。"):
        projects = self.tmp / "trace" / "projects"
        write_projects(projects, SID, trace_rows(SID, tools=tools, answer=answer))
        return trace.summarize(projects, f"projects/-workspace/{SID}.jsonl")

    def test_step_index(self):
        info = self.summary(TOOLS)
        steps = info["steps"]
        self.assertEqual([s["step"] for s in steps], [1, 2, 3, 4])
        self.assertEqual([s["kind"] for s in steps], ["read", "edit", "test", "edit"])
        self.assertEqual(steps[0]["target"], "src/clipcut/packer.py")
        self.assertEqual(steps[2]["target"], "make test")
        self.assertIn("Ran 1 test", steps[2]["output_tail"])
        self.assertEqual(info["counts"]["tools"], 4)
        self.assertEqual(info["counts"]["edits"], 2)
        self.assertEqual(info["counts"]["errors"], 0)
        self.assertEqual(info["final_answer"], "分片打包已实现。")
        self.assertTrue(info["started_at"] < info["ended_at"])

    def test_not_verified_after_last_edit(self):
        check = self.summary(TOOLS)["verification"]
        self.assertEqual(check["last_edit_step"], 4)
        self.assertFalse(check["verified_after_last_edit"])
        self.assertEqual(check["tests_run"], 1)
        self.assertEqual(check["tests_failed"], 0)

    def test_verified_after_last_edit(self):
        tools = TOOLS + [("Bash", {"command": "python3 -m pytest -q"}, "3 passed", False)]
        check = self.summary(tools)["verification"]
        self.assertTrue(check["verified_after_last_edit"])
        self.assertTrue(check["last_check_passed"])

    def test_failed_check_and_shell_mutation(self):
        tools = [("Bash", {"command": "make test"}, "FAILED tests/test_packer.py::test_ok", True),
                 ("Bash", {"command": "sed -i 's/a/b/' src/clipcut/packer.py"}, "", False),
                 ("Bash", {"command": "make"}, "built", False)]
        info = self.summary(tools)
        steps, check = info["steps"], info["verification"]
        self.assertTrue(steps[0]["is_error"])
        self.assertEqual(steps[1]["kind"], "run")
        self.assertTrue(steps[1]["mutates"])
        self.assertEqual(steps[2]["kind"], "build")
        self.assertEqual(check["last_edit_step"], 2)
        self.assertEqual(check["tests_failed"], 1)
        self.assertFalse(check["checks"][0]["passed"])
        self.assertEqual(info["counts"]["errors"], 1)

    def test_api_error_message_is_not_the_answer(self):
        rows = trace_rows(SID, tools=TOOLS, answer="改完了。")
        rows.append(dict(rows[-1], uuid="err", isApiErrorMessage=True,
                         message={"role": "assistant", "content": [{"type": "text", "text": "API Error: 529"}]}))
        self.assertEqual(trace.final_answer(trace.main_rows(rows)), "改完了。")
        self.assertEqual(len(trace.api_errors(rows)), 1)


class Attempts(Sandbox):
    def stream(self, text):
        return trace.read_stream(write(self.tmp / "stream.jsonl", text))

    def test_read_stream(self):
        info = self.stream(stream_text(SID) + "not json\n")
        self.assertEqual(info["session_id"], SID)
        self.assertEqual(info["cli_version"], VERSION)
        self.assertEqual(info["events"], 3)
        self.assertFalse(info["result"]["is_error"])
        self.assertEqual(self.stream("")["result"], None)

    def test_accept(self):
        verdict = trace.classify_attempt(0, False, self.stream(stream_text(SID)))
        self.assertEqual(verdict["verdict"], "accept")

    def test_wrong_answer_still_counts(self):
        # 此层只判断轨迹收口；零代码改动由运行层比较最终树后归档重跑
        verdict = trace.classify_attempt(0, False, self.stream(stream_text(SID, "这个需求做不了。")))
        self.assertEqual(verdict["verdict"], "accept")

    def test_retry_reasons(self):
        ok = self.stream(stream_text(SID))
        cut = self.stream(stream_text(SID, with_result=False))
        cases = [
            (trace.classify_attempt(0, True, ok), "进程超时"),
            (trace.classify_attempt(1, False, cut, "API Error: 529 overloaded"), "网关报错"),
            (trace.classify_attempt(137, False, cut, "Killed"), "进程异常退出"),
            (trace.classify_attempt(0, False, cut), "无故中断"),
            (trace.classify_attempt(0, False, self.stream(stream_text(SID, "rate limit exceeded", is_error=True))),
             "网关报错"),
            (trace.classify_attempt(0, False, self.stream(stream_text(SID, "stopped", is_error=True))), "无故中断"),
            (trace.classify_attempt(2, False, ok), "进程异常退出"),
            (trace.classify_attempt(0, False, self.stream(stream_text(SID, ""))), "无故中断"),
            (trace.classify_attempt(0, False, self.stream(stream_text(SID, "API Error: 500 internal"))), "网关报错"),
        ]
        for index, (verdict, reason) in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(verdict["verdict"], "retry")
                self.assertEqual(verdict["reason"], reason)


if __name__ == "__main__":
    unittest.main()
