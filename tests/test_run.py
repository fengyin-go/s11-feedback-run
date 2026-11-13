"""fbr_run：预检、起跑前检查、单轮运行与重跑、轨迹收集。docker 全部是假的。"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from fixtures import (BATCH, IMAGE, KEY, PROMPT, TOOLS, FakeDocker, Sandbox, sh, stream_text, trace_rows, write,
                      write_projects)

import fbrlib as lib
import fbr_run

SID = "aaaaaaaa-0000-4000-8000-000000000001"


class DockerCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()
        doctor = mock.patch("fbr_record.doctor", return_value={"ok": True, "problems": []})
        doctor.start()
        self.addCleanup(doctor.stop)
        for name in ("run", "run_stream"):
            patcher = mock.patch.object(lib, name, getattr(self.docker, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def projects(self, label="p1", session_id=SID, **kwargs):
        target = self.tmp / "premade" / label / "projects"
        write_projects(target, session_id, trace_rows(session_id, tools=TOOLS, **kwargs))
        return target

    def good(self, **more):
        return {"stdout": stream_text(SID), "projects": self.projects(),
                "effect": lambda ws: write(ws / "src/clipcut/packer.py", "def pack(path, segment):\n    return []\n"),
                **more}


class Preflight(DockerCase):
    def checks(self):
        with mock.patch.object(fbr_run.importlib.util, "find_spec", lambda name: object()):
            return {c["name"]: c for c in fbr_run.preflight(self.cfg)}

    def test_all_ok(self):
        checks = self.checks()
        self.assertEqual(len(checks), 5)
        self.assertTrue(all(c["ok"] for c in checks.values()), checks)
        version_call = [c for c in self.docker.calls if "--version" in c][0]
        self.assertNotIn("--rm", version_call)
        self.assertIn("--name", version_call)
        self.assertNotIn("--env-file", version_call)

    def test_mismatches(self):
        self.docker.image_id = "sha256:" + "00" * 32
        self.docker.context_limit = "200000"
        self.docker.version_line = "2.2.0 (Claude Code)"
        os.chmod(self.key_file, 0o644)
        checks = self.checks()
        for name in ("镜像摘要", "context 上限", "Claude Code 版本", "key 文件"):
            self.assertFalse(checks[name]["ok"], name)
        self.assertIn("200000", checks["context 上限"]["detail"])
        self.assertIn("644", checks["key 文件"]["detail"])
        self.assertNotIn(KEY, str(checks))

    def test_missing_key_and_deps(self):
        self.key_file.unlink()
        with mock.patch.object(fbr_run.importlib.util, "find_spec", lambda name: None):
            checks = {c["name"]: c for c in fbr_run.preflight(self.cfg)}
        self.assertIn("不存在", checks["key 文件"]["detail"])
        self.assertIn("openpyxl", checks["录屏与表格依赖"]["detail"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(fbr_run.print_checks(list(checks.values())))
        self.assertIn("不符  key 文件", out.getvalue())


class Ready(DockerCase):
    def problems(self, task_dir):
        return fbr_run.ready_problems(task_dir, lib.load_task(task_dir), self.cfg)

    def test_clean_task_is_ready(self):
        self.assertEqual(self.problems(self.make_task()), [])

    def test_unpublished_and_config_drift(self):
        task_dir = self.make_task(publish={"state": "pending"}, harness_version="2.0.1")
        found = self.problems(task_dir)
        self.assertIn("任务仓库还没发布（初始环境快照没有地址）", found)
        self.assertIn("task.json 里的镜像或 Harness 版本和当前配置不一致", found)

    def test_dirty_moved_and_container(self):
        task_dir = self.make_task()
        workspace = task_dir / "workspace"
        write(workspace / ".env", "X=1\n")      # 被忽略的文件也算不干净
        self.assertEqual(self.problems(task_dir), ["workspace 不干净（有未提交或被忽略的文件）"])
        (workspace / ".env").unlink()
        write(workspace / "extra.txt", "x\n")
        sh(workspace, "add", "-A")
        sh(workspace, "commit", "-q", "-m", "moved")
        self.assertEqual(self.problems(task_dir), ["workspace 的 HEAD 不是初始提交"])
        self.docker.containers.add("s11-000001")
        self.assertTrue(any("已有同名容器" in p for p in self.problems(task_dir)))

    def test_not_a_repo(self):
        self.assertEqual(self.problems(self.make_task(repo=False)), ["workspace 不是 git 仓库"])


class RunOne(DockerCase):
    def test_accept_first_attempt(self):
        task_dir = self.make_task()
        self.docker.outcomes = [self.good()]
        outcome = self.docker.outcomes[0]
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("ok", "第 1 次运行算数"))
        command = self.docker.docker_calls("run", "-i")[0]
        self.assertEqual(command[-2:], [IMAGE, "infinity"])
        self.assertEqual(self.docker.docker_calls("exec")[0][-2:], ["/usr/local/bin/entrypoint.sh", "print"])
        self.assertNotIn("--rm", command)
        self.assertEqual(command[command.index("--name") + 1], "s11-000001")
        self.assertEqual(command[command.index("--env-file") + 1], str(self.key_file))
        self.assertEqual(command[command.index("--mount") + 1],
                         f"type=bind,src={(task_dir / 'workspace').resolve()},dst=/workspace")
        for flag in ("--init", "--restart=no", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"):
            self.assertIn(flag, command)
        self.assertFalse(any(KEY in part for call in self.docker.calls for part in call))   # key 不进命令行
        self.assertEqual(outcome["stdin"], PROMPT)
        self.assertEqual((task_dir / "run" / "prompt.txt").read_text(encoding="utf-8"), PROMPT)
        self.assertTrue((task_dir / "run" / "git-config.before").is_file())
        info = lib.require_stage(task_dir, "run")
        self.assertEqual((info["attempts"], info["session_id"]), (1, SID))
        attempts = lib.load_json(task_dir / "run" / "attempts.json")
        self.assertEqual(attempts[0]["verdict"], "accept")
        self.assertNotIn("key_scrubbed", attempts[0])
        self.assertNotIn(KEY, (task_dir / "run" / "stream.jsonl").read_text())
        self.assertIn("s11-000001", self.docker.containers)     # 轨迹还没导出，容器留着
        self.assertEqual(self.docker.docker_calls("rm"), [])
        self.assertEqual(fbr_run.run_one(task_dir, self.cfg)["state"], "skip")

    def test_retry_then_accept(self):
        task_dir = self.make_task()
        workspace = task_dir / "workspace"
        initial = lib.load_task(task_dir)["initial_commit"]

        def half_done(ws):
            write(ws / "half.txt", "half\n")
            write(ws / ".env", "LEFTOVER=1\n")
            sh(ws, "config", "alias.zz", "status")

        self.docker.outcomes = [
            {"stdout": stream_text(SID, with_result=False), "stderr": "API Error: 529 overloaded\n", "rc": 1,
             "projects": self.projects("p0"), "effect": half_done},
            self.good()]
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("ok", "第 2 次运行算数"))
        archive = task_dir / "run" / "attempt-1"
        self.assertTrue((archive / "stream.jsonl").is_file())
        self.assertIn("529", (archive / "stderr.log").read_text())
        self.assertTrue(any((archive / "projects").rglob("*.jsonl")))
        self.assertIn("half.txt", (archive / "diff.patch").read_text())
        self.assertFalse((workspace / "half.txt").exists())
        self.assertFalse((workspace / ".env").exists())
        self.assertNotIn("alias", (workspace / ".git" / "config").read_text())
        self.assertEqual(sh(workspace, "rev-parse", "HEAD"), initial)
        attempts = lib.load_json(task_dir / "run" / "attempts.json")
        self.assertEqual([(a["verdict"], a["reason"]) for a in attempts], [("retry", "网关报错"), ("accept", "")])
        self.assertEqual(len(self.docker.docker_calls("rm")), 1)
        self.assertEqual(lib.require_stage(task_dir, "run")["attempts"], 2)

    def test_three_failures_give_up(self):
        task_dir = self.make_task()
        self.docker.outcomes = [{"stdout": stream_text(SID, with_result=False), "rc": 1, "projects": self.projects(f"p{i}")}
                                for i in range(4)]
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "deferred")
        self.assertEqual(row["detail"], "max_attempts")
        self.assertEqual(len(self.docker.outcomes), 1)      # 只跑了 3 次
        self.assertEqual(lib.load_state(task_dir)["stages"]["run"]["state"], "deferred")
        self.assertEqual(self.docker.containers, set())
        self.assertEqual(sh(task_dir / "workspace", "status", "--porcelain", "--ignored"), "")
        # 用尽之后再跑也不会再起容器
        self.assertEqual(fbr_run.run_one(task_dir, self.cfg)["state"], "deferred")
        self.assertEqual(len(self.docker.outcomes), 1)

    def test_timeout_kills_container(self):
        task_dir = self.make_task()
        self.docker.outcomes = [{"stdout": "", "rc": 124, "timed_out": True, "projects": self.projects("p0")},
                                self.good()]
        self.assertEqual(fbr_run.run_one(task_dir, self.cfg)["state"], "ok")
        self.assertEqual(self.docker.docker_calls("kill"), [["docker", "kill", "s11-000001"]])
        self.assertIn("未保留具体原因", lib.load_json(task_dir / "run" / "attempts.json")[0]["reason"])

    def test_archive_failure_keeps_container(self):
        task_dir = self.make_task()
        self.docker.outcomes = [{"stdout": stream_text(SID, with_result=False), "rc": 1}, self.good()]
        self.docker.cp_ok = False
        with self.assertRaisesRegex(lib.FbrError, "轨迹导出失败，容器先留着"):
            fbr_run.run_one(task_dir, self.cfg)
        self.assertIn("s11-000001", self.docker.containers)
        self.assertEqual(self.docker.docker_calls("rm"), [])
        self.assertEqual(len(self.docker.outcomes), 1)

    def test_blocked_task_does_not_start(self):
        task_dir = self.make_task(publish={"state": "pending"})
        row = fbr_run.run_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(self.docker.docker_calls("run"), [])


class Collect(DockerCase):
    def ran(self, session_id=SID, **kwargs):
        task_dir = self.make_task()
        fbr_run.bound_key(task_dir, self.cfg, start=True)
        lib.set_stage(task_dir, "run", "ok", attempts=1, session_id=SID)
        self.docker.containers.add("s11-000001")
        self.docker.projects["s11-000001"] = self.projects(session_id=session_id, **kwargs)
        return task_dir

    def test_collect_ok(self):
        task_dir = self.ran()
        row = fbr_run.collect_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("ok", f"SessionID {SID}"))
        cp = self.docker.docker_calls("cp")[0]
        self.assertEqual(cp[2], "s11-000001:/home/node/.claude/projects")
        ids = lib.load_json(task_dir / "trace" / "ids.json")
        self.assertEqual((ids["session_id"], ids["prompt_id"], ids["user_turns"]), (SID, "prm-0001", 1))
        self.assertTrue((task_dir / "trace" / ids["session_file"]).is_file())
        self.assertEqual(self.docker.docker_calls("rm"), [["docker", "rm", "s11-000001"]])
        self.assertTrue(lib.require_stage(task_dir, "collect")["container_removed"])
        self.assertEqual(fbr_run.collect_one(task_dir, self.cfg)["state"], "skip")

    def test_keep_container(self):
        task_dir = self.ran()
        self.assertEqual(fbr_run.collect_one(task_dir, self.cfg, keep_container=True)["state"], "ok")
        self.assertIn("s11-000001", self.docker.containers)
        self.assertFalse(lib.require_stage(task_dir, "collect")["container_removed"])

    def test_cp_failure_keeps_container(self):
        task_dir = self.ran()
        self.docker.cp_ok = False
        row = fbr_run.collect_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "failed")
        self.assertIn("容器先留着", row["detail"])
        self.assertIn("s11-000001", self.docker.containers)

    def test_bad_trace_keeps_container(self):
        cases = [({"session_id": "bbbbbbbb-0000-4000-8000-000000000002"}, "sessionId 和运行输出里的不一致"),
                 ({"version": "2.0.0"}, "轨迹里的版本 2.0.0 不是 2.1.319"),
                 ({"prompt": "换了一段输入"}, "用户输入和题面不一致")]
        for index, (kwargs, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                self.setUp()
                task_dir = self.ran(**kwargs)
                row = fbr_run.collect_one(task_dir, self.cfg)
                self.assertEqual(row["state"], "failed")
                self.assertIn(expected, row["detail"])
                self.assertTrue(row["detail"].endswith("（容器没删）"))
                self.assertIn("s11-000001", self.docker.containers)
                self.assertFalse((task_dir / "trace" / "ids.json").exists())

    def test_key_in_trace_stops_and_keeps_original(self):
        task_dir = self.ran(answer=f"完成了，顺便打印了环境变量 {KEY}")
        self.assertEqual(fbr_run.collect_one(task_dir, self.cfg)["state"], "failed")
        self.assertFalse((task_dir / "trace" / "ids.json").exists())
        self.assertIn(KEY, next((task_dir / "trace/projects").rglob("*.jsonl")).read_text())
        self.assertIn("s11-000001", self.docker.containers)

    def test_container_gone(self):
        task_dir = self.ran()
        self.docker.containers.clear()
        row = fbr_run.collect_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("failed", "容器不在了，本地也没有导出的轨迹"))
        # 本地已经有导出的轨迹时可以接着用
        fbr_run.shutil.copytree(self.docker.projects["s11-000001"], task_dir / "trace" / "projects")
        self.assertEqual(fbr_run.collect_one(task_dir, self.cfg)["state"], "ok")

    def test_needs_run_stage(self):
        with self.assertRaisesRegex(lib.FbrError, "run 阶段还没通过"):
            fbr_run.collect_one(self.make_task(), self.cfg)


class CommandLine(DockerCase):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        argv = ["fbr_run.py", "--root", str(self.root), *args]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(fbr_run.importlib.util, "find_spec", lambda name: object()), \
                mock.patch.object(fbr_run.lib, "active_github_account", return_value="selftest"):
            code = fbr_run.main()
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_starts_nothing(self):
        self.make_task(1)
        self.make_task(2, publish={"state": "pending"})
        code, out, _ = self.call("run", "--batch", BATCH)
        self.assertEqual(code, 1)
        self.assertIn("s11-000001  可以跑", out)
        self.assertIn("s11-000002  不能跑", out)
        self.assertIn("待运行 2 个，其中 1 个没准备好", out)
        self.assertEqual(self.docker.docker_calls("run"), [])
        self.assertNotIn(KEY, out)

    def test_execute_stops_when_preflight_fails(self):
        self.make_task(1)
        self.docker.context_limit = "200000"
        code, _, err = self.call("run", "--batch", BATCH, "--execute")
        self.assertEqual(code, 2)
        self.assertIn("预检没过，不起跑", err)
        self.assertEqual(self.docker.docker_calls("run", "-i"), [])

    def test_execute_runs_and_collects(self):
        task_dir = self.make_task(1)
        self.docker.outcomes = [self.good()]
        code, out, _ = self.call("run", "--batch", BATCH, "--execute")
        self.assertEqual(code, 0, out)
        self.assertIn("s11-000001  ok  第 1 次运行算数", out)
        code, out, _ = self.call("collect", "--batch", BATCH)
        self.assertEqual(code, 0, out)
        self.assertTrue(lib.stage_ok(task_dir, "collect"))


if __name__ == "__main__":
    unittest.main()
