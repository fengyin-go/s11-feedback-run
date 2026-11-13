"""fbr_snapshot：宿主机提交、密钥拦截、被改动的 git 配置，以及推送（远端是本地裸仓库，hub 是假的）。"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from fixtures import BATCH, KEY, OWNER, Sandbox, done, sh, write

import fbrlib as lib
import fbr_snapshot as snap

HUB_TOKEN = "selftest-hub-token-000111222333"
ME = ("-c", "user.name=Model", "-c", "user.email=model@example.com")


class FakeHub:
    """远端是临时目录里的裸仓库，git 真跑，不联网。"""

    def __init__(self, base: Path, *, public: bool = True, push: str = "real") -> None:
        self.base, self.owner, self.token = base, OWNER, HUB_TOKEN
        self.public, self.push_mode, self.calls = public, push, []

    def url(self, full: str) -> str:
        return str(self.base / (full.replace("/", "__") + ".git"))

    def git(self, repo, *args, timeout=300):
        self.calls.append(list(args))
        if args[0] == "push" and self.push_mode == "noop":
            return done(0)
        if args[0] == "push" and self.push_mode == "fail":
            return done(1, "", f"remote: denied for x-access-token:{HUB_TOKEN}")
        return subprocess.run(["git", *lib.GIT_SAFE, *args], cwd=repo, capture_output=True, text=True)

    def remote_main(self, repo, url):
        lines = self.git(repo, "ls-remote", url, "refs/heads/main").stdout.strip().splitlines()
        return lines[0].split()[0] if lines else None

    def is_public(self, full):
        return self.public

    def login(self):
        return self.owner

    def pushes(self):
        return [c for c in self.calls if c[0] == "push"]


class SnapshotCase(Sandbox):
    def collected(self, number=1, **extra):
        task_dir = self.make_task(number, **extra)
        lib.save_git_config(task_dir)
        self.mark(task_dir, "collect")
        return task_dir, task_dir / "workspace"

    def remote(self, hub, task_dir, publish=True) -> Path:
        record = lib.load_task(task_dir)
        url = Path(hub.url(record["task_repo_full"]))
        url.parent.mkdir(parents=True, exist_ok=True)
        sh(self.tmp, "init", "-q", "--bare", "-b", "main", str(url))
        if publish:
            sh(task_dir / "workspace", "push", "-q", str(url), f"{record['initial_commit']}:refs/heads/main")
        return url

    def snapshot(self, task_dir):
        return lib.load_json(task_dir / "review" / "snapshot.json")


class Commit(SnapshotCase):
    def test_host_commits_leftovers(self):
        task_dir, ws = self.collected()
        initial = lib.load_task(task_dir)["initial_commit"]
        write(ws / "src/clipcut/packer.py", "def pack(path, segment):\n    return [path] * 6\n")
        write(ws / "src/clipcut/index.py", "def index():\n    return {}\n")
        row = snap.commit_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "ok")
        info = self.snapshot(task_dir)
        self.assertEqual(row["detail"], f"{info['commit'][:12]}，改动 2 个文件，模型自己提交 0 次")
        self.assertEqual((info["host_commit"], info["model_commits"], info["history_rewritten"], info["no_change"]),
                         (True, 0, False, False))
        self.assertEqual((info["pushed"], info["artifact_snapshot"], info["branch"]), (False, "", "main"))
        self.assertEqual(sh(ws, "rev-parse", "HEAD"), info["commit"])
        self.assertEqual(sh(ws, "rev-parse", "HEAD^"), initial)
        self.assertEqual(sh(ws, "log", "-1", "--format=%an <%ae>|%s"),
                         "Self Test <selftest@example.com>|" + snap.COMMIT_MESSAGE)
        self.assertEqual(sh(ws, "status", "--porcelain"), "")
        self.assertIn("index.py", (task_dir / "review" / "diff.patch").read_text())
        self.assertEqual(lib.require_stage(task_dir, "commit")["commit"], info["commit"])
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["state"], "skip")

    def test_model_commits_are_kept(self):
        task_dir, ws = self.collected()
        initial = lib.load_task(task_dir)["initial_commit"]
        write(ws / "a.txt", "a\n")
        sh(ws, "add", "-A")
        sh(ws, *ME, "commit", "-q", "-m", "feat: segment")
        model_head = sh(ws, "rev-parse", "HEAD")
        write(ws / "b.txt", "b\n")       # 模型没提交的尾巴
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["state"], "ok")
        info = self.snapshot(task_dir)
        self.assertEqual((info["model_commits"], info["host_commit"], info["changed_files"]), (1, True, 2))
        self.assertEqual(sh(ws, "rev-list", f"{initial}..{info['commit']}").splitlines(), [info["commit"], model_head])
        self.assertEqual(sh(ws, "log", "-1", "--format=%an", model_head), "Model")    # 模型的提交原样保留

    def test_everything_already_committed(self):
        task_dir, ws = self.collected()
        write(ws / "a.txt", "a\n")
        sh(ws, "add", "-A")
        sh(ws, *ME, "commit", "-q", "-m", "done")
        head = sh(ws, "rev-parse", "HEAD")
        snap.commit_one(task_dir, self.cfg)
        info = self.snapshot(task_dir)
        self.assertEqual((info["commit"], info["host_commit"], info["model_commits"]), (head, False, 1))

    def test_no_change(self):
        task_dir, ws = self.collected()
        row = snap.commit_one(task_dir, self.cfg)
        info = self.snapshot(task_dir)
        self.assertEqual(row["state"], "ok")
        self.assertEqual((info["no_change"], info["changed_files"], info["host_commit"]), (True, 0, False))
        self.assertEqual(info["commit"], info["initial_commit"])
        self.assertEqual((task_dir / "review" / "diff.patch").read_text(), "")

    def test_rewritten_history(self):
        task_dir, ws = self.collected()
        initial = lib.load_task(task_dir)["initial_commit"]
        write(ws / "a.txt", "a\n")
        sh(ws, "add", "-A")
        sh(ws, *ME, "commit", "-q", "--amend", "-m", "rewritten")     # 初始提交被改写
        write(ws / "b.txt", "b\n")
        row = snap.commit_one(task_dir, self.cfg)
        info = self.snapshot(task_dir)
        self.assertTrue(row["detail"].endswith("，历史被改写过"))
        self.assertEqual((info["history_rewritten"], info["host_commit"], info["model_commits"]), (True, True, 0))
        self.assertEqual(sh(ws, "rev-parse", f"{info['commit']}^"), initial)
        self.assertEqual(sh(ws, "diff", "--name-only", initial, info["commit"]).splitlines(), ["a.txt", "b.txt"])

    def test_secrets_block_the_commit(self):
        token = "ghp_" + "A1b2" * 9
        cases = [
            (lambda ws: write(ws / "deploy/.env.local", "X=1\n"), "密钥类文件进了改动：deploy/.env.local"),
            (lambda ws: write(ws / "deploy/id_rsa", "x\n"), "密钥类文件进了改动：deploy/id_rsa"),
            (lambda ws: write(ws / "notes.txt", f"gateway {KEY}\n"), "网关 key 原文出现在 notes.txt"),
            (lambda ws: write(ws / "ci.yml", f"token: {token}\n"), "token 或私钥形态的内容出现在 ci.yml"),
            (lambda ws: (write(ws / ".env", "X=1\n"), sh(ws, "add", "-f", ".env"),
                         sh(ws, *ME, "commit", "-q", "-m", "oops")), "密钥类文件进了改动：.env"),
        ]
        for number, (effect, expected) in enumerate(cases, 1):
            with self.subTest(expected=expected):
                task_dir, ws = self.collected(number)
                effect(ws)
                head = sh(ws, "rev-parse", "HEAD")
                row = snap.commit_one(task_dir, self.cfg)
                self.assertEqual((row["state"], row["detail"]), ("blocked", expected))
                self.assertNotIn(KEY, row["detail"])
                self.assertFalse((task_dir / "review" / "snapshot.json").exists())
                self.assertEqual(lib.load_state(task_dir)["stages"]["commit"]["state"], "failed")
                self.assertEqual(sh(ws, "rev-parse", "HEAD"), head)             # 没有新提交
                self.assertEqual(sh(ws, "diff", "--cached", "--name-only"), "")  # 暂存区退回去了

    def test_harmless_names_and_waivers(self):
        task_dir, ws = self.collected()
        write(ws / ".env.example", "X=\n")
        write(ws / "certs/test.pem", "not a real key\n")
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["detail"], "密钥类文件进了改动：certs/test.pem")
        lib.save_json(task_dir / "review" / "secret-waivers.json", [{"path": "certs/test.pem"}])      # 没写原因不算
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["state"], "blocked")
        lib.save_json(task_dir / "review" / "secret-waivers.json",
                      [{"path": "certs/test.pem", "reason": "测试夹具里的假证书"}])
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["state"], "ok")
        self.assertEqual(self.snapshot(task_dir)["changed_files"], 2)

    def test_secrets_in_intermediate_model_commits_are_blocked(self):
        for number, path, content, expected in (
            (1, ".env", "X=1\n", "密钥类文件进了改动：.env"),
            (2, "notes.txt", f"gateway {KEY}\n", "网关 key 原文出现在 notes.txt"),
            (3, "db.txt", "postgresql://demo:password@db.example.invalid/store\n",
             "token 或私钥形态的内容出现在 db.txt"),
        ):
            with self.subTest(path=path):
                task_dir, ws = self.collected(number)
                write(ws / path, content)
                sh(ws, "add", "-f", path)
                sh(ws, *ME, "commit", "-qm", "add transient content")
                (ws / path).unlink()
                sh(ws, "add", "-A")
                sh(ws, *ME, "commit", "-qm", "remove transient content")
                head = sh(ws, "rev-parse", "HEAD")
                row = snap.commit_one(task_dir, self.cfg)
                self.assertEqual((row["state"], row["detail"]), ("blocked", expected))
                self.assertNotIn(KEY, row["detail"])
                self.assertEqual(sh(ws, "rev-parse", "HEAD"), head)
                self.assertFalse((task_dir / "review" / "snapshot.json").exists())

    def test_big_file(self):
        task_dir, ws = self.collected()
        write(ws / "out/big.bin", "x" * 64)
        with mock.patch.object(snap, "MAX_FILE_BYTES", 16):
            row = snap.commit_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("blocked", "单个文件超过 50MB：out/big.bin"))

    def test_binary_gateway_key_in_final_tree_and_history_is_blocked(self):
        for number, transient in ((1, False), (2, True)):
            with self.subTest(transient=transient):
                task_dir, ws = self.collected(number)
                (ws / "binary.dat").write_bytes(b"\x00\xff" + KEY.encode() + b"\x00\xfe")
                if transient:
                    sh(ws, "add", "binary.dat")
                    sh(ws, *ME, "commit", "-qm", "add binary")
                    (ws / "binary.dat").unlink()
                    sh(ws, "add", "-A")
                    sh(ws, *ME, "commit", "-qm", "remove binary")
                row = snap.commit_one(task_dir, self.cfg)
                self.assertEqual((row["state"], row["detail"]),
                                 ("blocked", "网关 key 原文出现在 binary.dat"))
                self.assertNotIn(KEY, str(row))
                self.assertFalse((task_dir / "review" / "snapshot.json").exists())

    def test_gateway_key_in_retained_commit_message_is_blocked(self):
        task_dir, ws = self.collected()
        write(ws / "notes.txt", "harmless content\n")
        sh(ws, "add", "notes.txt")
        sh(ws, *ME, "commit", "-qm", f"model message {KEY}")
        row = snap.commit_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("blocked", "网关 key 原文出现在模型提交消息"))
        self.assertNotIn(KEY, str(row))
        self.assertFalse((task_dir / "review" / "snapshot.json").exists())

    def test_sample_waiver_cannot_waive_exact_gateway_key(self):
        for number, binary in ((1, False), (2, True)):
            with self.subTest(binary=binary):
                task_dir, ws = self.collected(number)
                payload = KEY.encode() if not binary else b"\x00\xff" + KEY.encode()
                (ws / "sample.pem").write_bytes(payload)
                lib.save_json(task_dir / "review" / "secret-waivers.json",
                              [{"path": "sample.pem", "reason": "样例证书"}])
                row = snap.commit_one(task_dir, self.cfg)
                self.assertEqual((row["state"], row["detail"]),
                                 ("blocked", "网关 key 原文出现在 sample.pem"))
                self.assertNotIn(KEY, str(row))

    def test_nested_repo_is_left_out(self):
        task_dir, ws = self.collected()
        inner = ws / "vendor" / "lib"
        inner.mkdir(parents=True)
        sh(inner, "init", "-q", "-b", "main")
        write(inner / "x.txt", "x\n")
        sh(inner, "add", "-A")
        sh(inner, *ME, "commit", "-q", "-m", "inner")
        write(ws / "a.txt", "a\n")
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["state"], "ok")
        info = self.snapshot(task_dir)
        self.assertEqual(info["excluded"], ["vendor/lib"])
        self.assertEqual(sh(ws, "diff", "--name-only", info["initial_commit"], info["commit"]), "a.txt")

    def test_tampered_git_setup_does_not_run(self):
        task_dir, ws = self.collected()
        before = (ws / ".git" / "config").read_text()
        marker = self.tmp / "ran"
        for path in (ws / ".githooks" / "pre-commit", ws / ".git" / "hooks" / "pre-commit"):
            os.chmod(write(path, f"#!/bin/sh\ntouch '{marker}'\nexit 1\n"), 0o755)
        sh(ws, "config", "core.hooksPath", ".githooks")
        sh(ws, "config", "user.name", "Somebody Else")
        sh(ws, "config", "core.fsmonitor", f"touch '{marker}'")
        self.assertEqual(snap.commit_one(task_dir, self.cfg)["state"], "ok")
        self.assertFalse(marker.exists())
        hygiene = self.snapshot(task_dir)["git_hygiene"]
        self.assertTrue(hygiene["config_restored"])
        self.assertEqual(hygiene["hooks_moved"], ["pre-commit"])
        self.assertEqual((ws / ".git" / "config").read_text(), before)
        self.assertEqual(sh(ws, "log", "-1", "--format=%an"), "Self Test")

    def test_preconditions(self):
        task_dir = self.make_task()
        with self.assertRaisesRegex(lib.FbrError, "collect 阶段还没通过"):
            snap.commit_one(task_dir, self.cfg)
        self.mark(task_dir, "collect")
        with self.assertRaisesRegex(lib.FbrError, "没有起跑前的 git 配置备份"):
            snap.commit_one(task_dir, self.cfg)


class Push(SnapshotCase):
    def committed(self, number=1, change=True, **extra):
        task_dir, ws = self.collected(number, **extra)
        if change:
            write(ws / "a.txt", "a\n")
        snap.commit_one(task_dir, self.cfg)
        return task_dir, ws

    def test_push_ok(self):
        task_dir, ws = self.committed()
        hub = FakeHub(self.tmp / "remotes")
        url = self.remote(hub, task_dir)
        commit = self.snapshot(task_dir)["commit"]
        row = snap.push_one(hub, task_dir, self.cfg)
        link = f"https://github.com/{OWNER}/s11-000001-clipcut/commit/{commit}"
        self.assertEqual((row["state"], row["detail"]), ("ok", link))
        self.assertRegex(link, r"^https://github\.com/[\w.-]+/[\w.-]+/commit/[0-9a-f]{40}$")
        self.assertEqual(sh(self.tmp, "--git-dir", str(url), "rev-parse", "refs/heads/main"), commit)
        info = self.snapshot(task_dir)
        self.assertEqual((info["pushed"], info["artifact_snapshot"]), (True, link))
        self.assertEqual(hub.pushes(), [["push", str(url), f"{commit}:refs/heads/main"]])    # 没有 --force，也没有 +
        self.assertNotIn(HUB_TOKEN, (ws / ".git" / "config").read_text())
        self.assertEqual(snap.push_one(hub, task_dir, self.cfg)["state"], "skip")
        self.assertEqual(len(hub.pushes()), 1)

    def test_no_change_needs_no_push(self):
        task_dir, _ = self.committed(change=False)
        hub = FakeHub(self.tmp / "remotes")
        self.remote(hub, task_dir)
        self.assertEqual(snap.push_one(hub, task_dir, self.cfg)["state"], "ok")
        self.assertEqual(hub.pushes(), [])
        info = self.snapshot(task_dir)
        self.assertTrue(info["artifact_snapshot"].endswith(info["initial_commit"]))

    def test_rewritten_history_pushes_on_top_of_initial(self):
        task_dir, ws = self.collected()
        write(ws / "a.txt", "a\n")
        sh(ws, "add", "-A")
        sh(ws, *ME, "commit", "-q", "--amend", "-m", "rewritten")
        snap.commit_one(task_dir, self.cfg)
        hub = FakeHub(self.tmp / "remotes")
        url = self.remote(hub, task_dir)
        self.assertEqual(snap.push_one(hub, task_dir, self.cfg)["state"], "ok")
        info = self.snapshot(task_dir)
        self.assertEqual(sh(self.tmp, "--git-dir", str(url), "rev-parse", "refs/heads/main^"), info["initial_commit"])

    def test_remote_moved_is_not_forced(self):
        task_dir, _ = self.committed()
        hub = FakeHub(self.tmp / "remotes")
        url = self.remote(hub, task_dir)
        other = self.tmp / "other"
        sh(self.tmp, "clone", "-q", str(url), str(other))
        write(other / "someone.txt", "x\n")
        sh(other, "add", "-A")
        sh(other, *ME, "commit", "-q", "-m", "someone else")
        sh(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        moved = sh(other, "rev-parse", "HEAD")
        with self.assertRaisesRegex(lib.FbrError, "不是产物提交的祖先；不强推，先人工确认"):
            snap.push_one(hub, task_dir, self.cfg)
        self.assertEqual(hub.pushes(), [])
        self.assertEqual(sh(self.tmp, "--git-dir", str(url), "rev-parse", "refs/heads/main"), moved)
        self.assertFalse(self.snapshot(task_dir)["pushed"])

    def test_refusals(self):
        cases = [
            ({"publish": False}, {}, "远端没有 main，任务仓库还没发布"),
            ({}, {"push": "noop"}, "回读远端 main 和产物提交不一致"),
            ({}, {"public": False}, "可见性不是 public，拒绝推送"),
            ({}, {"push": "fail"}, "推送失败"),
        ]
        for number, (remote_args, hub_args, expected) in enumerate(cases, 1):
            with self.subTest(expected=expected):
                task_dir, _ = self.committed(number)
                hub = FakeHub(self.tmp / f"remotes{number}", **hub_args)
                self.remote(hub, task_dir, **remote_args)
                with self.assertRaises(lib.FbrError) as caught:
                    snap.push_one(hub, task_dir, self.cfg)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(HUB_TOKEN, str(caught.exception))
                self.assertFalse(self.snapshot(task_dir)["pushed"])
                if hub_args.get("public") is False:
                    self.assertEqual(hub.pushes(), [])

    def test_wrong_owner_and_missing_stage(self):
        task_dir, _ = self.committed(task_repo_full="someone-else/s11-000001-clipcut")
        hub = FakeHub(self.tmp / "remotes")
        with self.assertRaisesRegex(lib.FbrError, f"不在当前活跃账号 {OWNER} 名下"):
            snap.push_one(hub, task_dir, self.cfg)
        self.assertEqual(hub.calls, [])
        other, _ = self.collected(2)
        with self.assertRaisesRegex(lib.FbrError, "commit 阶段还没通过"):
            snap.push_one(hub, other, self.cfg)


class HubClass(SnapshotCase):
    """Hub 本身：gh 的输出是假的，只看 token 怎么传、接口返回怎么解析。"""

    def hub(self, responses):
        def fake(command, **kwargs):
            self.seen.append((command, kwargs.get("env") or {}))
            return responses[" ".join(command[1:3])]
        responses.setdefault("api user", done(0, OWNER + "\n"))
        self.seen = []
        patcher = mock.patch.object(lib, "run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return snap.Hub(OWNER)

    def test_token_goes_through_env_only(self):
        hub = self.hub({"auth token": done(0, HUB_TOKEN + "\n")})
        self.assertEqual(self.seen[0][0], ["gh", "api", "user", "--jq", ".login"])
        self.assertEqual(self.seen[1][0], ["gh", "auth", "token", "--hostname", "github.com"])
        ws = self.make_task() / "workspace"
        header = hub.git(ws, "config", "--get", "http.https://github.com/.extraHeader").stdout.strip()
        self.assertTrue(header.startswith("Authorization: Basic "))
        self.assertNotIn(HUB_TOKEN, header)                       # 是 base64 之后的
        self.assertEqual(hub.git(ws, "config", "--get", "credential.helper").stdout.strip(), "")
        self.assertNotIn("extraHeader", (ws / ".git" / "config").read_text())
        self.assertEqual(hub.url("a/b"), "https://github.com/a/b.git")

    def test_no_login(self):
        with self.assertRaisesRegex(lib.FbrError, f"gh 里没有当前活跃账号 {OWNER} 的登录态"):
            self.hub({"auth token": done(1, "", "no oauth token")})

    def test_api_parsing(self):
        def page(status, body):
            return done(0, f"HTTP/2.0 {status} OK\r\ncontent-type: application/json\r\n\r\n{body}")
        responses = {"auth token": done(0, HUB_TOKEN + "\n"),
                     "api --include": page(200, '{"visibility": "public", "private": false, "login": "fengyin-go"}')}
        hub = self.hub(responses)
        self.assertTrue(hub.is_public("fengyin-go/x"))
        self.assertEqual(hub.login(), OWNER)
        self.assertEqual(self.seen[-1][1]["GH_TOKEN"], HUB_TOKEN)
        self.assertFalse(any(HUB_TOKEN in part for command, _ in self.seen for part in command))
        responses["api --include"] = page(200, '{"visibility": "private", "private": true}')
        self.assertFalse(hub.is_public("fengyin-go/x"))
        responses["api --include"] = page(404, '{"message": "Not Found"}')
        self.assertFalse(hub.is_public("fengyin-go/x"))
        responses["api --include"] = done(1, "", f"error connecting, token {HUB_TOKEN}")
        with self.assertRaises(lib.FbrError) as caught:
            hub.is_public("fengyin-go/x")
        self.assertNotIn(HUB_TOKEN, str(caught.exception))


class CommandLine(SnapshotCase):
    def call(self, *args, hub=None):
        out, err = io.StringIO(), io.StringIO()
        made = []

        def factory(owner=None):
            made.append(owner)
            if hub is None:
                raise AssertionError("这里不该去拿 token")
            return hub

        argv = ["fbr_snapshot.py", "--root", str(self.root), *args]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(snap, "Hub", factory), \
                mock.patch.object(snap.time, "sleep", lambda seconds: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = snap.main()
        return code, out.getvalue(), err.getvalue(), made

    def test_commit_then_dry_run_then_push(self):
        task_dir, ws = self.collected()
        write(ws / "a.txt", "a\n")
        self.make_task(2)       # 还没收集的不动
        code, out, _, _ = self.call("commit", "--batch", BATCH)
        self.assertEqual(code, 0)
        self.assertIn("s11-000001  ok", out)
        self.assertNotIn("s11-000002", out)
        hub = FakeHub(self.tmp / "remotes")
        self.remote(hub, task_dir)
        code, out, _, made = self.call("push", "--batch", BATCH)
        self.assertEqual((code, made), (0, []))                  # 演练不取 token
        self.assertIn("演练结束：待推送 1 个", out)
        self.assertEqual(hub.calls, [])
        code, _, err, made = self.call("push", "--batch", BATCH, "--execute")
        self.assertEqual((code, made), (2, []))
        self.assertIn("真推需要 --confirm PUBLISH", err)
        code, out, _, made = self.call("push", "--batch", BATCH, "--execute", "--confirm", "PUBLISH", hub=hub)
        self.assertEqual((code, made), (0, [None]))
        self.assertIn("s11-000001  ok  https://github.com/", out)
        code, out, _, _ = self.call("push", "--batch", BATCH)
        self.assertIn("没有待推送的产物快照", out)

    def test_wrong_account_and_failure_streak(self):
        for number in range(1, 5):
            task_dir, ws = self.collected(number)
            write(ws / "a.txt", "a\n")
            snap.commit_one(task_dir, self.cfg)
        hub = FakeHub(self.tmp / "remotes")
        hub.login = lambda: "another-account"
        code, _, err, _ = self.call("push", "--batch", BATCH, "--execute", "--confirm", "PUBLISH", hub=hub)
        self.assertEqual(code, 2)
        self.assertIn(f"token 对应的账号不是当前活跃账号 {OWNER}", err)
        hub = FakeHub(self.tmp / "remotes")      # 四个任务都没有远端仓库
        code, out, _, _ = self.call("push", "--batch", BATCH, "--execute", "--confirm", "PUBLISH", hub=hub)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("  failed  "), 3)
        self.assertIn("连续失败 3 次，停下来等人看", out)
        self.assertNotIn("s11-000004", out)


if __name__ == "__main__":
    unittest.main()
