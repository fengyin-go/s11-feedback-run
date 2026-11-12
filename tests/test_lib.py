"""fbrlib：阶段状态机、任务读取、git 配置的备份与恢复、脱敏、步骤输出解析。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import fixtures
from fixtures import BATCH, KEY, Sandbox, sh, write

import fbrlib as lib


class StageMachine(Sandbox):
    def test_ok_drops_later_stages(self):
        task_dir = self.make_task(repo=False)
        self.mark(task_dir, "feedback")
        self.assertTrue(lib.stage_ok(task_dir, "feedback"))
        lib.set_stage(task_dir, "commit", "ok", commit="abc")
        stages = lib.load_state(task_dir)["stages"]
        self.assertEqual(sorted(stages), ["collect", "commit", "run"])
        self.assertEqual(stages["commit"]["commit"], "abc")

    def test_failed_drops_later_stages(self):
        task_dir = self.make_task(repo=False)
        self.mark(task_dir, "facts")
        lib.set_stage(task_dir, "record", "failed", problems=["画面是 640×480"])
        stages = lib.load_state(task_dir)["stages"]
        self.assertNotIn("facts", stages)
        self.assertIn("collect", stages)
        self.assertIn("commit", stages)
        self.assertFalse(lib.stage_ok(task_dir, "record"))

    def test_require_stage(self):
        task_dir = self.make_task(repo=False)
        with self.assertRaisesRegex(lib.FbrError, "run 阶段还没通过"):
            lib.require_stage(task_dir, "run")
        lib.set_stage(task_dir, "run", "ok", attempts=2)
        self.assertEqual(lib.require_stage(task_dir, "run")["attempts"], 2)


class Tasks(Sandbox):
    def test_settings_merge_and_required(self):
        self.assertEqual(self.cfg["concurrency"], 2)
        self.assertEqual(self.cfg["run"]["max_attempts"], 3)
        self.assertEqual(self.cfg["feedback_gate"]["min_chars"], 120)
        path = self.root / "_config" / "settings.json"
        data = lib.load_json(path)
        del data["image_id"]
        lib.save_json(path, data)
        with self.assertRaisesRegex(lib.FbrError, "settings.json 缺少 image_id"):
            lib.settings(self.root)

    def test_task_dirs(self):
        self.make_task(1, repo=False)
        self.make_task(2, repo=False)
        (self.root / "batches" / BATCH / "s11-000009").mkdir()   # 没有 task.json，不算
        names = [p.name for p in lib.task_dirs(self.root, BATCH)]
        self.assertEqual(names, ["s11-000001", "s11-000002"])
        self.assertEqual([p.name for p in lib.task_dirs(self.root, BATCH, "s11-000002")], ["s11-000002"])
        with self.assertRaisesRegex(lib.FbrError, "批次里没有这些任务"):
            lib.task_dirs(self.root, BATCH, "s11-000007")
        with self.assertRaisesRegex(lib.FbrError, "批次编号不合法"):
            lib.task_dirs(self.root, "batch-1")
        with self.assertRaisesRegex(lib.FbrError, "批次不存在"):
            lib.task_dirs(self.root, "b1111-01")

    def test_load_task_checks(self):
        task_dir = self.make_task(repo=False)
        self.assertEqual(lib.load_task(task_dir)["task_id"], "s11-000001")
        record = lib.load_json(task_dir / "task.json")
        lib.save_json(task_dir / "task.json", {**record, "task_id": "s11-000002"})
        with self.assertRaisesRegex(lib.FbrError, "和目录名"):
            lib.load_task(task_dir)
        record.pop("requirements")
        lib.save_json(task_dir / "task.json", record)
        with self.assertRaisesRegex(lib.FbrError, "缺少 requirements"):
            lib.load_task(task_dir)

    def test_json_helpers(self):
        path = self.tmp / "x" / "a.json"
        with self.assertRaisesRegex(lib.FbrError, "缺少文件"):
            lib.load_json(path)
        self.assertEqual(lib.load_json(path, default=[]), [])
        write(path, "{broken")
        with self.assertRaisesRegex(lib.FbrError, "不是合法 JSON"):
            lib.load_json(path)
        rows = self.tmp / "rows.jsonl"
        lib.append_jsonl(rows, {"a": 1})
        with rows.open("a", encoding="utf-8") as handle:
            handle.write("not json\n[1, 2]\n")
        lib.append_jsonl(rows, {"a": 2})
        self.assertEqual(lib.read_jsonl(rows), [{"a": 1}, {"a": 2}])


class GitConfig(Sandbox):
    def test_restore_puts_config_back_and_moves_hooks(self):
        task_dir = self.make_task()
        workspace = task_dir / "workspace"
        lib.save_git_config(task_dir)
        before = (workspace / ".git" / "config").read_text()
        # 被测助手在运行期间能改 .git：塞一个别名和一个钩子
        sh(workspace, "config", "alias.st", "!touch /tmp/should-not-run")
        marker = self.tmp / "hook-ran"
        hook = write(workspace / ".git" / "hooks" / "pre-commit", f"#!/bin/sh\ntouch '{marker}'\n")
        os.chmod(hook, 0o755)
        result = lib.restore_git_config(task_dir)
        self.assertTrue(result["config_restored"])
        self.assertEqual(result["hooks_moved"], ["pre-commit"])
        self.assertEqual((workspace / ".git" / "config").read_text(), before)
        self.assertIn("alias", (task_dir / "run" / "git-config.after").read_text())
        self.assertTrue((task_dir / "run" / "hooks-removed" / "pre-commit").is_file())
        self.assertFalse(hook.exists())
        # 再来一次什么也不用动
        again = lib.restore_git_config(task_dir)
        self.assertFalse(again["config_restored"])
        self.assertEqual(again["hooks_moved"], [])

    def test_save_does_not_overwrite(self):
        task_dir = self.make_task()
        lib.save_git_config(task_dir)
        before = (task_dir / "run" / "git-config.before").read_text()
        sh(task_dir / "workspace", "config", "alias.x", "status")
        lib.save_git_config(task_dir)
        self.assertEqual((task_dir / "run" / "git-config.before").read_text(), before)

    def test_refuses_without_backup(self):
        task_dir = self.make_task()
        with self.assertRaisesRegex(lib.FbrError, "没有起跑前的 git 配置备份"):
            lib.restore_git_config(task_dir)

    def test_host_git_ignores_hooks(self):
        task_dir = self.make_task()
        workspace = task_dir / "workspace"
        marker = self.tmp / "hook-ran"
        hook = write(workspace / ".git" / "hooks" / "pre-commit", f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        os.chmod(hook, 0o755)
        write(workspace / "new.txt", "x\n")
        lib.git_out(workspace, "add", "-A")
        lib.git_out(workspace, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(lib.FbrError, "git .* 失败"):
            lib.git_out(workspace, "rev-parse", "no-such-ref")

    def test_clean_clone(self):
        task_dir = self.make_task()
        workspace = task_dir / "workspace"
        commit = sh(workspace, "rev-parse", "HEAD")
        write(workspace / "untracked.txt", "x\n")
        with mock.patch.dict(os.environ, {"FBR_SCRATCH": str(self.tmp / "scratch")}):
            clone = lib.clean_clone(workspace, commit, "s11-selftest")
        self.addCleanup(fixtures.shutil.rmtree, clone.parent, True)
        self.assertEqual(clone.name, "workspace")
        self.assertTrue(clone.is_relative_to(self.tmp / "scratch"))
        self.assertEqual(sh(clone, "rev-parse", "HEAD"), commit)
        self.assertTrue((clone / "Makefile").is_file())
        self.assertFalse((clone / "untracked.txt").exists())


class Secrets(Sandbox):
    def test_read_key(self):
        self.assertEqual(lib.read_key(self.cfg), KEY)
        write(self.key_file, 'ANTHROPIC_AUTH_TOKEN="quoted-value-123456"\n')
        self.assertEqual(lib.read_key(self.cfg), "quoted-value-123456")
        self.key_file.unlink()
        self.assertEqual(lib.read_key(self.cfg), "")

    def test_redact(self):
        token = "ghp_" + "A1b2" * 9
        text = f"auth {KEY} / apikey=whatever-value / {token} / 普通内容"
        cleaned = lib.redact(text, [KEY])
        self.assertNotIn(KEY, cleaned)
        self.assertNotIn(token, cleaned)
        self.assertNotIn("whatever-value", cleaned)
        self.assertIn("apikey=***", cleaned)
        self.assertIn("普通内容", cleaned)
        # 太短的 secret 不替换，免得把正文搅烂
        self.assertEqual(lib.redact("abc 12345", ["12345"]), "abc 12345")

    def test_redact_basic_auth_form(self):
        import base64
        packed = base64.b64encode(f"x-access-token:{KEY}".encode()).decode()
        self.assertNotIn(packed, lib.redact(f"Authorization: Basic {packed}", [KEY]))

    def test_key_check_preserves_file(self):
        path = write(self.tmp / "stream.jsonl", f'{{"a": "{KEY}"}}\n{{"b": "{KEY}"}}\nclean\n')
        original = path.read_bytes()
        self.assertEqual(lib.key_occurrences(path, KEY), 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(lib.key_occurrences(path, KEY), 2)
        self.assertEqual(lib.key_occurrences(self.tmp / "missing.log", KEY), 0)


class Steps(unittest.TestCase):
    STEPS = [{"name": "setup", "cmd": "make setup", "timeout": 10}, {"name": "test", "cmd": "make test", "timeout": 10},
             {"name": "probe", "cmd": "true", "timeout": 10}]

    def test_parse_steps(self):
        output = "\n".join(["noise", "@@FBR step=0 rc=0 secs=3", "installed", "@@FBR end=0",
                            "@@FBR step=1 rc=2 secs=7", "FAIL: test_a", "Ran 3 tests", "@@FBR end=1"])
        rows = lib.parse_steps(self.STEPS, output)
        self.assertEqual([r["rc"] for r in rows], [0, 2, None])
        self.assertEqual(rows[0]["tail"], "installed")
        self.assertEqual(rows[1]["tail"], "FAIL: test_a\nRan 3 tests")
        self.assertEqual(rows[1]["seconds"], 7)
        self.assertEqual(rows[2]["tail"], "")

    def test_run_steps_command_shape(self):
        seen = {}

        def fake(command, **kwargs):
            if command[:2] == ["docker", "run"]:
                seen["command"] = command
            return fixtures.done(0, "@@FBR step=0 rc=0 secs=1\nok\n@@FBR end=0\n")

        with mock.patch.object(lib, "run", fake):
            rows = lib.run_steps_in_image("img:1", fixtures.SCRIPTS, self.STEPS[:1])
        command = seen["command"]
        self.assertEqual(command[:2], ["docker", "run"])
        self.assertNotIn("--rm", command)
        self.assertEqual(rows[0]["container_name"], command[command.index("--name") + 1])
        for flag in ("--init", "--restart=no", "--cap-drop", "no-new-privileges"):
            self.assertIn(flag, command)
        self.assertNotIn("--env-file", command)   # 取证和实跑的容器不带 key
        self.assertNotIn("-e", command)
        self.assertEqual(rows[0]["rc"], 0)

    def test_mount_arg(self):
        self.assertEqual(lib.mount_arg(fixtures.SCRIPTS), f"type=bind,src={fixtures.SCRIPTS},dst=/workspace")
        with self.assertRaisesRegex(lib.FbrError, "挂载路径不能带逗号"):
            lib.mount_arg(fixtures.SCRIPTS / "a,b")

    def test_run_missing_command_and_text_helpers(self):
        result = lib.run(["fbr-no-such-command-xyz"])
        self.assertEqual(result.returncode, 127)
        self.assertEqual(lib.normalize("Make  Test，通过_了！"), "maketest通过了")
        self.assertEqual(lib.han_count("abc 测试 12 通过"), 4)
        self.assertEqual(lib.clip("abcdef", 3), "abc…")


if __name__ == "__main__":
    unittest.main()
