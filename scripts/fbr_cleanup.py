#!/usr/bin/env python3
"""Archive task evidence, compact rebuildable data, and finalize after QC_PASSED."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib
import fbr_delivery as delivery
import fbr_retention as retention
import fbr_runtime as runtime
import fbr_submit


RETENTION_VERSION = 1
PENDING_STATUSES = {
    "QC_WAITING", "QC_RUNNING", "PENDING", "SUBMITTED", "UNDER_REVIEW",
}
FIX_STATUSES = {"PENDING_FIX"}


def _digest(path: Path) -> str:
    return delivery.digest(path)


def _path_size(path: Path) -> int:
    path = Path(path)
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories
                          if not (Path(folder) / name).is_symlink()]
        for name in files:
            item = Path(folder) / name
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    return total


def _task_entry(manifest: dict, task_id: str) -> dict:
    entry = next((row for row in manifest.get("tasks", []) if row.get("task_id") == task_id), None)
    if not entry:
        reason = next((row.get("reason") for row in manifest.get("excluded", [])
                       if row.get("task_id") == task_id), "未进入当前交付清单")
        raise lib.FbrError(reason)
    return entry


def _verify_result_task(root: Path, manifest: dict, task_id: str) -> None:
    entry = _task_entry(manifest, task_id)
    for item in entry.get("files", []):
        path = root / item.get("path", "")
        if not delivery.safe_file(delivery.delivery_dir(root, manifest["batch_id"]), path):
            raise lib.FbrError(f"结果文件缺失：{item.get('path')}")
        if path.stat().st_size != item.get("bytes") or _digest(path) != item.get("sha256"):
            raise lib.FbrError(f"结果文件摘要不一致：{item.get('path')}")


def _verify_snapshot_entry(snapshot: Path, entry: dict) -> None:
    task_id = entry.get("task_id")
    for item in entry.get("files", []):
        parts = Path(item.get("path", "")).parts
        try:
            offset = parts.index(task_id)
        except ValueError:
            raise lib.FbrError("历史交付清单路径不含任务编号") from None
        relative = Path(*parts[offset + 1:])
        path = snapshot / task_id / relative
        if not path.is_file() or path.is_symlink():
            raise lib.FbrError(f"历史结果缺少：{item.get('path')}")
        if path.stat().st_size != item.get("bytes") or _digest(path) != item.get("sha256"):
            raise lib.FbrError(f"历史结果摘要不一致：{item.get('path')}")


def restore_frozen_projection(root: Path, batch_id: str, tasks: str | None = None) -> dict:
    """Restore submitted tasks removed from the current results by later policy refreshes."""
    target = delivery.delivery_dir(root, batch_id)
    manifest_path = target / "delivery-manifest.json"
    manifest = lib.load_json(manifest_path, default={})
    if not manifest.get("batch_id"):
        raise lib.FbrError("缺少当前 delivery-manifest.json")
    entries = {row["task_id"]: row for row in manifest.get("tasks", [])
               if isinstance(row, dict) and row.get("task_id")}
    history = root / "_verification/delivery-history"
    snapshots = [path for path in sorted(history.glob(f"{batch_id}-*"), reverse=True)
                 if path.is_dir() and not path.is_symlink()] if history.is_dir() else []
    restored = []
    blocked = []
    for task_dir in lib.task_dirs(root, batch_id, tasks):
        if task_dir.name in entries:
            continue
        cleanup = lib.load_json(task_dir / "cleanup.json", default={})
        snapshot = lib.load_json(task_dir / "review/snapshot.json", default={})
        local_sealed = (
            cleanup.get("state") in ("local_compacted", "cleaned")
            and snapshot.get("pushed") is True
            and bool(snapshot.get("artifact_snapshot"))
        )
        if not (task_dir / "review/upload.json").is_file() and not local_sealed:
            blocked.append({"task_id": task_dir.name, "reason": "没有平台回执或已推送的本地封存"})
            continue
        found = None
        for snapshot in snapshots:
            old_manifest = lib.load_json(snapshot / "delivery-manifest.json", default={})
            entry = next((row for row in old_manifest.get("tasks", [])
                          if row.get("task_id") == task_dir.name), None)
            if not entry or not (snapshot / task_dir.name).is_dir():
                continue
            try:
                _verify_snapshot_entry(snapshot, entry)
            except (lib.FbrError, OSError, ValueError):
                continue
            found = (snapshot, entry)
            break
        if not found:
            blocked.append({"task_id": task_dir.name,
                            "reason": "历史交付快照中没有可验证的结果包"})
            continue
        snapshot, entry = found
        staging_root = root / "_verification/cleanup-restore"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{batch_id}-{task_dir.name}-", dir=staging_root))
        try:
            copied = shutil.copytree(snapshot / task_dir.name, temporary / task_dir.name,
                                     symlinks=False)
            destination = target / task_dir.name
            if destination.exists():
                retired = staging_root / f"existing-{task_dir.name}"
                if retired.exists():
                    shutil.rmtree(retired)
                os.replace(destination, retired)
            os.replace(copied, destination)
            entries[task_dir.name] = entry
            restored.append({"task_id": task_dir.name, "snapshot": str(snapshot)})
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    manifest["tasks"] = [entries[name] for name in sorted(entries)]
    manifest["excluded"] = [row for row in manifest.get("excluded", [])
                            if row.get("task_id") not in entries]
    delivery.atomic_save_json(manifest_path, manifest)
    return {"batch": batch_id, "restored": restored, "blocked": blocked}


def _write_copy(source: Path, target: Path) -> bool:
    if not source.is_file() or source.is_symlink():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    delivery.atomic_write_bytes(target, source.read_bytes())
    return True


def _write_retention(root: Path, task_dir: Path, manifest: dict, task: dict,
                     receipt: dict | None = None, detail: dict | None = None,
                     *, state: str = "qc_cleaned") -> dict:
    receipt = receipt or {}
    detail = detail or {}
    artifact = delivery.delivery_dir(root, task_dir.parent.name) / task_dir.name
    platform = artifact / "platform"
    platform.mkdir(parents=True, exist_ok=True)
    _write_copy(task_dir / "review/snapshot.json", platform / "snapshot.json")
    _write_copy(task_dir / "review/attestation.json", platform / "attestation.json")
    _write_copy(task_dir / "review/upload.json", platform / "upload.json")
    _write_copy(task_dir / "review/submit-preflight.json", platform / "submit-preflight.json")
    _write_copy(task_dir / "review/diff.patch", artifact / "review/diff.patch")
    _write_copy(task_dir / "recording-plan.json", artifact / "recording-plan.json")
    _write_copy(task_dir / "review/recording-plan.override.json",
                artifact / "recording-plan.override.json")
    _write_copy(task_dir / "run/attempts.json", artifact / "run/attempts.json")
    _write_copy(task_dir / "task-seed.json", artifact / "task-seed.json")
    _write_copy(task_dir / "state.json", artifact / "state.json")

    files = {}
    for path in sorted(artifact.rglob("*")):
        if path.is_file() and not path.is_symlink():
            if path.name in ("retention.json", "cleanup.json"):
                continue
            files[path.relative_to(artifact).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": _digest(path),
            }
    local_snapshot = lib.load_json(task_dir / "review/snapshot.json", default={})
    snapshot = receipt.get("remote_data") or {}
    retention = {
        "schema_version": RETENTION_VERSION,
        "task_id": task_dir.name,
        "batch": task_dir.parent.name,
        "state": state,
        "archived_at": lib.now_iso(),
        "platform": {
            "account": receipt.get("account"),
            "submission_id": receipt.get("submission_id"),
            "status": receipt.get("remote_status") or "local_sealed",
            "submitted_at": receipt.get("submitted_at"),
            "verified_at": receipt.get("verified_at"),
            "qc_checked_at": lib.now_iso(),
            "qc_summary": detail.get("qc_summary"),
            "qc_checks": detail.get("qc_checks"),
            "content_digest": receipt.get("content_digest"),
            "remote_trace": receipt.get("remote_trace"),
        },
        "reproduction": {
            "prompt": task.get("prompt"),
            "requirements": task.get("requirements"),
            "repo_url": (task.get("publish") or {}).get("repo_url"),
            "repo_full": task.get("task_repo_full"),
            "initial_commit": task.get("initial_commit"),
            "initial_snapshot": (task.get("publish") or {}).get("initial_snapshot"),
            "artifact_snapshot": snapshot.get("artifact_snapshot")
            or local_snapshot.get("artifact_snapshot"),
            "image": task.get("image"),
            "image_id": task.get("image_id"),
            "harness": task.get("harness"),
            "harness_version": task.get("harness_version"),
            "os": task.get("os"),
            "repro_level": task.get("repro_level"),
            "recording_plan": {
                "path": "recording-plan.json",
                "sha256": (_digest(task_dir / "recording-plan.json")
                           if (task_dir / "recording-plan.json").is_file() else None),
            },
        },
        "files": files,
    }
    delivery.atomic_save_json(artifact / "retention.json", retention)
    return retention


def _verify_retention(root: Path, task_id: str, batch_id: str) -> None:
    artifact = delivery.delivery_dir(root, batch_id) / task_id
    path = artifact / "retention.json"
    if not path.is_file() or path.is_symlink():
        raise lib.FbrError("缺少 retention.json")
    retention = lib.load_json(path, default={})
    if retention.get("schema_version") != RETENTION_VERSION:
        raise lib.FbrError("retention.json 版本不正确")
    for name, info in (retention.get("files") or {}).items():
        current = artifact / name
        if not delivery.safe_file(artifact, current):
            raise lib.FbrError(f"保留文件缺失：{name}")
        if current.stat().st_size != info.get("bytes") or _digest(current) != info.get("sha256"):
            raise lib.FbrError(f"保留文件摘要不一致：{name}")


def _refresh_remote(client, root: Path, task_dir: Path, manifest: dict) -> tuple[dict, dict, dict]:
    receipt = lib.load_json(task_dir / "review/upload.json", default={})
    if not isinstance(receipt, dict) or not fbr_submit.valid_id(receipt.get("submission_id")):
        raise lib.FbrError("没有有效平台提交回执")
    submission_id = receipt["submission_id"]
    detail = client.detail(submission_id)
    import fbr_sheet
    row, reason = fbr_sheet.build_row(root, task_dir, lib.settings(root), delivery_manifest=manifest)
    if not row:
        raise lib.FbrError(reason or '无法核对当前交付字段')
    plan = fbr_submit.plan_from_row(root, task_dir, row, manifest)
    if receipt.get('content_digest') != plan['content_digest']:
        raise lib.FbrError('平台回执与当前交付摘要不一致')
    if set(receipt.get('uploads') or {}) != set(fbr_submit.MEDIA_TYPES):
        raise lib.FbrError('平台附件回执不完整')
    fbr_submit.confirm_remote(client, plan, receipt, task_dir, submission_id, detail=detail)
    receipt = lib.load_json(task_dir / 'review/upload.json')
    detail = {**detail, 'status': receipt['remote_status']}
    status = detail.get("status")
    receipt.update({
        "remote_status": status,
        "qc_running": detail.get("qc_running"),
        "qc_stage": detail.get("stage"),
        "qc_summary": detail.get("qc_summary"),
        "qc_checks": detail.get("qc_checks"),
        "qc_checked_at": lib.now_iso(),
    })
    lib.save_json(task_dir / "review/upload.json", receipt)
    return row, plan, detail


def _task_active(task_dir: Path) -> str:
    current = lib.load_json(task_dir / "run/current.json", default={})
    if runtime.alive(current):
        return "任务仍在运行"
    if lib.stage_ok(task_dir, "record") is False:
        return "录屏阶段尚未完成"
    return ""


def _code_cleanup_blocker(task_dir: Path, *, cached_status=True) -> str:
    """Code checkouts may disappear only after the platform has accepted delivery."""
    receipt = lib.load_json(task_dir / "review/upload.json", default={})
    if (cached_status and receipt.get("state") != "confirmed") or not fbr_submit.valid_id(receipt.get("submission_id")):
        return "尚未完成平台提交，代码现场仍需保留给批检、预检和等待提交阶段"
    if cached_status and receipt.get("remote_status") != "QC_PASSED":
        return f"平台状态不是 QC_PASSED：{receipt.get('remote_status') or '未回读'}"
    return ""


def _cleanup_candidates(root: Path, task_dir: Path) -> list[Path]:
    candidates = [task_dir / "origin", task_dir / "workspace", task_dir / "run-tree"]
    run_dir = task_dir / "run"
    if run_dir.is_dir():
        candidates.extend(path for path in sorted(run_dir.iterdir())
                          if path.name not in ("attempts.json", "key-identity.json"))
    record_dir = task_dir / "record"
    for name in ("attempts", "containers.json", "last-attempt.json"):
        candidates.append(record_dir / name)
    task_id = task_dir.name
    scratch = root / "_verification/runtime-scratch"
    if scratch.is_dir():
        candidates.extend(path for path in sorted(scratch.iterdir())
                          if task_id in path.name and path.is_dir() and not path.is_symlink())
    return [path for path in candidates if path.exists() or path.is_symlink()]


def _recover_cleanup(root, task_dir):
    journal = task_dir / 'cleanup-pending.json'
    if journal.is_symlink():
        raise lib.FbrError('清理事务记录不能是符号链接')
    if not journal.is_file():
        return
    plan = lib.load_json(journal)
    token = str(plan['transaction'])
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise lib.FbrError('清理事务编号无效')
    trash = root / '_verification/cleanup-trash' / task_dir.parent.name / (task_dir.name + '-' + token)
    if trash.resolve() != trash:
        raise lib.FbrError('清理事务暂存目录越界')
    cleanup = lib.load_json(task_dir / 'cleanup.json', default={})
    if cleanup.get('transaction') == token:
        target = delivery.delivery_dir(root, task_dir.parent.name)
        delivery.atomic_save_json(target / task_dir.name / 'cleanup.json', cleanup)
        with lib.file_lock(root / '_verification' / 'cleanup-locks' / (task_dir.parent.name + '.lock')):
            ledger = target / 'cleanup-manifest.json'
            summary = lib.load_json(ledger, default={})
            rows = {row['task_id']: row for row in summary.get('cleaned', [])}
            rows[task_dir.name] = {'task_id': task_dir.name, 'removed': cleanup['removed'],
                                   'bytes_removed': cleanup['bytes_removed']}
            delivery.atomic_save_json(ledger, {'batch': task_dir.parent.name, 'at': lib.now_iso(),
                'cleaned': list(rows.values()), 'bytes_removed': sum(r['bytes_removed'] for r in rows.values())})
    else:
        for item in reversed(plan['removed']):
            relative = Path(item['path'])
            if relative.is_absolute() or '..' in relative.parts or not relative.parts:
                raise lib.FbrError('清理恢复路径无效')
            moved = trash / relative
            if not moved.parent.resolve().is_relative_to(trash):
                raise lib.FbrError('清理恢复来源越界')
            if not os.path.lexists(moved):
                continue
            if relative.parts[0] == 'external-scratch':
                if len(relative.parts) != 2 or task_dir.name not in relative.name:
                    raise lib.FbrError('暂存恢复路径无效')
                original = root / '_verification/runtime-scratch' / relative.name
            else:
                original = task_dir / relative
                if not original.parent.resolve().is_relative_to(task_dir):
                    raise lib.FbrError('清理恢复目标离开当前任务')
            if os.path.lexists(original):
                raise lib.FbrError(f'清理恢复位置已被占用：{relative}')
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(moved, original)
    if trash.exists():
        shutil.rmtree(trash)
    journal.unlink()


def _safe_remove(root: Path, task_dir: Path, candidates: list[Path], receipt: dict) -> list[dict]:
    root, task_dir = root.resolve(), task_dir.resolve()
    scratch = root / '_verification/runtime-scratch'
    token = uuid.uuid4().hex
    trash = root / '_verification/cleanup-trash' / task_dir.parent.name / (task_dir.name + '-' + token)
    removed = []
    paths = []
    for path in candidates:
        if path.parent.resolve().is_relative_to(task_dir):
            relative = path.relative_to(task_dir)
        elif path.parent.resolve() == scratch and task_dir.name in path.name:
            relative = Path('external-scratch') / path.name
        else:
            raise lib.FbrError('清理目标越出白名单目录')
        paths.append((path, trash / relative))
        removed.append({'path': relative.as_posix(), 'bytes': _path_size(path)})
    cleanup = {'transaction': token, 'state': 'cleaned', 'at': lib.now_iso(), 'removed': removed,
               'remote_status': receipt['remote_status'], 'submission_id': receipt['submission_id'],
               'bytes_removed': sum(row['bytes'] for row in removed),
               'retained': ['task.json', 'task-seed.json', 'state.json', 'recording-plan.json',
                            'run/attempts.json', 'run/key-identity.json', 'trace/', 'record/recording.mp4',
                            'record/recording.json', 'evidence/', 'review/']}
    lib.save_json(task_dir / 'cleanup-pending.json', cleanup)
    try:
        for original, moved in paths:
            moved.parent.mkdir(parents=True, exist_ok=True)
            os.replace(original, moved)
        lib.save_json(task_dir / 'cleanup.json', cleanup)
    finally:
        _recover_cleanup(root, task_dir)
    return removed


def _local_seal_problems(root: Path, batch_id: str, task_dir: Path,
                         manifest: dict, cfg: dict) -> list[str]:
    problems = []
    for name in ("run/attempts.json", "run/key-identity.json"):
        path = task_dir / name
        if not path.is_file() or path.is_symlink():
            problems.append(f"缺少保留文件：{name}")
    if not all(lib.stage_ok(task_dir, stage) for stage in lib.STAGES):
        problems.append("任务阶段尚未全部完成")
    if _task_active(task_dir):
        problems.append(_task_active(task_dir))
    snapshot = lib.load_json(task_dir / "review/snapshot.json", default={})
    require_remote = bool((cfg.get("cleanup") or {}).get("require_remote_snapshot", True))
    if require_remote and (snapshot.get("pushed") is not True or not snapshot.get("artifact_snapshot")):
        problems.append("产物快照尚未推送并回读")
    try:
        _verify_result_task(root, manifest, task_dir.name)
    except lib.FbrError as exc:
        problems.append(str(exc))
    return problems


def compact_local_task(root: Path, batch_id: str, task_dir: Path, cfg: dict,
                       *, force: bool = False, client=None) -> dict:
    """Maintenance and explicit pruning share one live verification and deletion path."""
    row = {'task_id': task_dir.name, 'bytes_removed': 0}
    if not retention.enabled(cfg, 'local', force=force):
        return {**row, 'state': 'disabled'}
    if not (task_dir / 'cleanup-pending.json').exists():
        if lib.load_json(task_dir / 'cleanup.json', default={}).get('state') == 'cleaned':
            return {**row, 'state': 'skip', 'reason': '已经清理'}
        blocker = _code_cleanup_blocker(task_dir, cached_status=False)
        if blocker:
            return {**row, 'state': 'blocked', 'reason': blocker}
    result = prune_batch(root, batch_id, task_dir.name, execute=True, confirm='CLEANUP', client=client)
    if result['cleaned']:
        return {**result['cleaned'][0], 'state': 'ok'}
    reason = next((r['reason'] for r in result['skipped']), '清理前复检未通过')
    return {**row, 'state': 'blocked', 'reason': reason}


def _task_terminal(task_dir: Path) -> tuple[bool, str]:
    if lib.platform_deprecated(task_dir):
        return True, "平台记录不可返修，已在本地标记废弃"
    state = lib.load_state(task_dir)["stages"]
    if state.get("run", {}).get("state") == "abandoned":
        return True, "运行已主动放弃"
    cleanup = lib.load_json(task_dir / "cleanup.json", default={})
    if cleanup.get("state") in ("local_compacted", "cleaned"):
        return True, "现场已压缩"
    current = lib.load_json(task_dir / "run/current.json", default={})
    if runtime.alive(current):
        return False, "任务仍在运行"
    return False, "尚未本地封存或清理"


def maintain_batch(root: Path, batch_id: str, cfg: dict, *,
                   execute: bool = False, tasks: str | None = None,
                   force: bool = False) -> dict:
    """Apply bounded retention at task and batch boundaries."""
    root = Path(root).resolve()
    manifest = lib.load_json(
        delivery.delivery_dir(root, batch_id) / "delivery-manifest.json", default={})
    task_dirs = lib.task_dirs(root, batch_id, tasks)
    all_task_dirs = lib.task_dirs(root, batch_id)
    rows = []
    for task_dir in task_dirs:
        if execute:
            try:
                row = compact_local_task(root, batch_id, task_dir, cfg, force=force)
            except (lib.FbrError, OSError, ValueError) as exc:
                row = {
                    "task_id": task_dir.name,
                    "state": "blocked",
                    "bytes_removed": 0,
                    "reason": str(exc),
                }
        else:
            candidates = _cleanup_candidates(root, task_dir)
            if lib.load_json(task_dir / "cleanup.json", default={}).get("state") == "cleaned":
                state, reason = "skip", "已经清理"
            elif not manifest.get("batch_id"):
                state, reason = "blocked", "缺少交付清单"
            else:
                problems = _local_seal_problems(root, batch_id, task_dir, manifest, cfg)
                platform_blocker = _code_cleanup_blocker(task_dir)
                if platform_blocker:
                    problems.append(platform_blocker)
                state = "ready" if not problems and candidates else "blocked"
                reason = "；".join(dict.fromkeys(problems)) if problems else (
                    "" if candidates else "没有可回收现场"
                )
            rows.append({
                "task_id": task_dir.name,
                "state": state,
                "reason": reason,
                "candidate_bytes": sum(retention.path_size(path) for path in candidates),
                "candidate_paths": [
                    str(path.relative_to(task_dir)) if path.is_relative_to(task_dir) else str(path)
                    for path in candidates
                ],
            })
            continue
        rows.append(row)
    history = None
    dependencies = None
    if execute:
        history = retention.trim_delivery_history(root, batch_id, cfg, force=force)
        terminal = [_task_terminal(task_dir) for task_dir in all_task_dirs]
        no_active = all(
            not runtime.alive(lib.load_json(task_dir / "run/current.json", default={}))
            for task_dir in all_task_dirs
        )
        if terminal and all(item[0] for item in terminal) and no_active:
            dependencies = retention.purge_dependencies(root, cfg, force=force)
    elif retention.enabled(cfg, "history", force=force):
        kept, removed = retention.history_candidates(
            root, batch_id, retention.history_keep(cfg))
        history = {
            "state": "preview",
            "kept": [str(path) for path in kept],
            "candidate_snapshots": len(removed),
            "candidate_bytes": sum(retention.path_size(path) for path in removed),
        }
    summary = {
        "batch": batch_id,
        "at": lib.now_iso(),
        "execute": execute,
        "policy": retention.policy(cfg),
        "tasks": rows,
        "bytes_removed": sum(int(row.get("bytes_removed", 0)) for row in rows),
        "history": history,
        "dependencies": dependencies,
    }
    if execute:
        lib.save_json(root / "_verification" / "retention" / f"{batch_id}-maintenance.json", summary)
    return summary


def hydrate_task(root: Path, batch_id: str, task: str) -> dict:
    task_dir = lib.task_dirs(root, batch_id, task)
    if len(task_dir) != 1:
        raise lib.FbrError("恢复 workspace 必须只指定一道题")
    return retention.ensure_workspace(task_dir[0])


def audit_batch(root: Path, batch_id: str, tasks: str | None = None, *, client=None) -> dict:
    manifest = lib.load_json(
        delivery.delivery_dir(root, batch_id) / "delivery-manifest.json", default={})
    if not manifest.get("batch_id"):
        raise lib.FbrError("缺少冻结的 delivery-manifest.json，不能确认清理前结果完整")
    if client is None:
        from fbr_platform import Client
        client = Client(account=fbr_submit.ACCOUNT)
    with fbr_submit.submit_lock(root):
        client.login()
        me = client.me()
        user = me.get("user", me)
        if user.get("username") != fbr_submit.ACCOUNT or user.get("must_change_password"):
            raise lib.FbrError("登录账号不符或需要人工修改初始密码")
        rows = []
        for task_dir in lib.task_dirs(root, batch_id, tasks):
            row = {"task_id": task_dir.name, "eligible": False, "reason": ""}
            try:
                if lib.load_json(task_dir / "cleanup.json", default={}).get("state") == "cleaned":
                    raise lib.FbrError("已经清理")
                if not (task_dir / "review/upload.json").is_file():
                    raise lib.FbrError("没有平台提交回执")
                active = _task_active(task_dir)
                if active:
                    raise lib.FbrError(active)
                _, plan, detail = _refresh_remote(client, root, task_dir, manifest)
                receipt = lib.load_json(task_dir / "review/upload.json")
                status = receipt.get("remote_status")
                if status in PENDING_STATUSES:
                    raise lib.FbrError(f"平台仍在质检：{status}")
                if status in FIX_STATUSES:
                    raise lib.FbrError(f"平台待返修：{status}")
                if status != "QC_PASSED":
                    raise lib.FbrError(f"平台状态不是 QC_PASSED：{status}")
                _verify_result_task(root, manifest, task_dir.name)
                trace = plan["files"]["trace_file"]
                remote_trace = receipt.get("remote_trace")
                if remote_trace and trace["sha256"] != remote_trace.get("sha256"):
                    raise lib.FbrError("原始轨迹与平台远程轨迹摘要不一致")
                candidates = _cleanup_candidates(root, task_dir)
                row.update({
                    "eligible": True,
                    "reason": "ready",
                    "submission_id": receipt.get("submission_id"),
                    "remote_status": status,
                    "candidate_bytes": sum(_path_size(path) for path in candidates),
                    "candidate_paths": [str(path.relative_to(task_dir))
                                        if path.is_relative_to(task_dir) else str(path)
                                        for path in candidates],
                })
            except (lib.FbrError, OSError, ValueError) as exc:
                row["reason"] = str(exc)
                from fbr_platform import PlatformError
                row["retryable"] = (
                    isinstance(exc, PlatformError)
                    or str(exc).startswith("平台仍在质检")
                )
            rows.append(row)
    return {"batch": batch_id, "tasks": rows,
            "eligible": sum(1 for row in rows if row["eligible"]),
            "candidate_bytes": sum(row.get("candidate_bytes", 0) for row in rows)}


def prune_batch(root: Path, batch_id: str, tasks: str | None = None, *, execute: bool = False,
                confirm: str = "", client=None) -> dict:
    if execute and confirm != "CLEANUP":
        raise lib.FbrError("真实清理需要 --execute --confirm CLEANUP")
    if execute and client is None:
        from fbr_platform import Client
        client = Client(account=fbr_submit.ACCOUNT)
    if execute:
        for task in lib.task_dirs(root, batch_id, tasks):
            with lib.file_lock(task / '.operation.lock'):
                _recover_cleanup(root.resolve(), task.resolve())
    audit = audit_batch(root, batch_id, tasks, client=client)
    if not execute:
        return audit
    manifest = lib.load_json(
        delivery.delivery_dir(root, batch_id) / "delivery-manifest.json", default={})
    if not manifest.get("batch_id"):
        raise lib.FbrError("缺少冻结的 delivery-manifest.json，不能执行清理")
    cleaned = []
    for item in audit["tasks"]:
        if not item.get("eligible"):
            continue
        task_dir = lib.batch_dir(root, batch_id) / item["task_id"]
        with lib.file_lock(task_dir / ".operation.lock"):
            manifest = lib.load_json(delivery.delivery_dir(root, batch_id) / 'delivery-manifest.json')
            problems = _local_seal_problems(root, batch_id, task_dir, manifest, lib.settings(root))
            if problems:
                item.update(eligible=False, reason='；'.join(problems))
                continue
            try:
                _, _, detail = _refresh_remote(client, root, task_dir, manifest)
            except (lib.FbrError, OSError, ValueError) as exc:
                item.update(eligible=False, reason=str(exc))
                continue
            receipt = lib.load_json(task_dir / 'review/upload.json')
            if detail.get("status") != "QC_PASSED":
                item.update(eligible=False, reason=f"清理前平台状态为 {detail.get('status')}")
                continue
            task = lib.load_task_readonly(task_dir)
            _write_retention(root, task_dir, manifest, task, receipt, detail)
            _verify_retention(root, task_dir.name, batch_id)
            candidates = _cleanup_candidates(root, task_dir)
            removed = _safe_remove(root, task_dir, candidates, receipt)
            cleaned.append({"task_id": task_dir.name, "removed": removed,
                            "bytes_removed": sum(row["bytes"] for row in removed)})
    summary = {
        "batch": batch_id,
        "at": lib.now_iso(),
        "cleaned": cleaned,
        "bytes_removed": sum(row["bytes_removed"] for row in cleaned),
        "skipped": [row for row in audit["tasks"] if not row.get("eligible")],
    }
    return summary


def verify_batch(root: Path, batch_id: str, tasks: str | None = None) -> dict:
    rows = []
    for task_dir in lib.task_dirs(root, batch_id, tasks):
        cleanup = lib.load_json(task_dir / "cleanup.json", default={})
        if cleanup.get("state") not in ("local_compacted", "cleaned"):
            continue
        row = {"task_id": task_dir.name, "valid": False, "errors": []}
        try:
            _verify_retention(root, task_dir.name, batch_id)
            required = (
                task_dir / "task.json", task_dir / "state.json",
                task_dir / "run/attempts.json", task_dir / "run/key-identity.json",
                task_dir / "trace/ids.json", task_dir / "record/recording.mp4",
                task_dir / "record/recording.json", task_dir / "evidence/facts.json",
                task_dir / "review/feedback.json", task_dir / "review/gate.json",
                task_dir / "review/snapshot.json",
            )
            feedback = lib.load_json(task_dir / "review/feedback.json", default={})
            if feedback.get("feedback_policy_version", 0) >= 2:
                required += (task_dir / "review/code-review.json",)
            if cleanup.get("state") == "cleaned":
                required += (task_dir / "review/upload.json",)
            missing = [str(path.relative_to(task_dir)) for path in required
                       if not path.is_file() or path.is_symlink()]
            if missing:
                raise lib.FbrError("缺少保留文件：" + "、".join(missing))
            row["valid"] = True
        except (lib.FbrError, OSError, ValueError) as exc:
            row["errors"].append(str(exc))
        rows.append(row)
    return {"batch": batch_id, "tasks": rows,
            "valid": all(row["valid"] for row in rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description="S11 task archive and process cleanup")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("restore", "audit", "prune", "verify", "maintain", "hydrate"):
        item = sub.add_parser(name)
        item.add_argument("--batch", required=True)
        item.add_argument("--tasks")
        if name == "prune":
            item.add_argument("--execute", action="store_true")
            item.add_argument("--confirm", default="")
        if name == "maintain":
            item.add_argument("--execute", action="store_true")
            item.add_argument("--confirm", default="")
        if name == "hydrate":
            item.add_argument("--task", required=True)
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        import fbr_submit
        fbr_submit.configure_account(root)
        if args.command == "restore":
            result = restore_frozen_projection(root, args.batch, args.tasks)
        elif args.command == "audit":
            result = audit_batch(root, args.batch, args.tasks)
        elif args.command == "prune":
            result = prune_batch(root, args.batch, args.tasks,
                                 execute=args.execute, confirm=args.confirm)
        elif args.command == "maintain":
            if args.execute and args.confirm != "MAINTAIN":
                raise lib.FbrError("真实维护需要 --execute --confirm MAINTAIN")
            result = maintain_batch(
                root, args.batch, lib.settings(root),
                execute=args.execute, tasks=args.tasks, force=args.execute,
            )
        elif args.command == "hydrate":
            result = hydrate_task(root, args.batch, args.task)
        else:
            result = verify_batch(root, args.batch, args.tasks)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
