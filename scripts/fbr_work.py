#!/usr/bin/env python3
"""Return one compact current-task-AI work packet at a time."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import fbr_evidence
import fbrlib as lib


def stage_brief(task: Path) -> Path:
    skill = Path(__file__).resolve().parent.parent
    if not lib.stage_ok(task, 'commit'):
        name = 'run.md'
    elif not lib.stage_ok(task, 'feedback'):
        name = 'review.md'
    else:
        name = 'submit.md'
    return skill/'references/stage-briefs'/name


def _task_packet(task: Path) -> dict | None:
    work_path = task / "review/task-agent-work.json"
    if not work_path.is_file() or lib.stage_ok(task, "feedback"):
        return None
    work = lib.load_json(work_path)
    check = lib.load_json(task / fbr_evidence.CHECK, default={})
    packet = {
        "task_id": task.name,
        "task_path": str(task),
        "queued_at": str(work.get("at") or ""),
        "brief": str(task / "review/task-agent-brief.md"),
        "checkpoint": str(work_path),
        "trace_pack": str(task / fbr_evidence.PACK),
        "trace_check": str(task / fbr_evidence.CHECK),
        "candidate_schema": str(task / "review/candidate-schema.json"),
        "decision_input": str(task / "review/decision-input.json"),
        "stage_brief": str(stage_brief(task)),
        "counts": check.get("counts", {}),
        "repair_errors": work.get("repair_errors", [])[:20],
        "next_steps": work.get("next_steps", []),
        "evidence_rule": "只读 brief 和事件包；原始轨迹用 fbr_evidence.py fetch 按行/block查询，不整文件读取",
    }
    contract = task / "review-contract.json"
    if contract.is_file() and not contract.is_symlink():
        packet["review_contract"] = str(contract)
        packet["review_contract_rule"] = (
            "结果和测试句落到业务行为映射；环境噪声不作为模型扣分依据；"
            "key_business_constraint 用于语言复核"
        )
    return packet


def pending(root: Path, batch: str) -> list[dict]:
    rows = []
    for task in lib.task_dirs(root, batch):
        packet = _task_packet(task)
        if packet:
            rows.append(packet)
    rows.sort(key=lambda row: (
        -len(row.get("repair_errors", [])),
        row.get("queued_at") or "9999",
        row["task_id"],
    ))
    return rows


def next_task(root: Path, batch: str, *, wait: bool = False, timeout: int = 300) -> dict:
    deadline = time.time() + max(1, timeout)
    while True:
        rows = pending(root, batch)
        if rows:
            return {"state": "ok", "task": rows[0], "backlog": len(rows)}
        if not wait or time.time() >= deadline:
            return {"state": "empty", "task": None, "backlog": 0}
        remaining = max(1, int(deadline - time.time()))
        event = wait_for_event(
            root, batch,
            {"needs_task_agent", "completed", "blocked", "budget", "paused"},
            timeout=remaining)
        if event["state"] in ("completed", "blocked", "budget", "paused"):
            more = pending(root, batch)
            if more:
                return {"state": "ok", "task": more[0], "backlog": len(more)}
            return {"state": "empty", "task": None, "backlog": 0,
                    "flow": event["state"]}


def closeout(root: Path, batch: str, task_id: str) -> dict:
    task = lib.task_dirs(root, batch, task_id)[0]
    state = lib.load_state(task)
    gate = lib.load_json(task/'review/gate.json', default={})
    feedback = lib.load_json(task/'review/feedback.json', default={})
    upload = lib.load_json(task/'review/upload.json', default={})
    document = {
        "version": 1,
        "task_id": task.name,
        "batch": batch,
        "at": lib.now_iso(),
        "stages": {name: info.get('state') for name, info in state.get('stages', {}).items()},
        "gate_state": 'passed' if gate.get('passed') else 'blocked' if gate else 'pending',
        "gate_errors": gate.get('errors', [])[:10],
        "score": feedback.get('score'),
        "requirements": [
            {"id": row.get("requirement_id"), "status": row.get("status"),
             "user_text": row.get("user_text", "")}
            for row in feedback.get('requirements_status', []) if isinstance(row, dict)
        ],
        "process_claims": [row.get("sentence") for row in feedback.get('process', [])
                           if isinstance(row, dict)],
        "submission": {"state": upload.get('state') or 'not_submitted',
                       "submission_id": upload.get('submission_id'),
                       "remote_status": upload.get('remote_status')},
    }
    target = task/'review/task-closeout.json'
    document['path'] = str(target)
    lib.save_json(target, document)
    return document


def wait_for_event(root: Path, batch: str, events: set[str], timeout: int = 300,
                   interval: float = 1.0, clock=time.time, sleeper=time.sleep) -> dict:
    deadline = clock()+max(1, timeout)
    while True:
        flow = lib.batch_dir(root, batch)/'flow'
        waiting = lib.load_json(flow/'waiting.json', default={})
        tasks = waiting.get('tasks') or []
        if tasks and 'needs_task_agent' in events:
            return {'state': 'needs_task_agent', 'batch': batch, 'tasks': tasks}
        stop = lib.load_json(flow/'stop.json', default={})
        state = stop.get('reason')
        if state and state in events:
            return {'state': state, 'batch': batch,
                    'results': stop.get('results', [])[:10],
                    'platform': stop.get('platform', {})}
        if state and state in ('completed', 'blocked', 'budget', 'paused'):
            return {'state': state, 'batch': batch,
                    'detail': '运行阶段已结束，不含请求的事件',
                    'platform': stop.get('platform', {})}
        if clock() >= deadline:
            return {'state': 'timeout', 'batch': batch,
                    'detail': f'{timeout} 秒内没有出现 {sorted(events)}'}
        sleeper(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("next", "status", "wait"):
        command = subs.add_parser(name)
        command.add_argument("--batch", required=True)
    subs.choices["next"].add_argument("--wait", action="store_true")
    subs.choices["next"].add_argument("--timeout", type=int, default=300)
    subs.choices["wait"].add_argument("--events", default="needs_task_agent")
    subs.choices["wait"].add_argument("--timeout", type=int, default=300)
    close = subs.add_parser("closeout")
    close.add_argument("--batch", required=True)
    close.add_argument("--task", required=True)
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        if args.command == "closeout":
            result = closeout(root, args.batch, args.task)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "wait":
            result = wait_for_event(root, args.batch,
                                    {item.strip() for item in args.events.split(',') if item.strip()},
                                    timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['state'] != 'timeout' else 1
        rows = pending(root, args.batch)
        if args.command == "next":
            result = next_task(root, args.batch, wait=args.wait, timeout=args.timeout)
        else:
            result = {"state": "ok", "count": len(rows),
                      "tasks": [{key: row.get(key) for key in
                                 ("task_id", "task_path", "brief", "checkpoint")}
                                for row in rows]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except lib.FbrError as exc:
        print("错误：" + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
