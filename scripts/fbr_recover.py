#!/usr/bin/env python3
"""Reuse valid trace analysis and hand acceptance repairs to the current task AI."""
from __future__ import annotations
import argparse
import json
import importlib.util
from pathlib import Path
import fbrlib as lib
import fbr_reviewer as reviewer
import fbr_gate as gate


def load_engine():
    # Each rolling worker gets separate invoke globals; concurrent tasks must not
    # replace one another's model caller or write candidates into the wrong task.
    spec = importlib.util.spec_from_file_location('fbr_reviewer_recovery_engine', reviewer.__file__)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def recover(task: Path, base: str | None = None, secret: str | None = None, *, repair_gate: bool = False,
            max_cycles: int | None = None, model=None, effort=None, env_file=None) -> dict:
    with lib.file_lock(task / '.operation.lock'):
        errors = reviewer.provenance_errors(task)
        failed_gate = lib.load_json(task/'review/gate.json', default={})
        if not errors and not (repair_gate and failed_gate.get('passed') is False):
            return {'state': 'already_valid', 'task_id': task.name}
        if not reviewer.analysis_errors(task):
            return reviewer.prepare_task_agent(task)
        # Credentials and model settings belong exclusively to a needed trace analysis.
        if base is None or secret is None:
            config = Path(env_file or reviewer.DEFAULT_ENV).expanduser()
            base, secret = reviewer.credentials(config)
            model, effort = reviewer.trace_configuration(config)
        model = model or reviewer.MODEL
        effort = effort or reviewer.EFFORT
        return reviewer.review(task, base, secret, model=model, effort=effort)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    parser.add_argument('--batch', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--env-file', default=reviewer.DEFAULT_ENV)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'state':'dry','detail':'继续修复评价并保留候选；不修改产物或提交平台'},ensure_ascii=False))
        return
    root = lib.find_root(args.root)
    task = lib.task_dirs(root, args.batch, args.task)[0]
    print(json.dumps(recover(task, env_file=args.env_file), ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
