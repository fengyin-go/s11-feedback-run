#!/usr/bin/env python3
"""逐题滚动生产、录屏、取证、独立评价和落表；默认演练，不自动发布或提交。"""
from __future__ import annotations
import argparse
import concurrent.futures as futures
import copy
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import fbrlib as lib
import fbr_runtime as runtime
import fbr_run


def home(root, batch):
    return lib.batch_dir(root, batch) / 'flow'


def phase(task):
    import fbr_delivery
    if fbr_run.key_hold(task):
        return 'blocked_key'
    current = lib.load_json(task / 'run/current.json', default={})
    if current.get('state') in ('starting', 'running', 'exited', 'continue_ready') and not lib.stage_ok(task, 'run'):
        return 'recover'
    for stage in lib.STAGES:
        if not lib.stage_ok(task, stage):
            return stage
    valid, _ = fbr_delivery.current_feedback(task)
    if not valid:
        return 'feedback'
    upload = lib.load_json(task / 'review/upload.json', default={})
    return 'submitted' if upload.get('state') == 'confirmed' else 'local_done'


def frozen(task):
    return bool(lib.load_json(task / 'review/upload.json', default={}).get('submission_id'))


def checked(row):
    if row.get('state') not in ('ok', 'skip', 'idle'):
        raise lib.FbrError(row.get('detail', str(row)))
    return row


def produce(task, cfg, options):
    import fbr_snapshot
    if frozen(task):
        return {'task_id': task.name, 'state': 'frozen', 'detail': '已有平台单，冻结生产证据'}
    if options.stopped():
        return {'task_id': task.name, 'state': 'deferred', 'detail': '已暂停、排空或达到批次预算'}
    if not lib.stage_ok(task, 'run'):
        result = fbr_run.run_one(task, cfg)
        while result.get('state') == 'running' and not options.stopped():
            time.sleep(5)
            result = fbr_run.run_one(task, cfg)
        checked(result)
    checked(fbr_run.collect_one(task, cfg))
    checked(fbr_snapshot.commit_one(task, cfg))
    return {'task_id': task.name, 'state': 'ok', 'detail': '快照就绪'}


def record_task(task, cfg):
    import fbr_record
    checked(fbr_record.record_one(task, cfg))
    return task


def facts_task(task, root, cfg):
    import fbr_facts
    checked(fbr_facts.facts_one(task, root, cfg))
    return task


def review_task(task, root, cfg, options):
    import fbr_feedback
    import fbr_reviewer
    import fbr_recover
    existing = False
    if lib.stage_ok(task, 'feedback'):
        # 回执与证据必须仍有效；已提交的由 produce 直接冻结。
        if fbr_feedback.gate_task(task, cfg)['errors'] == []:
            existing = True
    if not existing:
        base, secret = fbr_reviewer.credentials(Path(options.reviewer_env).expanduser())
    for turn in range(0 if existing else 3):
        fbr_feedback.card_one(task)
        result = fbr_recover.recover(task, base, secret)
        if result['state'] == 'already_valid':
            result = {**result, 'state': 'ok'}
        if result['state'] == 'needs_probes':
            requests = lib.load_json(task / 'review/probe-requests.json')['requests']
            if turn == 2 or not isinstance(requests, list) or len(requests) > 3:
                raise lib.FbrError('补证循环达到上限，保留 probe 请求，不编造结论')
            for request in requests:
                if not isinstance(request, dict) or not isinstance(request.get('cmd'), str) or not request['cmd'].strip():
                    raise lib.FbrError('probe 请求必须给出明确 cmd，不能自动猜测命令')
                fbr_feedback.probe_one(task, cfg, request['cmd'], min(180, max(1, int(request.get('timeout', 180)))))
            continue
        checked(result)
        break
    # 查重和落表共享串行写入通道，避免两个评价同时过闸留下相互重复。
    with options.delivery_lock:
        rows, _ = fbr_feedback.gate_batch(root, options.batch, cfg, task.name)
        match = next(row for row in rows if row['task_id'] == task.name)
        if match['state'] != 'ok':
            raise lib.FbrError('Feedback 闸门未通过：' + '；'.join(match['errors']))
        import fbr_sheet
        # 先落本地进度；推送失败也保留已通过的独立验收。
        fbr_sheet.write_sheet(root, options.batch, cfg)
        if getattr(options, 'publish', False):
            import fbr_snapshot
            try:
                checked(fbr_snapshot.push_one(fbr_snapshot.Hub(cfg['github_owner']), task, cfg))
            finally:
                fbr_sheet.write_sheet(root, options.batch, cfg)
            row, reason = fbr_sheet.build_row(root, task, cfg)
            if row is None or row['数据状态'] != '可提交':
                raise lib.FbrError('交付必填字段闸门未通过：' + (reason or row['数据状态']))
        if options.auto_submit:
            snapshot = lib.load_json(task / 'review/snapshot.json')
            if not snapshot.get('pushed'):
                raise lib.FbrError('未获发布授权或产物尚未推送，不能提交平台')
            import fbr_submit
            fbr_submit.run_batch(root, options.batch, tasks=task.name, execute=True, confirm='SUBMIT')
            fbr_sheet.write_sheet(root, options.batch, cfg)
    return {'task_id': task.name, 'state': 'ok', 'detail': '本地评价与结果表完成'}


def run_flow(root, args):
    directory = home(root, args.batch)
    directory.mkdir(parents=True, exist_ok=True)
    with lib.file_lock(directory / 'flow.lock'):
        cfg = lib.settings(root)
        cfg['_managed'] = True
        args.delivery_lock = threading.Lock()
        saved = lib.load_json(directory / 'plan.json', default={})
        # 恢复同批次沿用绝对截止；不能靠看门狗清零预算。
        deadline = min(saved.get('deadline', float('inf')), runtime.batch_deadline(root, args.batch, cfg))
        cfg.update(_deadline=deadline, _drain=str(directory / 'drain'))
        args.stopped = lambda: time.time() >= deadline or any((directory / f).exists() for f in ('pause', 'drain'))
        plan = {k: v for k, v in vars(args).items() if k not in ('delivery_lock', 'stopped')}
        plan.update(deadline=deadline, root=str(root))
        lib.save_json(directory / 'plan.json', plan)
        lib.save_json(directory / 'process.json', {'pid': os.getpid(), 'identity': runtime.process_identity()})
        (directory / 'stop.json').unlink(missing_ok=True)
        if not fbr_run.print_checks(fbr_run.preflight(cfg)):
            raise lib.FbrError('预检未通过，不启动流水线')
        tasks = lib.task_dirs(root, args.batch, args.tasks)
        if any(not frozen(t) for t in tasks):
            import fbr_reviewer
            fbr_reviewer.credentials(Path(args.reviewer_env).expanduser())
        if not args.no_watchdog:
            spawn(root, args, watchdog=True)
        rows = []
        def safe(fn, *values):
            try:
                return fn(*values)
            except (lib.FbrError, OSError, ValueError) as exc:
                return {'task_id': values[0].name, 'state': 'blocked', 'detail': str(exc)}
        pools = [futures.ThreadPoolExecutor(max_workers=max(1, int(n))) for n in
                 (cfg['concurrency'], cfg['record_parallel'], cfg.get('facts_parallel', 2), cfg.get('review_parallel', 1))]
        pending = {pools[0].submit(safe, produce, task, cfg, args): (0, task) for task in tasks}
        try:
            while pending:
                completed, _ = futures.wait(pending, timeout=5, return_when=futures.FIRST_COMPLETED)
                for future in completed:
                    step, task = pending.pop(future)
                    result = future.result()
                    if isinstance(result, dict) and result.get('state') not in ('ok', 'skip'):
                        rows.append(result)
                    elif step == 3:
                        rows.append(result)
                    elif time.time() >= deadline or (directory / 'pause').exists():
                        rows.append({'task_id': task.name, 'state': 'deferred', 'detail': '排空或批次预算到顶'})
                    else:
                        next_step = step + 1
                        fn, values = ((record_task, (task, cfg)), (facts_task, (task, root, cfg)),
                                      (review_task, (task, root, cfg, args)))[step]
                        pending[pools[next_step].submit(safe, fn, *values)] = (next_step, task)
                report = {'at': lib.now_iso(), 'active': [{'task_id': t.name, 'stage': s} for s, t in pending.values()],
                          'results': rows, 'deadline': deadline,
                          'phases': {t.name: phase(t) for t in tasks}}
                lib.save_json(directory / 'state.json', report)
        finally:
            for pool in pools:
                pool.shutdown(wait=True, cancel_futures=True)
        import fbr_sheet
        with args.delivery_lock:
            fbr_sheet.write_sheet(root, args.batch, cfg)
        problem = any(r['state'] not in ('ok', 'frozen') for r in rows)
        reason = 'budget' if time.time() >= deadline else 'paused' if args.stopped() else 'blocked' if problem else 'completed'
        lib.save_json(directory / 'stop.json', {'reason': reason, 'at': lib.now_iso(), 'results': rows})
        return {'state': reason, 'results': rows}


def spawn(root, args, watchdog=False):
    directory = home(root, args.batch)
    path = directory / ('watchdog.json' if watchdog else 'process.json')
    previous = lib.load_json(path, default={})
    if runtime.alive(previous):
        return previous
    argv = [sys.executable, str(Path(__file__).resolve()), '--root', str(root),
            'watchdog' if watchdog else 'resume', '--batch', args.batch, '--execute']
    if not watchdog:
        argv += ['--confirm', 'FLOW']
        for key in ('tasks', 'reviewer_env'):
            value = getattr(args, key, None)
            if value:
                argv += ['--' + key.replace('_', '-'), value]
        for key, confirm in (('publish', 'PUBLISH'), ('auto_submit', 'SUBMIT')):
            if getattr(args, key, False):
                argv += ['--' + key.replace('_', '-'), '--confirm-' + ('publish' if key == 'publish' else 'submit'), confirm]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ('watchdog.log' if watchdog else 'flow.log')).open('ab') as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    value = {'pid': proc.pid, 'identity': runtime.process_identity(proc.pid), 'argv': argv}
    lib.save_json(path, value)
    return value


def watchdog(root, args):
    directory = home(root, args.batch)
    with lib.file_lock(directory / 'watchdog.lock'):
        for _ in range(50):
            plan = lib.load_json(directory / 'plan.json', default={})
            if not plan or time.time() >= plan['deadline'] or (directory / 'pause').exists() or (directory / 'drain').exists():
                return {'state': 'stopped'}
            proc = lib.load_json(directory / 'process.json', default={})
            stop = lib.load_json(directory / 'stop.json', default={})
            if stop.get('reason') in ('completed', 'blocked', 'budget', 'paused'):
                return {'state': stop['reason']}
            if not runtime.alive(proc):
                revive = lib.load_json(directory / 'revives.json', default={'count': 0})
                if revive['count'] >= 50:
                    return {'state': 'revive_limit'}
                options = argparse.Namespace(**plan)
                options.no_watchdog = True
                spawn(root, options)
                lib.save_json(directory / 'revives.json', {'count': revive['count'] + 1, 'at': lib.now_iso()})
            # 守护只恢复机器进程，不改评价、不绕过冻结和授权。
            for _ in range(120):
                if (directory / 'pause').exists() or (directory / 'drain').exists():
                    return {'state': 'paused'}
                time.sleep(5)
    return {'state': 'watchdog_budget'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    subs = parser.add_subparsers(dest='command', required=True)
    for command in ('run', 'resume', 'status', 'recover', 'pause', 'drain', 'watchdog'):
        p = subs.add_parser(command)
        p.add_argument('--batch', required=True)
        p.add_argument('--tasks')
        p.add_argument('--execute', action='store_true')
        p.add_argument('--confirm', default='')
        p.add_argument('--detach', action='store_true')
        p.add_argument('--publish', action='store_true')
        p.add_argument('--confirm-publish', default='')
        p.add_argument('--auto-submit', action='store_true')
        p.add_argument('--confirm-submit', default='')
        p.add_argument('--reviewer-env', default='~/.codex/solo11/reviewer.env')
        p.add_argument('--no-watchdog', action='store_true')
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        directory = home(root, args.batch)
        if args.command == 'status' or not args.execute:
            result = {'state': 'dry' if args.command != 'status' else 'status',
                      'tasks': {t.name: phase(t) for t in lib.task_dirs(root, args.batch, args.tasks)}}
        elif args.command in ('pause', 'drain'):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / args.command).touch()
            result = {'state': args.command, 'detail': '在跑任务保留，停止起新任务；resume 恢复'}
        elif args.command == 'recover':
            result = [fbr_run.recover_one(t, lib.settings(root)) for t in lib.task_dirs(root, args.batch, args.tasks)]
        elif args.command == 'watchdog':
            result = watchdog(root, args)
        else:
            if args.confirm != 'FLOW' or (args.publish and args.confirm_publish != 'PUBLISH') or (args.auto_submit and args.confirm_submit != 'SUBMIT'):
                raise lib.FbrError('执行需 --confirm FLOW；发布/提交另需各自明确确认')
            if args.command == 'resume':
                for name in ('pause', 'drain'):
                    (directory / name).unlink(missing_ok=True)
            result = spawn(root, args) if args.detach else run_flow(root, args)
        print(__import__('json').dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get('state') in ('blocked', 'budget') else 0
    except (lib.FbrError, OSError, ValueError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
