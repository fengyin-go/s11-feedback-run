#!/usr/bin/env python3
"""Read exact original evidence, including decoded JSONL blocks, without guessing excerpts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
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
    raw = path.read_text(errors='replace')
    lines = raw.splitlines(keepends=True)
    end = start if end is None else end
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
    p.add_argument('--task', required=True); p.add_argument('--path', required=True)
    p.add_argument('--start', type=int, required=True); p.add_argument('--end', type=int); p.add_argument('--block', type=int)
    a = p.parse_args()
    print(json.dumps(read(a.task, a.path, a.start, a.end, a.block), ensure_ascii=False))
