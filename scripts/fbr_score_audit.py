#!/usr/bin/env python3
"""Evidence-only audit for candidates whose score may move from 4 to 3."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbr_feedback
import fbr_gate as gate
import fbrlib as lib

CLASSES = {
    "required_path_failure",
    "compatibility_regression",
    "idempotency_failure",
    "mild_false_success",
    "combined_detail_block",
}
INFRA_CODES = {124, 126, 127, 137}
FOCUS = ("并发", "同时", "幂等", "重复", "中断", "恢复", "回滚", "重试", "同步",
         "一致", "导出", "校验", "非法", "冲突", "空态", "边界", "删除")


def _load(task: Path):
    record, facts = fbr_feedback.load_evidence(task)
    return (record, facts, lib.load_json(task / "evidence/audit.json"),
            lib.load_json(task / "review/feedback.json"),
            lib.read_jsonl(task / "evidence/probes.jsonl"))


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else ""


def scan_task(task: Path, cfg: dict) -> dict | None:
    try:
        record, facts, audit, doc, probes = _load(task)
    except (lib.FbrError, OSError, ValueError):
        return None
    if doc.get("score") != 4:
        return None
    ev = gate.build_evidence(record, facts, audit, probes)
    cap, reasons = gate.score_cap(facts, audit, ev, cfg["feedback_gate"])
    details = [r for r in audit.get("requirements", [])
               if isinstance(r, dict) and r.get("verdict") == "有问题" and r.get("level") == "细节"]
    valid_probes = gate.valid_counterexamples(audit, ev)
    process_text = " ".join(str(row.get("sentence", "")) for row in doc.get("process", [])
                            if isinstance(row, dict))
    text = str(doc.get("feedback", ""))
    classes = []
    if details:
        classes.append("detail_severity_review")
    if any(row.get("found_problem") for row in valid_probes):
        classes.append("counterexample_problem")
    if len(valid_probes) < int(cfg["feedback_gate"]["min_counterexamples"]):
        classes.append("verification_gap")
    if cap == 5 and (doc.get("score_basis") or {}).get("category") == "process_issue":
        classes.append("process_only")
    if any(word in process_text + text for word in ("没有真正", "未实测", "仍未成立", "没有实际",
                                                    "只记录了", "标记为跳过", "未实际")):
        classes.append("explicit_unverified")
    suggestions = []
    prompt = str(record.get("prompt", ""))
    for requirement in record.get("requirements", []):
        value = f"{requirement.get('text', '')} {requirement.get('check', '')}"
        focus = [word for word in FOCUS if word in value]
        if focus:
            suggestions.append({"requirement_id": str(requirement.get("id")),
                                "focus": focus,
                                "check": requirement.get("check", "")})
    priority = 0
    for name, weight in (("counterexample_problem", 50), ("detail_severity_review", 40),
                         ("explicit_unverified", 35), ("verification_gap", 20),
                         ("process_only", 10)):
        if name in classes:
            priority += weight
    return {
        "task_id": task.name,
        "batch": task.parent.name,
        "score": 4,
        "category": (doc.get("score_basis") or {}).get("category"),
        "cap": cap,
        "cap_reasons": reasons,
        "classes": classes,
        "valid_probes": len(valid_probes),
        "detail_requirements": [{"id": row.get("id"), "note": row.get("note", "")} for row in details],
        "priority": priority,
        "probe_suggestions": suggestions,
    }


def scan_batch(root: Path, batch: str, wanted: str | None = None) -> list[dict]:
    cfg = lib.settings(root)
    rows = []
    for task in lib.task_dirs(root, batch, wanted):
        row = scan_task(task, cfg)
        if row:
            rows.append(row)
    return sorted(rows, key=lambda row: (-row["priority"], row["task_id"]))


def _probe(task: Path, cfg: dict, side: str, command: str, timeout: int, with_setup: bool) -> dict:
    record, facts = fbr_feedback.load_evidence(task)
    commit = record["initial_commit"] if side == "baseline" else facts["commit"]
    clone = lib.clean_clone(task / "workspace", commit, f"s11-score-audit-{side}")
    steps = [{"name": "probe", "cmd": command, "timeout": timeout}]
    if with_setup:
        steps.insert(0, {"name": "setup", "cmd": cfg["facts"]["setup_cmd"],
                         "timeout": cfg["facts"]["setup_timeout"]})
    rows = lib.run_steps_in_image(record["image"], clone, steps, tail=100)
    done = rows[-1]
    if side == "artifact":
        path = task / "evidence/probes.jsonl"
        number = len(lib.read_jsonl(path)) + 1
        probe_id = f"P{number}"
    else:
        path = task / "evidence/downgrade-probes.jsonl"
        number = len(lib.read_jsonl(path)) + 1
        probe_id = f"D{number}"
    row = {
        "id": probe_id,
        "side": side,
        "task_id": task.name,
        "commit": commit,
        "cmd": command,
        "rc": done.get("rc"),
        "seconds": done.get("seconds"),
        "setup_rc": rows[0].get("rc") if with_setup else None,
        "tail": lib.redact(done.get("tail", ""), [lib.read_key(cfg)]),
        "container_name": done.get("container_name", ""),
        "clone": str(clone),
        "at": lib.now_iso(),
    }
    if done.get("output_file"):
        target = task / "evidence/command-logs/downgrade" / f"{row['id']}.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(done["output_file"], target)
        row.update(output_file=str(target.relative_to(task)), output_sha256=done.get("output_sha256"))
    lib.append_jsonl(path, row)
    return row


def record_evidence(task: Path, args) -> dict:
    probes = {row.get("id"): row for row in lib.read_jsonl(task / "evidence/downgrade-probes.jsonl")}
    probes.update({row.get("id"): row for row in fbr_feedback.load_probes(task)})
    for probe_id in (args.baseline_probe, args.artifact_probe):
        if probe_id and probe_id not in probes:
            raise lib.FbrError(f"没有编号 {probe_id} 的降分 probe")
    anchors = [json.loads(value) for value in args.anchor]
    doc = {
        "from_score": 4,
        "proposed_score": 3,
        "downgrade_class": args.downgrade_class,
        "requirement_ids": [item.strip() for item in args.requirements.split(",") if item.strip()],
        "baseline_probe": args.baseline_probe,
        "artifact_probe": args.artifact_probe,
        "expected": args.expected,
        "actual": args.actual,
        "user_impact": args.user_impact,
        "workaround": args.workaround,
        "anchors": anchors,
        "reviewed_by": "current_task_agent",
        "reviewed_at": lib.now_iso(),
    }
    lib.save_json(task / "review/downgrade-review.json", doc)
    return doc


def validate_evidence(task: Path, cfg: dict) -> dict:
    path = task / "review/downgrade-review.json"
    doc = lib.load_json(path)
    errors = []
    if doc.get("from_score") != 4 or doc.get("proposed_score") != 3:
        errors.append("降分范围只能是 4 -> 3")
    if doc.get("downgrade_class") not in CLASSES:
        errors.append("downgrade_class 不在允许范围内")
    record, facts, audit, feedback, _ = _load(task)
    if feedback.get("score") != 4:
        errors.append("当前 Feedback 不是 4 分，不能使用 4 -> 3 流程")
    req_ids = {str(row.get("id")) for row in record.get("requirements", [])}
    selected = {str(item) for item in doc.get("requirement_ids", [])}
    if not selected or not selected <= req_ids:
        errors.append("requirement_ids 必须选择本题真实需求")
    for key in ("expected", "actual", "user_impact", "workaround"):
        value = str(doc.get(key) or "").strip()
        if len(value) < (1 if key == "workaround" else 4):
            errors.append(f"{key} 必须写清楚")
    probes = {row.get("id"): row for row in
              lib.read_jsonl(task / "evidence/downgrade-probes.jsonl")}
    probes.update({row.get("id"): row for row in fbr_feedback.load_probes(task)})
    baseline = probes.get(doc.get("baseline_probe"))
    artifact = probes.get(doc.get("artifact_probe"))
    if artifact is None and doc.get("downgrade_class") != "mild_false_success":
        errors.append("缺少产物侧失败 probe")
    elif artifact is not None:
        if artifact.get("side") != "artifact" or artifact.get("setup_rc") != 0:
            errors.append("产物 probe 必须 setup 成功且在 artifact 侧执行")
        if artifact.get("rc") in INFRA_CODES:
            errors.append("产物 probe 是基础设施或超时失败，不能据此降分")
        if artifact.get("rc") == 0:
            errors.append("产物 probe 没有复现失败，不能降分")
    if doc.get("downgrade_class") == "compatibility_regression":
        if baseline is None or baseline.get("side") != "baseline" or baseline.get("rc") != 0:
            errors.append("兼容回归必须由 baseline 成功、artifact 失败的同一 probe 证明")
    if doc.get("downgrade_class") == "combined_detail_block":
        if len(selected) < 2:
            errors.append("combined_detail_block 至少关联两个需求")
    ev = gate.build_evidence(record, facts, audit, lib.read_jsonl(task / "evidence/probes.jsonl"))
    anchors = doc.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        errors.append("降分证据包缺少 anchors")
    else:
        errors += gate._anchor_problems(anchors, ev, "降分证据")
        if doc.get("downgrade_class") == "mild_false_success" and not any(
                isinstance(anchor, dict) and anchor.get("type") in ("missing", "file", "test", "log")
                for anchor in anchors):
            errors.append("轻微虚假成功必须给出缺失文件、实际代码、失败用例或原始日志锚点")
    action = {"suggested_score": 3}
    if doc.get("downgrade_class") == "mild_false_success":
        action["suggested_false_success"] = "轻微"
    elif doc.get("downgrade_class") == "required_path_failure":
        action["suggested_audit_level"] = "明显"
    else:
        action["suggested_audit_level"] = "明显"
    result = {
        "task_id": task.name,
        "eligible": not errors,
        "errors": errors,
        **action,
        "evidence": str(path),
        "evidence_sha256": _sha(path),
        "artifact_probe": doc.get("artifact_probe", ""),
        "probes_sha256": _sha(task / "evidence/probes.jsonl"),
        "downgrade_probes_sha256": _sha(task / "evidence/downgrade-probes.jsonl"),
    }
    lib.save_json(task / "review/downgrade-validation.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="4 -> 3 分证据审计")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--batch", required=True)
    scan.add_argument("--tasks")
    for name in ("probe", "record", "validate"):
        item = sub.add_parser(name)
        item.add_argument("--batch", required=True)
        item.add_argument("--task", required=True)
    probe = sub.choices["probe"]
    probe.add_argument("--side", choices=("baseline", "artifact", "both"), default="both")
    probe.add_argument("--cmd", required=True)
    probe.add_argument("--timeout", type=int, default=180)
    probe.add_argument("--no-setup", action="store_true")
    record = sub.choices["record"]
    record.add_argument("--class", dest="downgrade_class", choices=sorted(CLASSES), required=True)
    record.add_argument("--requirements", required=True)
    record.add_argument("--baseline-probe")
    record.add_argument("--artifact-probe")
    record.add_argument("--expected", required=True)
    record.add_argument("--actual", required=True)
    record.add_argument("--user-impact", required=True)
    record.add_argument("--workaround", default="无")
    record.add_argument("--anchor", action="append", default=[])
    for item in (scan, probe, record, sub.choices["validate"]):
        item.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        cfg = lib.settings(root)
        if args.command == "scan":
            rows = scan_batch(root, args.batch, args.tasks)
            target = root/'_verification/score-audit'/f'{args.batch}.json'
            lib.save_json(target, {'version': 1, 'batch': args.batch, 'at': lib.now_iso(), 'rows': rows})
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"4→3 候选 {len(rows)} 题；完整报告 {target}")
                for row in rows[:8]:
                    print(f"- {row['task_id']} priority={row['priority']} "
                          f"classes={','.join(row['classes'])} cap={row['cap']}")
                if len(rows) > 8:
                    print(f"- 其余 {len(rows)-8} 题见报告")
            return 0
        task = lib.task_dirs(root, args.batch, args.task)[0]
        if args.command == "probe":
            sides = ("baseline", "artifact") if args.side == "both" else (args.side,)
            rows = [_probe(task, cfg, side, args.cmd, args.timeout, not args.no_setup)
                    for side in sides]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['id']} {row['side']} rc={row['rc']} "
                          f"{lib.clip((row.get('tail') or '').replace(chr(10), ' / '), 240)}")
            return 0 if all(row.get("rc") is not None for row in rows) else 2
        if args.command == "record":
            result = record_evidence(task, args)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else f"{task.name} {result['downgrade_class']} 4→3 证据已记录")
            return 0
        result = validate_evidence(task, cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else f"{task.name} eligible={result['eligible']} errors={len(result['errors'])}")
        return 0 if result["eligible"] else 2
    except (lib.FbrError, OSError, ValueError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
