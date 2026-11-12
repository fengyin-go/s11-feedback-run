#!/usr/bin/env python3
"""R0 预检、R1 单轮运行、R2 收集轨迹。

    fbr_run.py preflight
    fbr_run.py run     --batch b1110-01 [--tasks s11-000001,…] [--limit N]            # 演练：只检查、列出命令
    fbr_run.py run     --batch b1110-01 --execute                                    # 真跑（会起容器、消耗网关额度）
    fbr_run.py collect --batch b1110-01 [--tasks …] [--keep-container]

边界：
- 一题一个新容器，容器名就是任务编号；不加 --rm，轨迹导出并核对通过后才 docker rm（不带 -f）。
- key 只通过 --env-file 传入；输出或轨迹出现 key 时保留原件、保留容器并立即停止，不重跑。
- 模型中断可在同一会话累计最多继续两次；工程异常和零代码改动归档重跑，含首次最多 max_attempts 次。
- 重跑前把这次的轨迹和改动归档到 run/attempt-N/，导出失败就停下，不删容器。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import stat
import sys
import uuid
import os
import re
import time
import contextlib
import subprocess
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib  # noqa: E402
import fbr_trace  # noqa: E402
import fbr_runtime as runtime


# ---------- R0 ----------

def preflight(cfg: dict) -> list[dict]:
    checks = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    result = lib.run(["docker", "image", "inspect", "--format", "{{.Id}}", cfg["image"]], timeout=60)
    image_id = result.stdout.strip()
    add("镜像摘要", result.returncode == 0 and image_id == cfg["image_id"],
        f"本机 {image_id or '没有这个镜像'}，要求 {cfg['image_id']}")

    result = lib.run(["docker", "image", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}",
                      cfg["image"]], timeout=60)
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    limit = env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "")
    add("context 上限", result.returncode == 0 and limit == str(cfg["run"]["context_limit"]),
        f"镜像里是 {limit or '未设置'}，要求 {cfg['run']['context_limit']}")

    version_container = f"s11-preflight-{uuid.uuid4().hex[:12]}"
    result = lib.run(["docker", "run", *lib.docker_hardening(), "--name", version_container,
                      "--entrypoint", "claude", cfg["image"],
                      "--version"], timeout=120)
    version = result.stdout.strip().split(" ")[0] if result.stdout.strip() else ""
    add("Claude Code 版本", result.returncode == 0 and version == cfg["harness_version"],
        f"镜像里是 {version or '读不到'}，要求 {cfg['harness_version']}；检查容器 {version_container}（保留）")

    path = lib.key_path(cfg)
    if not path.is_file():
        add("key 文件", False, f"{path} 不存在")
    else:
        mode = stat.S_IMODE(path.stat().st_mode)
        problems = []
        if mode != 0o600:
            problems.append(f"权限是 {mode:o}，要 600")
        if not lib.read_key(cfg):
            problems.append("里面没有 apikey=… 这一行")
        add("key 文件", not problems, "；".join(problems))

    missing = [name for name in ("playwright", "imageio_ffmpeg", "openpyxl") if importlib.util.find_spec(name) is None]
    report = {'ok': False, 'problems': []}
    if not missing:
        import fbr_record
        report = fbr_record.doctor()
    add("录屏与表格依赖", not missing and report['ok'], "缺 " + "、".join(missing) if missing else '；'.join(report['problems']))
    return checks


def print_checks(checks: list[dict]) -> bool:
    for item in checks:
        print(f"  {'通过' if item['ok'] else '不符'}  {item['name']}" + (f"  {item['detail']}" if item["detail"] else ""))
    return all(item["ok"] for item in checks)


# ---------- R1 ----------

def container_exists(name: str) -> bool:
    return lib.run(["docker", "container", "inspect", "--format", "{{.State.Status}}", name], timeout=30).returncode == 0


def key_hold(task_dir: Path) -> bool:
    return any(info.get("key_leak") for info in lib.load_state(task_dir)["stages"].values())


def halt_for_key(task_dir: Path, stage: str) -> dict:
    detail = "检测到网关 key 泄露：保留原始输出、轨迹和容器，停止重跑与后续步骤，等待人工处理"
    lib.set_stage(task_dir, stage, "failed", key_leak=True, problems=[detail])
    return {"task_id": task_dir.name, "state": "failed", "detail": detail}


def evidence_has_key(paths, secret: str) -> bool:
    return any(lib.key_occurrences(path, secret) for path in paths)


def prepare_legacy_progress_recovery(task_dir: Path, cfg: dict, *, execute: bool) -> dict:
    """Issue one re-dispatch token iff an old controller killed a progressing run."""
    path = task_dir / "run/legacy-progress-recovery.json"
    marker = lib.load_json(path, default={})
    current = lib.load_json(task_dir / "run/current.json", default={})
    attempts = lib.load_json(task_dir / "run/attempts.json", default=[])
    if not attempts:
        return {"task_id": task_dir.name, "state": "blocked", "detail": "没有可核验的旧运行尝试"}
    last = attempts[-1]
    if current.get("state") != "archived" or current.get("attempt") != last.get("n"):
        return {"task_id": task_dir.name, "state": "blocked", "detail": "旧运行不是已归档的当前尝试"}
    if not last.get("timed_out") or str(last.get("reason", "")) not in ("进程超时", "timeout"):
        return {"task_id": task_dir.name, "state": "blocked", "detail": "旧运行不是控制器超时"}
    heartbeat = lib.load_json(task_dir / "run/heartbeat.json", default={})
    budget = lib.load_json(task_dir / "run/budget.json", default={})
    active_seconds = float(budget.get("active_seconds") or 0)
    elapsed = float(heartbeat.get("elapsed") or 0)
    stall = float(cfg["run"].get("stall_seconds", 480))
    idle = float(heartbeat.get("idle") if heartbeat.get("idle") is not None else stall + 1)
    output_idle = float(heartbeat.get("output_idle") if heartbeat.get("output_idle") is not None else stall + 1)
    events = int(heartbeat.get("events") or 0)
    tools = int(heartbeat.get("tools") or 0)
    if active_seconds < cfg["run"]["task_budget"] or idle > stall or output_idle > stall or events <= 0 or tools <= 0:
        return {"task_id": task_dir.name, "state": "blocked", "detail": "旧运行缺少持续进展证据"}
    if marker.get("state") == "ready" and marker.get("attempt") == last.get("n"):
        return {"task_id": task_dir.name, "state": "ready", "detail": "已有一次性进度恢复令牌"}
    if marker.get("used_at"):
        return {"task_id": task_dir.name, "state": "used", "detail": "一次性进度恢复令牌已使用"}
    if not execute:
        return {"task_id": task_dir.name, "state": "dry", "detail": "可发放一次性进度恢复令牌"}
    lib.save_json(path, {
        "state": "ready",
        "attempt": last.get("n"),
        "created_at": lib.now_iso(),
        "reason": "旧控制器在持续进展时按累计预算超时",
        "evidence": {"active_seconds": round(active_seconds, 3), "segment_elapsed": round(elapsed, 3),
                     "idle": idle, "output_idle": output_idle,
                     "events": events, "tools": tools},
    })
    return {"task_id": task_dir.name, "state": "ready", "detail": "已发放一次性进度恢复令牌"}


def use_legacy_progress_recovery(task_dir: Path, attempt: int) -> bool:
    """Consume a validated one-shot recovery token for the next attempt."""
    path = task_dir / "run/legacy-progress-recovery.json"
    marker = lib.load_json(path, default={})
    current = lib.load_json(task_dir / "run/current.json", default={})
    if (marker.get("state") != "ready" or marker.get("used_at")
            or marker.get("attempt") != current.get("attempt")):
        return False
    marker.update(state="used", used_at=lib.now_iso(), used_attempt=attempt)
    lib.save_json(path, marker)
    return True


def bound_key(task_dir: Path, cfg: dict, *, start: bool = False) -> str:
    """只能用起跑时的同一 key 核对泄露；缺 key 或换 key 不能降级为空扫描。"""
    path = lib.key_path(cfg)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise lib.FbrError("key 文件不存在或权限不是 600，保留原始证据与容器，不继续")
    secret = lib.read_key(cfg)
    if not secret:
        raise lib.FbrError("key 文件没有可用的 apikey，保留原始证据与容器，不继续")
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()
    identity_path = task_dir / "run" / "key-identity.json"
    if identity_path.is_file():
        identity = lib.load_json(identity_path)
        if not isinstance(identity, dict) or identity.get("sha256") != fingerprint:
            raise lib.FbrError("当前 key 与起跑时的 SHA256 指纹不符，保留原始证据与容器，不继续")
    elif start and not (task_dir / "run" / "attempts.json").exists():
        lib.save_json(identity_path, {"sha256": fingerprint})
    else:
        raise lib.FbrError("缺少起跑 key 的 SHA256 指纹，无法确认泄露扫描有效，保留原始证据与容器")
    return secret


def ready_problems(task_dir: Path, record: dict, cfg: dict, *, prepared: bool = False) -> list[str]:
    workspace = task_dir / "workspace"
    problems = []
    if key_hold(task_dir):
        return ["已检测到网关 key 泄露，保留证据与容器，等待人工处理"]
    current = lib.load_json(task_dir / "run/current.json", default={})
    if current.get("state") in ("running", "starting") and not prepared:
        return ["存在未收口运行，先执行 recover；不在活跃工作区执行宿主机 Git"]
    if (record.get("publish") or {}).get("state") != "published":
        problems.append("任务仓库还没发布（初始环境快照没有地址）")
    if record["image"] != cfg["image"] or record.get("harness_version") != cfg["harness_version"]:
        problems.append("task.json 里的镜像或 Harness 版本和当前配置不一致")
    if record.get("handoff_version") == 2:
        for field in ("image_id", "harness", "os"):
            if record.get(field) != cfg.get(field):
                problems.append(f"冻结基线的 {field} 与当前配置不一致")
    if not (workspace / ".git").exists():
        return problems + ["workspace 不是 git 仓库"]
    lib.validate_git_layout(task_dir)
    if "," in str(workspace.resolve()):
        problems.append("workspace 路径带逗号")
    run_dir = task_dir / "run"
    if (run_dir / "git-config.before").is_file():
        lib.restore_git_config(task_dir)
    elif any((run_dir / name).exists() for name in ("stream.jsonl", "attempts.json")):
        return problems + ["没有起跑前的 git 配置备份，不在宿主机上对这个 workspace 执行 git"]
    if record.get("producer") == "s11-gsb-prompt-create":
        branch = lib.git(workspace, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if branch != "main":
            problems.append("S11 workspace 必须停在 main 分支")
    if lib.git(workspace, "rev-parse", "HEAD").stdout.strip() != record["initial_commit"]:
        problems.append("workspace 的 HEAD 不是初始提交")
    if record.get("handoff_version") == 2:
        tree = lib.git(workspace, "rev-parse", "HEAD^{tree}").stdout.strip()
        if tree != record["baseline"].get("tree"):
            problems.append("workspace 文件树与冻结基线不一致")
    if __import__("fbr_dependencies").unexpected_ignored(task_dir, lib.git(workspace, "status", "--porcelain", "--ignored").stdout.strip()):
        problems.append("workspace 不干净（有未提交或被忽略的文件）")
    import fbr_dependencies
    if lib.git(workspace, 'cat-file', '-e', record['initial_commit']).returncode == 0:
        problems += fbr_dependencies.snapshot_problems(workspace, record['initial_commit'])
    if not prepared and container_exists(record["task_id"]):
        problems.append(f"已有同名容器 {record['task_id']}：先确认它的轨迹导出过，再手动 docker rm")
    return problems


def run_command(task_dir: Path, record: dict, cfg: dict) -> list[str]:
    return ["docker", "run", "-i", "-d", *lib.docker_hardening(), "--name", record["task_id"],
            "--label", "solo11.owner=" + lib.content_hash(str(task_dir.resolve())),
            "--mount", lib.mount_arg(task_dir / "workspace"), "--env-file", str(lib.key_path(cfg)),
            "--entrypoint", "sleep", record["image"], "infinity"]


def execution_command(name: str, session_id: str = "") -> list[str]:
    if not session_id:
        return ["docker", "exec", "-i", name, "/usr/local/bin/entrypoint.sh", "print"]
    # 仅同一道题恢复。首轮仍执行镜像入口的空会话检查；续写沿用同一组隔离参数。
    uuid.UUID(session_id)
    shell = """export ANTHROPIC_AUTH_TOKEN="${apikey:-${APIKEY:-${ANTHROPIC_AUTH_TOKEN:-}}}"
unset apikey APIKEY ANTHROPIC_API_KEY NODE_OPTIONS NODE_PATH
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 CLAUDE_CODE_SAFE_MODE=1 DISABLE_AUTOUPDATER=1
exec claude --safe-mode --disable-slash-commands --setting-sources '' \
  --settings '{"autoMemoryEnabled":false}' --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --tools 'Bash,Read,Write,Edit,Glob,Grep' --dangerously-skip-permissions \
  -p --output-format stream-json --verbose --resume "$1"
"""
    return ["docker", "exec", "-i", name, "bash", "-c", shell, "fbr-continue", session_id]


def archive_attempt(task_dir: Path, record: dict, number: int, secret: str = "") -> None:
    """把一次不算数的运行归档，然后把容器和工作区恢复到可以重跑的状态。"""
    name, workspace = record["task_id"], task_dir / "workspace"
    target = task_dir / "run" / f"attempt-{number}"
    target.mkdir(parents=True, exist_ok=True)
    for item in ("stream.jsonl", "stderr.log", "continuations.json",
                 *(p.name for p in (task_dir / "run").glob(f"continue-{number}-*"))):
        if (task_dir / "run" / item).is_file():
            shutil.move(str(task_dir / "run" / item), str(target / item))
    if container_exists(name):
        lib.run(["docker", "stop", name], timeout=60)
        copied = lib.run(["docker", "cp", f"{name}:{lib.TRACE_SRC}", str(target / "projects")], timeout=300)
        if copied.returncode != 0:
            raise lib.FbrError(f"{name}：第 {number} 次运行的轨迹导出失败，容器先留着：{copied.stderr.strip()[:200]}")
        if evidence_has_key(target.rglob("*"), secret):
            raise lib.KeyLeakError("不算数的运行轨迹含网关 key，保留证据与容器，停止重跑")
        lib.run(["docker", "stop", name], timeout=60)
        removed = lib.run(["docker", "rm", name], timeout=60)
        if removed.returncode != 0:
            raise lib.FbrError(f"{name}：删除旧容器失败：{removed.stderr.strip()[:200]}")
    lib.restore_git_config(task_dir)
    lib.git(workspace, "add", "-A")
    (target / "diff.patch").write_text(
        lib.git(workspace, "diff", "--cached", record["initial_commit"]).stdout, encoding="utf-8")
    lib.git_out(workspace, "reset", "--hard", record["initial_commit"])
    lib.git_out(workspace, "clean", "-fdx")
    lib.save_json(task_dir / "run/current.json", {"state": "archived", "attempt": number})


def code_changes(task_dir: Path, record: dict) -> list[str]:
    """比较最终树；提交次数、改过又还原、缓存和文档均不能充作代码改动。"""
    lib.restore_git_config(task_dir)
    workspace = task_dir / "workspace"
    lib.git_out(workspace, "add", "-A")
    tree = lib.git_out(workspace, "write-tree")
    paths = lib.git_out(workspace, "diff", "--no-renames", "--name-only", "-z",
                        record["initial_commit"], tree).split("\0")
    noise = re.compile(r"(^|/)(node_modules|__pycache__|\.cache|\.pytest_cache|dist|coverage|\.venv)(/|$)|\.(log|pyc|mp4|webm)$", re.I)
    docs = {'.md', '.rst', '.txt'}
    return [p for p in paths if p and not noise.search(p) and Path(p).suffix.lower() not in docs]


def prepared_problems(task_dir: Path, record: dict, cfg: dict, current: dict) -> list[str]:
    """Prove a dead controller stopped before creating any dispatch request."""
    if current.get('state') != 'starting' or any(current.get(k) for k in
            ('pid', 'request_id', 'result', 'session_id', 'continuations')):
        return ['已有派发身份，不能按未派发容器恢复']
    owner = {'pid': current.get('owner_pid'), 'identity': current.get('owner_identity')}
    if runtime.alive(owner):
        return ['原调度仍活着，不接管正在等待派发的容器']
    if not current.get('container_id') or not container_exists(task_dir.name):
        return ['未派发容器缺失或身份不完整']
    attempts = lib.load_json(task_dir / 'run/attempts.json', default=[])
    if current.get('attempt') != len(attempts) + 1 or len(attempts) >= cfg['run']['max_attempts']:
        return ['未派发记录与已有尝试次数不符，保留现场']
    try:
        started = datetime.fromisoformat(current['started_at']).timestamp()
        for path in (task_dir / 'run').glob('exec-*.json'):
            request = lib.load_json(path)
            if not request.get('started_epoch') or request['started_epoch'] >= started:
                return ['存在本次派发请求或无法确认请求时间，保留现场']
    except (KeyError, TypeError, ValueError):
        return ['派发时间材料不完整，保留现场']
    if any(p.stat().st_size for name in ('stream.jsonl', 'stderr.log')
           if (p := task_dir / 'run' / name).exists()):
        return ['存在未归档运行输出，不能认定尚未派发']
    processes = lib.run(['docker', 'top', task_dir.name, '-eo', 'pid,comm'], timeout=30)
    names = processes.stdout.splitlines()[1:]
    if processes.returncode or sorted(n.split()[-1] for n in names if n.strip()) != ['docker-init', 'sleep']:
        return ['容器不只包含初始等待进程，不能认定尚未派发']
    sessions = lib.run(['docker', 'exec', task_dir.name, 'python3', '-c',
        'from pathlib import Path; import sys; sys.exit(int(any(p.is_file() for p in Path("'
        + lib.TRACE_SRC + '").rglob("*"))))'], timeout=30)
    if sessions.returncode:
        return ['容器已有会话文件或无法检查会话，不能重新发送题面']
    return ready_problems(task_dir, record, cfg, prepared=True)


@lib.task_locked
def recover_one(task_dir: Path, cfg: dict) -> dict:
    """只认本任务持久回执及容器归属；活进程不杀、不重复投题。"""
    current_path = task_dir / 'run/current.json'
    current = lib.load_json(current_path, default={})
    row = {'task_id': task_dir.name}
    if key_hold(task_dir):
        return {**row, 'state': 'blocked', 'detail': 'key 泄露任务不能自动恢复'}
    if lib.stage_ok(task_dir, 'run'):
        return {**row, 'state': 'ok', 'detail': '运行已收口，可继续 collect'}
    if current.get('state') not in ('running', 'exited', 'starting', 'continue_ready'):
        return {**row, 'state': 'idle', 'detail': '没有需要接管的运行'}
    record = lib.load_task(task_dir)
    if current.get('generation') != lib.content_hash([record['prompt'], record['initial_commit'], record['task_repo_full']]):
        raise lib.FbrError('恢复材料与起跑题面/基线/仓库不符，保留现场')
    if container_exists(task_dir.name):
        owner = lib.run(['docker', 'inspect', '--format', '{{index .Config.Labels "solo11.owner"}}', task_dir.name], timeout=30)
        cid = lib.run(['docker', 'inspect', '--format', '{{.Id}}', task_dir.name], timeout=30)
        if owner.stdout.strip() != lib.content_hash(str(task_dir.resolve())) or (
                current.get('container_id') and current['container_id'] != cid.stdout.strip()):
            raise lib.FbrError('同名容器不属于本次运行，不接管、不删除')
    if current.get('state') == 'continue_ready':
        if not container_exists(task_dir.name):
            return {**row, 'state': 'blocked', 'detail': '原会话容器缺失，不能继续'}
        return {**row, 'state': 'continue_ready', 'detail': '原会话已核验，可发送获准的继续'}
    if runtime.alive(current):
        if cfg.get('_managed'):
            bound_key(task_dir, cfg)
            runtime.Gateway(cfg).track_existing(current)
            owner = {'pid': current.get('owner_pid'), 'identity': current.get('owner_identity')}
            if not runtime.alive(owner):
                runtime.monitor_existing(task_dir, cfg, current)
                current = lib.load_json(current_path)
            else:
                return {**row, 'state': 'running', 'detail': '持久 worker 仍活着，原调度继续监控，不重跑'}
        else:
            return {**row, 'state': 'running', 'detail': '持久 worker 仍活着，继续等待，不重跑'}
    if (current.get('state') == 'starting' and not current.get('container_id')
            and not container_exists(task_dir.name)):
        # The operation lock is held: a failed dependency setup cannot still dispatch.
        # Require positive absence of any execution identity or output before redoing setup.
        if any(current.get(k) for k in ('pid', 'request_id', 'result', 'session_id', 'continuations')):
            return {**row, 'state': 'blocked', 'detail': '起跑身份不完整，保留现场'}
        if list((task_dir/'run').glob('exec-*.json')) or any(
                p.stat().st_size for name in ('stream.jsonl', 'stderr.log')
                if (p := task_dir/'run'/name).is_file()):
            return {**row, 'state': 'blocked', 'detail': '已有请求或输出，不能重做派发前准备'}
        problems = ready_problems(task_dir, record, cfg, prepared=True)
        if problems:
            return {**row, 'state': 'blocked', 'detail': '；'.join(problems)}
        history_path = task_dir/'run/pre-container-recovery.json'
        history = lib.load_json(history_path, default={'events': []})
        history['events'].append({'at': lib.now_iso(), 'previous': current,
                                 'reason': '容器尚未创建、请求和输出均不存在，仅重做依赖准备'})
        lib.save_json(history_path, history)
        lib.save_json(current_path, {**current, 'state': 'setup_ready'})
        return {**row, 'state': 'idle', 'detail': '已核实未创建容器、未发送题面；原次数及预算保留'}
    if current.get('state') == 'starting' and not current.get('request_id'):
        problems = prepared_problems(task_dir, record, cfg, current)
        return {**row, 'state': 'blocked' if problems else 'prepared',
                'detail': '；'.join(problems) if problems else '未派发容器和干净基线已核验，可接续同一次起跑'}
    done = lib.load_json(Path(current.get('result', '/nonexistent')), default={})
    if done.get('id') != current.get('request_id') or 'rc' not in done:
        return {**row, 'state': 'blocked', 'detail': 'worker 已退出但无匹配回执，保留容器和全部证据'}
    secret = bound_key(task_dir, cfg)
    if evidence_has_key((p for p in (task_dir / 'run').rglob('*') if p.is_file()), secret):
        return halt_for_key(task_dir, 'run')
    stdout, stderr = Path(current['stdout']), Path(current['stderr'])
    segment = fbr_trace.read_stream(stdout)
    tail = stderr.read_text(errors='replace')[-4000:]
    request = lib.load_json(task_dir / 'run' / ('exec-' + str(current.get('request_id')) + '.json'), default={})
    if request.get('started_epoch') and done.get('ended_epoch'):
        budget_path = task_dir / 'run/budget.json'
        budget = lib.load_json(budget_path, default={'active_seconds': 0, 'waiting_seconds': 0})
        initial_budget = request.get('base_budget', {})
        waited = max(0, budget.get('waiting_seconds', 0) - initial_budget.get('waiting_seconds', 0))
        budget['active_seconds'] = max(budget['active_seconds'], initial_budget.get('active_seconds', 0) +
            max(0, done['ended_epoch'] - request['started_epoch'] - waited))
        lib.save_json(budget_path, budget)
    import fbr_completion
    decision = fbr_trace.classify_attempt(done['rc'], current.get('timed_out', False) or done.get('timed_out', False), segment, tail,
                                          completed_cleanup=fbr_completion.valid(task_dir, current, done),
                                          termination_reason=current.get('reason', 'worker_guard' if done.get('timed_out') else ''))
    if segment.get('model') and segment['model'] != cfg.get('model', 'auto_model/urm'):
        decision = {'verdict': 'stop', 'reason': '实际模型不符', 'detail': '模型与起跑预期不符'}
    if runtime.auth_error(segment, tail):
        decision = {'verdict': 'stop', 'reason': '网关鉴权失败', 'detail': '鉴权失败，停止开新轮'}
        if cfg.get('_managed'):
            runtime.Gateway(cfg).event('auth')
    if current.get('segment', 0):
        for source, target in ((stdout, task_dir / 'run/stream.jsonl'), (stderr, task_dir / 'run/stderr.log')):
            content = source.read_bytes()
            if not target.read_bytes().endswith(content):
                with target.open('ab') as handle:
                    handle.write(b'\n' + content)
    continuations = current.get('continuations', [])
    attempts_path = task_dir / 'run/attempts.json'
    attempts = lib.load_json(attempts_path, default=[])
    if decision['reason'] == '无故中断' and segment.get('session_id') and not segment['problems']:
        if len(continuations) + sum(len(a.get('continuations', [])) for a in attempts) >= cfg['run']['max_continuations']:
            lib.set_stage(task_dir, 'run', 'failed', reason='模型中断，已达“继续”次数上限')
            return {**row, 'state': 'blocked', 'detail': '继续次数用尽，保留现场，不重跑刷结果'}
        uuid.UUID(segment['session_id'])
        continuations.append({'n': len(continuations) + 1, 'prompt': '继续', 'reason': '无故中断',
                              'session_id': segment['session_id'], 'at': lib.now_iso()})
        current.update(state='continue_ready', continuations=continuations, session_id=segment['session_id'])
        lib.save_json(current_path, current)
        lib.save_json(task_dir / 'run/continuations.json', continuations)
        return {**row, 'state': 'continue_ready', 'detail': '中断已核验，resume 将在原会话发送继续'}
    if decision['verdict'] == 'accept':
        lib.run(['docker', 'stop', task_dir.name], timeout=60)
        if not code_changes(task_dir, record):
            decision = {'verdict': 'retry', 'reason': '零代码改动', 'detail': '最终树没有代码改动'}
    if not any(a['n'] == current['attempt'] for a in attempts):
        attempts.append({'n': current['attempt'], 'started_at': current['started_at'], 'ended_at': lib.now_iso(),
                         'rc': done['rc'], 'timed_out': current.get('timed_out', False) or done.get('timed_out', False),
                         'termination_reason': current.get('reason', 'worker_guard' if done.get('timed_out') else ''),
                         'continuations': continuations, 'session_id': segment['session_id'], **decision})
        lib.save_json(attempts_path, attempts)
    if decision['verdict'] == 'accept':
        lib.set_stage(task_dir, 'run', 'ok', attempts=current['attempt'], started_at=current['started_at'],
                      ended_at=lib.now_iso(), session_id=segment['session_id'], continuations=continuations)
        current.update(state='finished')
        lib.save_json(current_path, current)
        if cfg.get('_managed'):
            runtime.Gateway(cfg).event('success')
        return {**row, 'state': 'ok', 'detail': '匹配退出回执，运行已恢复收口'}
    if decision['verdict'] == 'retry':
        archive_attempt(task_dir, record, current['attempt'], secret)
        lib.set_stage(task_dir, 'run', 'deferred', reason=decision['reason'])
        return {**row, 'state': 'deferred', 'detail': '失败运行已归档，可在剩余预算和次数内 resume'}
    lib.set_stage(task_dir, 'run', 'failed', reason=decision['reason'])
    return {**row, 'state': 'blocked', 'detail': decision['detail']}


@lib.task_locked
def run_one(task_dir: Path, cfg: dict) -> dict:
    record = lib.load_task(task_dir)
    name = record["task_id"]
    if key_hold(task_dir):
        return {"task_id": name, "state": "blocked", "detail": "已检测到网关 key 泄露，等待人工处理"}
    if lib.stage_ok(task_dir, "run"):
        return {"task_id": name, "state": "skip", "detail": "已经跑过"}
    current = lib.load_json(task_dir / 'run/current.json', default={})
    recovering = current.get('request_id') and current.get('state') in ('running', 'exited')
    legacy_recovery = lib.load_json(task_dir / 'run/legacy-progress-recovery.json', default={})
    legacy_grant = (legacy_recovery.get('state') == 'ready' and not legacy_recovery.get('used_at')
                    and legacy_recovery.get('attempt') == current.get('attempt'))
    if not recovering and not legacy_grant and (time.time() >= cfg.get('_deadline', float('inf'))
            or (cfg.get('_drain') and Path(cfg['_drain']).exists())):
        return {'task_id': name, 'state': 'deferred', 'detail': '批次已排空或预算到顶，不起新轮'}
    try:
        secret = bound_key(task_dir, cfg, start=True)
    except lib.FbrError as exc:
        return {"task_id": name, "state": "blocked", "detail": str(exc)}
    current = lib.load_json(task_dir / 'run/current.json', default={})
    resume = None
    if current.get('state') in ('running', 'exited', 'starting', 'continue_ready'):
        recovered = recover_one(task_dir, cfg)
        if recovered['state'] not in ('deferred', 'continue_ready', 'idle', 'prepared'):
            return recovered
        if recovered['state'] in ('continue_ready', 'prepared'):
            resume = lib.load_json(task_dir / 'run/current.json')
    problems = [] if resume else ready_problems(task_dir, record, cfg)
    if problems:
        return {"task_id": name, "state": "blocked", "detail": "；".join(problems)}
    run_dir = task_dir / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "prompt.txt").write_text(record["prompt"], encoding="utf-8")
    if not resume:
        lib.save_git_config(task_dir)
    attempts = lib.load_json(run_dir / "attempts.json", default=[])
    budget_path = run_dir / "budget.json"
    budget = lib.load_json(budget_path, default={"active_seconds": 0, "waiting_seconds": 0})
    import fbr_retry
    while True:
        admission = fbr_retry.admission(attempts, cfg)
        lib.save_json(run_dir / 'retry-ledger.json', admission)
        if not admission['allowed']:
            lib.set_stage(task_dir, 'run', 'deferred', reason=admission['reason'])
            return {'task_id': name, 'state': 'deferred', 'detail': admission['reason']}
        if not legacy_grant and (time.time() >= cfg.get('_deadline', float('inf'))
                or (cfg.get('_drain') and Path(cfg['_drain']).exists())):
            return {'task_id': name, 'state': 'deferred', 'detail': '批次已排空或预算到顶，不起新轮'}
        if budget["active_seconds"] >= cfg["run"]["task_budget"] and not legacy_grant:
            lib.set_stage(task_dir, "run", "deferred", reason="单题累计运行预算耗尽，恢复不会清零预算")
            return {"task_id": name, "state": "deferred", "detail": "单题累计运行预算耗尽"}
        if cfg['run'].get('gateway_wait_task_total') is not None and budget.get('waiting_seconds', 0) >= cfg['run']['gateway_wait_task_total']:
            lib.set_stage(task_dir, 'run', 'deferred', reason='单题累计网关等待预算耗尽，未派发新的尝试')
            return {'task_id': name, 'state': 'deferred', 'detail': '单题累计网关等待预算耗尽，保留次数与容器，不发送题面'}
        number = (resume or {}).get('attempt') or max([a.get('n', 0) for a in attempts] or [0]) + 1
        if legacy_grant and not use_legacy_progress_recovery(task_dir, number):
            return {"task_id": name, "state": "deferred", "detail": "进度恢复令牌已被消费"}
        lib.open_permissions(task_dir / "workspace")
        started = lib.now_iso()
        lib.save_json(run_dir / "current.json", {**(resume or {}), "state": "starting", "attempt": number,
            "started_at": started, "owner_pid": os.getpid(), "owner_identity": runtime.process_identity(),
            "attempt_waiting_base": (resume or {}).get("attempt_waiting_base", budget.get("waiting_seconds", 0)),
            "generation": lib.content_hash([record['prompt'], record['initial_commit'], record['task_repo_full']]),
            "script_sha": lib.content_hash({p.name: runtime.hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in Path(__file__).parent.glob('*.py')}),
            "image": record["image"], "image_id": cfg["image_id"], "continuations": (resume or {}).get('continuations', [])})
        if not resume:
            import fbr_dependencies
            if cfg.get('dependencies', {}).get('enabled'):
                fbr_dependencies.seed(task_dir/'workspace', cfg, task=task_dir)
            created = lib.run(run_command(task_dir, record, cfg), timeout=120)
            if created.returncode != 0:
                raise lib.FbrError("跑题容器启动失败，保留容器与证据供排查")
            current = lib.load_json(run_dir / 'current.json')
            current['container_id'] = created.stdout.strip()
            lib.save_json(run_dir / 'current.json', current)
        continuations = (resume or {}).get('continuations', [])
        used = sum(len(a.get("continuations", [])) for a in attempts)
        session_id = (resume or {}).get('session_id', '')
        resume = None
        while True:
            index = len(continuations)
            stdout = run_dir / (f"continue-{number}-{index}.jsonl" if index else "stream.jsonl")
            stderr = run_dir / (f"continue-{number}-{index}.stderr.log" if index else "stderr.log")
            gateway = runtime.Gateway(cfg) if cfg.get('_managed') else None
            wall_started = time.time()
            context = contextlib.nullcontext()
            if gateway:
                gateway.health_probe()
                if not gateway.acquire(time.time() + max(1, cfg["run"]["task_budget"] - budget["active_seconds"])):
                    return {"task_id": name, "state": "deferred", "detail": "等待共享额度超时，保留现场"}
                context = runtime.stream_context(task_dir, cfg, number, index, gateway)
            wall_started = time.time()
            try:
                with context:
                    rc, timed_out = lib.run_stream(
                        execution_command(name, session_id), stdin_text="继续" if index else record["prompt"],
                        stdout_path=stdout, stderr_path=stderr,
                        timeout=int(min(cfg["run"]["timeout"], max(1, cfg["run"]["task_budget"] - budget["active_seconds"]))))
            finally:
                if gateway:
                    gateway.release()
            heartbeat = lib.load_json(run_dir / "heartbeat.json", default={}) if gateway else {}
            waiting = heartbeat.get('waiting_seconds', 0)
            # 持久 monitor 已逐次记账；自测替身/旧执行器则在这里补记一次。
            updated = lib.load_json(budget_path, default={})
            if updated.get('active_seconds', 0) > budget['active_seconds'] or updated.get('waiting_seconds', 0) > budget['waiting_seconds']:
                budget = updated
            else:
                budget['active_seconds'] += max(0, time.time() - wall_started - waiting)
                budget['waiting_seconds'] += waiting
            lib.save_json(budget_path, budget)
            if timed_out:
                lib.run(["docker", "kill", name], timeout=60)
            if evidence_has_key((stdout, stderr), secret):
                attempts.append({"n": number, "started_at": started, "ended_at": lib.now_iso(),
                                 "rc": rc, "timed_out": timed_out, "continuations": continuations,
                                 "verdict": "stop", "reason": "检测到网关 key 泄露"})
                lib.save_json(run_dir / "attempts.json", attempts)
                return halt_for_key(task_dir, "run")
            segment = fbr_trace.read_stream(stdout)
            tail = "\n".join(stderr.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
            import fbr_completion
            completion = lib.load_json(run_dir / 'current.json', default={})
            completed_exit = lib.load_json(Path(completion.get('result', '/nonexistent')), default={})
            decision = fbr_trace.classify_attempt(rc, timed_out, segment, tail,
                                                  completed_cleanup=fbr_completion.valid(task_dir, completion, completed_exit),
                                                  termination_reason=completion.get('reason', 'worker_guard' if completed_exit.get('timed_out') else ''))
            if runtime.auth_error(segment, tail):
                decision = {"verdict": "stop", "reason": "网关鉴权失败", "detail": "鉴权失败，停止开新轮，检查凭据"}
            if segment.get('model') and segment['model'] != cfg.get('model', 'auto_model/urm'):
                decision = {"verdict": "stop", "reason": "实际模型不符", "detail": "流式模型与预期配置不一致"}
            if index:
                with (run_dir / "stream.jsonl").open("ab") as out:
                    out.write(b"\n" + stdout.read_bytes())
                with (run_dir / "stderr.log").open("ab") as out:
                    out.write(b"\n" + stderr.read_bytes())
                if segment["session_id"] and segment["session_id"] != session_id:
                    lib.set_stage(task_dir, "run", "failed", problems=["续写 SessionID 与首轮不一致"])
                    return {"task_id": name, "state": "failed", "detail": "续写 SessionID 与首轮不一致（容器保留）"}
            if (decision["reason"] != "无故中断" or not segment["session_id"]
                    or segment["problems"] or timed_out):
                break
            if used + len(continuations) >= cfg["run"]["max_continuations"]:
                lib.save_json(run_dir / "continuations.json", continuations)
                lib.set_stage(task_dir, "run", "failed", problems=["模型中断，已达“继续”次数上限"],
                              continuations=continuations)
                return {"task_id": name, "state": "failed", "detail": "模型中断，已达“继续”次数上限；保留容器与全部内容，不重跑刷结果"}
            # 续写前先保存、扫描原始轨迹；不可带着已泄露的 key 继续。
            exported = run_dir / f"interruption-{number}-{index}"
            exported.mkdir(exist_ok=True)
            copied = lib.run(["docker", "cp", f"{name}:{lib.TRACE_SRC}", str(exported / "projects")], timeout=300)
            if copied.returncode != 0:
                raise lib.FbrError("中断轨迹导出失败，容器先留着，不发送继续")
            if evidence_has_key(exported.rglob("*"), secret):
                return halt_for_key(task_dir, "run")
            session_id = segment["session_id"]
            try:
                uuid.UUID(session_id)
            except ValueError:
                raise lib.FbrError("中断会话的 SessionID 无效，保留容器，不发送继续") from None
            continuations.append({"n": len(continuations) + 1, "prompt": "继续", "reason": "无故中断",
                                  "session_id": session_id, "at": lib.now_iso()})
            lib.save_json(run_dir / "continuations.json", continuations)
            current = lib.load_json(run_dir / "current.json")
            current['continuations'] = continuations
            lib.save_json(run_dir / "current.json", current)
        if evidence_has_key((run_dir / item for item in ("stream.jsonl", "stderr.log")), secret):
            attempts.append({"n": number, "started_at": started, "ended_at": lib.now_iso(), "rc": rc,
                             "timed_out": timed_out, "verdict": "stop", "reason": "检测到网关 key 泄露"})
            lib.save_json(run_dir / "attempts.json", attempts)
            return halt_for_key(task_dir, "run")
        stream = fbr_trace.read_stream(run_dir / "stream.jsonl")
        verdict = decision  # 以最后一次执行判定，合并日志保留首轮和全部续写
        if verdict['verdict'] == 'accept':
            lib.run(['docker', 'stop', name], timeout=60)
            changed = code_changes(task_dir, record)
            if not changed:
                verdict = {"verdict": "retry", "reason": "零代码改动", "detail": "最终产物没有代码改动，归档后新容器重跑"}
        terminal = lib.load_json(run_dir / 'current.json', default={})
        if timed_out and terminal.get('reason') not in ('batch_budget', 'worker_guard'):
            # Gateway errors can reach disk only after process termination; retain both decisions.
            time.sleep(cfg['run'].get('reclassify_settle_seconds', 8))
            stream = fbr_trace.read_stream(run_dir / 'stream.jsonl')
            with (run_dir / 'stream.jsonl').open('rb') as late_stream:
                late_stream.seek(terminal.get('stream_bytes_before_stop', (run_dir / 'stream.jsonl').stat().st_size))
                late_tail = late_stream.read().decode('utf-8', 'replace')
            late_signals = []
            for line in late_tail.splitlines():
                try:
                    signal = runtime.gateway_signal(json.loads(line))
                except (ValueError, TypeError):
                    continue
                if signal:
                    late_signals.append(signal)
            if late_signals and terminal.get('reason') in ('', 'stalled', 'timeout') and not ((stream.get('result') or {}).get('is_error') is False):
                verdict = {**verdict, 'failure_category': 'throttle' if any(x in ('limit', 'soft') for x in late_signals) else 'infra',
                           'reclassified': {'from': verdict['reason'], 'source': 'late_stream_api_error'},
                           'detail': verdict['detail'] + '；停止后重读原始流发现网关错误'}
        terminal_api = stream.get("result") if (stream.get("result") or {}).get("is_error") else None
        attempts.append({"n": number, "started_at": started, "ended_at": lib.now_iso(), "rc": rc,
                         "api_error": terminal_api, "termination_reason": terminal.get('reason', 'worker_guard' if completed_exit.get('timed_out') else ''),
                         "timed_out": timed_out, "events": stream["events"], "session_id": stream["session_id"],
                         "continuations": continuations, **verdict, "detail": lib.redact(verdict["detail"], [secret])})
        lib.save_json(run_dir / "attempts.json", attempts)
        current = lib.load_json(run_dir / 'current.json', default={})
        current.update(state='finished', session_id=stream['session_id'], model=stream.get('model', ''),
                       continuations=continuations, verdict=verdict['verdict'])
        lib.save_json(run_dir / 'current.json', current)
        if verdict["verdict"] == "accept":
            if gateway:
                gateway.event('success')
            lib.set_stage(task_dir, "run", "ok", attempts=number, started_at=started,
                          ended_at=attempts[-1]["ended_at"], session_id=stream["session_id"],
                          continuations=continuations)
            return {"task_id": name, "state": "ok", "detail": f"第 {number} 次运行算数"}
        if verdict["verdict"] == "stop":
            if gateway and verdict['reason'] == '网关鉴权失败':
                gateway.event('auth')
            lib.set_stage(task_dir, "run", "failed", reason=verdict["reason"], problems=[verdict["detail"]])
            return {"task_id": name, "state": "failed", "detail": verdict["detail"] + "（容器没删）"}
        try:
            archive_attempt(task_dir, record, number, secret)
        except lib.KeyLeakError:
            return halt_for_key(task_dir, "run")
        assessment = fbr_retry.classify_attempt(attempts[-1])
        if gateway and assessment['category'] in ('throttle', 'infra'):
            gateway.event('limit' if runtime.LIMIT.search(tail) else 'soft' if runtime.SOFT.search(tail) else 'network')
        delay = min(cfg['gateway']['retry_max'], cfg['gateway']['retry_base'] * 2 ** min(number - 1, 8)) if assessment['category'] in ('throttle', 'infra') else 0
        retry_after = time.time() + delay
        lib.save_json(run_dir / 'retry-ledger.json', fbr_retry.admission(attempts, cfg))
        lib.set_stage(task_dir, 'run', 'deferred', reason=verdict['reason'], retry_after=retry_after)
        if cfg.get('_single_attempt'):
            return {'task_id': name, 'state': 'retry', 'retry_after': retry_after,
                    'detail': verdict['reason'], 'failure_category': assessment['category']}
        while time.time() < retry_after:
            time.sleep(min(5, retry_after - time.time()))



# ---------- R2 ----------

@lib.task_locked
def collect_one(task_dir: Path, cfg: dict, keep_container: bool = False) -> dict:
    record = lib.load_task(task_dir)
    name = record["task_id"]
    if key_hold(task_dir):
        return {"task_id": name, "state": "blocked", "detail": "已检测到网关 key 泄露，等待人工处理"}
    if lib.stage_ok(task_dir, "collect"):
        return {"task_id": name, "state": "skip", "detail": "已经收集过"}
    run_info = lib.require_stage(task_dir, "run")
    try:
        secret = bound_key(task_dir, cfg)
    except lib.FbrError as exc:
        lib.set_stage(task_dir, "collect", "failed", problems=[str(exc)], key_check_blocked=True)
        return {"task_id": name, "state": "blocked", "detail": str(exc)}
    trace_dir = task_dir / "trace"
    target = trace_dir / "projects"
    trace_dir.mkdir(exist_ok=True)
    existing = list(target.rglob("*")) if target.is_dir() else []
    existing.extend(task_dir / "run" / item for item in ("stream.jsonl", "stderr.log"))
    if evidence_has_key(existing, secret):
        return halt_for_key(task_dir, "collect")
    if container_exists(name):
        if target.exists():
            shutil.rmtree(target)   # 上次导出的半成品；原件还在容器里
        copied = lib.run(["docker", "cp", f"{name}:{lib.TRACE_SRC}", str(target)], timeout=600)
        if copied.returncode != 0:
            lib.set_stage(task_dir, "collect", "failed", problems=["轨迹导出失败"])
            return {"task_id": name, "state": "failed",
                    "detail": f"docker cp 失败，容器先留着：{copied.stderr.strip()[:200]}"}
    elif not target.is_dir():
        lib.set_stage(task_dir, "collect", "failed", problems=["容器不在了，本地也没有导出的轨迹"])
        return {"task_id": name, "state": "failed", "detail": "容器不在了，本地也没有导出的轨迹"}
    if evidence_has_key(target.rglob("*"), secret):
        return halt_for_key(task_dir, "collect")
    ids = fbr_trace.read_ids(target, record["prompt"], run_info.get("continuations", []))
    problems = list(ids["problems"])
    if run_info.get("session_id") and ids["session_id"] and run_info["session_id"] != ids["session_id"]:
        problems.append("轨迹的 sessionId 和运行输出里的不一致")
    if ids["cli_version"] and ids["cli_version"] != cfg["harness_version"]:
        problems.append(f"轨迹里的版本 {ids['cli_version']} 不是 {cfg['harness_version']}")
    if problems:
        lib.set_stage(task_dir, "collect", "failed", problems=problems)
        return {"task_id": name, "state": "failed", "detail": "；".join(problems) + "（容器没删）"}
    lib.save_json(trace_dir / "ids.json", {
        "session_id": ids["session_id"], "prompt_id": ids["prompt_id"], "session_file": ids["session_file"],
        "user_turns": ids["user_turns"], "rows": ids["rows"], "cli_version": ids["cli_version"],
        "continuations": ids["continuations"], "key_detected": False, "exported_at": lib.now_iso()})
    removed = False
    if not keep_container and container_exists(name):
        lib.run(["docker", "stop", name], timeout=60)
        result = lib.run(["docker", "rm", name], timeout=60)
        removed = result.returncode == 0
    lib.set_stage(task_dir, "collect", "ok", session_id=ids["session_id"], prompt_id=ids["prompt_id"],
                  container_removed=removed)
    return {"task_id": name, "state": "ok", "detail": f"SessionID {ids['session_id']}"}


# ---------- 命令行 ----------

def fan_out(items: list[Path], worker, parallel: int) -> list[dict]:
    def safe(task_dir: Path) -> dict:
        try:
            return worker(task_dir)
        except lib.FbrError as exc:
            return {"task_id": task_dir.name, "state": "failed", "detail": str(exc)}

    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(items)))) as pool:
        for result in as_completed([pool.submit(safe, item) for item in items]):
            row = result.result()
            rows.append(row)
            print(f"{row['task_id']}  {row['state']}  {row['detail']}", flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 单轮运行与轨迹收集")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    for name in ("run", "resume", "collect", "recover", "legacy-recover", "status", "drain"):
        item = sub.add_parser(name)
        item.add_argument("--batch", required=True)
        item.add_argument("--tasks")
        item.add_argument("--limit", type=int)
    sub.choices["run"].add_argument("--execute", action="store_true")
    sub.choices['run'].add_argument('--detach', action='store_true')
    sub.choices['resume'].add_argument('--execute', action='store_true')
    sub.choices['resume'].add_argument('--detach', action='store_true')
    sub.choices['recover'].add_argument('--execute', action='store_true')
    sub.choices['legacy-recover'].add_argument('--execute', action='store_true')
    sub.choices['drain'].add_argument('--execute', action='store_true')
    sub.choices["collect"].add_argument("--keep-container", action="store_true")
    args = parser.parse_args()
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit 必须是正整数")
    try:
        root = lib.find_root(args.root)
        cfg = lib.settings(root)
        cfg['_managed'] = True
        if args.command == "preflight":
            return 0 if print_checks(preflight(cfg)) else 1
        items = lib.task_dirs(root, args.batch, args.tasks)
        directory = lib.batch_dir(root, args.batch) / 'runner'
        if args.command == 'status':
            print(json.dumps([{'task_id': p.name, 'current': lib.load_json(p / 'run/current.json', default={}),
                              'heartbeat': lib.load_json(p / 'run/heartbeat.json', default={})} for p in items], ensure_ascii=False))
            return 0
        if args.command == 'recover':
            if not args.execute:
                print('演练：recover --execute 才接管并收口，不投新题')
                return 0
            print(json.dumps([recover_one(p, cfg) for p in items], ensure_ascii=False))
            return 0
        if args.command == 'legacy-recover':
            rows = [prepare_legacy_progress_recovery(p, cfg, execute=args.execute) for p in items]
            print(json.dumps(rows, ensure_ascii=False))
            return 0 if all(row['state'] in ('ready', 'dry') for row in rows) else 1
        if args.command == 'drain':
            if args.execute:
                directory.mkdir(parents=True, exist_ok=True)
                (directory / 'drain').touch()
            print('排空：在跑的继续，停止起新轮' if args.execute else '演练：加 --execute 排空')
            return 0
        if args.command == "collect":
            todo = [p for p in items if lib.stage_ok(p, "run")][: args.limit or None]
            rows = fan_out(todo, lambda p: collect_one(p, cfg, args.keep_container), cfg["concurrency"])
            return 0 if all(r["state"] in ("ok", "skip") for r in rows) else 1
        if getattr(args, 'detach', False) and args.execute:
            argv = [sys.executable, str(Path(__file__).resolve()), *[v for v in sys.argv[1:] if v != '--detach']]
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / 'runner.log').open('ab') as log:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            print(json.dumps({'pid': proc.pid, 'log': str(directory / 'runner.log')}))
            return 0
        todo = [p for p in items if not lib.stage_ok(p, "run")][: args.limit or None]
        if not todo:
            print("没有待运行的任务")
            return 0
        if not args.execute:
            blocked = 0
            for task_dir in todo:
                record = lib.load_task(task_dir)
                problems = ready_problems(task_dir, record, cfg)
                blocked += bool(problems)
                print(f"{record['task_id']}  " + ("不能跑：" + "；".join(problems) if problems else "可以跑"))
            print("命令形如：" + " ".join(run_command(todo[0], lib.load_task(todo[0]), cfg)) + "；随后 docker exec -i <任务编号> /usr/local/bin/entrypoint.sh print < run/prompt.txt")
            print(f"演练结束：待运行 {len(todo)} 个，其中 {blocked} 个没准备好。真跑加 --execute")
            return 1 if blocked else 0
        print("预检：")
        if not print_checks(preflight(cfg)):
            raise lib.FbrError("预检没过，不起跑")
        with lib.file_lock(directory / 'runner.lock'):
            if args.command == 'resume':
                (directory / 'drain').unlink(missing_ok=True)
            budget_path = directory / 'budget.json'
            batch_budget = {'deadline': runtime.batch_deadline(root, args.batch, cfg)}
            lib.save_json(budget_path, batch_budget)
            cfg.update(_deadline=batch_budget['deadline'], _drain=str(directory / 'drain'))
            import fbr_schedule
            cfg['_single_attempt'] = True
            rows = fbr_schedule.rolling(todo, lambda p: run_one(p, cfg), parallel=cfg['concurrency'],
                deadline=cfg['_deadline'], stopped=lambda: Path(cfg['_drain']).exists(),
                on_result=lambda task, row: print(f"{task.name}  {row['state']}  {row.get('detail', '')}", flush=True))
        return 0 if all(r["state"] in ("ok", "skip") for r in rows) else 1
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
