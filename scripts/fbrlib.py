#!/usr/bin/env python3
"""s11-feedback-run 的公共库：工作目录定位、配置、任务目录与阶段状态、外部命令、脱敏。

所有碰 docker / gh 的地方都走这里的 run / run_stream，自测时替换这两个函数即可，不会真的起容器。
git 只操作本地仓库，直接调用。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
import contextlib
import fcntl
import functools
import inspect
import threading
from pathlib import Path


class FbrError(Exception):
    """可预期的失败：信息直接给人看。"""


class KeyLeakError(FbrError):
    """原始证据含网关 key，必须留存并停止，不能按普通运行异常重跑。"""


TASK_ID_RE = re.compile(r"^(?:s11-(\d{6})|[A-Za-z0-9_.-]+-(?:0[1-9]|[1-9][0-9])-(?:development|bugfix|optimization|refactor|migration))$")
BATCH_ID_RE = re.compile(r"^b(\d{4})-(\d{2})$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STAGES = ["run", "collect", "commit", "record", "facts", "feedback"]
MARK = "@@FBR"
TRACE_SRC = "/home/node/.claude/projects"
FORBIDDEN_ACTIVE_GITHUB_ACCOUNTS = {"fengyin-solo"}

DEFAULTS = {
    "image": "adminfather/benzhi-claude-code2:20261110",
    "harness": "Claude Code",
    "harness_version": "2.1.319",
    "os": "MacOS/Linux",
    "concurrency": 5,
    "review_parallel": 5, "facts_parallel": 2,
    "publish_after_review": False,
    "dependencies": {"enabled": True, "paths": [], "smoke_cmd": ""},
    "record_parallel": 2,
    "key_file": "~/.codex/solo11/gateway.env",
    "run": {
        "timeout": 3600,
        "grace_seconds": 900,
        "task_budget": 4500,
        "batch_budget": 25200,
        "stall_seconds": 480,
        "gateway_grace": 900,
        "gateway_grace_max": 3600,
        "gateway_wait_limit": 1800,
        "gateway_wait_total": 2700,
        "poll_seconds": 5,
        "max_attempts": 3,          # 模型责任尝试上限；物理编号独立递增
        "max_infra_attempts": 3, "max_throttle_attempts": 8,
        "max_physical_attempts": None, # 已授权批次可额外限制物理派发总数
        "gateway_wait_task_total": None, # 显式批次授权的跨尝试等待上限
        "reclassify_settle_seconds": 8,
        "max_continuations": 2,     # 同题累计最多两次，只发送“继续”
        "context_limit": 1000000,   # 镜像里固化的 max context token，不符不起跑
    },
    "gateway": {"machine_parallel": 5, "soft_floor": 3, "downgrade_dedup": 120,
                "stable_seconds": 300, "probe_interval": 300, "probe_successes": 3,
                "spike_interval": 1200, "spike_watch": 300, "spike_backoff": 2400,
                "retry_base": 30, "retry_max": 300},
    "record": {
        "max_seconds": 90, "max_attempts": 3, "setup_timeout": 900, "step_timeout": 60,
        "width": 1280, "height": 720,
    },
    "facts": {"setup_cmd": "make setup", "test_cmd": "make test", "setup_timeout": 900, "test_timeout": 600},
    "feedback_gate": {
        "min_chars": 120, "max_chars": 700,
        "dup_block": 12, "dup_block_han": 8,      # 跨记录公共块：≥12 字且其中汉字 ≥8
        "head_len": 6, "head_ratio": 0.2,         # 句首前 6 字雷同占比
        "five_ratio": 0.4,                        # 一批里 5 分占比超过就提示重看
        "min_counterexamples": 2,                 # 5 分至少要有这么多个实跑过的反例
    },
}


# ---------- 工作目录与文件 ----------

def find_root(explicit: str | None = None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    elif os.environ.get("SOLO11_ROOT"):
        candidates.append(Path(os.environ["SOLO11_ROOT"]).expanduser())
    else:
        here = Path.cwd().resolve()
        candidates.extend([here, *here.parents])
    for item in candidates:
        if (item / "_config" / "settings.json").is_file():
            return item.resolve()
    raise FbrError("找不到工作目录：用 --root 指定，或设置 SOLO11_ROOT（目录下要有 _config/settings.json）")


def containing_root(path: Path) -> Path:
    """Locate a repository/temporary clone's enclosing S11 root without changing cwd."""
    here = Path(path).resolve()
    for item in (here, *here.parents):
        if (item/'_config/settings.json').is_file(): return item
    raise FbrError('仓库或缓存克隆不在 S11 工作目录内')


def load_json(path: Path, default=None):
    path = Path(path)
    if not path.is_file():
        if default is not None:
            return default
        raise FbrError(f"缺少文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FbrError(f"{path} 不是合法 JSON：{exc}") from None


def save_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """容错读取：坏行跳过（轨迹被截断时最后一行可能不完整）。"""
    rows = []
    path = Path(path)
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def settings(root: Path) -> dict:
    cfg = _merge(DEFAULTS, load_json(root / "_config" / "settings.json"))
    cfg.pop("github_owner", None)
    for key in ("image", "image_id", "harness", "harness_version"):
        if not cfg.get(key):
            raise FbrError(f"_config/settings.json 缺少 {key}")
    cfg["review_parallel"] = min(5, max(1, int(cfg["review_parallel"])))
    count = cfg["run"]["max_continuations"]
    if type(count) is not int or not 0 <= count <= 2:
        raise FbrError("run.max_continuations 必须是 0 到 2 的整数")
    for section, keys in (("run", ("timeout", "task_budget", "batch_budget", "max_attempts", "poll_seconds")),
                          ("gateway", ("machine_parallel", "probe_interval", "probe_successes"))):
        for name in keys:
            if type(cfg[section][name]) not in (int, float) or cfg[section][name] <= 0:
                raise FbrError(f"{section}.{name} 必须为正数")
    if type(cfg["run"]["max_attempts"]) is not int or not 1 <= cfg["run"]["max_attempts"] <= 3:
        raise FbrError("run.max_attempts 必须为 1 到 3")
    return cfg


def active_github_account() -> str:
    """Return the login behind the currently effective gh token."""
    result = run(["gh", "api", "user", "--jq", ".login"], timeout=60)
    account = result.stdout.strip()
    if result.returncode != 0 or not account:
        detail = (result.stderr or result.stdout or "gh api user returned no login").strip()
        raise FbrError(f"无法确认本机当前活跃 GitHub 账号：{detail[:300]}")
    if account.lower() in FORBIDDEN_ACTIVE_GITHUB_ACCOUNTS:
        raise FbrError(
            f"当前活跃 GitHub 账号为 {account}，禁止开始任务；"
            "请立即停止并切换到允许的账号后再重试"
        )
    return account


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------- 批次、任务目录、阶段状态 ----------

def batch_dir(root: Path, batch_id: str) -> Path:
    if not BATCH_ID_RE.match(batch_id or ""):
        raise FbrError(f"批次编号不合法：{batch_id}")
    path = root / "batches" / batch_id
    if not path.is_dir():
        raise FbrError(f"批次不存在：{path}")
    return path


def task_dirs(root: Path, batch_id: str, wanted: str | None = None) -> list[Path]:
    names = {item.strip() for item in (wanted or "").split(",") if item.strip()}
    found = [p for p in sorted(batch_dir(root, batch_id).iterdir())
             if TASK_ID_RE.fullmatch(p.name) and (p / "task.json").is_file()]
    missing = names - {p.name for p in found}
    if missing:
        raise FbrError(f"批次里没有这些任务：{'、'.join(sorted(missing))}")
    return [p for p in found if not names or p.name in names]


def load_task(task_dir: Path) -> dict:
    record = load_json(Path(task_dir) / "task.json")
    for key in ("task_id", "prompt", "initial_commit", "image", "requirements"):
        if not record.get(key):
            raise FbrError(f"{task_dir.name}/task.json 缺少 {key}")
    if record["task_id"] != Path(task_dir).name:
        raise FbrError(f"task.json 的编号 {record['task_id']} 和目录名 {Path(task_dir).name} 不一致")
    validate_handoff(Path(task_dir), record)
    return record


def content_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def handoff_payload(
    record: dict,
    recording: dict,
    *,
    include_domain_contract: bool = True,
) -> dict:
    keys = ("task_id", "repo_id", "baseline_id", "initial_commit", "prompt", "task_type", "difficulty",
            "domain", "subdirection", "language", "frameworks", "repro_level", "requirements",
            "image", "image_id", "harness", "harness_version", "os", "task_repo", "task_repo_full")
    if record.get("producer") == "s11-gsb-prompt-create":
        keys += ("producer", "execution_mode", "branch", "origin", "workspace")
        if include_domain_contract:
            keys += ("domain_contract", "domain_gate_version")
    return {"task": {key: record.get(key) for key in keys}, "recording": recording}


def validate_handoff(task_dir: Path, record: dict) -> None:
    """新批次核对冻结的出题输入；旧批次继续使用原来的逐阶段检查。"""
    version = record.get("handoff_version", 1)
    manifest = load_json(task_dir.parent / "batch-manifest.json", default={})
    if (manifest.get("schema_version") == 2 or any(key in record for key in ("baseline", "baseline_id", "handoff_hash"))) and version != 2:
        raise FbrError(f"{task_dir.name}：新版批次不能降级或删除交接版本")
    if version == 1:
        return
    if version != 2:
        raise FbrError(f"{task_dir.name}：不支持交接版本 {version}")
    if record.get("producer") == "s11-gsb-prompt-create" or manifest.get("producer") == "s11-gsb-prompt-create":
        validate_single_layout(task_dir, record, manifest)
        if record.get("domain_gate_version") is not None:
            contract = record.get("domain_contract")
            if record.get("domain_gate_version") != 1 or not isinstance(contract, dict):
                raise FbrError(f"{task_dir.name}：领域题位契约缺失或版本不支持")
            for key in ("domain", "subdirection", "topic_id", "main_task", "business_result", "conclusion"):
                if not str(contract.get(key) or "").strip():
                    raise FbrError(f"{task_dir.name}：领域题位契约缺少 {key}")
            for key in ("source_evidence", "domain_objects", "domain_results"):
                if not isinstance(contract.get(key), list) or not contract[key]:
                    raise FbrError(f"{task_dir.name}：领域题位契约缺少 {key}")
            if contract.get("conclusion") != "pass":
                raise FbrError(f"{task_dir.name}：领域题位契约未通过")
            if contract.get("domain") != record.get("domain") or contract.get("subdirection") != record.get("subdirection"):
                raise FbrError(f"{task_dir.name}：领域题位契约与冻结领域字段不一致")
    baseline = record.get("baseline")
    if not isinstance(baseline, dict) or content_hash(baseline) != record.get("baseline_id"):
        raise FbrError(f"{task_dir.name}：基线摘要不一致")
    for key in ("repo_id", "domain", "subdirection", "language", "frameworks", "repro_level",
                "image", "image_id", "harness", "harness_version", "os"):
        if baseline.get(key) != record.get(key):
            raise FbrError(f"{task_dir.name}：{key} 与冻结基线不一致")
    if baseline.get("commit") != record["initial_commit"] or record.get("base_commit") != record["initial_commit"]:
        raise FbrError(f"{task_dir.name}：初始提交与冻结基线不一致")
    recording = load_json(task_dir / "recording-plan.json")
    handoff_hashes = {
        content_hash(handoff_payload(record, recording)),
    }
    if record.get("domain_contract") is None and record.get("domain_gate_version") is None:
        # The domain contract was added after some producer tasks were already frozen.
        # Accept their original digest only when no contract fields existed at all.
        handoff_hashes.add(content_hash(handoff_payload(
            record,
            recording,
            include_domain_contract=False,
        )))
    if record.get("handoff_hash") not in handoff_hashes:
        raise FbrError(f"{task_dir.name}：题面、需求点、录屏方案或任务基础信息发生变化，请重新交接")
    prompt_file = task_dir / "prompt.txt"
    if not prompt_file.is_file() or prompt_file.read_text(encoding="utf-8") != record["prompt"]:
        raise FbrError(f"{task_dir.name}：prompt.txt 与 task.json 题面不一致")
    if manifest.get("schema_version") != 2:
        raise FbrError(f"{task_dir.name}：缺少第 2 版批次交接清单")
    rows = manifest.get("tasks", [])
    matches = [item for item in rows if item.get("task_id") == record["task_id"]]
    if len(matches) != 1 or len({item.get("task_id") for item in rows}) != len(rows):
        raise FbrError(f"{task_dir.name}：批次交接清单任务缺失或重复")
    row = matches[0]
    for key in ("repo_id", "initial_commit", "task_type", "difficulty", "baseline_id", "handoff_hash", "task_repo"):
        if row.get(key) != record.get(key):
            raise FbrError(f"{task_dir.name}：批次清单的 {key} 与任务不一致")
    if (manifest.get("batch_id") != task_dir.parent.name or record.get("batch") != task_dir.parent.name
            or manifest.get("count") != len(rows)):
        raise FbrError(f"{task_dir.name}：批次编号或任务数量不一致")
    if row.get("workspace") != f"{record['task_id']}/workspace" or row.get("task_file") != f"{record['task_id']}/task.json":
        raise FbrError(f"{task_dir.name}：交接目录不一致")
    if manifest.get("baselines", {}).get(record["baseline_id"]) != baseline:
        raise FbrError(f"{task_dir.name}：批次基线快照不一致")
    for other in rows:
        if (other.get("repo_id"), other.get("initial_commit")) == (record["repo_id"], record["initial_commit"]):
            if other.get("baseline_id") != record["baseline_id"]:
                raise FbrError(f"{task_dir.name}：同一初始提交的基础信息不一致")


def validate_single_layout(task_dir: Path, record: dict, manifest: dict) -> None:
    """S11 producer emits exactly one origin and one workspace, both on main."""
    expected = {"producer": "s11-gsb-prompt-create", "execution_mode": "single", "branch": "main"}
    for key, value in expected.items():
        if record.get(key) != value or manifest.get(key) != value:
            raise FbrError(f"{task_dir.name}：单次运行交接的 {key} 不一致")
    for key in ("origin", "workspace"):
        if record.get(key) != f"{record['task_id']}/{key}":
            raise FbrError(f"{task_dir.name}：单次运行交接的 {key} 路径不一致")
    rows = [row for row in manifest.get("tasks", []) if row.get("task_id") == record["task_id"]]
    if len(rows) != 1:
        raise FbrError(f"{task_dir.name}：单次运行交接任务缺失或重复")
    for key in (*expected, "origin", "workspace"):
        if rows[0].get(key) != record.get(key):
            raise FbrError(f"{task_dir.name}：单次运行清单的 {key} 不一致")
    if record.get("task_type") not in {"功能开发", "Bugfix", "代码重构", "功能优化", "依赖升级与迁移"}:
        raise FbrError(f"{task_dir.name}：S11 任务类型无效")
    for name in ("originA", "originB", "workspaceA", "workspaceB"):
        if (task_dir / name).exists():
            raise FbrError(f"{task_dir.name}：不接受 A/B 双侧目录 {name}")
    origin = task_dir / "origin"
    if not (origin / ".git").is_dir():
        raise FbrError(f"{task_dir.name}：缺少独立 origin 克隆")
    if git(origin, "rev-parse", "HEAD").stdout.strip() != record["initial_commit"]:
        raise FbrError(f"{task_dir.name}：origin 不在初始提交")
    if git(origin, "status", "--porcelain").stdout.strip():
        raise FbrError(f"{task_dir.name}：origin 有未提交改动")
    if git(origin, "symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
        raise FbrError(f"{task_dir.name}：origin 必须在 main")


def load_state(task_dir: Path) -> dict:
    return load_json(Path(task_dir) / "state.json", default={"stages": {}})


_held_locks = threading.local()


@contextlib.contextmanager
def file_lock(path: Path):
    """进程/线程互斥；同一线程可重入。锁文件不删除，避免 inode 替换导致双持有。"""
    path = Path(path).absolute()
    held = getattr(_held_locks, "paths", set())
    if str(path) in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise FbrError("锁文件不能是符号链接")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FbrError(f"另一个进程正在操作：{path.parent.name}") from None
        _held_locks.paths = held | {str(path)}
        try:
            yield
        finally:
            _held_locks.paths = held
            fcntl.flock(handle, fcntl.LOCK_UN)


def task_locked(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        task = bound.get("task_dir", bound.get("task"))
        with file_lock(Path(task) / ".operation.lock"):
            if fn.__name__ in ('run_one', 'recover_one', 'collect_one', 'commit_one', 'record_one', 'facts_one', 'review', 'finalize', 'probe_one'):
                receipt = load_json(Path(task) / 'review/upload.json', default={})
                if receipt.get('submission_id') and not bound.get('allow_platform_repair', False):
                    raise FbrError('任务已有平台单，生产证据已冻结；不能自动重跑、重录或重写评价')
            return fn(*args, **kwargs)
    return guarded


@task_locked
def set_stage(task_dir: Path, stage: str, state: str, **info) -> dict:
    """state 取 ok / failed / dry。写某一阶段时，把它之后的阶段作废（上游变了，下游要重做）。"""
    data = load_state(task_dir)
    data["stages"][stage] = {"state": state, "at": now_iso(), **info}
    for later in STAGES[STAGES.index(stage) + 1:]:
        data["stages"].pop(later, None)
    save_json(Path(task_dir) / "state.json", data)
    return data


def stage_ok(task_dir: Path, stage: str) -> bool:
    return load_state(task_dir)["stages"].get(stage, {}).get("state") == "ok"


def require_stage(task_dir: Path, stage: str) -> dict:
    stages = load_state(task_dir)["stages"]
    if any(info.get("key_leak") for info in stages.values()):
        raise FbrError(f"{Path(task_dir).name}：已检测到网关 key 泄露，保留证据与容器，等待人工处理")
    info = stages.get(stage, {})
    if info.get("state") != "ok":
        raise FbrError(f"{Path(task_dir).name}：{stage} 阶段还没通过，不能往下做")
    return info


# ---------- 外部命令 ----------

def run(command: list[str], *, cwd=None, timeout=None, env=None, input_text=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=env, input=input_text)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", f"找不到命令：{command[0]}")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return subprocess.CompletedProcess(command, 124, out, "超时")


def run_stream(command: list[str], *, stdin_text: str, stdout_path: Path, stderr_path: Path,
               timeout: int) -> tuple[int, bool]:
    """跑一个长命令：stdin 给一段文本，stdout / stderr 直接落文件。返回 (退出码, 是否超时)。"""
    import fbr_runtime
    context = fbr_runtime.STREAM.get()
    if context is not None:
        return fbr_runtime.monitored_stream(command, stdin_text, stdout_path, stderr_path, timeout, context)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            process.communicate(stdin_text.encode("utf-8"), timeout=timeout)
            return process.returncode, False
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return 124, True


GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.pager=cat"]


def git(repo: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    """workspace 在运行期间归被测助手所有，它能改 .git 里的东西。宿主机上的 git 一律不跑钩子和 fsmonitor。"""
    return subprocess.run(["git", *GIT_SAFE, *args], cwd=repo, capture_output=True, text=True,
                          errors="replace", env=env)


def save_git_config(task_dir: Path) -> None:
    """起跑前留一份 workspace/.git/config。"""
    validate_git_layout(task_dir)
    source = Path(task_dir) / "workspace" / ".git" / "config"
    target = Path(task_dir) / "run" / "git-config.before"
    if source.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def validate_git_layout(task_dir: Path) -> Path:
    """只接受本题独立 .git 目录；commondir 能让 Git 绕过这里备份的 config。"""
    task_dir = Path(task_dir)
    git_dir = task_dir / "workspace" / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise FbrError(f"{task_dir.name}：workspace 的 .git 不是独立目录，不在宿主机上执行 git")
    for name in ("commondir", "gitdir", "worktrees", "config.worktree"):
        marker = git_dir / name
        if marker.exists() or marker.is_symlink():
            raise FbrError(f"{task_dir.name}：.git/{name} 不是独立仓库布局，不在宿主机上执行 git")
    return git_dir


def restore_git_config(task_dir: Path) -> dict:
    """运行后先把 .git/config 换回起跑前那份，并挪走新出现的钩子，再在宿主机上动 git。

    配置里能挂外部命令（过滤器、差异工具、凭据助手），不能带着被改过的配置在宿主机上执行。
    """
    task_dir = Path(task_dir)
    git_dir = validate_git_layout(task_dir)
    saved = task_dir / "run" / "git-config.before"
    out = {"config_restored": False, "hooks_moved": []}
    if not saved.is_file():
        raise FbrError(f"{task_dir.name}：没有起跑前的 git 配置备份，不在宿主机上对这个 workspace 执行 git")
    current = git_dir / "config"
    if current.is_symlink() or not current.is_file() or current.read_bytes() != saved.read_bytes():
        if current.is_file() and not current.is_symlink():
            (task_dir / "run" / "git-config.after").write_bytes(current.read_bytes())
        # 替换目录项，不能沿着模型新建的 config 符号链接写到 workspace 之外。
        handle, temp = tempfile.mkstemp(dir=git_dir, prefix=".config.restore-")
        with os.fdopen(handle, "wb") as stream:
            stream.write(saved.read_bytes())
        os.replace(temp, current)
        out["config_restored"] = True
    hooks = git_dir / "hooks"
    if hooks.is_symlink():
        keep = task_dir / "run" / "hooks-removed"
        keep.mkdir(parents=True, exist_ok=True)
        os.replace(hooks, keep / "hooks-directory-link")
        out["hooks_moved"].append("hooks-directory-link")
    elif hooks.is_dir():
        for item in sorted(hooks.iterdir()):
            if (item.is_file() or item.is_symlink()) and not item.name.endswith(".sample"):
                keep = task_dir / "run" / "hooks-removed"
                keep.mkdir(parents=True, exist_ok=True)
                os.replace(item, keep / item.name)
                out["hooks_moved"].append(item.name)
    return out


def git_out(repo: Path, *args: str) -> str:
    result = git(repo, *args)
    if result.returncode != 0:
        raise FbrError(f"git {' '.join(args[:3])} 失败：{result.stderr.strip()[:300]}")
    return result.stdout.strip()


def clean_clone(workspace: Path, commit: str, prefix: str) -> Path:
    """把某个提交克隆到独立目录（只含已提交的内容），给录屏和取证用；保留目录供核查。"""
    dest = scratch_dir(prefix) / "workspace"
    result = git(Path(workspace), "clone", "-q", "--no-hardlinks", "--no-checkout", str(Path(workspace).resolve()),
                 str(dest))
    if result.returncode != 0:
        raise FbrError(f"克隆失败：{result.stderr.strip()[:300]}")
    # 产物提交可能不在任何分支上（历史被改写时），直接从原仓库取这个对象
    fetched = git(dest, "fetch", "-q", str(Path(workspace).resolve()), commit)
    checked = git(dest, "checkout", "-q", "--detach", commit)
    if checked.returncode != 0:
        raise FbrError(f"检出 {commit[:12]} 失败：{(checked.stderr or fetched.stderr).strip()[:300]}")
    open_permissions(dest)
    return dest


def docker_hardening() -> list[str]:
    return ["--init", "--restart=no", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


def mount_arg(path: Path) -> str:
    text = str(Path(path).resolve())
    if "," in text:
        raise FbrError(f"挂载路径不能带逗号：{text}")
    return f"type=bind,src={text},dst=/workspace"


def open_permissions(path: Path) -> None:
    """容器里是非 root 的 node 用户，挂进去的目录要可读写。只动这一个目录。"""
    result = run(["chmod", "-R", "a+rwX", str(path)])
    if result.returncode != 0:
        raise FbrError(f"chmod 失败：{result.stderr.strip()[:200]}")


def scratch_dir(prefix: str) -> Path:
    parent = os.environ.get("FBR_SCRATCH")
    scratch = Path(parent).expanduser().resolve() if parent else find_root() / "_verification" / "runtime-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=scratch))
    if "," in str(path):
        raise FbrError(f"临时目录路径带逗号：{path}")
    return path


def run_steps_in_image(image: str, workdir: Path, steps: list[dict], *, tail: int = 60,
                       stop_on_fail: bool = True, _seed: bool = True) -> list[dict]:
    """在任务镜像的一个新容器里依次跑几条命令（同一个容器，前一步装的东西后一步能用），不带 key。

    steps: [{name, cmd, timeout}]；返回 [{name, cmd, rc, seconds, tail}]，没跑到的步骤 rc 为 None。
    """
    if _seed and steps and steps[0].get('name') == 'setup':
        try:
            cfg = settings(containing_root(workdir))
        except FbrError:
            cfg = None
        if cfg and cfg.get('dependencies', {}).get('enabled'):
            import fbr_dependencies
            prepared = fbr_dependencies.seed(workdir, cfg, setup_cmd=steps[0]['cmd'])
            if prepared:
                steps = [dict(steps[0], cmd=cfg.get('dependencies', {}).get('setup_after_restore_cmd') or 'true'), *steps[1:]]
    lines = ["cd /workspace || exit 97"]
    for index, step in enumerate(steps):
        guard = "if [ \"$_stop\" != 1 ]; then" if stop_on_fail else "if true; then"
        lines += [
            guard,
            f"_t0=$(date +%s); timeout -k 10 {int(step['timeout'])} bash -lc {shlex.quote(step['cmd'])} "
            f"> /tmp/fbr-step-{index}.log 2>&1; _rc=$?",
            f"echo \"{MARK} step={index} rc=$_rc secs=$(( $(date +%s) - _t0 ))\"",
            f"tail -n {tail} /tmp/fbr-step-{index}.log", f"echo \"{MARK} end={index}\"",
            "[ $_rc -ne 0 ] && _stop=1", "fi",
        ]
    name = f"s11-fbr-{uuid.uuid4().hex[:10]}"
    command = ["docker", "run", *docker_hardening(), "--name", name, "--entrypoint", "bash",
               "--mount", mount_arg(workdir), image, "-c", "\n".join(lines)]
    total = sum(int(step["timeout"]) for step in steps) + 120
    result = run(command, timeout=total)
    if result.returncode == 124 and result.stderr == "超时":
        run(["docker", "kill", name], timeout=30)
    rows = parse_steps(steps, result.stdout)
    logs = scratch_dir('command-logs')
    for index, row in enumerate(rows):
        row["container_name"] = name
        target = logs / f'{index}.log'
        copied = run(['docker', 'cp', f'{name}:/tmp/fbr-step-{index}.log', str(target)], timeout=60)
        if copied.returncode == 0 and target.is_file():
            row['output_file'] = str(target)
            row['output_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    return rows


def parse_steps(steps: list[dict], output: str) -> list[dict]:
    rows = [{"name": s["name"], "cmd": s["cmd"], "rc": None, "seconds": None, "tail": ""} for s in steps]
    current, buffer = None, []
    for line in output.splitlines():
        head = re.match(rf"^{MARK} step=(\d+) rc=(-?\d+) secs=(-?\d+)$", line)
        tail_mark = re.match(rf"^{MARK} end=(\d+)$", line)
        if head and int(head.group(1)) < len(rows):
            current, buffer = int(head.group(1)), []
            rows[current].update(rc=int(head.group(2)), seconds=int(head.group(3)))
        elif tail_mark and current is not None:
            rows[current]["tail"] = "\n".join(buffer)
            current = None
        elif current is not None:
            buffer.append(line)
    return rows


# ---------- 密钥与脱敏 ----------

def key_path(cfg: dict) -> Path:
    return Path(cfg["key_file"]).expanduser()


def read_key(cfg: dict) -> str:
    """读出网关 key 的值，只用于检查输出与保护汇报，绝不打印。没有就返回空串。"""
    path = key_path(cfg)
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        found = re.match(r"^\s*(?:apikey|APIKEY|ANTHROPIC_AUTH_TOKEN)\s*=\s*(\S+)\s*$", line)
        if found:
            return found.group(1).strip("'\"")
    return ""


TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b")


def redact(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
            text = text.replace(base64.b64encode(f"x-access-token:{secret}".encode()).decode(), "***")
    text = re.sub(r"(?i)\b(apikey|ANTHROPIC_AUTH_TOKEN)=\S+", r"\1=***", text)
    return TOKEN_RE.sub("***", text)


def key_occurrences(path: Path, secret: str) -> int:
    """只读统计 key 原文，不改写原始输出或轨迹，也不返回 key 内容。"""
    if not secret or not Path(path).is_file():
        return 0
    data = Path(path).read_bytes()
    return data.count(secret.encode())


# ---------- 文本 ----------

def normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE).lower()


def han_count(text: str) -> int:
    return len(re.findall(r"[一-鿿]", text))


def clip(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"
