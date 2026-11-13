#!/usr/bin/env python3
"""逐题滚动生产、录屏、取证、独立评价和落表；默认演练；执行包含验收后发布，平台提交单独授权。"""
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
        if result.get('state') == 'retry':
            return result
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
    if lib.stage_ok(task, 'feedback') and not fbr_feedback.gate_task(task,cfg)['errors']:
        result = {'state':'already_valid'}
    else:
        fbr_feedback.card_one(task)
        gate=lib.load_json(task/'review/gate.json',default={})
        result=fbr_recover.recover(task,repair_gate=gate.get('passed') is False,max_cycles=1,env_file=options.reviewer_env)
    if result['state'] in ('needs_probes','repairing','needs_task_agent'):
        return {'task_id':task.name,**result}
    checked({**result,'state':'ok' if result['state']=='already_valid' else result['state']})
    with options.delivery_lock:
        rows,_=fbr_feedback.gate_batch(root,options.batch,cfg,task.name)
        match=next(row for row in rows if row['task_id']==task.name)
    if match['state']!='ok':
        errors=match.get('errors',[])
        path=task/'review/gate-repair.json'
        history=lib.load_json(path,default={'attempts':[]})
        signature=lib.content_hash({'errors':errors,'feedback':lib.load_json(task/'review/feedback.json',default={})})
        repeats=1
        for row in reversed(history['attempts']):
            if row.get('signature')!=signature:break
            repeats+=1
        history['attempts'].append({'at':lib.now_iso(),'errors':errors,'signature':signature})
        history['state']='repairing';lib.save_json(path,history)
        if repeats>=3: raise lib.FbrError('相同候选和错误重复，需要调度补充证据：'+'；'.join(errors))
        return {'task_id':task.name,'state':'repairing','detail':'；'.join(errors)}
    return finish_review(task,root,cfg,options)


def probe_task(task, root, cfg, options):
    import fbr_feedback
    requests=lib.load_json(task/'review/probe-requests.json')['requests']
    if not isinstance(requests,list) or not 1<=len(requests)<=3:
        raise lib.FbrError('probe 请求范围不合法，保留原请求')
    signature=lib.content_hash(requests)
    path=task/'review/probe-dispatch.json'
    prior=lib.load_json(path,default={})
    repeats=prior.get('repeats',0)+1 if prior.get('signature')==signature else 1
    if repeats>=3: raise lib.FbrError('相同补证请求重复且没有解决，需调度核对环境')
    lib.save_json(path,{'signature':signature,'repeats':repeats})
    for request in requests:
        if not isinstance(request,dict) or not isinstance(request.get('cmd'),str) or not request['cmd'].strip():
            raise lib.FbrError('probe 必须给出明确 cmd')
        request['timeout']=min(180,max(1,int(request.get('timeout',180))))
    fbr_feedback.probe_many(task,cfg,requests)
    return {'task_id':task.name,'state':'ok'}


def finish_review(task, root, cfg, options):
    import fbr_sheet
    import fbr_common
    # Finish acceptance first, then proactively publish and re-read the snapshot before field audit.
    with options.delivery_lock:
        fbr_sheet.write_sheet(root,options.batch,cfg)
    history={'attempts':[]}
    target=task/'review/delivery-repair.json'
    publishing=getattr(options,'publish',None)
    if publishing is None: publishing=True
    if publishing:
        import fbr_snapshot
        for attempt in range(1,4):
            try:
                checked(fbr_snapshot.push_one(fbr_snapshot.Hub(),task,cfg))
                break
            except (lib.FbrError,OSError) as exc:
                history['attempts'].append({'at':lib.now_iso(),'stage':'publish','attempt':attempt,'error':str(exc)})
                lib.save_json(target,history)
                # Retry transport/temporary failures only; never force-push a divergent main.
                if attempt==3 or not any(w in str(exc).lower() for w in ('timeout','timed out','connection','502','503','504','推送失败','回读远端')):
                    with options.delivery_lock:fbr_sheet.write_sheet(root,options.batch,cfg)
                    raise
                if time.time()+attempt*2>=cfg.get('_deadline',float('inf')):raise
                time.sleep(attempt*2)
    for attempt in range(2):
        with options.delivery_lock:
            fbr_sheet.write_sheet(root,options.batch,cfg)
            row,reason=fbr_sheet.build_row(root,task,cfg)
        problems = fbr_sheet.row_problems(row, lib.load_json(task/'trace/ids.json'), lib.load_json(task/'review/snapshot.json')) if row else [reason]
        if not publishing:
            problems = [p for p in problems if p not in ('缺产物快照', '产物快照还没推送')]
        if row is not None and not problems:break
        repaired=fbr_common.repair_from_peer(task,root) if attempt==0 else False
        history['attempts'].append({'at':lib.now_iso(),'stage':'fields','error':reason or (row or {}).get('数据状态'),'repaired':repaired})
        lib.save_json(target,history)
        if not repaired:
            raise lib.FbrError('交付核查仍有缺失，来源不足不能猜值：'+(reason or (row or {}).get('数据状态','缺少有效本地行')))
    if getattr(options,'auto_submit',False):
        if not lib.load_json(task/'review/snapshot.json').get('pushed'):raise lib.FbrError('产物未发布，不能提交平台')
        import fbr_submit
        fbr_submit.run_batch(root,options.batch,tasks=task.name,execute=True,confirm='SUBMIT')
        with options.delivery_lock:fbr_sheet.write_sheet(root,options.batch,cfg)
    history['state']='passed';lib.save_json(target,history)
    return {'task_id':task.name,'state':'ok','detail':'验收、快照推送及本地字段核查完成' if publishing else '本地验收完成（显式不发布）'}


def run_flow(root, args):
    directory = home(root, args.batch)
    directory.mkdir(parents=True, exist_ok=True)
    with lib.file_lock(directory / 'flow.lock'):
        cfg = lib.settings(root)
        cfg['_managed'] = True
        cfg['_single_attempt'] = True
        args.delivery_lock = threading.Lock()
        if getattr(args,'publish',None) is None: args.publish=True
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
        # Recover every surviving worker before waiting to dispatch prepared tasks.
        tasks.sort(key=lambda task: not runtime.alive(lib.load_json(task / 'run/current.json', default={})))
        if not args.no_watchdog:
            spawn(root, args, watchdog=True)
        rows = []
        def safe(fn, *values):
            try:
                return fn(*values)
            except (lib.FbrError, OSError, ValueError) as exc:
                return {'task_id': values[0].name, 'state': 'blocked', 'detail': str(exc)}
        pools = [futures.ThreadPoolExecutor(max_workers=max(1, int(n))) for n in
                 (cfg['concurrency'], cfg['record_parallel'], cfg.get('facts_parallel', 2), min(5,cfg.get('review_parallel',5)), cfg.get('facts_parallel',2))]
        import fbr_metrics
        def enqueue(step,task):
            fn,values=((produce,(task,cfg,args)),(record_task,(task,cfg)),(facts_task,(task,root,cfg)),
                       (review_task,(task,root,cfg,args)),(probe_task,(task,root,cfg,args)))[step]
            queued=time.time()
            return pools[step].submit(safe,lambda t: fbr_metrics.stage(t,str(step),queued,fn,*values),task)
        def resume_step(task):
            if frozen(task) or any(not lib.stage_ok(task, stage) for stage in ('run', 'collect', 'commit')):
                return 0
            if not lib.stage_ok(task, 'record'):
                return 1
            if not lib.stage_ok(task, 'facts'):
                return 2
            return 3
        pending={}
        retry_queue=[]
        author_waiting={}
        for task in tasks:
            step=resume_step(task)
            pending[enqueue(step,task)]=(step,task)
        try:
            while pending or retry_queue or author_waiting:
                for name, task in list(author_waiting.items()):
                    if lib.stage_ok(task, 'feedback'):
                        pending[enqueue(3,task)] = (3,task)
                        author_waiting.pop(name)
                lib.save_json(directory / 'waiting.json', {'at':lib.now_iso(), 'state':'waiting_for_task_ai' if author_waiting else 'active',
                    'tasks':[{'task_id':task.name,'reason':'待当前任务 AI 完成产物验收、Feedback 或具体返修',
                              'checkpoint':str(task/'review/task-agent-checkpoint.json'),
                              'gate':str(task/'review/gate.json')} for task in author_waiting.values()],
                    'next':'依据检查点真实补证/修正文并 finalize；不调用 reviewer.env 生成产物评价'})
                if args.stopped() and author_waiting:
                    rows.extend({'task_id':task.name,'state':'needs_task_agent','detail':'保留任务AI验收/返修检查点'} for task in author_waiting.values())
                    author_waiting.clear()
                due = [(when, task) for when, task in retry_queue if when <= time.time()]
                for item in due:
                    retry_queue.remove(item)
                    if not args.stopped():
                        pending[enqueue(0,item[1])] = (0,item[1])
                    else:
                        rows.append({'task_id':item[1].name,'state':'deferred','detail':'排空或批次截止，保留待重排'})
                if args.stopped() and retry_queue:
                    rows.extend({'task_id':task.name,'state':'deferred','detail':'排空或批次截止，未派发'} for _, task in retry_queue)
                    retry_queue.clear()
                if not pending:
                    time.sleep(1)
                    continue
                completed, _ = futures.wait(pending, timeout=1, return_when=futures.FIRST_COMPLETED)
                for future in completed:
                    step, task = pending.pop(future)
                    result = future.result()
                    if isinstance(result,dict) and result.get('state') == 'retry':
                        retry_queue.append((result.get('retry_after',time.time()),task))
                    elif isinstance(result,dict) and result.get('state') == 'needs_task_agent':
                        author_waiting[task.name]=task
                    elif isinstance(result,dict) and result.get('state') in ('repairing','needs_probes'):
                        if time.time()>=deadline or (directory/'pause').exists():
                            rows.append({'task_id':task.name,'state':'deferred','detail':'保留评价/补证检查点'})
                        else:
                            next_step=4 if result['state']=='needs_probes' else 3
                            pending[enqueue(next_step,task)]=(next_step,task)
                    elif isinstance(result,dict) and result.get('state') not in ('ok','skip'):
                        rows.append(result)
                    elif step==3:
                        rows.append(result)
                    elif time.time()>=deadline or (directory/'pause').exists():
                        rows.append({'task_id':task.name,'state':'deferred','detail':'排空或批次预算到顶'})
                    else:
                        next_step=3 if step==4 else step+1
                        pending[enqueue(next_step,task)]=(next_step,task)
                report = {'at': lib.now_iso(), 'active': [{'task_id': t.name, 'stage': s} for s, t in pending.values()],
                          'results': rows, 'deadline': deadline, 'waiting_for_task_ai': list(author_waiting),
                          'phases': {t.name: phase(t) for t in tasks}}
                lib.save_json(directory / 'state.json', report)
        finally:
            for pool in pools:
                pool.shutdown(wait=True, cancel_futures=True)
        import fbr_sheet
        with args.delivery_lock:
            fbr_sheet.write_sheet(root, args.batch, cfg)
        problem = any(r['state'] not in ('ok', 'frozen') for r in rows)
        reason = 'budget' if time.time() >= deadline else 'paused' if args.stopped() else 'needs_task_agent' if any(r['state']=='needs_task_agent' for r in rows) else 'blocked' if problem else 'completed'
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
        if getattr(args,'publish',None) is False: argv += ['--no-publish']
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
            if stop.get('reason') in ('completed', 'blocked', 'budget', 'paused', 'needs_task_agent'):
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
        p.add_argument('--publish', action='store_true', default=None)
        p.add_argument('--no-publish', action='store_false', dest='publish')
        p.add_argument('--confirm-publish', default='')
        p.add_argument('--auto-submit', action='store_true')
        p.add_argument('--confirm-submit', default='')
        p.add_argument('--reviewer-env', default='~/.codex/solo11/reviewer.env')
        p.add_argument('--no-watchdog', action='store_true')
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        if args.execute:
            lib.active_github_account()
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
            if args.confirm != 'FLOW' or (args.auto_submit and args.confirm_submit != 'SUBMIT'):
                raise lib.FbrError('执行含验收后快照发布，需 --confirm FLOW；平台提交另需 --confirm-submit SUBMIT')
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
