"""中断续写的可观察行为：上限、首轮标识、完整评价、平台字段隔离。"""
from __future__ import annotations

import copy
import unittest
from fixtures import PROMPT, trace_rows, stream_text, write_projects
from test_run import DockerCase, SID
from test_submit import SubmitCase, SCHEMA
import fbrlib as lib
import fbr_run
import fbr_trace
import fbr_submit as submit


def entries(count=1):
    return [{"n": n + 1, "prompt": "继续", "reason": "无故中断", "session_id": SID} for n in range(count)]


def rows_with_continuations(count=1, text="继续"):
    rows = trace_rows(SID, answer="首轮尚未完成")
    for n in range(count):
        extra = trace_rows(SID, prompt=text, prompt_id=f"prm-continue-{n}", answer=f"续写{n}完成")
        for row in extra:
            row["uuid"] += f"-continue-{n}"
        rows.extend(extra)
    return rows


class Runtime(DockerCase):
    def test_two_continuations_share_container_and_first_ids(self):
        task = self.make_task()
        projects = self.projects()
        self.docker.outcomes = [
            self.good(stdout=stream_text(SID, with_result=False)),
            self.good(stdout=stream_text(SID, with_result=False)),
            self.good(projects=projects)]
        write_projects(projects, SID, rows_with_continuations(2))
        outcomes = list(self.docker.outcomes)
        self.assertEqual(fbr_run.run_one(task, self.cfg)["state"], "ok")
        self.assertEqual([o["stdin"] for o in outcomes], [PROMPT, "继续", "继续"])
        self.assertEqual(len(self.docker.docker_calls("run", "-i")), 1)
        calls = self.docker.docker_calls("exec")
        self.assertTrue(all(SID == c[-1] and "--resume" in c[-3] for c in calls[1:]))
        self.assertEqual(fbr_run.collect_one(task, self.cfg)["state"], "ok")
        ids = lib.load_json(task / "trace/ids.json")
        self.assertEqual((ids["session_id"], ids["prompt_id"], ids["user_turns"]), (SID, "prm-0001", 3))
        summary = fbr_trace.summarize(task / "trace/projects", ids["session_file"])
        self.assertEqual(summary["assistant_answers"], ["首轮尚未完成", "续写0完成", "续写1完成"])
        self.assertEqual(summary["final_answer"], "续写1完成")

    def test_limit_stops_without_fourth_input_or_reset(self):
        task = self.make_task()
        self.docker.outcomes = [self.good(stdout=stream_text(SID, with_result=False)) for _ in range(4)]
        row = fbr_run.run_one(task, self.cfg)
        self.assertEqual(row["state"], "failed")
        self.assertIn("上限", row["detail"])
        self.assertEqual(len(self.docker.outcomes), 1)
        self.assertEqual(self.docker.docker_calls("rm"), [])
        self.assertEqual(len(lib.load_json(task / "run/continuations.json")), 2)
        self.assertIn(task.name, self.docker.containers)

    def test_changed_session_is_rejected(self):
        task = self.make_task()
        self.docker.outcomes = [self.good(stdout=stream_text(SID, with_result=False)),
                                self.good(stdout=stream_text("bbbbbbbb-0000-4000-8000-000000000001"))]
        self.assertEqual(fbr_run.run_one(task, self.cfg)["state"], "failed")
        self.assertEqual(self.docker.docker_calls("rm"), [])

    def test_only_literal_continue_and_recorded_interruptions_are_allowed(self):
        projects = self.projects()
        for text, records, expected in (("继续", entries(), True), ("请继续", entries(), False),
                                         ("继续", [], False), ("继续", entries(3), False)):
            write_projects(projects, SID, rows_with_continuations(1, text))
            ids = fbr_trace.read_ids(projects, PROMPT, records)
            self.assertEqual(not bool(ids["problems"]), expected)

    def test_missing_first_prompt_id_is_rejected_even_if_resume_has_one(self):
        rows = rows_with_continuations()
        rows[0].pop("promptId")
        projects = self.projects()
        write_projects(projects, SID, rows)
        self.assertIn("用户消息上没有 promptId", fbr_trace.read_ids(projects, PROMPT, entries())["problems"])

    def test_timeout_during_continuation_kills_container_and_uses_engineering_retry(self):
        task = self.make_task()
        self.docker.outcomes = [self.good(stdout=stream_text(SID, with_result=False)),
                                self.good(stdout="", rc=124, timed_out=True), self.good()]
        self.assertEqual(fbr_run.run_one(task, self.cfg)["state"], "ok")
        self.assertEqual(len(self.docker.docker_calls("kill")), 1)
        attempts = lib.load_json(task / "run/attempts.json")
        self.assertEqual(attempts[0]["reason"], "进程超时")
        self.assertEqual(len(attempts[0]["continuations"]), 1)

    def test_configuration_cannot_increase_hard_limit(self):
        for count in (3, -1, True, "2"):
            cfg = copy.deepcopy(self.cfg)
            cfg["run"]["max_continuations"] = count
            lib.save_json(self.root / "_config/settings.json", cfg)
            with self.assertRaisesRegex(lib.FbrError, "0 到 2"):
                lib.settings(self.root)


class Platform(SubmitCase):
    def test_normal_run_and_feedback_repair_notes_still_submit_empty_other_issues(self):
        task = self.ready()
        doc = lib.load_json(task / "review/feedback.json")
        doc["note"] = "返修说明：录屏准备失败；模型改写历史；其他问题"
        lib.save_json(task / "review/feedback.json", doc)
        self.review_receipt(task)
        item = submit.prepare(self.root, task.parent.name, SCHEMA)[0]
        self.assertEqual(item["data"]["other_issues"], "")

    def test_continuation_submission_binds_first_id_and_only_interruption_note(self):
        task = self.ready()
        ids = lib.load_json(task / "trace/ids.json")
        ids.update(session_id=SID, prompt_id="prm-0001", user_turns=2, continuations=entries())
        lib.save_json(task / "trace/ids.json", ids)
        record = lib.load_task(task)
        rows = rows_with_continuations()
        rows[0]["message"]["content"] = record["prompt"]
        session = task / "trace" / ids["session_file"]
        # Fixture's pre-existing path is preserved; the filename must match SessionID.
        session.unlink()
        session = write_projects(task / "trace/projects", SID, rows)
        ids["session_file"] = str(session.relative_to(task / "trace"))
        lib.save_json(task / "trace/ids.json", ids)
        state = lib.load_state(task)
        state["stages"]["run"]["continuations"] = entries()
        lib.save_json(task / "state.json", state)
        self.refresh_source_fixture(task)
        self.review_receipt(task)
        item = submit.prepare(self.root, task.parent.name, SCHEMA)[0]
        data = item["data"]
        self.assertEqual((data["session_id"], data["turn_id"]), (SID, "prm-0001"))
        self.assertEqual(data["other_issues"], fbr_trace.interruption_note(ids))
        self.assertIn("1次", data["other_issues"])

    def test_scalar_payload_never_forwards_arbitrary_notes(self):
        self.assertEqual(submit.scalar_payload({"备注": "任何返修或运行备注"})["other_issues"], "")


if __name__ == "__main__":
    unittest.main()
