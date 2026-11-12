#!/usr/bin/env python3
"""R4 录屏：在任务镜像的新容器里跑演示命令，宿主机用无头浏览器把终端画面录成 mp4。

    fbr_record.py --batch b1110-01 [--tasks …] [--redo]

做法：
- 把产物提交克隆到临时目录，挂进一个新容器（不带 key）。准备命令在录制开始前跑完，不进画面。
- 演示命令逐条在同一个容器里执行，输出原样进画面；命令报错、跑不起来也照录，不重试到成功为止。
- 准备命令失败会自动重来，最多 max_attempts 次；仍失败就带着这个状态录，并记进 recording.json。
- 成片 1280×720、H.264、不超过 90 秒；超出的部分截掉并在 recording.json 里标明哪一步被截断。
- 只录终端。页面类演示这一版没有实现。
"""

from __future__ import annotations

import argparse
import codecs
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib  # noqa: E402

PAGE = """<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;width:%(w)dpx;height:%(h)dpx;background:#0d1117;overflow:hidden}
.win{position:absolute;left:24px;top:20px;right:24px;bottom:20px;background:#161b22;border-radius:10px;
border:1px solid #30363d;overflow:hidden}
.bar{height:34px;background:#21262d;display:flex;align-items:center;padding-left:14px;gap:8px}
.bar i{width:12px;height:12px;border-radius:50%%;display:inline-block}
#output{position:absolute;top:34px;left:0;right:0;bottom:0;margin:0;padding:12px 16px;color:#e6edf3;
font:16px/23px Menlo,Monaco,Consolas,monospace;white-space:pre-wrap;word-break:break-all;overflow:hidden}
</style><div class="win"><div class="bar"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i>
<i style="background:#27c93f"></i></div><pre id="output"></pre></div>"""


class ScreenBuffer:
    """把终端字节流还原成屏幕上的文本：去掉控制序列，处理回车覆盖和退格。"""

    def __init__(self, limit: int = 400) -> None:
        self.lines: list[str] = [""]
        self.limit = limit
        self.pending_cr = False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.escape = ""
        self.lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self.lock:
            text = self.decoder.decode(chunk)
            for char in text:
                if self.escape:
                    self.escape += char
                    if self.escape.startswith("\x1b["):
                        if len(self.escape) > 2 and "@" <= char <= "~":
                            self.escape = ""
                    elif self.escape.startswith("\x1b]"):
                        if char == "\x07" or self.escape.endswith("\x1b\\"):
                            self.escape = ""
                    elif len(self.escape) >= 2:
                        self.escape = ""
                    continue
                if char == "\x1b":
                    self.escape = char
                elif char == "\n":
                    self.pending_cr = False
                    self.lines.append("")
                elif char == "\r":
                    self.pending_cr = True
                elif char == "\b":
                    self.lines[-1] = self.lines[-1][:-1]
                elif char >= " " or char == "\t":
                    if self.pending_cr:
                        self.lines[-1] = ""
                        self.pending_cr = False
                    self.lines[-1] += char
            if len(self.lines) > self.limit:
                self.lines = self.lines[-self.limit:]

    def line(self, text: str) -> None:
        self.feed((text + "\n").encode("utf-8"))

    def snapshot(self) -> str:
        with self.lock:
            return "\n".join(self.lines)


def run_in_pty(command: list[str], screen: ScreenBuffer, raw_log, limit: float, tick) -> tuple[int, bool, float]:
    """在伪终端里跑一条命令，输出喂给 screen。返回 (退出码, 是否被截断, 用时)。tick 每 0.1 秒调一次刷新画面。"""
    master, slave = pty.openpty()
    started = time.time()
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                               env=dict(os.environ, TERM="xterm-256color"), preexec_fn=os.setsid)
    os.close(slave)
    cut = False
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    raw_log.write(chunk)
                    screen.feed(chunk)
                elif process.poll() is not None:
                    break
            elif process.poll() is not None:
                break
            tick()
            if time.time() - started > limit:
                cut = True
                with_suppress(lambda: os.killpg(process.pid, signal.SIGKILL))
                break
        process.wait(timeout=10)
    finally:
        os.close(master)
    return (124 if cut else process.returncode), cut, round(time.time() - started, 1)


def with_suppress(action) -> None:
    try:
        action()
    except (OSError, subprocess.SubprocessError):
        pass


def ffmpeg_path() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def doctor() -> dict:
    """实际检查 PTY、编码器和浏览器能否启动；不碰 Docker 或用户项目。"""
    import tempfile
    problems = []
    try:
        master, slave = pty.openpty()
        os.close(master)
        os.close(slave)
    except OSError:
        problems.append('PTY 不可用')
    try:
        result = subprocess.run([ffmpeg_path(), '-version'], capture_output=True, timeout=15)
        if result.returncode:
            problems.append('ffmpeg 不可用')
    except (OSError, ImportError, subprocess.TimeoutExpired):
        problems.append('ffmpeg 不可用')
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 720})
            page.set_content('<p>录屏体检</p>')
            page.screenshot()
            browser.close()
    except Exception:
        problems.append('Playwright 浏览器无法启动，请安装对应浏览器后重试')
    return {'ok': not problems, 'problems': problems}


def to_mp4(source: Path, target: Path, max_seconds: int, width: int, height: int) -> None:
    scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:-1:-1"
    result = lib.run([ffmpeg_path(), "-y", "-t", str(max_seconds), "-i", str(source), "-vf", scale, "-r", "30",
                      "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an",
                      "-movflags", "+faststart", str(target)], timeout=600)
    if result.returncode != 0 or not target.is_file():
        raise lib.FbrError("转码失败：" + result.stderr.strip()[-300:])


def probe_mp4(path: Path) -> dict:
    """读 mp4 的时长和画面尺寸（moov/mvhd 与 trak/tkhd），不依赖 ffprobe。"""
    data = Path(path).read_bytes()

    def boxes(start: int, end: int):
        offset = start
        while offset + 8 <= end:
            size, kind = struct.unpack(">I4s", data[offset:offset + 8])
            header = 8
            if size == 1:
                size, header = struct.unpack(">Q", data[offset + 8:offset + 16])[0], 16
            elif size == 0:
                size = end - offset
            if size < header:
                return
            yield kind.decode("latin1"), offset + header, offset + size
            offset += size

    out = {"width": 0, "height": 0, "duration": 0.0}
    for kind, start, end in boxes(0, len(data)):
        if kind != "moov":
            continue
        for inner, a, b in boxes(start, end):
            if inner == "mvhd":
                version = data[a]
                if version == 1:
                    scale, duration = struct.unpack(">IQ", data[a + 20:a + 32])
                else:
                    scale, duration = struct.unpack(">II", data[a + 12:a + 20])
                out["duration"] = round(duration / scale, 2) if scale else 0.0
            elif inner == "trak":
                for leaf, c, d in boxes(a, b):
                    if leaf == "tkhd":
                        width, height = struct.unpack(">II", data[d - 8:d])
                        if width >> 16 and height >> 16:
                            out["width"], out["height"] = width >> 16, height >> 16
    return out


def render(session_cmds: list[dict], exec_prefix: list[str], out_dir: Path, rec: dict) -> list[dict]:
    """真正的录制：开无头浏览器录屏，逐条跑演示命令。返回每步结果，webm 落到 out_dir/raw.webm。"""
    from playwright.sync_api import sync_playwright

    size = {"width": rec["width"], "height": rec["height"]}
    screen = ScreenBuffer()
    video_dir = out_dir / "video-tmp"
    steps = []
    with sync_playwright() as play, open(out_dir / "terminal.raw.log", "wb") as raw_log:
        browser = play.chromium.launch(headless=True)
        context = browser.new_context(viewport=size, record_video_dir=str(video_dir), record_video_size=size)
        page = context.new_page()
        page.set_content(PAGE % {"w": rec["width"], "h": rec["height"]})
        # 等待首次绘制，避免短演示在视频首帧仍是 about:blank 时就结束。
        page.screenshot()

        def tick() -> None:
            page.evaluate("async t => { const o = document.getElementById('output'); o.textContent = t;"
                          " o.scrollTop = o.scrollHeight; await new Promise(requestAnimationFrame); }",
                          screen.snapshot())

        began = time.time()
        budget = rec["max_seconds"] - 2
        for item in session_cmds:
            left = budget - (time.time() - began)
            if left < 3:
                steps.append({"cmd": item["cmd"], "show": item.get("show", ""), "rc": None, "seconds": 0,
                              "cut": True, "tail": "", "skipped": True})
                continue
            screen.line(f"$ {item['cmd']}")
            raw_log.write(f"$ {item['cmd']}\n".encode())
            tick()
            mark = len(screen.snapshot())
            rc, cut, seconds = run_in_pty([*exec_prefix, "bash", "-lc", item["cmd"]], screen, raw_log,
                                          min(rec["step_timeout"], left), tick)
            tick()
            page.wait_for_timeout(1200)
            steps.append({"cmd": item["cmd"], "show": item.get("show", ""), "rc": rc, "seconds": seconds,
                          "cut": cut, "tail": lib.clip(screen.snapshot()[mark:][-1500:], 1500), "skipped": False})
        page.wait_for_timeout(500)
        video = page.video
        context.close()
        shutil.move(video.path(), out_dir / "raw.webm")
        browser.close()
    shutil.rmtree(video_dir, ignore_errors=True)
    return steps


def start_container(image: str, clone: Path, port: int | None = None) -> str:
    name = f"s11-rec-{uuid.uuid4().hex[:10]}"
    ports = ['-p', f'127.0.0.1:{port}:{port}'] if port else []
    result = lib.run(["docker", "run", "-d", *lib.docker_hardening(), *ports, "--name", name, "--entrypoint",
                      "sleep", "--mount", lib.mount_arg(clone), image, "3600"], timeout=120)
    if result.returncode != 0:
        raise lib.FbrError("录屏容器起不来：" + result.stderr.strip()[:300])
    return name


@lib.task_locked
def record_one(task_dir: Path, cfg: dict, redo: bool = False) -> dict:
    record = lib.load_task(task_dir)
    name = record["task_id"]
    if lib.stage_ok(task_dir, "record") and not redo:
        return {"task_id": name, "state": "skip", "detail": "已经录过"}
    commit = lib.require_stage(task_dir, "commit")["commit"]
    lib.restore_git_config(task_dir)
    plan = lib.load_json(task_dir / "recording-plan.json")
    override = lib.load_json(task_dir / "review" / "recording-plan.override.json", default={})
    if override:
        if (override.get("source_sha256") != lib.content_hash(plan)
                or not override.get("authorization") or not isinstance(override.get("plan"), dict)):
            raise lib.FbrError("录屏方案覆盖缺少授权或不匹配冻结原方案")
        plan = override["plan"]
    if plan.get("business_demo_status") == "pending_implementation":
        raise lib.FbrError("录屏方案仍为基础构建占位，必须补齐真实服务启动和业务演示")
    plan_digest = lib.content_hash(plan)
    web = plan.get('mode') == 'web'
    web_info = {}
    if web:
        import fbr_web
        fbr_web.validate_plan(plan)
    demo = [item for item in plan.get("demo", []) if isinstance(item, dict) and item.get("cmd")]
    if not demo and not web:
        raise lib.FbrError(f"{name}：recording-plan.json 里没有演示命令")
    previous_ok = lib.stage_ok(task_dir, "record")
    if not previous_ok:
        lib.set_stage(task_dir, "record", "dry", reason="正在重新录制")
    rec = cfg["record"]
    out_dir = task_dir / "record"
    out_dir.mkdir(exist_ok=True)
    attempt_dir = out_dir / "attempts" / uuid.uuid4().hex
    attempt_dir.mkdir(parents=True)
    setup_cmd = (plan.get("setup_cmd") or "").strip()
    setup_runs, steps, container, clone = [], [], "", None
    retained = []
    try:
        for attempt in range(1, int(rec["max_attempts"]) + 1):
            if container:
                lib.run(["docker", "kill", container], timeout=60)
            clone = lib.clean_clone(task_dir / "workspace", commit, f"s11-rec-{name}")
            seeded = False
            if cfg.get('dependencies', {}).get('enabled') and setup_cmd:
                import fbr_dependencies
                seeded = fbr_dependencies.seed(clone, cfg, setup_cmd=setup_cmd)
            port = fbr_web.free_port() if web else None
            container = start_container(record["image"], clone, port) if web else start_container(record["image"], clone)
            retained.append({"name": container, "clone": str(clone), "attempt": attempt})
            lib.save_json(out_dir / "containers.json", {"containers": retained, "retained": True})
            if not setup_cmd:
                break
            done = lib.run(["docker", "exec", "-w", "/workspace", container, "bash", "-lc", (cfg.get("dependencies", {}).get("setup_after_restore_cmd") or 'true') if seeded else setup_cmd],
                           timeout=int(rec["setup_timeout"]))
            tail = "\n".join((done.stdout + "\n" + done.stderr).strip().splitlines()[-30:])
            setup_runs.append({"attempt": attempt, "rc": done.returncode, "tail": lib.clip(tail, 2000)})
            if done.returncode == 0:
                break
        if web:
            steps, web_info = fbr_web.render(plan, container, port, attempt_dir, rec)
        else:
            steps = render(demo, ["docker", "exec", "-it", "-w", "/workspace", container], attempt_dir, rec)
    finally:
        if container:
            lib.run(["docker", "kill", container], timeout=60)
    target = attempt_dir / "recording.mp4"
    to_mp4(attempt_dir / "raw.webm", target, int(rec["max_seconds"]), rec["width"], rec["height"])
    (attempt_dir / "raw.webm").unlink(missing_ok=True)
    video = probe_mp4(target)
    problems = []
    if (video["width"], video["height"]) != (rec["width"], rec["height"]):
        problems.append(f"画面是 {video['width']}×{video['height']}")
    if not 0 < video["duration"] <= rec["max_seconds"] + 0.5:
        problems.append(f"时长 {video['duration']} 秒")
    if web and web_info.get('startup_seconds', 0) < 8:
        problems.append('真实启动段不足 8 秒')
    if web and web_info.get('startup_ready') and web_info.get('page_seconds', 0) < 8:
        problems.append('页面段不足 8 秒')
    if web and web_info.get('startup_ready') and not web_info.get('page_segment'):
        problems.append('服务就绪但缺少页面段')
    if any(step.get('skipped') or step.get('cut') for step in steps):
        problems.append('关键演示被截断或未执行，需要检查方案时长分配')
    setup_ok = not setup_cmd or (bool(setup_runs) and setup_runs[-1]["rc"] == 0)
    if not setup_ok:
        problems.append('环境准备失败，不能作为有效录屏')
    failure_kind = 'none'
    if not setup_ok:
        detail = setup_runs[-1]['tail']
        if setup_runs[-1]['rc'] in (124, 137) or re.search(r'ENOTFOUND|ETIMEDOUT|ECONN|TLS|SSL|Could not resolve|network', detail, re.I):
            failure_kind = 'environment'
        elif re.search(r'compilation failed|compile error|error TS\d|SyntaxError|BUILD FAILED|cannot find symbol', detail, re.I):
            failure_kind = 'product'
        else:
            failure_kind = 'unresolved'
    metadata = {
        "task_id": name, "commit": commit, "plan_sha256": plan_digest, "plan_override": override, "setup_cmd": setup_cmd, "setup_ok": setup_ok, "setup_runs": setup_runs,
        "containers": retained,
        "steps": steps, "video": {"file": "record/recording.mp4", **video}, "problems": problems,
        "recorded_at": lib.now_iso(), 'failure_kind': failure_kind, **web_info}
    lib.save_json(attempt_dir / "recording.json", metadata)
    lib.save_json(out_dir / "last-attempt.json", {"path": str(attempt_dir), "problems": problems})
    if problems:
        if not previous_ok:
            lib.set_stage(task_dir, "record", "failed", problems=problems)
        return {"task_id": name, "state": "failed", "detail": "成片不合规：" + "；".join(problems)}
    # 候选通过后才使下游失效并替换；候选及原始日志保留，失败不触碰当前合格版本。
    lib.set_stage(task_dir, "record", "dry", reason="候选录屏通过，更新当前版本")
    for filename in ("recording.mp4", "terminal.raw.log"):
        source = attempt_dir / filename
        if source.is_file():
            staging = out_dir / (filename + ".new")
            shutil.copy2(source, staging)
            staging.replace(out_dir / filename)
    lib.save_json(out_dir / "recording.json", metadata)
    lib.set_stage(task_dir, "record", "ok", duration=video["duration"], setup_ok=setup_ok)
    failed = sum(1 for s in steps if s["rc"] not in (0, None))
    return {"task_id": name, "state": "ok",
            "detail": f"{video['duration']} 秒，演示 {len(steps)} 步，其中 {failed} 步退出码非 0"
                      + ("" if setup_ok else "，准备命令没成功")}


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 运行录屏")
    parser.add_argument("--root")
    parser.add_argument("--batch")
    parser.add_argument('--doctor', action='store_true')
    parser.add_argument("--tasks")
    parser.add_argument("--redo", action="store_true")
    args = parser.parse_args()
    if args.doctor:
        import json
        report = doctor()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['ok'] else 1
    if not args.batch:
        parser.error('必须提供 --batch 或 --doctor')
    try:
        root = lib.find_root(args.root)
        cfg = lib.settings(root)
        todo = [p for p in lib.task_dirs(root, args.batch, args.tasks) if lib.stage_ok(p, "commit")]

        def safe(task_dir: Path) -> dict:
            try:
                return record_one(task_dir, cfg, args.redo)
            except lib.FbrError as exc:
                if not lib.stage_ok(task_dir, "record"):
                    lib.set_stage(task_dir, "record", "failed", reason=str(exc))
                return {"task_id": task_dir.name, "state": "failed", "detail": str(exc)}

        with ThreadPoolExecutor(max_workers=max(1, int(cfg["record_parallel"]))) as pool:
            rows = list(pool.map(safe, todo))
        for row in rows:
            print(f"{row['task_id']}  {row['state']}  {row['detail']}", flush=True)
        return 0 if all(r["state"] in ("ok", "skip") for r in rows) else 1
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
