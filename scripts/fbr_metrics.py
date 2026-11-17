"""Stage timings plus batch token/cost accounting without storing prompts or credentials."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import fbrlib as lib


def stage(task, name, queued, fn, *args):
    start = time.time()
    try:
        result = fn(*args)
        state = result.get('state','ok') if isinstance(result,dict) else 'ok'
        return result
    except Exception:
        state = 'error'
        raise
    finally:
        lib.append_jsonl(task/'review/timings.jsonl', {'stage':name,'at':lib.now_iso(),
            'queue_seconds':round(start-queued,3),'execution_seconds':round(time.time()-start,3),'state':state})


def stream_usage(task: Path) -> dict:
    path = Path(task)/'run/stream.jsonl'
    result = {}
    for row in lib.read_jsonl(path):
        if row.get('type') != 'result':
            continue
        result = row
    usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
    return {
        'completed': bool(result),
        'input_tokens': int(usage.get('input_tokens') or 0),
        'output_tokens': int(usage.get('output_tokens') or 0),
        'cache_read_input_tokens': int(usage.get('cache_read_input_tokens') or 0),
        'cache_creation_input_tokens': int(usage.get('cache_creation_input_tokens') or 0),
        'cost_usd': float(result.get('total_cost_usd') or 0),
        'num_turns': int(result.get('num_turns') or 0),
        'duration_ms': int(result.get('duration_ms') or 0),
    }


def reviewer_usage(task: Path) -> dict:
    doc = lib.load_json(Path(task)/'review/metrics.json', default={})
    receipt = (doc.get('stages') or {}).get('trace_analysis') or {}
    usage = receipt.get('usage') if isinstance(receipt.get('usage'), dict) else {}
    result = {
        'calls': 1 if receipt else 0,
        'input_tokens': int(usage.get('input_tokens') or 0),
        'output_tokens': int(usage.get('output_tokens') or 0),
        'reasoning_output_tokens': int(usage.get('reasoning_output_tokens') or 0),
        'prompt_bytes': int(receipt.get('prompt_bytes') or 0),
        'response_bytes': int(receipt.get('response_bytes') or 0),
        'pack_bytes': int(receipt.get('pack_bytes') or 0),
        'raw_trace_bytes': int(receipt.get('raw_trace_bytes') or 0),
    }
    return result


def run_usage(task: Path) -> dict:
    attempts = lib.load_json(Path(task)/'run/attempts.json', default=[])
    reasons = {}
    for row in attempts if isinstance(attempts, list) else []:
        key = str(row.get('reason') or row.get('verdict') or 'unknown')
        reasons[key] = reasons.get(key, 0)+1
    return {
        'attempts': len(attempts) if isinstance(attempts, list) else 0,
        'retries': sum(str(row.get('verdict') or '') == 'retry'
                       for row in attempts if isinstance(row, dict)),
        'reasons': reasons,
    }


def feedback_usage(task: Path) -> dict:
    history = lib.load_json(Path(task)/'review/task-agent-history.json', default={})
    if isinstance(history, list):
        rows = history
    elif isinstance(history, dict):
        rows = history.get('attempts', [])
    else:
        rows = []
    attempts = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    candidates = [row.get('candidate_sha') for row in attempts if row.get('candidate_sha')]
    duplicates = len(candidates)-len(set(candidates))
    errors = sum(len(row.get('errors') or []) for row in attempts)
    return {
        'attempts': len(attempts),
        'failed_attempts': sum(bool(row.get('errors')) for row in attempts),
        'errors': errors,
        'duplicate_candidate_attempts': duplicates,
        'idempotent_reuses': sum(
            int(row.get('reuse_count') or 0) for row in attempts
            if type(row.get('reuse_count')) is int
        ),
    }


def evidence_usage(task: Path) -> dict:
    import fbr_evidence
    return fbr_evidence.access_summary(Path(task))


def timing_usage(task: Path) -> dict:
    rows = lib.read_jsonl(Path(task)/'review/timings.jsonl')
    return {
        'stages': len(rows),
        'queue_seconds': round(sum(float(row.get('queue_seconds') or 0) for row in rows), 3),
        'execution_seconds': round(sum(float(row.get('execution_seconds') or 0) for row in rows), 3),
    }


def _sum(rows: list[dict], keys: tuple[str, ...]) -> dict:
    return {key: sum(int(row.get(key) or 0) for row in rows) for key in keys}


def aggregate(root: Path, batch: str) -> dict:
    tasks = []
    for task in lib.task_dirs(Path(root), batch):
        tasks.append({
            'task_id': task.name,
            'product': stream_usage(task),
            'run': run_usage(task),
            'reviewer': reviewer_usage(task),
            'feedback': feedback_usage(task),
            'evidence': evidence_usage(task),
            'timing': timing_usage(task),
        })
    product = _sum([row['product'] for row in tasks],
                   ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                    'cache_creation_input_tokens', 'num_turns', 'duration_ms'))
    product['cost_usd'] = round(sum(float(row['product'].get('cost_usd') or 0) for row in tasks), 6)
    product['completed_tasks'] = sum(bool(row['product'].get('completed')) for row in tasks)
    reviewer = _sum([row['reviewer'] for row in tasks],
                    ('calls', 'input_tokens', 'output_tokens', 'reasoning_output_tokens',
                     'prompt_bytes', 'response_bytes', 'pack_bytes', 'raw_trace_bytes'))
    feedback = _sum([row['feedback'] for row in tasks],
                    ('attempts', 'failed_attempts', 'errors', 'duplicate_candidate_attempts',
                     'idempotent_reuses'))
    run = _sum([row['run'] for row in tasks], ('attempts', 'retries'))
    run_reasons = {}
    for row in tasks:
        for key, value in row['run'].get('reasons', {}).items():
            run_reasons[key] = run_reasons.get(key, 0)+value
    run['reasons'] = run_reasons
    evidence = _sum([row['evidence'] for row in tasks],
                    ('calls', 'full_reads', 'file_opens', 'bytes_returned', 'refs'))
    timing = _sum([row['timing'] for row in tasks],
                  ('stages', 'queue_seconds', 'execution_seconds'))
    report = {
        'version': 1,
        'batch': batch,
        'generated_at': lib.now_iso(),
        'task_count': len(tasks),
        'product_model': product,
        'run_workflow': run,
        'reviewer_model': reviewer,
        'feedback_workflow': feedback,
        'evidence_access': evidence,
        'stage_timing': timing,
        'tasks': tasks,
    }
    target = Path(root)/'_verification/metrics'/f'{batch}.json'
    lib.save_json(target, report)
    return {**report, 'report': str(target)}


def summary(report: dict) -> str:
    p, r = report['product_model'], report['reviewer_model']
    f, e = report['feedback_workflow'], report['evidence_access']
    rw = report['run_workflow']
    return '\n'.join([
        f"批次 {report['batch']}：{report['task_count']} 题，产物模型完成 {p['completed_tasks']} 题",
        f"产物模型：input {p['input_tokens']:,} / output {p['output_tokens']:,} / "
        f"cache read {p['cache_read_input_tokens']:,} / cost ${p['cost_usd']:.4f}",
        f"运行尝试：{rw['attempts']} 次，retry {rw['retries']}，"
        + " / ".join(f"{key} {value}" for key, value in sorted(rw['reasons'].items())),
        f"reviewer：{r['calls']} 次，input {r['input_tokens']:,} / output {r['output_tokens']:,}，"
        f"prompt {r['prompt_bytes']:,} bytes",
        f"Feedback：{f['attempts']} 次尝试，失败 {f['failed_attempts']}，"
        f"重复 candidate {f['duplicate_candidate_attempts']}，"
        f"幂等复用 {f['idempotent_reuses']}",
        f"证据读取：{e['calls']} 次，indexed reads {e['file_opens']}，full reads {e['full_reads']}，"
        f"返回 {e['bytes_returned']:,} bytes",
        f"完整报告：{report['report']}",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    subs = parser.add_subparsers(dest='command', required=True)
    item = subs.add_parser('aggregate')
    item.add_argument('--batch', required=True)
    item.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        report = aggregate(root, args.batch)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else summary(report))
        return 0
    except lib.FbrError as exc:
        print('错误：'+str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
