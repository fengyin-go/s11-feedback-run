"""回归：轨迹中的网关 key 保留原文、记录为备注，不阻断有效数据交付。"""

from __future__ import annotations

import contextlib
import io
import json
from unittest import mock

from fixtures import KEY, PROMPT, stream_text, trace_rows, write, write_projects
from test_run import DockerCase, SID

import fbrlib as lib
import fbr_run
import fbr_trace


class EvidenceSafety(DockerCase):
    def test_stream_key_is_preserved_and_marked_without_retry(self):
        task_dir = self.make_task()
        raw = stream_text(SID) + f'{{"type":"debug","note":"{KEY}"}}\n'
        self.docker.outcomes = [self.good(stdout=raw), self.good()]
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "ok")
        self.assertNotIn(KEY, str(row))
        self.assertEqual((task_dir / "run/stream.jsonl").read_text(), raw)
        self.assertEqual(len(self.docker.outcomes), 1)
        self.assertNotIn(KEY, (task_dir / "run/attempts.json").read_text())
        self.assertEqual(fbr_run.run_one(task_dir, self.cfg)["state"], "skip")
        self.assertEqual(len(self.docker.outcomes), 1)

    def test_stderr_key_does_not_replace_real_gateway_retry_decision(self):
        task_dir = self.make_task()
        raw = f"API Error: 529 {KEY}\n"
        self.docker.outcomes = [{"stdout": stream_text(SID, with_result=False), "stderr": raw,
                                 "projects": self.projects()}, self.good()]
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "ok")
        self.assertIn("529", (task_dir / "run/attempt-1/stderr.log").read_text())
        self.assertEqual(len(self.docker.docker_calls("exec")), 2)
        self.assertNotIn(KEY, str(row))

    def test_failed_attempt_trace_key_is_archived_before_clean_retry(self):
        task_dir = self.make_task()
        projects = self.projects("leaked-trace", answer=f"debug {KEY}")
        self.docker.outcomes = [{"stdout": stream_text(SID, with_result=False), "rc": 1, "projects": projects,
                                 "effect": lambda ws: write(ws / "evidence.txt", "keep me\n")}, self.good()]
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "ok")
        self.assertFalse((task_dir / "workspace/evidence.txt").exists())
        exported = list((task_dir / "run/attempt-1/projects").rglob("*.jsonl"))
        self.assertEqual(len(exported), 1)
        self.assertIn(KEY, exported[0].read_text())
        self.assertEqual(len(self.docker.docker_calls("exec")), 2)

    def test_collect_key_preserves_original_and_records_platform_note(self):
        task_dir = self.make_task()
        fbr_run.bound_key(task_dir, self.cfg, start=True)
        lib.set_stage(task_dir, "run", "ok", attempts=1, session_id=SID)
        self.docker.containers.add("s11-000001")
        source = self.projects(answer=f"debug {KEY}")
        self.docker.projects["s11-000001"] = source
        original = next(source.rglob("*.jsonl")).read_bytes()
        row = fbr_run.collect_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "ok")
        target = next((task_dir / "trace/projects").rglob("*.jsonl"))
        self.assertEqual(target.read_bytes(), original)
        ids = lib.load_json(task_dir / "trace/ids.json")
        self.assertTrue(ids["api_key_read"])
        self.assertEqual(fbr_trace.run_note(ids), fbr_trace.API_KEY_NOTE)
        self.assertNotIn("s11-000001", self.docker.containers)
        self.assertNotIn(KEY, str(row))
        self.assertEqual(fbr_run.collect_one(task_dir, self.cfg)["state"], "skip")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(lib.require_stage(task_dir, "run")["session_id"], SID)

    def test_single_turn_rejects_repeated_user_rows_with_same_prompt_id(self):
        rows = trace_rows(SID)
        rows.append(dict(rows[0], uuid="again"))
        projects = self.tmp / "single-turn/projects"
        write_projects(projects, SID, rows)
        ids = fbr_trace.read_ids(projects, PROMPT)
        self.assertEqual(ids["user_turns"], 2)
        self.assertTrue(any("不是单轮" in p for p in ids["problems"]))

    def test_successful_preflight_displays_context_actual_value(self):
        with mock.patch.object(fbr_run.importlib.util, "find_spec", lambda name: object()):
            checks = fbr_run.preflight(self.cfg)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(fbr_run.print_checks(checks))
        self.assertIn("镜像里是 1000000", out.getvalue())
        version_call = [c for c in self.docker.calls if "--version" in c][0]
        self.assertNotIn("--rm", version_call)
        self.assertIn("--name", version_call)

    def test_stream_schema_is_checked_instead_of_accepting_missing_fields(self):
        cases = [
            [{"type": "result", "is_error": False, "result": "done", "session_id": SID}],
            [{"type": "system", "subtype": "init", "session_id": SID, "claude_code_version": "2.1.319"},
             {"type": "result", "result": "done", "session_id": SID}],
        ]
        for index, events in enumerate(cases):
            with self.subTest(index=index):
                path = write(self.tmp / f"schema-{index}.jsonl", "".join(json.dumps(r) + "\n" for r in events))
                info = fbr_trace.read_stream(path)
                verdict = fbr_trace.classify_attempt(0, False, info)
                self.assertNotEqual(verdict["verdict"], "accept")

    def test_rechecking_modified_workspace_restores_config_before_git(self):
        task_dir = self.make_task()
        lib.save_git_config(task_dir)
        config = task_dir / "workspace/.git/config"
        before = (task_dir / "run/git-config.before").read_bytes()
        config.write_text(config.read_text() + "\n[alias]\n    risky = !echo unsafe\n")
        seen = []
        original_git = lib.git

        def checked_git(*args, **kwargs):
            seen.append(config.read_bytes())
            return original_git(*args, **kwargs)

        with mock.patch.object(lib, "git", checked_git):
            fbr_run.ready_problems(task_dir, lib.load_task(task_dir), self.cfg)
        self.assertTrue(seen)
        self.assertTrue(all(data == before for data in seen))

    def test_git_config_restore_does_not_follow_model_symlink(self):
        task_dir = self.make_task()
        lib.save_git_config(task_dir)
        external = write(self.tmp / "outside-config", "keep external\n")
        config = task_dir / "workspace/.git/config"
        config.unlink()
        config.symlink_to(external)
        lib.restore_git_config(task_dir)
        self.assertEqual(external.read_text(), "keep external\n")
        self.assertFalse(config.is_symlink())
        self.assertEqual(config.read_bytes(), (task_dir / "run/git-config.before").read_bytes())

    def test_failed_or_dry_redo_invalidates_later_results(self):
        for state in ("failed", "dry"):
            with self.subTest(state=state):
                task_dir = self.make_task(10 if state == "failed" else 11)
                self.mark(task_dir, "feedback")
                lib.set_stage(task_dir, "record", state)
                stages = lib.load_state(task_dir)["stages"]
                self.assertNotIn("facts", stages)
                self.assertNotIn("feedback", stages)
