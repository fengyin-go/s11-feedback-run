#!/usr/bin/env python3
"""Query bounded log windows instead of loading multi-megabyte run streams."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import fbrlib as lib


SOURCES = {
    'stream': ('run/stream.jsonl',),
    'stderr': ('run/stderr.log',),
    'test-before': ('evidence/command-logs/before/*.log',),
    'test-after': ('evidence/command-logs/after/*.log',),
    'probe': ('evidence/command-logs/probes/*.log',),
}


def files(task: Path, source: str) -> list[Path]:
    patterns = SOURCES.get(source)
    if not patterns:
        raise lib.FbrError('未知日志来源')
    result = []
    for pattern in patterns:
        result.extend(sorted(Path(task).glob(pattern)))
    return [path for path in result if path.is_file() and not path.is_symlink()]


def query(task: Path, source: str = 'stream', *, grep: str = '', context: int = 2,
          last: int = 40, max_bytes: int = 8000) -> str:
    paths = files(task, source)
    if not paths:
        raise lib.FbrError('没有匹配的日志文件')
    pattern = re.compile(grep, re.I) if grep else None
    selected = []
    for path in paths:
        lines = path.read_text(errors='replace').splitlines()
        if pattern:
            keep = set()
            for index, line in enumerate(lines):
                if pattern.search(line):
                    keep.update(range(max(0, index-context), min(len(lines), index+context+1)))
            picks = [(index+1, lines[index]) for index in sorted(keep)]
        else:
            picks = [(max(1, len(lines)-last+1)+offset, line)
                     for offset, line in enumerate(lines[-last:])]
        for number, line in picks:
            selected.append(f"{path.relative_to(task).as_posix()}:{number}: {lib.clip(line, 600)}")
    if not selected:
        return '没有匹配的日志行'
    output = '\n'.join(selected)
    encoded = output.encode('utf-8')
    if len(encoded) <= max_bytes:
        return output
    return encoded[-max_bytes:].decode('utf-8', errors='replace')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--source', choices=sorted(SOURCES), default='stream')
    parser.add_argument('--grep', default='')
    parser.add_argument('--context', type=int, default=2)
    parser.add_argument('--last', type=int, default=40)
    parser.add_argument('--max-bytes', type=int, default=8000)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        task = Path(args.task).expanduser().resolve()
        text = query(task, args.source, grep=args.grep, context=args.context,
                     last=args.last, max_bytes=max(1000, args.max_bytes))
        print(json.dumps({'source': args.source, 'text': text}, ensure_ascii=False, indent=2)
              if args.json else text)
        return 0
    except lib.FbrError as exc:
        print('错误：'+str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
