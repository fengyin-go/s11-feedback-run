#!/usr/bin/env python3
"""交付投影：只复制白名单证据到 results，不搬 workspace 或运行状态。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath

import fbrlib as lib
import fbr_reviewer as reviewer


class DeliveryBlocked(lib.FbrError):
    """起跑 key 无法验证时，整次交付刷新必须停止。"""


SUMMARY_FIELDS = ("task_id", "batch", "repo_id", "prompt", "domain", "subdirection", "task_type", "difficulty",
                  "language", "frameworks", "harness", "harness_version", "os", "repro_level", "initial_commit",
                  "task_repo", "task_repo_full", "requirements")
FILES = ("trace/ids.json", "record/recording.mp4", "record/recording.json", "evidence/facts.json",
         "evidence/audit.json", "evidence/probes.jsonl", "review/feedback.json", "review/gate.json",
         "review/model-review.json", "review/quality.json", "review/field-revisions.json",
         "review/trace-analysis.json", "review/trace-check.json")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def delivery_dir(root: Path, batch_id: str) -> Path:
    root = Path(root).resolve()
    lib.batch_dir(root, batch_id)
    target = root / "results" / batch_id
    if (root / "results").is_symlink() or target.is_symlink():
        raise lib.FbrError("results 交付目录不能是符号链接")
    return target


def safe_file(base: Path, path: Path) -> bool:
    """不追随模型或人工放入的链接，也不允许跨题引用。"""
    base, path = Path(base).absolute(), Path(path).absolute()
    try:
        relative = path.relative_to(base)
    except ValueError:
        raise lib.FbrError("交付文件越出本题目录") from None
    current = base
    if base.is_symlink():
        raise lib.FbrError("交付源任务目录不能是符号链接")
    for part in relative.parts:
        if part in (".", ".."):
            raise lib.FbrError("交付文件不能跨任务引用")
        current /= part
        if current.is_symlink():
            raise lib.FbrError(f"交付白名单文件不能是符号链接：{relative.as_posix()}")
    return path.is_file()


def current_feedback(task_dir: Path) -> tuple[bool, str]:
    states = lib.load_state(task_dir)["stages"]
    info = states.get("feedback", {})
    if info.get("state") != "ok":
        return False, "Feedback 还没过闸门"
    doc = lib.load_json(task_dir / "review/feedback.json", default={})
    gate = lib.load_json(task_dir / "review/gate.json", default={})
    sha = hashlib.sha256(str(doc.get("feedback", "")).encode()).hexdigest()
    if sha != info.get("feedback_sha"):
        return False, "过闸门之后正文改过，要重新过一遍"
    if doc.get("score") != info.get("score"):
        return False, "过闸门之后分数改过，要重新过一遍"
    if (not gate.get("passed") or gate.get("feedback_sha") != sha or gate.get("score") != doc.get("score")
            or gate.get("task_id") != task_dir.name):
        return False, "Feedback 闸门文件与当前结果不一致"
    facts = lib.load_json(task_dir / "evidence/facts.json", default={})
    if gate.get("commit") != facts.get("commit"):
        return False, "Feedback 闸门对应的产物提交已变化"
    spot = lib.load_json(task_dir / "review/spotcheck.json", default={})
    if spot.get("feedback_sha") == sha and spot.get("state") == "退回":
        return False, "抽检被退回"
    problems = reviewer.provenance_errors(task_dir)
    if problems:
        return False, problems[0]
    return True, ""


def _session_file(task_dir: Path) -> str:
    ids = lib.load_json(task_dir / "trace/ids.json", default={})
    text = ids.get("session_file", "")
    if not text:
        return ""
    path = PurePosixPath(str(text))
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "projects" or path.suffix != ".jsonl":
        raise lib.FbrError(f"{task_dir.name}：主会话路径必须在本题 trace/projects 内")
    safe_file(task_dir, task_dir / "trace" / str(path))
    return path.as_posix()


def _source_files(task_dir: Path, feedback_sha: str) -> list[Path]:
    files = [task_dir / name for name in FILES if safe_file(task_dir, task_dir / name)]
    revision_sources = task_dir / 'evidence/field-revisions'
    if revision_sources.is_symlink():
        raise lib.FbrError('字段修订证据目录不能是符号链接')
    if revision_sources.is_dir():
        for path in sorted(revision_sources.rglob('*')):
            if path.is_symlink():
                raise lib.FbrError('字段修订证据不能是符号链接')
            if path.suffix in ('.json', '.log') and safe_file(task_dir, path):
                files.append(path)
    logs = task_dir / 'evidence/command-logs'
    if logs.is_symlink():
        raise lib.FbrError('证据日志目录不能是符号链接')
    if logs.exists():
        for path in sorted(logs.rglob('*')):
            if path.is_symlink():
                raise lib.FbrError('证据日志不能是符号链接')
            if path.is_file() and path.suffix == '.log' and safe_file(task_dir, path):
                files.append(path)
    projects = task_dir / "trace/projects"
    if projects.is_symlink():
        raise lib.FbrError(f"{task_dir.name}：trace/projects 不能是符号链接")
    if projects.is_dir():
        for folder, directories, names in os.walk(projects, followlinks=False):
            directories.sort()
            for name in directories:
                if (Path(folder) / name).is_symlink():
                    raise lib.FbrError(f"{task_dir.name}：轨迹子目录不能是符号链接")
            for name in sorted(names):
                path = Path(folder) / name
                if path.suffix == ".jsonl" and safe_file(task_dir, path):
                    files.append(path)
    spot_path = task_dir / "review/spotcheck.json"
    if safe_file(task_dir, spot_path) and lib.load_json(spot_path).get("feedback_sha") == feedback_sha:
        files.append(spot_path)
    return sorted(files)


def _verify_files(root: Path, target: Path, manifest: dict) -> bool:
    expected = {"delivery-manifest.json", "result.xlsx", "preview.html"}
    try:
        for task in manifest.get("tasks", []):
            for item in task["files"]:
                path = root / item["path"]
                if not safe_file(target, path) or digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
                    return False
                expected.add(path.relative_to(target).as_posix())
        for folder, dirs, files in os.walk(target, followlinks=False):
            if any((Path(folder) / d).is_symlink() for d in dirs):
                return False
            for name in files:
                path = Path(folder) / name
                if path.is_symlink() or path.relative_to(target).as_posix() not in expected:
                    return False
        return True
    except (OSError, KeyError, lib.FbrError, ValueError):
        return False


def export_batch(root: Path, batch_id: str, cfg: dict | None = None) -> dict:
    """完整刷新一个批次；旧投影移入验证历史，不改源任务或状态。"""
    root = Path(root).resolve()
    target = delivery_dir(root, batch_id)
    cfg = cfg or lib.settings(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{batch_id}.export-", dir=target.parent))
    tasks, excluded, pending_copies = [], [], []
    try:
        for source in lib.task_dirs(root, batch_id):
            try:
                # 先核对所有会读的控制文件，避免先读入跨题链接。
                for name in ("state.json", "task.json", *FILES, "review/spotcheck.json", "review/snapshot.json", "run/attempts.json"):
                    safe_file(source, source / name)
                valid, reason = current_feedback(source)
                if not valid:
                    excluded.append({"task_id": source.name, "reason": reason})
                    continue
                import fbr_fields
                record = fbr_fields.effective_record(source)
                if not lib.TASK_ID_RE.fullmatch(source.name):
                    raise lib.FbrError("交付任务编号格式不合法")
                # 不修改 gate 或状态；再用当前事实执行同一套单题证据规则。
                import fbr_feedback
                checked = fbr_feedback.gate_task(source, cfg)
                if not checked.get("ready") or checked.get("errors"):
                    raise lib.FbrError("当前证据已不能通过 Feedback 闸门：" + "；".join(checked.get("errors", [])[:5]))
                import fbr_run
                try:
                    secret = fbr_run.bound_key(source, cfg)
                except lib.FbrError as exc:
                    raise DeliveryBlocked(f"{source.name}：无法用起跑 key 核验交付文件，停止整次导出；{exc}") from None
                _session_file(source)
                feedback_sha = lib.load_state(source)["stages"]["feedback"]["feedback_sha"]
                dest = staging / source.name
                dest.mkdir()
                files = _source_files(source, feedback_sha)
                inputs = {str(p.relative_to(source)): digest(p) for p in files}
                gateway_key_files = []
                for name in ("state.json", "task.json", "review/snapshot.json", "run/attempts.json"):
                    path = source / name
                    if safe_file(source, path):
                        inputs[name] = digest(path)
                entries = []
                for path in files:
                    if secret and lib.key_occurrences(path, secret):
                        gateway_key_files.append(path.relative_to(source).as_posix())
                    relative = path.relative_to(source)
                    copied = dest / relative
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    pending_copies.append((path,copied))
                    entries.append({"path": (target / source.name / relative).relative_to(root).as_posix(),
                                    "bytes": path.stat().st_size, "sha256": inputs[str(relative)]})
                summary = {k: record[k] for k in SUMMARY_FIELDS if k in record}
                summary["publish"] = {k: v for k, v in (record.get("publish") or {}).items()
                                      if k in ("state", "repo_url", "initial_snapshot")}
                summary_path = dest / "task-summary.json"
                lib.save_json(summary_path, summary)
                entries.append({"path": (target / source.name / "task-summary.json").relative_to(root).as_posix(),
                                "bytes": summary_path.stat().st_size, "sha256": digest(summary_path)})
                tasks.append({"task_id": source.name, "feedback_sha": feedback_sha,
                              "source_digest": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
                              "api_key_files": gateway_key_files,
                              "files": entries})
            except (lib.FbrError, OSError) as exc:
                if isinstance(exc, (lib.KeyLeakError, DeliveryBlocked)):
                    raise
                shutil.rmtree(staging / source.name, ignore_errors=True)
                excluded.append({"task_id": source.name, "reason": str(exc)})
        manifest = {"schema_version": 1, "batch_id": batch_id, "tasks": tasks, "excluded": excluded}
        old = lib.load_json(target / "delivery-manifest.json", default={})
        if all(old.get(k) == manifest[k] for k in manifest) and _verify_files(root, target, old):
            return old
        import subprocess
        for source,copied in pending_copies:
            if not copied.parent.exists(): continue  # task was excluded after preparing entries
            # APFS CoW retains independent files and history without re-reading unchanged videos.
            old_file=target/copied.relative_to(staging)
            original=old_file if old_file.is_file() and not old_file.is_symlink() and digest(old_file)==digest(source) else source
            done=subprocess.run(['cp','-c',str(original),str(copied)],capture_output=True) if __import__('sys').platform=='darwin' else None
            if done is None or done.returncode:shutil.copyfile(source,copied)
            if digest(copied)!=digest(source):raise lib.FbrError('导出期间来源发生变化，拒绝发布混合版本')
        manifest["generated_at"] = lib.now_iso()
        lib.save_json(staging / "delivery-manifest.json", manifest)
        if target.exists():
            history = root / "_verification" / "delivery-history"
            history.mkdir(parents=True, exist_ok=True)
            os.replace(target, history / f"{batch_id}-{uuid.uuid4().hex[:12]}")
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def exported_task(root: Path, batch_id: str, task_id: str, manifest: dict | None = None) -> Path | None:
    target = delivery_dir(root, batch_id)
    manifest = manifest or lib.load_json(target / "delivery-manifest.json", default={})
    if not lib.TASK_ID_RE.fullmatch(task_id) or task_id not in {r["task_id"] for r in manifest.get("tasks", [])}:
        return None
    return target / task_id
