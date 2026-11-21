#!/usr/bin/env python3
"""按用户指定顺序运行多个项目，只有上一项目进入模型尾段才启动下一项目。"""
import argparse
from pathlib import Path
import subprocess
import sys
import time

import fbrlib as lib
import fbr_runtime as runtime


def capacity(predecessors, queue_id):
    limit = lib.MODEL_CONCURRENCY_LIMIT
    for index, directory in enumerate(predecessors):
        progress = lib.load_json(Path(directory) / 'handoff.json', default={})
        if progress.get('queue_id') != queue_id or not progress.get('started'):
            return 0
        remaining = progress['remaining']
        if remaining and (index < len(predecessors) - 1 or remaining >= progress['capacity']):
            return 0
        limit -= remaining
    return max(0, limit)


def dispatched(task):
    if lib.stage_ok(task, 'run'):
        return True
    current = lib.load_json(task / 'run/current.json', default={})
    return bool(current.get('request_id') and current.get('state') in
                ('running', 'exited', 'continue_ready', 'finished'))


def report_handoff(directory, queue_id, active, retry_queue, limit):
    remaining = len(active) + len(retry_queue)
    started = not retry_queue and remaining < limit and all(dispatched(task) for task in active)
    progress = dict(queue_id=queue_id, remaining=remaining, capacity=limit, started=started)
    path = directory / 'handoff.json'
    if lib.load_json(path, default={}) != progress:
        lib.save_json(path, progress)


def run(projects, *, execute, local=False):
    flows = [lib.batch_dir(lib.find_root(root), batch) / 'flow' for root, batch in projects]
    if len(set(flows)) != len(flows):
        raise lib.FbrError('队列中有重复的项目批次')
    queue_id = lib.content_hash([list(map(str, flows)), local])
    if not execute:
        for flow in flows:
            print(flow.parent)
        return 0
    # A child retains its own root, account, task IDs, and normal recovery plan.
    children = {}
    finished = {}
    with lib.file_lock(flows[0] / 'project-queue.lock'):
        while len(finished) < len(flows):
            for index, flow in enumerate(flows):
                if flow in finished:
                    continue
                process = lib.load_json(flow / 'process.json', default={})
                plan = lib.load_json(flow / 'plan.json', default={})
                stop = lib.load_json(flow / 'stop.json', default={})
                child = children.get(flow)
                if child is not None:
                    code = child.poll()
                    if code is None:
                        continue
                    if code and (plan.get('queue_id') != queue_id or not stop.get('reason')):
                        raise lib.FbrError(f'{flow.parent} 启动或运行失败，退出码 {code}')
                if runtime.alive(process):
                    if plan.get('queue_id') != queue_id:
                        raise lib.FbrError(f'{flow.parent} 已由其他运行入口启动')
                    continue
                if plan.get('queue_id') == queue_id and stop.get('reason'):
                    finished[flow] = stop['reason']
                    if stop['reason'] in ('paused', 'budget'):
                        raise lib.FbrError(f'{flow.parent} 已暂停或到达预算')
                    continue
                if not capacity(flows[:index], queue_id):
                    if all(p in finished for p in flows[:index]):
                        raise lib.FbrError(f'{flow.parent} 的前序项目未满足交接条件')
                    break
                root, batch = projects[index]
                argv = [sys.executable, str(Path(__file__).with_name('fbr_flow.py')), '--root', root,
                        'pre-submit' if local else 'rolling', '--batch', batch, '--queue-id', queue_id]
                for previous in flows[:index]:
                    argv.extend(['--after-project', str(previous)])
                children[flow] = subprocess.Popen(argv)
                break
            if len(finished) < len(flows):
                time.sleep(1)
    return int(any(reason != 'completed' for reason in finished.values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', action='append', nargs=2, metavar=('ROOT', 'BATCH'), required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--pre-submit', action='store_true', help='各项目均只做到提交前')
    args = parser.parse_args()
    try:
        sys.exit(run(args.project, execute=args.execute, local=args.pre_submit))
    except (lib.FbrError, ValueError, OSError) as exc:
        sys.exit(str(exc))
