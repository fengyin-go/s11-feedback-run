#!/usr/bin/env python3
"""Freeze an accepted review and verify that later submission checks did not change it."""
from __future__ import annotations

import json
from pathlib import Path

import fbrlib as lib

VERSION = 1
FILE = "review/attestation.json"
BATCH_FILE = "batch-closeout.json"

REVIEW_FILES = (
    "task.json",
    "task-seed.json",
    "recording-plan.json",
    "recording-plan.override.json",
    "trace/ids.json",
    "trace/trace-check.json",
    "evidence/facts.json",
    "evidence/audit.json",
    "evidence/probes.jsonl",
    "review/feedback.json",
    "review/model-review.json",
    "review/quality.json",
    "review/style-report.json",
    "review/trace-analysis.json",
    "review/review-capsule.json",
)

DELIVERY_FILES = (
    "review/field-revisions.json",
    "review/spotcheck.json",
    "review/snapshot.json",
    "record/recording.mp4",
    "record/recording.json",
    "run/attempts.json",
)

LEGACY_DELIVERY_INPUTS = {"state.json"}


def path(task: Path) -> Path:
    return Path(task) / FILE


def feedback_sha(task: Path) -> str:
    doc = lib.load_json(Path(task) / "review/feedback.json", default={})
    return lib.content_hash(str(doc.get("feedback", "")))


def _safe(task: Path, relative: str) -> Path:
    import fbr_delivery

    target = Path(task) / relative
    if not fbr_delivery.safe_file(Path(task), target):
        raise lib.FbrError(f"评审凭证输入缺失：{relative}")
    return target


def _trace_files(task: Path) -> list[str]:
    projects = Path(task) / "trace/projects"
    if not projects.is_dir() or projects.is_symlink():
        raise lib.FbrError("评审凭证缺少原始轨迹目录")
    rows = []
    for item in sorted(projects.rglob("*.jsonl")):
        if item.is_symlink():
            raise lib.FbrError("原始轨迹不能是符号链接")
        rows.append(item.relative_to(task).as_posix())
    if not rows:
        raise lib.FbrError("评审凭证缺少原始轨迹文件")
    return rows


def _command_logs(task: Path) -> list[str]:
    root = Path(task) / "evidence/command-logs"
    if not root.exists():
        return []
    if not root.is_dir() or root.is_symlink():
        raise lib.FbrError("证据日志目录无效")
    rows = []
    for item in sorted(root.rglob("*.log")):
        if item.is_symlink():
            raise lib.FbrError("证据日志不能是符号链接")
        rows.append(item.relative_to(task).as_posix())
    return rows


def _hashes(task: Path, names: tuple[str, ...]) -> dict[str, str]:
    import fbr_delivery

    rows = {}
    for name in names:
        target = Path(task) / name
        if target.is_file():
            rows[name] = fbr_delivery.digest(_safe(task, name))
    return rows


def _review_hashes(task: Path) -> dict[str, str]:
    import fbr_delivery

    rows = _hashes(task, REVIEW_FILES)
    for name in (*_trace_files(task), *_command_logs(task)):
        rows[name] = fbr_delivery.digest(_safe(task, name))
    return dict(sorted(rows.items()))


def _delivery_hashes(task: Path) -> dict[str, str]:
    import fbr_delivery

    rows = _hashes(task, DELIVERY_FILES)
    root = Path(task) / "evidence/field-revisions"
    if root.exists():
        if not root.is_dir() or root.is_symlink():
            raise lib.FbrError("字段修订证据目录无效")
        for item in sorted(root.rglob("*")):
            if item.is_symlink():
                raise lib.FbrError("字段修订证据不能是符号链接")
            if item.is_file() and item.suffix in (".json", ".log"):
                name = item.relative_to(task).as_posix()
                rows[name] = fbr_delivery.digest(_safe(task, name))
    return dict(sorted(rows.items()))


def _plan(task: Path, root: Path, cfg: dict, manifest: dict | None = None) -> dict:
    import fbr_delivery
    import fbr_sheet
    import fbr_submit

    manifest = manifest or lib.load_json(
        fbr_delivery.delivery_dir(root, task.parent.name) / "delivery-manifest.json",
        default={},
    )
    row, reason = fbr_sheet.build_row(root, task, cfg, delivery_manifest=manifest)
    if row is None:
        raise lib.FbrError(reason or "评审凭证无法生成提交行")
    ids = lib.load_json(task / "trace/ids.json", default={})
    snapshot = lib.load_json(task / "review/snapshot.json", default={})
    problems = fbr_sheet.row_problems(row, ids, snapshot)
    if problems:
        raise lib.FbrError("评审凭证的交付字段不完整：" + "；".join(problems))
    return fbr_submit.plan_from_row(root, task, row, manifest)


def materialize(task: Path, root: Path, cfg: dict, *,
                manifest: dict | None = None, force: bool = False) -> dict:
    """Create a content-bound attestation from an already accepted local review."""
    task = Path(task)
    import fbr_delivery

    if not force:
        current = validate(task, root, cfg, manifest=manifest)
        if current["valid"]:
            return current["attestation"]
    if not lib.stage_ok(task, "feedback"):
        raise lib.FbrError("Feedback 还没过闸门，不能生成评审凭证")
    review_inputs = _review_hashes(task)
    prior = lib.load_json(path(task), default={})
    trusted_prior = (
        isinstance(prior, dict)
        and prior.get("state") == "accepted"
        and prior.get("review_inputs") == review_inputs
        and prior.get("review_digest") == lib.content_hash(review_inputs)
    )
    if not trusted_prior:
        valid, reason = fbr_delivery.current_feedback(task)
        if not valid:
            raise lib.FbrError(reason)
    manifest = manifest or lib.load_json(
        fbr_delivery.delivery_dir(root, task.parent.name) / "delivery-manifest.json",
        default={},
    )
    if not any(row.get("task_id") == task.name for row in manifest.get("tasks", [])):
        manifest = fbr_delivery.export_batch(root, task.parent.name, cfg)
    plan = _plan(task, root, cfg, manifest)
    feedback = lib.load_json(task / "review/feedback.json")
    facts = lib.load_json(task / "evidence/facts.json")
    delivery_inputs = _delivery_hashes(task)
    document = {
        "version": VERSION,
        "task_id": task.name,
        "batch": task.parent.name,
        "state": "accepted",
        "review_digest": lib.content_hash(review_inputs),
        "delivery_digest": lib.content_hash(delivery_inputs),
        "review_inputs": review_inputs,
        "delivery_inputs": delivery_inputs,
        "feedback_sha": feedback_sha(task),
        "score": feedback.get("score"),
        "commit": facts.get("commit"),
        "source_digest": plan["binding"]["source_digest"],
        "content_digest": plan["content_digest"],
        "artifact_snapshot": plan["data"].get("artifact_snapshot"),
        "session_id": plan["data"].get("session_id"),
        "platform": plan["binding"]["platform"],
        "account": plan["binding"]["account"],
        "created_at": lib.now_iso(),
    }
    lib.save_json(path(task), document)
    return document


def validate(task: Path, root: Path, cfg: dict, *, manifest: dict | None = None) -> dict:
    task = Path(task)
    target = path(task)
    if not target.is_file() or target.is_symlink():
        return {"valid": False, "state": "missing", "reason": "缺少评审凭证，需要刷新交付凭证", "attestation": {}}
    try:
        document = lib.load_json(target)
        if not isinstance(document, dict) or document.get("version") != VERSION:
            raise lib.FbrError("评审凭证版本无效")
        if document.get("task_id") != task.name or document.get("batch") != task.parent.name:
            raise lib.FbrError("评审凭证任务身份不一致")
        if not lib.stage_ok(task, "feedback"):
            raise lib.FbrError("Feedback 阶段不是通过状态")
        review_inputs = _review_hashes(task)
        if review_inputs != document.get("review_inputs"):
            return {
                "valid": False,
                "state": "review_changed",
                "reason": "评审输入在执行后发生变化，需要重新评价",
                "attestation": document,
            }
        delivery_inputs = _delivery_hashes(task)
        recorded_delivery = document.get("delivery_inputs")
        comparable_delivery = (
            {key: value for key, value in recorded_delivery.items()
             if key not in LEGACY_DELIVERY_INPUTS}
            if isinstance(recorded_delivery, dict) else {}
        )
        if delivery_inputs != comparable_delivery:
            return {
                "valid": False,
                "state": "delivery_changed",
                "reason": "交付附件或产物快照发生变化，需要重做交付核查",
                "attestation": document,
            }
        import fbr_delivery

        valid, reason = fbr_delivery.current_feedback(task)
        if not valid:
            raise lib.FbrError(reason)
        if lib.content_hash(review_inputs) != document.get("review_digest"):
            raise lib.FbrError("评审凭证内部摘要不一致")
        allowed_delivery_digests = {lib.content_hash(delivery_inputs)}
        if isinstance(recorded_delivery, dict):
            allowed_delivery_digests.add(lib.content_hash(recorded_delivery))
        if document.get("delivery_digest") not in allowed_delivery_digests:
            raise lib.FbrError("交付凭证内部摘要不一致")
        plan = _plan(task, root, cfg, manifest)
        feedback = lib.load_json(task / "review/feedback.json")
        facts = lib.load_json(task / "evidence/facts.json")
        checks = {
            "source_digest": plan["binding"]["source_digest"],
            "content_digest": plan["content_digest"],
            "feedback_sha": feedback_sha(task),
            "score": feedback.get("score"),
            "commit": facts.get("commit"),
            "session_id": plan["data"].get("session_id"),
            "artifact_snapshot": plan["data"].get("artifact_snapshot"),
            "platform": plan["binding"]["platform"],
            "account": plan["binding"]["account"],
        }
        for key, value in checks.items():
            if document.get(key) != value:
                if key in {"source_digest", "content_digest", "artifact_snapshot"}:
                    return {
                        "valid": False,
                        "state": "delivery_changed",
                        "reason": "交付投影发生变化，需要重做交付核查",
                        "attestation": document,
                    }
                raise lib.FbrError(f"评审凭证与当前交付不一致：{key}")
        return {"valid": True, "state": "accepted", "reason": "", "attestation": document}
    except (lib.FbrError, KeyError, OSError, TypeError, ValueError) as exc:
        reason = str(exc) or type(exc).__name__
        return {"valid": False, "state": "invalid", "reason": reason, "attestation": {}}


def refresh_delivery(task: Path, root: Path, cfg: dict, *, manifest: dict | None = None) -> dict:
    current = validate(task, root, cfg, manifest=manifest)
    if current["valid"]:
        return current["attestation"]
    if current["state"] != "delivery_changed":
        raise lib.FbrError(current["reason"])
    return materialize(task, root, cfg, manifest=manifest, force=True)


def batch_closeout(root: Path, batch: str, cfg: dict, tasks: str | None = None, *,
                   manifest: dict | None = None, write: bool = True) -> dict:
    """Run deterministic submission checks without asking an AI to review again."""
    import fbr_delivery
    import fbr_feedback
    import fbr_gate

    selected = lib.task_dirs(root, batch, tasks)
    manifest = manifest or fbr_delivery.export_batch(root, batch, cfg)
    rows, errors = [], []
    records = {}
    sessions: dict[str, str] = {}
    domains = {}
    for task in selected:
        try:
            deprecated = lib.platform_deprecated(task)
            if deprecated:
                rows.append({
                    "task_id": task.name,
                    "state": "deprecated",
                    "reason": deprecated.get("reason"),
                    "submission_id": deprecated.get("submission_id"),
                })
                continue
            feedback = lib.load_json(task / "review/feedback.json", default={})
            if str(feedback.get("feedback", "")).strip():
                records[task.name] = str(feedback["feedback"])
            attestation = validate(task, root, cfg, manifest=manifest)
            if not attestation["valid"] and attestation["state"] == "missing":
                materialize(task, root, cfg, manifest=manifest)
                attestation = validate(task, root, cfg, manifest=manifest)
            elif not attestation["valid"] and attestation["state"] == "delivery_changed":
                refresh_delivery(task, root, cfg, manifest=manifest)
                attestation = validate(task, root, cfg, manifest=manifest)
            if not attestation["valid"]:
                raise lib.FbrError(attestation["reason"])
            doc = attestation["attestation"]
            session_id = str(doc.get("session_id") or "")
            if session_id in sessions:
                raise lib.FbrError(f"SessionID 与 {sessions[session_id]} 重复")
            if session_id:
                sessions[session_id] = task.name
            domains[task.name] = _plan(task, root, cfg, manifest)["data"].get("domain")
            rows.append({
                "task_id": task.name,
                "state": "ready",
                "attestation_digest": lib.content_hash(doc),
                "source_digest": doc.get("source_digest"),
                "content_digest": doc.get("content_digest"),
                "feedback_sha": doc.get("feedback_sha"),
                "artifact_snapshot": doc.get("artifact_snapshot"),
            })
        except (lib.FbrError, KeyError, OSError, TypeError, ValueError) as exc:
            errors.append({"task_id": task.name, "error": str(exc) or type(exc).__name__})
            rows.append({"task_id": task.name, "state": "blocked"})
    if len({value for value in domains.values() if value}) > 1:
        message = "所选任务跨领域，一个平台账号不能一起提交"
        errors.append({"task_id": "*", "error": message})
    duplicates, _ = fbr_gate.check_duplicates(
        records, fbr_feedback.other_feedback(root, batch), cfg["feedback_gate"])
    selected_ids = {task.name for task in selected}
    for task_id, items in duplicates.items():
        if task_id not in selected_ids or not items:
            continue
        message = "；".join(items)
        errors.append({"task_id": task_id, "error": message})
        for row in rows:
            if row["task_id"] == task_id:
                row["state"] = "blocked"
    result = {
        "version": 1,
        "batch": batch,
        "state": "ready" if not errors else "blocked",
        "checked_at": lib.now_iso(),
        "tasks": rows,
        "errors": errors,
        "counts": {"selected": len(selected), "ready": sum(row["state"] == "ready" for row in rows),
                   "blocked": sum(row["state"] == "blocked" for row in rows),
                   "deprecated": sum(row["state"] == "deprecated" for row in rows)},
    }
    if write:
        lib.save_json(fbr_delivery.delivery_dir(root, batch) / BATCH_FILE, result)
    return result
