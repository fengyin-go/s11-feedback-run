#!/usr/bin/env python3
"""Bounded retention and recovery for rebuildable S11 working data."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import fbrlib as lib


POLICIES = {"safe", "balanced", "minimal"}


def policy(cfg: dict | None) -> str:
    value = str(((cfg or {}).get("cleanup") or {}).get("policy", "minimal"))
    if value not in POLICIES:
        raise lib.FbrError(f"未知清理策略：{value}")
    return value


def enabled(cfg: dict | None, capability: str, *, force: bool = False) -> bool:
    settings = (cfg or {}).get("cleanup") or {}
    if not force and settings.get("auto") is not True:
        return False
    switches = {
        "record": "compact_record_attempts",
        "scratch": "compact_scratch_after_facts",
        "local": "compact_task_after_qc_passed",
        "dependencies": "purge_dependencies_on_batch_complete",
    }
    switch = switches.get(capability)
    if switch and settings.get(switch) is not True:
        return False
    current = policy(cfg)
    if current == "safe":
        return False
    if capability == "local" or capability == "dependencies":
        return current == "minimal"
    return capability in {"record", "scratch", "history"}


def history_keep(cfg: dict | None) -> int:
    value = int(((cfg or {}).get("cleanup") or {}).get("history_keep", 1))
    if value < 0:
        raise lib.FbrError("cleanup.history_keep 不能小于 0")
    return value


def path_size(path: Path) -> int:
    path = Path(path)
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, directories, files in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories
            if not (Path(folder) / name).is_symlink()
        ]
        for name in files:
            item = Path(folder) / name
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    return total


def _maintenance_path(root: Path, batch: str, task: str, label: str) -> Path:
    return root / "_verification" / "retention" / f"{batch}-{task}-{label}.json"


def _record(root: Path, batch: str, task: str, label: str, row: dict) -> dict:
    target = _maintenance_path(root, batch, task, label)
    lib.save_json(target, row)
    return row


def _safe_task_path(task_dir: Path, path: Path) -> Path:
    task_abs = task_dir.absolute()
    path_abs = path.absolute()
    if path.is_symlink():
        path_abs.relative_to(task_abs)
        return path_abs
    path_abs.resolve().relative_to(task_abs.resolve())
    return path_abs


def _safe_scratch_path(root: Path, task_id: str, path: Path) -> Path:
    scratch = (root / "_verification" / "runtime-scratch").absolute()
    path_abs = path.absolute()
    if path.is_symlink():
        path_abs.relative_to(scratch)
        if task_id not in path_abs.name:
            raise lib.FbrError(f"临时目录不属于当前任务：{path}")
        return path_abs
    resolved = path_abs.resolve()
    resolved.relative_to(scratch.resolve())
    if task_id not in resolved.name:
        raise lib.FbrError(f"临时目录不属于当前任务：{path}")
    return path_abs


def _move_candidates(root: Path, batch: str, task_id: str,
                     candidates: list[Path], label: str) -> tuple[list[dict], Path]:
    trash = root / "_verification" / "cleanup-trash" / batch / f"{task_id}-{label}-{uuid.uuid4().hex[:8]}"
    trash.mkdir(parents=True, exist_ok=True)
    removed = []
    try:
        for candidate in candidates:
            candidate = Path(candidate)
            if not candidate.exists() and not candidate.is_symlink():
                continue
            try:
                relative = candidate.absolute().relative_to((root / "batches" / batch / task_id).absolute())
                destination = trash / "task" / relative
            except ValueError:
                relative = candidate.absolute().relative_to(
                    (root / "_verification" / "runtime-scratch").absolute())
                destination = trash / "external-scratch" / relative
            size = path_size(candidate)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, destination)
            removed.append({
                "path": relative.as_posix(),
                "bytes": size,
                "trashed_at": lib.now_iso(),
            })
        return removed, trash
    except Exception as exc:
        raise lib.FbrError(f"回收中止，已移动内容保留在 {trash}：{exc}") from exc


def _remove_known(root: Path, task_dir: Path, candidates: list[Path],
                  label: str, ledger: dict) -> dict:
    task_dir = Path(task_dir)
    batch = task_dir.parent.name
    task_id = task_dir.name
    checked = []
    for candidate in candidates:
        candidate = Path(candidate)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            checked.append(_safe_task_path(task_dir, candidate))
        except ValueError:
            checked.append(_safe_scratch_path(root, task_id, candidate))
    removed, trash = _move_candidates(root, batch, task_id, checked, label)
    row = {
        **ledger,
        "task_id": task_id,
        "batch": batch,
        "at": lib.now_iso(),
        "removed": removed,
        "bytes_removed": sum(item["bytes"] for item in removed),
    }
    target = _record(root, batch, task_id, label, row)
    shutil.rmtree(trash, ignore_errors=True)
    return {**row, "maintenance": str(target)}


@lib.task_locked
def compact_record_attempts(root: Path, task_dir: Path, cfg: dict) -> dict:
    if not enabled(cfg, "record"):
        return {"state": "disabled", "bytes_removed": 0}
    if not lib.stage_ok(task_dir, "record"):
        return {"state": "blocked", "reason": "record 阶段尚未通过"}
    record = task_dir / "record"
    if not (record / "recording.mp4").is_file() or not (record / "recording.json").is_file():
        return {"state": "blocked", "reason": "正式录屏文件不完整"}
    candidates = [
        record / "attempts",
        record / "containers.json",
        record / "last-attempt.json",
    ]
    ledgers = [path for path in candidates if path.exists() or path.is_symlink()]
    if not ledgers:
        return {"state": "skip", "bytes_removed": 0}
    row = _remove_known(root, task_dir, candidates, "record", {
        "state": "compacted",
        "reason": "正式录屏已通过，回收候选与容器索引",
    })
    return {"state": "ok", **_record_values(row)}


@lib.task_locked
def compact_task_scratch(root: Path, task_dir: Path, cfg: dict) -> dict:
    if not enabled(cfg, "scratch"):
        return {"state": "disabled", "bytes_removed": 0}
    scratch = root / "_verification" / "runtime-scratch"
    if not scratch.is_dir():
        return {"state": "skip", "bytes_removed": 0}
    candidates = [
        path for path in sorted(scratch.iterdir())
        if path.is_dir() and not path.is_symlink() and task_dir.name in path.name
    ]
    if not candidates:
        return {"state": "skip", "bytes_removed": 0}
    row = _remove_known(root, task_dir, candidates, "scratch", {
        "state": "compacted",
        "reason": "阶段证据已落盘，回收本题临时克隆",
    })
    return {"state": "ok", **_record_values(row)}


def _record_values(row: dict) -> dict:
    return {
        "bytes_removed": row.get("bytes_removed", 0),
        "maintenance": str(row.get("maintenance", "")),
    }


def trim_delivery_history(root: Path, batch: str, cfg: dict, *, force: bool = False) -> dict:
    if not enabled(cfg, "history", force=force):
        return {"state": "disabled", "removed": [], "bytes_removed": 0}
    keep = history_keep(cfg)
    lock = root / "_verification" / "delivery-locks" / f"{batch}.lock"
    with lib.file_lock(lock):
        keep_paths, remove = history_candidates(root, batch, keep)
        trash = root / "_verification" / "cleanup-trash" / "history" / f"{batch}-{uuid.uuid4().hex[:8]}"
        trash.mkdir(parents=True, exist_ok=True)
        removed = []
        try:
            for path in remove:
                size = path_size(path)
                destination = trash / path.name
                os.replace(path, destination)
                removed.append({"path": str(path), "bytes": size})
        except Exception as exc:
            raise lib.FbrError(f"历史快照回收中止，内容保留在 {trash}：{exc}") from exc
        row = {
            "state": "compacted",
            "batch": batch,
            "at": lib.now_iso(),
            "keep": keep,
            "kept": [str(path) for path in keep_paths],
            "removed": removed,
            "bytes_removed": sum(item["bytes"] for item in removed),
        }
        lib.save_json(root / "_verification" / "retention" / f"{batch}-history.json", row)
        shutil.rmtree(trash, ignore_errors=True)
        return row


def history_candidates(root: Path, batch: str, keep: int) -> tuple[list[Path], list[Path]]:
    history = root / "_verification" / "delivery-history"
    snapshots = [
        path for path in history.glob(f"{batch}-*")
        if path.is_dir() and not path.is_symlink()
    ] if history.is_dir() else []
    snapshots.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    return snapshots[:keep], snapshots[keep:]


def purge_dependencies(root: Path, cfg: dict, *, force: bool = False) -> dict:
    if not enabled(cfg, "dependencies", force=force):
        return {"state": "disabled", "bytes_removed": 0}
    directory = root / "_registry" / "dependencies"
    if not directory.is_dir():
        return {"state": "skip", "bytes_removed": 0}
    size = path_size(directory)
    trash = root / "_verification" / "cleanup-trash" / "registry" / f"dependencies-{uuid.uuid4().hex[:8]}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    os.replace(directory, trash)
    row = {
        "state": "purged",
        "at": lib.now_iso(),
        "removed": str(directory),
        "bytes_removed": size,
        "reason": "批次结束，依赖缓存可按身份重建",
    }
    lib.save_json(root / "_verification" / "retention" / "dependencies.json", row)
    shutil.rmtree(trash, ignore_errors=True)
    return row


def _restore_checkout(task_dir: Path, name: str, url: str, commit: str,
                      staging_root: Path, env: dict[str, str]) -> dict:
    """Restore one clean `main` checkout without overwriting live state."""
    target = task_dir / name
    git_dir = target / ".git"
    if git_dir.is_dir():
        head = subprocess.run(
            ["git", *lib.GIT_SAFE, "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        branch = subprocess.run(
            ["git", *lib.GIT_SAFE, "-C", str(target), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        dirty = subprocess.run(
            ["git", *lib.GIT_SAFE, "-C", str(target), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if (head.returncode != 0 or head.stdout.strip() != commit
                or branch.returncode != 0 or branch.stdout.strip() != "main"
                or dirty.returncode != 0 or dirty.stdout.strip()):
            raise lib.FbrError(f"{name} 已存在但不在预期提交或 main 分支，拒绝覆盖")
        return {"state": "skip", "path": str(target), "commit": commit}
    if target.exists() or target.is_symlink():
        raise lib.FbrError(f"{name} 已存在但不是可校验的 Git 仓库，拒绝覆盖")
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{task_dir.name}-{name}-", dir=staging_root))
    clone = staging / name
    try:
        cloned = subprocess.run(
            ["git", *lib.GIT_SAFE, "clone", "-q", "--no-checkout", url, str(clone)],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if cloned.returncode != 0:
            raise lib.FbrError(f"恢复 {name} 失败：" + cloned.stderr.strip()[-300:])
        configured = subprocess.run(
            ["git", *lib.GIT_SAFE, "-C", str(clone), "config", "core.hooksPath", os.devnull],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if configured.returncode != 0:
            raise lib.FbrError(f"恢复 {name} 时无法禁用 hooks")
        checked = subprocess.run(
            ["git", *lib.GIT_SAFE, "-C", str(clone), "checkout", "-q", "-B", "main", commit],
            capture_output=True, text=True, timeout=180, env=env,
        )
        if checked.returncode != 0:
            raise lib.FbrError(f"恢复 {name} 时检出提交失败：" + checked.stderr.strip()[-300:])
        lib.open_permissions(clone)
        os.replace(clone, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"state": "restored", "path": str(target), "commit": commit}


@lib.task_locked
def ensure_workspace(task_dir: Path) -> dict:
    """Restore both origin and workspace for a compacted task."""
    task_dir = Path(task_dir).resolve()
    workspace = task_dir / "workspace"
    # A cleaned task intentionally has no origin/workspace/run-tree. The
    # immutable task.json still carries the prompt, baseline and handoff hash;
    # validate those while deferring the live layout check until after restore.
    record = lib.load_task(task_dir, validate_layout=False)
    snapshot = lib.load_json(task_dir / "review" / "snapshot.json", default={})
    commit = str(snapshot.get("commit") or "")
    if snapshot.get("pushed") is not True or not snapshot.get("artifact_snapshot"):
        raise lib.FbrError(f"{task_dir.name}：产物快照尚未推送，不能恢复 workspace")
    if not lib.SHA_RE.fullmatch(commit):
        raise lib.FbrError(f"{task_dir.name}：快照提交 SHA 不合法")
    url = str((record.get("publish") or {}).get("repo_url") or "")
    if not url:
        full = str(record.get("task_repo_full") or "")
        if not full:
            raise lib.FbrError(f"{task_dir.name}：缺少任务仓库地址，不能恢复 workspace")
        url = f"https://github.com/{full}.git"
    root = lib.containing_root(task_dir)
    staging_root = root / "_verification" / "workspace-restore"
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )
    initial_commit = str(record.get("initial_commit") or "")
    if not lib.SHA_RE.fullmatch(initial_commit):
        raise lib.FbrError(f"{task_dir.name}：初始提交 SHA 不合法")
    origin_result = _restore_checkout(
        task_dir, "origin", url, initial_commit, staging_root, env)
    workspace_result = _restore_checkout(
        task_dir, "workspace", url, commit, staging_root, env)
    if origin_result["state"] == "restored" or workspace_result["state"] == "restored":
        lib.save_git_config(task_dir)
    cleanup_path = task_dir / "cleanup.json"
    cleanup = lib.load_json(cleanup_path, default={})
    if cleanup:
        now = lib.now_iso()
        if workspace_result["state"] == "restored":
            cleanup["workspace_restored_at"] = now
        if origin_result["state"] == "restored":
            cleanup["origin_restored_at"] = now
        cleanup["restored_from"] = {
            "repo_url": url,
            "origin_commit": initial_commit,
            "workspace_commit": commit,
            "artifact_snapshot": snapshot.get("artifact_snapshot"),
        }
        lib.save_json(cleanup_path, cleanup)
        result_cleanup = root / "results" / task_dir.parent.name / task_dir.name / "cleanup.json"
        if result_cleanup.is_file():
            lib.save_json(result_cleanup, cleanup)
    state = ("restored" if origin_result["state"] == "restored"
             or workspace_result["state"] == "restored" else "skip")
    return {
        "state": state,
        "path": str(workspace),
        "commit": commit,
        "repo_url": url,
        "origin": origin_result,
        "workspace": workspace_result,
    }
