"""fbr_record 与 fbr_facts：终端画面还原、mp4 探测、录屏流程、测试输出解析、改动清单、取证。

docker、浏览器录制、转码都是假的；git 用临时目录里的真实本地仓库。
"""

from __future__ import annotations

import contextlib
import io
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

from fixtures import BATCH, IMAGE, KEY, TOOLS, FakeDocker, Sandbox, done, sh, step_rows, trace_rows, write, \
    write_projects

import fbrlib as lib
import fbr_facts
import fbr_record
import fbr_snapshot

SID = "cccccccc-0000-4000-8000-000000000003"


# ---------- 手工拼的最小 mp4 ----------

def box(kind: str, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind.encode()) + payload


def mvhd(millis: int, version: int = 0) -> bytes:
    if version == 1:
        body = bytes([1, 0, 0, 0]) + bytes(16) + struct.pack(">IQ", 1000, millis)
    else:
        body = bytes(4) + bytes(8) + struct.pack(">II", 1000, millis)
    return box("mvhd", body + bytes(80))


def tkhd(width: int, height: int) -> bytes:
    return box("tkhd", bytes(76) + struct.pack(">II", width << 16, height << 16))


def mp4(width: int = 1280, height: int = 720, millis: int = 12500, *, version: int = 0, audio_first: bool = False,
        big_mdat: bool = False) -> bytes:
    traks = ([box("trak", tkhd(0, 0))] if audio_first else []) + [box("trak", tkhd(width, height))]
    head = box("ftyp", b"isom" + bytes(4))
    if big_mdat:
        head += struct.pack(">I4sQ", 1, b"mdat", 16 + 5) + bytes(5)
    return head + box("moov", mvhd(millis, version) + b"".join(traks))


class Screen(unittest.TestCase):
    def test_utf8_characters_split_across_reads(self):
        screen = fbr_record.ScreenBuffer()
        for byte in "中文验证完成".encode("utf-8"):
            screen.feed(bytes([byte]))
        self.assertEqual(screen.snapshot(), "中文验证完成")

    def test_escape_sequences_split_across_reads(self):
        screen = fbr_record.ScreenBuffer()
        for byte in b"\x1b[32mok\x1b[0m\x1b]0;title\x1b\\done":
            screen.feed(bytes([byte]))
        self.assertEqual(screen.snapshot(), "okdone")

    def test_ansi_and_newlines(self):
        screen = fbr_record.ScreenBuffer()
        screen.feed(b"\x1b[32mok\x1b[0m\r\n\x1b]0;title\x07done")
        self.assertEqual(screen.snapshot(), "ok\ndone")

    def test_carriage_return_overwrites(self):
        screen = fbr_record.ScreenBuffer()
        screen.feed(b"10%\r50%\r100%\n")
        self.assertEqual(screen.snapshot(), "100%\n")

    def test_backspace_and_line(self):
        screen = fbr_record.ScreenBuffer()
        screen.feed(b"abc\b\bd")
        self.assertEqual(screen.snapshot(), "ad")
        screen.line("")
        screen.line("$ make test")
        self.assertEqual(screen.snapshot(), "ad\n$ make test\n")

    def test_limit_keeps_the_tail(self):
        screen = fbr_record.ScreenBuffer(limit=3)
        screen.feed("".join(f"line {i}\n" for i in range(10)).encode())
        self.assertEqual(screen.lines, ["line 8", "line 9", ""])


class Pty(unittest.TestCase):
    """本机上跑一条无害的 sh 命令，不碰 docker。"""

    def test_exit_code_and_output(self):
        screen, raw, ticks = fbr_record.ScreenBuffer(), io.BytesIO(), []
        rc, cut, seconds = fbr_record.run_in_pty(["/bin/sh", "-c", "printf 'hello\\n'; exit 3"], screen, raw, 20,
                                                 lambda: ticks.append(1))
        self.assertEqual((rc, cut), (3, False))
        self.assertIn("hello", screen.snapshot())
        self.assertIn(b"hello", raw.getvalue())
        self.assertLess(seconds, 20)

    def test_limit_cuts_the_command(self):
        screen = fbr_record.ScreenBuffer()
        rc, cut, seconds = fbr_record.run_in_pty(["/bin/sh", "-c", "echo start; sleep 30"], screen, io.BytesIO(), 0.4,
                                                 lambda: None)
        self.assertEqual((rc, cut), (124, True))
        self.assertLess(seconds, 10)
        self.assertIn("start", screen.snapshot())


class Probe(Sandbox):
    def probe(self, data: bytes) -> dict:
        return fbr_record.probe_mp4(write(self.tmp / "v.bin", "").parent / self.put(data))

    def put(self, data: bytes) -> str:
        (self.tmp / "v.mp4").write_bytes(data)
        return "v.mp4"

    def test_version0(self):
        self.assertEqual(self.probe(mp4()), {"width": 1280, "height": 720, "duration": 12.5})

    def test_version1_audio_track_and_64bit_box(self):
        info = self.probe(mp4(640, 360, 95000, version=1, audio_first=True, big_mdat=True))
        self.assertEqual(info, {"width": 640, "height": 360, "duration": 95.0})

    def test_not_a_video(self):
        self.assertEqual(self.probe(box("ftyp", b"isom")), {"width": 0, "height": 0, "duration": 0.0})
        self.assertEqual(self.probe(b"junk"), {"width": 0, "height": 0, "duration": 0.0})


class Transcode(Sandbox):
    def test_command_and_failure(self):
        source, target, seen = self.tmp / "raw.webm", self.tmp / "out.mp4", []

        def ok(command, **kwargs):
            seen.append([str(c) for c in command])
            target.write_bytes(mp4())
            return done(0)

        with mock.patch.object(fbr_record, "ffmpeg_path", lambda: "ffmpeg-fake"):
            with mock.patch.object(lib, "run", ok):
                fbr_record.to_mp4(source, target, 90, 1280, 720)
            command = seen[0]
            self.assertEqual(command[:4], ["ffmpeg-fake", "-y", "-t", "90"])
            self.assertIn("libx264", command)
            self.assertIn("-an", command)
            self.assertTrue(any(part.startswith("scale=1280:720") for part in command))
            target.unlink()
            with mock.patch.object(lib, "run", lambda command, **kwargs: done(1, "", "Invalid data found")):
                with self.assertRaisesRegex(lib.FbrError, "转码失败：Invalid data found"):
                    fbr_record.to_mp4(source, target, 90, 1280, 720)
            with mock.patch.object(lib, "run", lambda command, **kwargs: done(0)):      # 退出码 0 但没出文件
                with self.assertRaisesRegex(lib.FbrError, "转码失败"):
                    fbr_record.to_mp4(source, target, 90, 1280, 720)


class RecordCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()
        self.video = mp4()
        self.renders: list[dict] = []
        self.render_error = None
        for target, name, fake in ((lib, "run", self.docker.run), (fbr_record, "render", self.fake_render),
                                   (fbr_record, "to_mp4", self.fake_to_mp4)):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def clones(self) -> list[Path]:
        out = []
        for call in self.docker.docker_calls("run", "-d"):
            mount = call[call.index("--mount") + 1]
            out.append(Path(mount.split("src=", 1)[1].split(",dst=", 1)[0]))
        return out

    def fake_render(self, session_cmds, exec_prefix, out_dir, rec):
        clone = self.clones()[-1]
        self.renders.append({"cmds": [c["cmd"] for c in session_cmds], "prefix": list(exec_prefix),
                             "clone_ok": (clone / "Makefile").is_file() and not (clone / ".env").exists(),
                             "head": sh(clone, "rev-parse", "HEAD"), "size": (rec["width"], rec["height"])})
        if self.render_error:
            raise self.render_error
        (out_dir / "raw.webm").write_bytes(b"webm")
        return [{"cmd": c["cmd"], "show": c.get("show", ""), "rc": index, "seconds": 1.0, "cut": False,
                 "tail": f"output {index}", "skipped": False} for index, c in enumerate(session_cmds)]

    def fake_to_mp4(self, source, target, max_seconds, width, height):
        assert Path(source).is_file(), "转码前应当已有 raw.webm"
        self.transcode = (max_seconds, width, height)
        Path(target).write_bytes(self.video)

    def committed(self, number=1, **plan) -> Path:
        task_dir = self.make_task(number)
        lib.save_git_config(task_dir)
        write(task_dir / "workspace" / ".env", "LOCAL=1\n")     # 没提交的东西不该进录屏用的克隆
        self.mark(task_dir, "commit", commit={"commit": lib.load_task(task_dir)["initial_commit"]})
        if plan:
            path = task_dir / "recording-plan.json"
            lib.save_json(path, {**lib.load_json(path), **plan})
        return task_dir


class RecordOne(RecordCase):
    def test_requires_git_backup_before_any_host_git(self):
        task_dir = self.make_task()
        self.mark(task_dir, "commit", commit={"commit": lib.load_task(task_dir)["initial_commit"]})
        with mock.patch.object(lib, "git") as host_git, mock.patch.object(lib, "clean_clone") as clone:
            with self.assertRaisesRegex(lib.FbrError, "没有起跑前的 git 配置备份"):
                fbr_record.record_one(task_dir, self.cfg)
        host_git.assert_not_called()
        clone.assert_not_called()
        self.assertEqual(self.docker.calls, [])

    def test_records(self):
        task_dir = self.committed()
        row = fbr_record.record_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("ok", "12.5 秒，演示 2 步，其中 1 步退出码非 0"))
        start = self.docker.docker_calls("run", "-d")[0]
        self.assertEqual(start[-2:], [IMAGE, "3600"])
        self.assertEqual(start[start.index("--entrypoint") + 1], "sleep")
        self.assertNotIn("--env-file", start)       # 录屏容器不带 key
        self.assertNotIn("--rm", start)
        for flag in ("--init", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"):
            self.assertIn(flag, start)
        container = start[start.index("--name") + 1]
        self.assertRegex(container, r"^s11-rec-[0-9a-f]{10}$")
        self.assertEqual(self.docker.docker_calls("exec"),
                         [["docker", "exec", "-w", "/workspace", container, "bash", "-lc", "make setup"]])
        shot = self.renders[0]
        self.assertEqual(shot["prefix"], ["docker", "exec", "-it", "-w", "/workspace", container])
        self.assertEqual(shot["cmds"], ["python3 -m clipcut pack fixtures/short.mp4 --segment 10", "make test"])
        self.assertTrue(shot["clone_ok"])
        self.assertEqual(shot["head"], lib.load_task(task_dir)["initial_commit"])
        self.assertEqual(shot["size"], (1280, 720))
        self.assertEqual(self.transcode, (90, 1280, 720))
        self.assertEqual(self.docker.docker_calls("kill"), [["docker", "kill", container]])
        self.assertTrue(self.clones()[0].parent.exists())      # 保留容器及其挂载目录供核查
        retained = lib.load_json(task_dir / "record" / "containers.json")
        self.assertEqual(retained["containers"][0]["name"], container)
        self.assertEqual(retained["containers"][0]["clone"], str(self.clones()[0]))
        self.assertTrue(self.clones()[0].parent.name.startswith("s11-rec-s11-000001"))
        self.assertFalse((task_dir / "record" / "raw.webm").exists())
        self.assertFalse(any(KEY in part for call in self.docker.calls for part in call))
        info = lib.load_json(task_dir / "record" / "recording.json")
        self.assertEqual(info["video"], {"file": "record/recording.mp4", "width": 1280, "height": 720,
                                         "duration": 12.5})
        self.assertTrue(info["setup_ok"])
        self.assertEqual([(r["attempt"], r["rc"], r["tail"]) for r in info["setup_runs"]], [(1, 0, "setup output")])
        self.assertEqual([s["rc"] for s in info["steps"]], [0, 1])
        self.assertEqual(info["problems"], [])
        stage = lib.require_stage(task_dir, "record")
        self.assertEqual((stage["duration"], stage["setup_ok"]), (12.5, True))

    def test_skip_and_redo(self):
        task_dir = self.committed()
        fbr_record.record_one(task_dir, self.cfg)
        row = fbr_record.record_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("skip", "已经录过"))
        self.assertEqual(len(self.renders), 1)
        self.assertEqual(fbr_record.record_one(task_dir, self.cfg, redo=True)["state"], "ok")
        self.assertEqual(len(self.renders), 2)

    def test_setup_failure_is_not_valid_product_recording(self):
        task_dir = self.committed()
        self.docker.exec_rcs = [1, 1, 1]
        row = fbr_record.record_one(task_dir, self.cfg)
        self.assertEqual(row["state"], "failed")
        self.assertIn("环境准备失败", row["detail"])
        self.assertEqual(len(self.docker.docker_calls("run", "-d")), 3)     # 每次重来都是新容器、新克隆
        self.assertEqual(len(self.docker.docker_calls("kill")), 3)
        self.assertEqual(len(set(self.clones())), 3)
        self.assertTrue(all(clone.parent.exists() for clone in self.clones()))
        attempt = lib.load_json(task_dir / "record" / "last-attempt.json")
        info = lib.load_json(Path(attempt["path"]) / "recording.json")
        self.assertFalse(info["setup_ok"])
        self.assertEqual([r["rc"] for r in info["setup_runs"]], [1, 1, 1])
        self.assertFalse(lib.stage_ok(task_dir, "record"))
        self.assertEqual(len(self.renders), 1)      # 演示只录一遍，不重试到成功

    def test_setup_passes_on_second_try(self):
        task_dir = self.committed()
        self.docker.exec_rcs = [1, 0]
        self.assertEqual(fbr_record.record_one(task_dir, self.cfg)["detail"], "12.5 秒，演示 2 步，其中 1 步退出码非 0")
        info = lib.load_json(task_dir / "record" / "recording.json")
        self.assertTrue(info["setup_ok"])
        self.assertEqual([r["rc"] for r in info["setup_runs"]], [1, 0])

    def test_no_setup_command(self):
        task_dir = self.committed(setup_cmd="")
        self.assertEqual(fbr_record.record_one(task_dir, self.cfg)["state"], "ok")
        self.assertEqual(self.docker.docker_calls("exec"), [])
        info = lib.load_json(task_dir / "record" / "recording.json")
        self.assertEqual((info["setup_ok"], info["setup_runs"]), (True, []))

    def test_bad_video_fails_the_stage(self):
        task_dir = self.committed()
        self.video = mp4(640, 360, 95000)
        row = fbr_record.record_one(task_dir, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("failed", "成片不合规：画面是 640×360；时长 95.0 秒"))
        state = lib.load_state(task_dir)["stages"]["record"]
        self.assertEqual(state["state"], "failed")
        self.assertEqual(len(state["problems"]), 2)
        self.assertFalse(lib.stage_ok(task_dir, "record"))
        self.video = mp4(1280, 720, 90400)          # 90 秒留 0.5 秒余量
        self.assertEqual(fbr_record.record_one(task_dir, self.cfg)["state"], "ok")

    def test_render_error_still_cleans_up(self):
        task_dir = self.committed()
        self.render_error = lib.FbrError("浏览器起不来")
        with self.assertRaisesRegex(lib.FbrError, "浏览器起不来"):
            fbr_record.record_one(task_dir, self.cfg)
        self.assertEqual(len(self.docker.docker_calls("kill")), 1)
        self.assertTrue(self.clones()[0].parent.exists())
        self.assertTrue(lib.load_json(task_dir / "record" / "containers.json")["retained"])
        self.assertFalse(lib.stage_ok(task_dir, "record"))

    def test_preconditions(self):
        with self.assertRaisesRegex(lib.FbrError, "commit 阶段还没通过"):
            fbr_record.record_one(self.make_task(1), self.cfg)
        task_dir = self.committed(2, demo=[{"show": "没有命令"}, "not a dict"])
        with self.assertRaisesRegex(lib.FbrError, "s11-000002：recording-plan.json 里没有演示命令"):
            fbr_record.record_one(task_dir, self.cfg)
        self.assertEqual(self.docker.docker_calls("run"), [])

    def test_container_start_failure(self):
        with mock.patch.object(lib, "run", lambda command, **kwargs: done(125, "", "Unable to find image")):
            with self.assertRaisesRegex(lib.FbrError, "录屏容器起不来：Unable to find image"):
                fbr_record.start_container(IMAGE, self.tmp)

    def test_command_line(self):
        self.committed(1)
        self.committed(2, demo=[])
        self.make_task(3)       # 还没提交产物的不处理
        out = io.StringIO()
        argv = ["fbr_record.py", "--root", str(self.root), "--batch", BATCH]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            code = fbr_record.main()
        self.assertEqual(code, 1)
        self.assertIn("s11-000001  ok  12.5 秒", out.getvalue())
        self.assertIn("s11-000002  failed  s11-000002：recording-plan.json 里没有演示命令", out.getvalue())
        self.assertNotIn("s11-000003", out.getvalue())


class CommandInput(unittest.TestCase):
    def test_start_command_is_typed_before_execution(self):
        import io
        screen = fbr_record.ScreenBuffer()
        ticks = []
        raw = io.BytesIO()
        seconds = fbr_record.type_command(
            screen, raw, "npm run dev", lambda: ticks.append(screen.snapshot()),
            max_input_seconds=0.04,
        )
        self.assertGreater(seconds, 0)
        self.assertGreater(len(ticks), len("npm run dev"))
        self.assertEqual(screen.snapshot(), "$ npm run dev\n")
        self.assertEqual(raw.getvalue(), b"$ npm run dev\n")


# ---------- 取证 ----------

class ParseTests(unittest.TestCase):
    def test_formats(self):
        cases = [
            ("FAILED tests/test_a.py::test_x - AssertionError\n==== 1 failed, 4 passed in 0.10s ====",
             (4, 1, ["tests/test_a.py::test_x"])),
            ("ok 1 - short\nnot ok 3 - handles long input\n# pass 2\n# fail 1", (2, 1, ["handles long input"])),
            ("✔ short (1.2ms)\n✖ packs long input (12.3ms)\nℹ pass 5\nℹ fail 1", (5, 1, ["packs long input"])),
            ("[ERROR]   PackerTest.testLong:42 expected 360 but was 0\n"
             "[ERROR] Tests run: 10, Failures: 1, Errors: 1, Skipped: 0", (8, 2, ["PackerTest.testLong"])),
            ("FAIL: test_long (tests.test_packer.T)\n----\nRan 5 tests in 0.01s\n\nFAILED (failures=1, errors=1)",
             (3, 2, ["test_long (tests.test_packer.T)"])),
            ("Ran 1 test in 0.01s\n\nOK", (1, 0, [])),
            ("Ran 5 tests in 0.01s\n\nOK (skipped=2)", (3, 0, [])),
            ("Ran 7 tests in 0.01s\n\nFAILED (failures=1, errors=1, skipped=2)", (3, 2, [])),
            ("Ran 5 tests in 0.01s\n\nOK (skipped=1, expected failures=1)", (3, 0, [])),
            ("Ran 5 tests in 0.01s\n\nFAILED (unexpected successes=1)", (4, 1, [])),
            ("Tests run: 5, Failures: 0, Errors: 0, Skipped: 2", (3, 0, [])),
            (" FAIL  src/a.test.ts\n Tests  1 failed | 3 passed (4)", (3, 1, ["src/a.test.ts"])),
            ("make: *** No rule to make target 'test'.  Stop.", (None, None, [])),
        ]
        for index, (text, expected) in enumerate(cases):
            with self.subTest(case=index):
                found = fbr_facts.parse_tests(text)
                self.assertEqual((found["passed"], found["failed"], found["failed_names"]), expected)


class CompareTests(unittest.TestCase):
    @staticmethod
    def result(setup=0, test=0, names=()):
        return {"setup_rc": setup, "test_rc": test, "failed_names": list(names)}

    def test_cases(self):
        r = self.result
        cases = [
            ("都通过", r(), r(), dict(regression=False, after_ok=True, after_runnable=True)),
            ("改坏了", r(), r(test=1, names=["a"]), dict(regression=True, new_failures=["a"], after_ok=False)),
            ("原样就有的失败", r(test=1, names=["a"]), r(test=1, names=["a"]),
             dict(regression=False, new_failures=[], before_ok=False, after_ok=False)),
            ("旧失败之外又多一个", r(test=1, names=["a"]), r(test=1, names=["a", "b"]),
             dict(regression=True, new_failures=["b"])),
            ("修好了旧失败", r(test=1, names=["a"]), r(), dict(regression=False, fixed=["a"], after_ok=True)),
            ("产物上测试命令跑不起来", r(), r(test=127), dict(regression=True, after_runnable=False)),
            ("产物上环境装不上", r(), r(setup=1, test=None), dict(regression=True, after_runnable=False)),
            ("初始提交上本来就跑不起来", r(setup=1, test=None), r(setup=1, test=None),
             dict(regression=False, after_runnable=False)),
            ("原来能跑，现在超时", r(test=1, names=["a"]), r(test=124, names=[]),
             dict(regression=True, after_runnable=False)),
        ]
        for label, before, after, expected in cases:
            with self.subTest(label):
                found = fbr_facts.compare_tests(before, after)
                self.assertEqual({k: found[k] for k in expected}, expected)


class Changes(Sandbox):
    def test_change_list(self):
        task_dir = self.make_task()
        workspace, initial = task_dir / "workspace", lib.load_task(task_dir)["initial_commit"]
        write(workspace / "src/clipcut/packer.py",
              "def pack(path, segment):\n    # TODO: 一小时以上的输入\n    raise NotImplementedError\n")
        write(workspace / "tests/test_packer.py",
              "import unittest\n\n\n@unittest.skip('later')\nclass T(unittest.TestCase): pass\n")
        write(workspace / "tests/test_segment.py", "def test_segment():\n    assert True\n")
        (workspace / "logo.bin").write_bytes(bytes(range(256)) * 4)
        (workspace / "README.md").unlink()
        sh(workspace, "add", "-A")
        sh(workspace, "commit", "-q", "-m", "work")
        found = fbr_facts.collect_changes(workspace, initial, sh(workspace, "rev-parse", "HEAD"))
        files = {f["path"]: f for f in found["files"]}
        self.assertFalse(found["no_change"])
        self.assertEqual(found["totals"]["files"], 5)
        self.assertEqual({p: f["status"] for p, f in files.items()},
                         {"src/clipcut/packer.py": "M", "tests/test_packer.py": "M", "tests/test_segment.py": "A",
                          "logo.bin": "A", "README.md": "D"})
        self.assertTrue(files["logo.bin"]["binary"])
        self.assertEqual((files["logo.bin"]["added"], files["logo.bin"]["deleted"]), (0, 0))
        self.assertEqual((files["README.md"]["added"], files["README.md"]["deleted"]), (0, 1))
        self.assertTrue(files["tests/test_segment.py"]["is_test"])
        self.assertFalse(files["src/clipcut/packer.py"]["is_test"])
        self.assertEqual(found["tests_weakened"], ["tests/test_packer.py"])
        self.assertEqual(found["tests_added"], ["tests/test_segment.py"])
        self.assertEqual(found["tests_deleted"], [])
        self.assertEqual(found["skips_added"], [{"file": "tests/test_packer.py", "line": "@unittest.skip('later')"}])
        self.assertEqual({p["file"] for p in found["placeholders"]}, {"src/clipcut/packer.py"})
        self.assertEqual(len(found["placeholders"]), 2)

    def test_deleted_test_and_no_change(self):
        task_dir = self.make_task()
        workspace, initial = task_dir / "workspace", lib.load_task(task_dir)["initial_commit"]
        same = fbr_facts.collect_changes(workspace, initial, initial)
        self.assertTrue(same["no_change"])
        self.assertEqual(same["totals"], {"files": 0, "added": 0, "deleted": 0})
        sh(workspace, "rm", "-q", "tests/test_packer.py")
        sh(workspace, "commit", "-q", "-m", "drop tests")
        found = fbr_facts.collect_changes(workspace, initial, sh(workspace, "rev-parse", "HEAD"))
        self.assertEqual(found["tests_deleted"], ["tests/test_packer.py"])

    def test_test_file_patterns(self):
        yes = ["tests/a.py", "src/__tests__/a.js", "pkg/test_a.py", "a_test.go", "src/a.spec.ts", "src/a.test.mjs",
               "src/main/PackerTest.java", "spec/a.rb"]
        no = ["src/clipcut/packer.py", "src/contest.py", "docs/testing.md", "latest/a.py"]
        for path in yes:
            self.assertTrue(fbr_facts.TEST_FILE_RE.search(path), path)
        for path in no:
            self.assertFalse(fbr_facts.TEST_FILE_RE.search(path), path)


class Claims(unittest.TestCase):
    TREE = ["src/clipcut/packer.py", "src/clipcut/cli.py", "README.md", "tests/test_packer.py"]

    def test_mentions(self):
        answer = ("改了packer.py文件和 src/clipcut/cli.py，新增 src/clipcut/segment.py，文档见 README.md。"
                  "测试在 ./tests/test_packer.py，版本 1.2.3 和 v2.0 不算文件，e.g. 也不算。")
        found = fbr_facts.check_claims(answer, self.TREE, {"src/clipcut/packer.py"})
        self.assertEqual(found["final_answer"], answer)
        self.assertEqual(found["mentioned"], sorted(["packer.py", "src/clipcut/cli.py", "src/clipcut/segment.py",
                                                     "README.md", "tests/test_packer.py"]))
        self.assertEqual(found["mentioned_missing"], ["src/clipcut/segment.py"])
        self.assertEqual(found["mentioned_unchanged"], ["src/clipcut/cli.py", "README.md", "tests/test_packer.py"])

    def test_empty_answer(self):
        found = fbr_facts.check_claims("", self.TREE, set())
        self.assertEqual((found["final_answer"], found["mentioned"], found["mentioned_missing"]), ("", [], []))
        self.assertEqual(fbr_facts.check_claims(None, self.TREE, set())["final_answer"], "")


GOOD = {"setup": (0, "ok"), "test": (0, "Ran 1 test in 0.01s\n\nOK")}
BAD = {"setup": (0, ""),
       "test": (1, "FAIL: test_long (tests.test_packer.T)\n\nRan 2 tests in 0.01s\n\nFAILED (failures=1)")}
BROKEN = {"setup": (1, "pip: no matching distribution")}
ANSWER = "改了 src/clipcut/packer.py，新增 src/clipcut/segment.py 和 docs/usage.md。"
RECORDING = {
    "setup_ok": True, "setup_runs": [{"attempt": 1, "rc": 0, "tail": "setup output"}],
    "steps": [{"cmd": "python3 -m clipcut pack fixtures/short.mp4 --segment 10", "show": "生成 6 个分片", "rc": 0,
               "seconds": 1.2, "cut": False, "tail": "6 segments", "skipped": False},
              {"cmd": "make test", "show": "测试通过", "rc": 2, "seconds": 3.0, "cut": False, "tail": "FAILED",
               "skipped": False}]}


class FactsCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()
        self.results: list[dict] = []
        self.step_calls: list[dict] = []
        for name, fake in (("run", self.docker.run), ("run_steps_in_image", self.fake_steps)):
            patcher = mock.patch.object(lib, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_steps(self, image, workdir, steps, *, tail=60, stop_on_fail=True):
        workdir = Path(workdir)
        self.step_calls.append({
            "image": image, "workdir": workdir, "head": sh(workdir, "rev-parse", "HEAD"), "tail": tail,
            "segment": (workdir / "src/clipcut/segment.py").exists(), "env": (workdir / ".env").exists(),
            "steps": [(s["name"], s["cmd"], s["timeout"]) for s in steps]})
        return step_rows(steps, self.results.pop(0))

    def prepared(self, number=1, *, change=True, recording=None) -> Path:
        task_dir = self.make_task(number)
        import fbr_run
        fbr_run.bound_key(task_dir, self.cfg, start=True)
        lib.save_git_config(task_dir)
        workspace = task_dir / "workspace"
        if change:
            write(workspace / "src/clipcut/packer.py",
                  "def pack(path, segment):\n    # TODO: 一小时以上的输入\n    return [path]\n")
            write(workspace / "src/clipcut/segment.py",
                  "def split(total, segment):\n    return list(range(0, total, segment))\n")
        write(workspace / ".env", "LOCAL=1\n")      # 被忽略的文件不进产物，也不进取证用的克隆
        self.mark(task_dir, "collect")
        self.assertEqual(fbr_snapshot.commit_one(task_dir, self.cfg)["state"], "ok")
        write_projects(task_dir / "trace" / "projects", SID, trace_rows(SID, tools=TOOLS, answer=ANSWER))
        lib.save_json(task_dir / "trace" / "ids.json", {"session_id": SID, "prompt_id": "prm-0001",
                                                        "session_file": f"projects/-workspace/{SID}.jsonl"})
        lib.save_json(task_dir / "record" / "recording.json", recording or RECORDING)
        lib.set_stage(task_dir, "record", "ok", duration=12.5, setup_ok=True)
        return task_dir

    def facts(self, task_dir) -> dict:
        return lib.load_json(task_dir / "evidence" / "facts.json")


class FactsOne(FactsCase):
    def test_changed_task(self):
        task_dir = self.prepared()
        record = lib.load_task(task_dir)
        commit = lib.load_json(task_dir / "review" / "snapshot.json")["commit"]
        self.results = [GOOD, BAD]
        row = fbr_facts.facts_one(task_dir, self.root, self.cfg)
        self.assertEqual((row["state"], row["detail"]),
                         ("ok", "改动 2 个文件，产物测试不通过，新增失败 1 个，轨迹 4 步"))
        first, second = self.step_calls
        self.assertEqual((first["head"], first["segment"]), (record["initial_commit"], False))
        self.assertEqual((second["head"], second["segment"]), (commit, True))
        self.assertEqual(first["steps"], [("setup", "make setup", 900), ("test", "make test", 600)])
        self.assertEqual((first["image"], first["tail"], first["env"], second["env"]), (IMAGE, 200, False, False))
        self.assertTrue(first["workdir"].parent.exists())
        self.assertTrue(second["workdir"].parent.exists())
        facts = self.facts(task_dir)
        self.assertEqual(set(facts), {"task_id", "initial_commit", "commit", "generated_at", "demo", "tests", "changes",
                                      "trace", "verification", "claims", "tree_files", "test_identity", "source_identity"})
        self.assertEqual((facts["initial_commit"], facts["commit"]), (record["initial_commit"], commit))
        self.assertEqual(facts["demo"]["setup_ok"], True)
        self.assertEqual(facts["demo"]["setup_tail"], "")
        self.assertEqual([set(s) for s in facts["demo"]["steps"]],
                         [{"cmd", "show", "rc", "cut", "skipped", "tail"}] * 2)
        self.assertEqual([s["rc"] for s in facts["demo"]["steps"]], [0, 2])
        tests = facts["tests"]
        self.assertEqual((tests["setup_cmd"], tests["test_cmd"]), ("make setup", "make test"))
        self.assertEqual((tests["before"]["test_rc"], tests["before"]["passed"], tests["before"]["failed"]), (0, 1, 0))
        self.assertEqual((tests["after"]["test_rc"], tests["after"]["passed"], tests["after"]["failed"]), (1, 1, 1))
        self.assertEqual(tests["new_failures"], ["test_long (tests.test_packer.T)"])
        self.assertEqual((tests["before_ok"], tests["after_ok"], tests["after_runnable"], tests["regression"]),
                         (True, False, True, True))
        changes = facts["changes"]
        self.assertEqual(sorted(f["path"] for f in changes["files"]),
                         ["src/clipcut/packer.py", "src/clipcut/segment.py"])
        self.assertEqual((changes["model_commits"], changes["host_commit"], changes["history_rewritten"]),
                         (0, True, False))
        self.assertEqual(changes["placeholders"][0]["file"], "src/clipcut/packer.py")
        self.assertEqual(facts["trace"]["counts"]["tools"], 4)
        self.assertEqual([s["step"] for s in facts["trace"]["steps"]], [1, 2, 3, 4])
        self.assertFalse(facts["verification"]["verified_after_last_edit"])
        self.assertEqual(facts["claims"]["final_answer"], ANSWER)
        self.assertEqual(facts["claims"]["mentioned_missing"], ["docs/usage.md"])
        self.assertEqual(facts["claims"]["mentioned_unchanged"], [])
        self.assertIn("src/clipcut/segment.py", facts["tree_files"])
        self.assertNotIn(".env", facts["tree_files"])
        self.assertEqual(lib.require_stage(task_dir, "facts")["commit"], commit)
        cache = next((self.root / "_registry" / "baseline-tests").glob("*.json"))
        self.assertEqual(lib.load_json(cache)["result"]["test_rc"], 0)
        self.assertTrue(all(c[1:3] == ["image", "inspect"] for c in self.docker.docker_calls()))  # 只读核对镜像身份

        row = fbr_facts.facts_one(task_dir, self.root, self.cfg)
        self.assertEqual((row["state"], row["detail"]), ("skip", "已经取证过"))
        self.results = [GOOD]       # 重做时基线走缓存，只再跑产物这一遍
        self.assertEqual(fbr_facts.facts_one(task_dir, self.root, self.cfg, redo=True)["detail"],
                         "改动 2 个文件，产物测试通过，新增失败 0 个，轨迹 4 步")
        self.assertEqual(len(self.step_calls), 3)
        self.assertFalse(self.facts(task_dir)["tests"]["regression"])

    def test_no_change_runs_once(self):
        task_dir = self.prepared(change=False)
        self.results = [GOOD]
        row = fbr_facts.facts_one(task_dir, self.root, self.cfg)
        self.assertEqual(row["detail"], "改动 0 个文件，产物测试通过，新增失败 0 个，轨迹 4 步")
        self.assertEqual(len(self.step_calls), 1)
        facts = self.facts(task_dir)
        self.assertTrue(facts["changes"]["no_change"])
        self.assertEqual(facts["tests"]["before"], facts["tests"]["after"])
        self.assertEqual(facts["claims"]["mentioned_missing"], ["src/clipcut/segment.py", "docs/usage.md"])
        self.assertEqual(facts["claims"]["mentioned_unchanged"], ["src/clipcut/packer.py"])

    def test_broken_baseline_is_not_cached(self):
        task_dir = self.prepared()
        self.results = [BROKEN, BROKEN]
        self.assertEqual(fbr_facts.facts_one(task_dir, self.root, self.cfg)["state"], "ok")
        tests = self.facts(task_dir)["tests"]
        self.assertEqual((tests["before"]["setup_rc"], tests["before"]["test_rc"]), (1, None))
        self.assertIn("no matching distribution", tests["before"]["setup_tail"])
        self.assertEqual((tests["after_ok"], tests["after_runnable"], tests["regression"]), (False, False, False))
        self.assertEqual(list((self.root / "_registry" / "baseline-tests").glob("*.json")), [])

    def test_failed_demo_setup_is_carried_over(self):
        recording = dict(RECORDING, setup_ok=False, setup_runs=[{"attempt": 1, "rc": 1, "tail": "first"},
                                                                 {"attempt": 2, "rc": 1, "tail": "npm ERR! 404"}])
        task_dir = self.prepared(recording=recording)
        self.results = [GOOD, GOOD]
        fbr_facts.facts_one(task_dir, self.root, self.cfg)
        demo = self.facts(task_dir)["demo"]
        self.assertEqual((demo["setup_ok"], demo["setup_tail"]), (False, "npm ERR! 404"))

    def test_git_hygiene_and_preconditions(self):
        task_dir = self.prepared()
        git_dir = task_dir / "workspace" / ".git"
        sh(task_dir / "workspace", "config", "diff.external", "/bin/false")
        hook = write(git_dir / "hooks" / "post-checkout", "#!/bin/sh\ntouch \"$HOME/should-not-exist\"\n")
        hook.chmod(0o755)
        self.results = [GOOD, GOOD]
        self.assertEqual(fbr_facts.facts_one(task_dir, self.root, self.cfg)["state"], "ok")
        self.assertNotIn("external", (git_dir / "config").read_text())
        self.assertFalse(hook.exists())
        self.assertTrue((task_dir / "run" / "hooks-removed" / "post-checkout").is_file())

        other = self.make_task(2)
        lib.save_git_config(other)
        self.mark(other, "commit", commit={"commit": "0" * 40})
        with self.assertRaisesRegex(lib.FbrError, "record 阶段还没通过"):
            fbr_facts.facts_one(other, self.root, self.cfg)

    def test_command_line(self):
        self.prepared(1)
        self.make_task(2)       # 没录屏的不处理
        self.results = [GOOD, GOOD]
        out = io.StringIO()
        argv = ["fbr_facts.py", "--root", str(self.root), "--batch", BATCH]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            code = fbr_facts.main()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("s11-000001  ok  改动 2 个文件", out.getvalue())
        self.assertNotIn("s11-000002", out.getvalue())


if __name__ == "__main__":
    unittest.main()
