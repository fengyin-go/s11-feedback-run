#!/usr/bin/env python3
"""Read exact original evidence, including decoded JSONL blocks, without guessing excerpts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import fbr_evidence
import fbrlib as lib


def read(task, relative, start, end=None, block=None):
    task = Path(task).resolve()
    path = task / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise lib.FbrError('来源路径必须属于当前任务')
    current = task
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise lib.FbrError('来源路径不能包含符号链接')
    allowed = relative == 'task.json' or relative == 'review/diff.patch' or relative == 'record/recording.json' or relative.startswith(('trace/projects/', 'evidence/command-logs/', 'workspace/')) or relative == 'evidence/probes.jsonl'
    if not allowed or not path.is_file():
        raise lib.FbrError('不是可读取的原始证据')
    end = start if end is None else end
    if relative.startswith('trace/projects/') and relative.endswith('.jsonl'):
        checked = fbr_evidence.fetch_refs(
            task, [{'path': relative, 'start_line': start, 'end_line': end, 'block': block}],
            operation='fbr_sources', require_excerpt=False)
        if checked['state'] != 'ok':
            raise lib.FbrError(checked['errors'][0]['error'])
        row = checked['evidence'][0]
        return {'path': relative, 'start': start, 'end': end, 'sha256': row['sha256'],
                'excerpt': row['raw'], 'decoded_block': row.get('decoded')}
    raw = path.read_text(errors='replace')
    lines = raw.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines):
        raise lib.FbrError('行号越界；先读取索引定位')
    excerpt = ''.join(lines[start-1:end])
    result = {'path': relative, 'start': start, 'end': end, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'excerpt': excerpt}
    if block is not None:
        if end != start or not relative.endswith('.jsonl'):
            raise lib.FbrError('block 仅用于一行原始 JSONL')
        row = json.loads(excerpt)
        content = (row.get('message') or {}).get('content', row.get('content'))
        if not isinstance(content, list) or not 0 <= block < len(content):
            raise lib.FbrError('block 越界')
        result['decoded_block'] = content[block]
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task', required=True); p.add_argument('--path')
    p.add_argument('--start', type=int); p.add_argument('--end', type=int); p.add_argument('--block', type=int)
    p.add_argument('--ref', action='append', default=[])
    p.add_argument('--ref-file')
    a = p.parse_args()
    refs = []
    if a.path is not None:
        if a.start is None:
            raise SystemExit('--path 需要同时提供 --start')
        refs.append({'path': a.path, 'start_line': a.start,
                     'end_line': a.end or a.start, 'block': a.block})
    if a.ref_file:
        loaded = lib.load_json(Path(a.ref_file))
        refs.extend(loaded if isinstance(loaded, list) else loaded.get('refs', [loaded]))
    refs.extend(json.loads(raw) for raw in a.ref)
    if not refs:
        raise SystemExit('至少提供 --path/--start 或 --ref')
    checked = fbr_evidence.fetch_refs(Path(a.task), refs, operation='fbr_sources', require_excerpt=False)
    print(json.dumps(checked, ensure_ascii=False))
