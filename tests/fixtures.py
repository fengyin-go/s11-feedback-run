"""自测公共夹具：临时工作目录、假批次、本地 git 仓库、假轨迹，以及替换 docker 调用的假实现。

所有东西都建在临时目录里。不联网、不起容器、不调 gh、不读真实 key 文件、不碰真实工作目录。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# 本机的全局 git 配置（签名、钩子模板等）不带进测试
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ.pop("SOLO11_ROOT", None)

import fbrlib as lib  # noqa: E402

REAL_RUN = lib.run
IMAGE = "example/claude-code:selftest"
IMAGE_ID = "sha256:" + "ab" * 32
VERSION = "2.1.319"
OWNER = "demo-owner"
BATCH = "b1110-01"
KEY = "selftest-key-0123456789abcdef"
PROMPT = ("给 clipcut 的 pack 子命令加上分片打包：按 --segment 指定的秒数切分输入，在输出目录里生成 index.json 清单；"
          "时长超过一小时的输入也要能处理，非法参数要报错退出。")
REQUIREMENTS = [
    {"id": "R1", "text": "pack 支持 --segment 分片", "check": "python3 -m clipcut pack fixtures/short.mp4 --segment 10"},
    {"id": "R2", "text": "一小时以上的输入能处理", "check": "python3 -m clipcut pack fixtures/long.mp4 --segment 10"},
    {"id": "R3", "text": "非法参数报错退出", "check": "python3 -m clipcut pack fixtures/short.mp4 --segment 0"},
]
FILES = {
    "Makefile": "setup:\n\t@true\ntest:\n\tpython3 -m unittest -q\n",
    "README.md": "# clipcut\n",
    ".gitignore": ".env\n__pycache__/\n",
    "src/clipcut/packer.py": "def pack(path, segment):\n    return [path]\n",
    "tests/test_packer.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
}


def sh(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def task_name(number: int) -> str:
    return f"s11-{number:06d}"


class Sandbox(unittest.TestCase):
    """每个用例一个临时工作目录。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fbr-selftest-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        runtime_before = os.environ.get("FBR_RUNTIME_HOME")
        os.environ["FBR_RUNTIME_HOME"] = str(self.tmp / "runtime")
        self.addCleanup(lambda: os.environ.pop("FBR_RUNTIME_HOME", None) if runtime_before is None else os.environ.__setitem__("FBR_RUNTIME_HOME", runtime_before))
        scratch_before = os.environ.get("FBR_SCRATCH")
        os.environ["FBR_SCRATCH"] = str(self.tmp / "scratch")
        def restore_scratch():
            if scratch_before is None:
                os.environ.pop("FBR_SCRATCH", None)
            else:
                os.environ["FBR_SCRATCH"] = scratch_before
        self.addCleanup(restore_scratch)
        self.root = self.tmp / "root"
        self.key_file = write(self.tmp / "secrets" / "gateway.env", f"apikey={KEY}\n")
        os.chmod(self.key_file, 0o600)
        lib.save_json(self.root / "_config" / "settings.json", {
            "image": IMAGE, "image_id": IMAGE_ID, "harness": "Claude Code", "harness_version": VERSION,
            "github_owner": OWNER, "git_user_name": "Self Test", "git_user_email": "selftest@example.com",
            "submitter": "测试员", "key_file": str(self.key_file), "concurrency": 2, "gateway": {"retry_base": 0}})
        (self.root / "batches" / BATCH).mkdir(parents=True)
        self.cfg = lib.settings(self.root)

    # ---------- 任务目录 ----------

    def make_task(self, number: int = 1, *, repo: bool = True, **extra) -> Path:
        """铺一道题。repo=True 时 workspace 是一个真实的本地 git 仓库，停在初始提交。"""
        name = task_name(number)
        task_dir = self.root / "batches" / BATCH / name
        workspace = task_dir / "workspace"
        workspace.mkdir(parents=True)
        initial = f"{number:040x}"
        if repo:
            sh(workspace, "init", "-q", "-b", "main")
            sh(workspace, "config", "user.name", "Task Author")
            sh(workspace, "config", "user.email", "author@example.com")
            for rel, text in FILES.items():
                write(workspace / rel, text)
            sh(workspace, "add", "-A")
            sh(workspace, "commit", "-q", "-m", "initial")
            initial = sh(workspace, "rev-parse", "HEAD")
        repo_name = f"{name}-clipcut"
        record = {
            "task_id": name, "repo_id": "s11r001-clipcut", "draft_id": "d1", "base_commit": initial,
            "domain": "游戏、图形与多媒体", "subdirection": "音视频处理", "task_type": "功能开发", "difficulty": "困难",
            "language": "Python", "frameworks": "Python / ffmpeg", "repro_level": "已容器化，可一键起环境",
            "prompt": PROMPT, "requirements": REQUIREMENTS, "batch": BATCH, "task_repo": repo_name,
            "task_repo_full": f"{OWNER}/{repo_name}", "harness": "Claude Code", "harness_version": VERSION,
            "os": "MacOS/Linux", "image": IMAGE, "initial_commit": initial,
            "publish": {"state": "published", "repo_url": f"https://github.com/{OWNER}/{repo_name}",
                        "initial_snapshot": f"https://github.com/{OWNER}/{repo_name}/commit/{initial}",
                        "published_at": "2026-11-10T09:00:00+0800"},
        }
        record.update(extra)
        lib.save_json(task_dir / "task.json", record)
        lib.save_json(task_dir / "recording-plan.json", {
            "task_id": name, "setup_cmd": "make setup",
            "demo": [{"cmd": "python3 -m clipcut pack fixtures/short.mp4 --segment 10", "show": "生成 6 个分片"},
                     {"cmd": "make test", "show": "测试通过"}],
            "notes": "", "max_seconds": 90, "resolution": "1280x720", "format": "mp4"})
        return task_dir

    def mark(self, task_dir: Path, upto: str, **info) -> None:
        """把 upto 及之前的阶段记成 ok。info 按阶段名给额外字段。"""
        for stage in lib.STAGES[:lib.STAGES.index(upto) + 1]:
            lib.set_stage(task_dir, stage, "ok", **info.get(stage, {}))


# ---------- 假轨迹与假流式输出 ----------

def trace_rows(session_id: str, prompt: str = PROMPT, tools=(), answer: str = "分片打包已实现，make test 通过。",
               version: str = VERSION, prompt_id: str = "prm-0001") -> list[dict]:
    """tools: [(工具名, 入参, 输出, 是否报错)]。"""
    clock = [0]

    def row(kind: str, content, **more) -> dict:
        clock[0] += 1
        return {"type": kind, "sessionId": session_id, "version": version, "cwd": "/workspace", "isSidechain": False,
                "uuid": f"u{clock[0]}", "timestamp": f"2026-11-10T10:00:{clock[0]:02d}.000Z",
                "message": {"role": kind, "content": content}, **more}

    rows = [row("user", prompt, promptId=prompt_id)]
    for index, (tool, payload, output, is_error) in enumerate(tools, 1):
        rows.append(row("assistant", [{"type": "tool_use", "id": f"t{index}", "name": tool, "input": payload}]))
        rows.append(row("user", [{"type": "tool_result", "tool_use_id": f"t{index}", "is_error": is_error,
                                  "content": output}], toolUseResult={"stdout": output}))
    rows.append(row("assistant", [{"type": "text", "text": answer}]))
    return rows


TOOLS = [
    ("Read", {"file_path": "/workspace/src/clipcut/packer.py"}, "def pack(path, segment): ...", False),
    ("Edit", {"file_path": "/workspace/src/clipcut/packer.py"}, "ok", False),
    ("Bash", {"command": "make test"}, "Ran 1 test in 0.01s\n\nOK", False),
    ("Edit", {"file_path": "/workspace/src/clipcut/packer.py"}, "ok", False),
]


def write_projects(projects: Path, session_id: str, rows: list[dict]) -> Path:
    path = projects / "-workspace" / f"{session_id}.jsonl"
    write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return path


def stream_text(session_id: str, answer: str = "分片打包已实现，make test 通过。", *, is_error: bool = False,
                version: str = VERSION, with_result: bool = True) -> str:
    events = [{"type": "system", "subtype": "init", "session_id": session_id, "claude_code_version": version,
               "model": "auto_model/urm"},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}}]
    if with_result:
        events.append({"type": "result", "subtype": "error_during_execution" if is_error else "success",
                       "is_error": is_error, "result": answer, "num_turns": 5, "session_id": session_id})
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


# ---------- 假 docker ----------

def done(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class FakeDocker:
    """替换 fbrlib.run / fbrlib.run_stream。docker 命令按子命令给出预设结果，chmod 放行，其余一律算测试写错了。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers: set[str] = set()
        self.projects: dict[str, Path] = {}     # 容器名 -> 预制的 projects 目录
        self.outcomes: list[dict] = []          # run_stream 依次取用
        self.exec_rcs: list[int] = []           # docker exec 依次取用，取完默认 0
        self.cp_ok = True
        self.image_id = IMAGE_ID
        self.context_limit = "1000000"
        self.version_line = f"{VERSION} (Claude Code)"

    def docker_calls(self, *head: str) -> list[list[str]]:
        return [c for c in self.calls if c[:1 + len(head)] == ["docker", *head]]

    def run(self, command, *, cwd=None, timeout=None, env=None, input_text=None):
        command = [str(part) for part in command]
        self.calls.append(command)
        if command[0] == "chmod":
            return REAL_RUN(command, cwd=cwd, timeout=timeout)
        if command[0] != "docker":
            raise AssertionError(f"自测里不该执行的外部命令：{command[:3]}")
        verb = command[1]
        if verb == "container" and command[2] == "inspect":
            return done(0, "exited\n") if command[-1] in self.containers else done(1, "", "No such container")
        if verb == "image" and command[2] == "inspect":
            if "{{.Id}}" in command:
                return done(0, self.image_id + "\n")
            return done(0, f"PATH=/usr/bin\nCLAUDE_CODE_MAX_CONTEXT_TOKENS={self.context_limit}\n")
        if verb == "cp":
            name = command[2].split(":", 1)[0]
            if not self.cp_ok or name not in self.projects:
                return done(1, "", "Error response from daemon: cp failed")
            shutil.copytree(self.projects[name], command[3])
            return done(0)
        if verb == "rm":
            if "-f" in command:
                raise AssertionError("不许强删容器")
            self.containers.discard(command[-1])
            return done(0)
        if verb == "stop":
            return done(0)
        if verb == "kill":
            return done(0)      # kill 只是停掉，容器还在
        if verb == "exec":
            return done(self.exec_rcs.pop(0) if self.exec_rcs else 0, "setup output\n")
        if verb == "run" and "--version" in command:
            return done(0, self.version_line + "\n")
        if verb == "run" and "-d" in command:
            name = command[command.index("--name") + 1]
            self.containers.add(name)
            self.mounts = getattr(self, "mounts", {})
            self.mounts[name] = command[command.index("--mount") + 1]
            return done(0, "c0ffee\n")
        raise AssertionError(f"没有预设结果的 docker 命令：{command[:4]}")

    def run_stream(self, command, *, stdin_text, stdout_path, stderr_path, timeout):
        command = [str(part) for part in command]
        self.calls.append(command)
        outcome = self.outcomes.pop(0)
        name = command[3] if command[1] == "exec" else command[command.index("--name") + 1]
        mount = self.mounts[name] if command[1] == "exec" else command[command.index("--mount") + 1]
        workspace = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        outcome["stdin"] = stdin_text
        self.containers.add(name)
        Path(stdout_path).write_text(outcome.get("stdout", ""), encoding="utf-8")
        Path(stderr_path).write_text(outcome.get("stderr", ""), encoding="utf-8")
        if outcome.get("effect"):
            outcome["effect"](workspace)
        if outcome.get("projects"):
            self.projects[name] = outcome["projects"]
        return outcome.get("rc", 0), outcome.get("timed_out", False)


def step_rows(steps: list[dict], results: dict[str, tuple[int, str]]) -> list[dict]:
    """假 run_steps_in_image 的返回：results 按步骤名给 (退出码, 输出)。没给的算没跑到。"""
    rows = []
    for step in steps:
        rc, tail = results.get(step["name"], (None, ""))
        rows.append({"name": step["name"], "cmd": step["cmd"], "rc": rc, "seconds": None if rc is None else 1,
                     "tail": tail})
    return rows
