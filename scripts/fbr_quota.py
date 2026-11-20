"""SOLO2 new-submission reservations shared by all producers for one account."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import socket
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median

import fbr_platform
import fbr_runtime
import fbrlib as lib

BJ = timezone(timedelta(hours=8))
DAY = timedelta(days=1)
FIRST_READ = set()
BLOCKED_RECHECK_SECONDS = 60


def now():
    return datetime.now(BJ)


def moment(value, *, platform=False):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            if not platform:
                raise ValueError
            stamp = stamp.replace(tzinfo=BJ)
        return stamp.astimezone(BJ)
    except (AttributeError, TypeError, ValueError):
        raise lib.FbrError('额度核对缺少有效时间戳') from None


def due(stamp):
    return stamp.date() + (DAY if stamp.time() > time(20) else timedelta())


def trace_info(path, session):
    last = None
    sha = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            for line in stream:
                sha.update(line)
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get('sessionId') != session or row.get('isSidechain'):
                    continue
                if row.get('type') in ('user', 'assistant', 'system', 'progress'):
                    stamp = moment(row.get('timestamp'))
                    last = max(last, stamp) if last else stamp
    except (OSError, ValueError, AttributeError) as exc:
        raise lib.FbrError('无法核对上传主轨迹的最后运行时间') from exc
    if not last:
        raise lib.FbrError('主轨迹缺少可核对的运行时间，保留预留')
    return {'end': last.isoformat(), 'due': str(due(last)), 'session': session,
            'trace_sha256': sha.hexdigest()}


def directory():
    return Path(os.environ.get('SOLO11_QUOTA_DIR') or fbr_runtime.runtime_home({})).expanduser()


def path_for(account):
    return directory() / f'platform-quota-{account}.json'


@contextlib.contextmanager
def book(account):
    path = path_for(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = lib.load_json(path, default={'holds': {}, 'dates': {}, 'roots': {}, 'samples': []})
        try:
            yield data
        finally:
            lib.save_json(path, data)


def day_row(data, day):
    return data['dates'].setdefault(str(day), {'ids': {}, 'overview': 0, 'old_repairs': 0})


def used(row):
    if 'unlisted' in row:
        return row['overview'] + len(row['unlisted'])
    return max(len(row['ids']), row['overview'] - row['old_repairs'])  # legacy, until first live sync


def snapshot(client, account, day, created_days=None):
    client = client or fbr_platform.Client(account=account, timeout=20)
    user = client.me()
    user = user.get('user', user)
    if user.get('username') != account or not user.get('id'):
        raise lib.FbrError('概览账号身份校验失败')
    if created_days is None:
        created_days = {}
    filters = dict(user_id=user['id'], date_from=str(day), date_to=str(day))
    overview = client.list_submissions(page_size=1, **filters)['meta']['total']
    ids, old, seen = {}, 0, set()
    if type(overview) is not int or overview < 0:
        raise lib.FbrError('概览今日提交数无效')
    page = 1
    while True:
        result = client.list_submissions(page=page, page_size=100, **filters)
        if type(result['meta']['total']) is not int or result['meta']['total'] != overview:
            raise lib.FbrError('概览与分页总数变化，稍后重新核对')
        for row in result['items']:
            identifier = str(row['id'])
            if not identifier.isascii() or not identifier.isdecimal() or int(identifier) <= 0:
                raise lib.FbrError('概览记录缺少有效建单 ID')
            if row.get('user_id') != user['id'] or identifier in seen:
                raise lib.FbrError('概览账号或分页记录重复，稍后重新核对')
            seen.add(identifier)
            created_day = created_days.get(identifier)
            if created_day is None:
                created = row.get('created_at') or client.detail(row['id']).get('created_at')
                created_day = str(moment(created, platform=True).date())
                created_days[identifier] = created_day
            if created_day == str(day):
                ids[identifier] = row.get('session_id') or (row.get('data') or {}).get('session_id')
            else:
                old += 1
        if page >= result['meta']['total_pages']:
            if len(seen) != result['meta']['total']:
                raise lib.FbrError('概览分页数量变化，稍后重新核对')
            break
        if not result['items'] or page >= 10000:
            raise lib.FbrError('概览分页中断，等待重新核对')
        page += 1
    return overview, ids, old


def notify_increase(row, account, day, capacity):
    count = len(row['ids'])
    if count <= capacity:
        return ''
    if not row.get('notice'):
        row['notice'] = f'{account} 在 {day} 已确认新增 {count} 条，每日 {capacity} 条标准可能变化。'
        print(row['notice'], flush=True)
    return row.get('notice', '')


def refresh(data, account, client=None, force=False):
    stamp = now()
    row = day_row(data, stamp.date())
    first = (str(path_for(account)), str(stamp.date()))
    if force or first not in FIRST_READ or stamp.timestamp() - row.get('checked', 0) >= 600:
        created_days = data.get('created_days', {}).copy()
        try:
            overview, ids, old = snapshot(client, account, stamp.date(), created_days)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise lib.FbrError('概览字段或分页格式变化，等待重新核对') from exc
        if now().date() != stamp.date():
            return refresh(data, account, client, True)
        # Each complete list supersedes the previous one, including legacy cumulative IDs.
        # Bridge list propagation only for fresh confirmations never seen in a snapshot.
        unlisted = {key: created for key, created in row.get('unlisted', {}).items()
                    if key not in ids and stamp.timestamp() - created < BLOCKED_RECHECK_SECONDS}
        active = {key: row['ids'][key] for key in unlisted}
        active.update(ids)
        for key in row['ids']:
            created_days.setdefault(key, str(stamp.date()))
        data['created_days'] = created_days
        if row.get('full'):
            rejected_at = row.get('rejected_overview', max(row['overview'], len(row['ids']) + row['old_repairs']))
            if overview < rejected_at:
                for field in ('full', 'rejected_overview', 'rejection_notice', 'recheck_error'):
                    row.pop(field, None)
            else:
                row['rejected_overview'] = rejected_at
        row.update(ids=active, unlisted=unlisted, overview=overview, old_repairs=old, checked=stamp.timestamp())
        FIRST_READ.add(first)
        settled = set(row['ids'].values()) - {None, ''}
        if row.get('pending_probe') in settled:
            notify_increase(row, account, stamp.date(), row['probe_baseline'])
            row.pop('pending_probe', None)
        for key, hold in list(data['holds'].items()):
            if hold.get('session') in settled:
                del data['holds'][key]
    return row


def current_row(data, account, capacity, client=None, *, force=False):
    while True:
        stamp = now()
        day = stamp.date()
        prior = day_row(data, day)
        available = capacity - used(prior) - occupied(data, day)
        age = stamp.timestamp() - prior.get('checked', 0)
        if prior.get('full') or available <= 0:
            force = force or age >= BLOCKED_RECHECK_SECONDS
        elif 0 < available <= 2 and due(stamp) == day:
            force = force or age > 10
        row = refresh(data, account, client, force=force)
        stamp = now()
        if row is data['dates'].get(str(stamp.date())):
            return stamp, row


def key_for(task):
    record = lib.load_json(task / 'task.json')
    return lib.content_hash([record['task_repo_full'], record['initial_commit'], record['prompt']])


def occupied(data, day, exclude=None):
    return sum((h['day'] == str(day) or h.get('attempt_day') == str(day))
               for k, h in data['holds'].items() if k != exclude)


def limit(cfg):
    value = cfg['submit']['daily_limit']
    if type(value) is not int or value < 1:
        raise lib.FbrError('submit.daily_limit 必须为正整数')
    return value


def sample(data, identifier, stamp, started):
    if not started or any(item.get('id') == str(identifier) for item in data['samples']):
        return
    delay = (stamp - moment(started, platform=True)).total_seconds()
    if delay >= 0:
        data['samples'].append({'id': str(identifier), 'at': stamp.timestamp(),
                                'day': str(stamp.date()), 'delay': delay})
        data['samples'] = sorted(data['samples'], key=lambda s: s['at'])[-40:]


def bind(data, key, info, capacity):
    hold = data['holds'][key]
    hold.update(info)
    target = info['due']
    target_used = occupied(data, target, key) + used(day_row(data, target))
    if target <= hold['day'] or target_used < capacity:
        hold['day'] = target


def inventory(data, root, cfg, force=False):
    # Reconcile all known local projects before the first admission of a new day.
    # A dormant batch may otherwise keep yesterday's reservation in another batch.
    host = socket.gethostname()
    today = str(now().date())
    scanned = data.setdefault('inventory_days', {})
    if scanned.get(host) != today:
        for prior in list(data['roots']):
            project = Path(prior)
            if project != root and (project / '_config/settings.json').is_file():
                prior_cfg = lib.settings(project)
                if lib.platform_account(prior_cfg) == lib.platform_account(cfg):
                    inventory_project(data, project, prior_cfg, force=True)
        force = True
    inventory_project(data, root, cfg, force=force)
    scanned[host] = today


def unfinished_run_due(task, current, state, hold):
    """Provisional freshness while collect has not produced a bound main trace."""
    times = [value for value in (current.get('started_at'), current.get('ended_at'),
                                 state.get('started_at'), state.get('ended_at'), hold.get('started')) if value]
    attempts = lib.load_json(task / 'run/attempts.json', default=[])
    if attempts and attempts[-1].get('ended_at'):
        times.append(attempts[-1]['ended_at'])
    request = current.get('request_id')
    if request and Path(str(request)).name == request:
        done = lib.load_json(task / 'run' / f'exit-{request}.json', default={})
        if done.get('id') == request and done.get('ended_epoch'):
            times.append(datetime.fromtimestamp(done['ended_epoch'], BJ).isoformat())
    if times:
        return str(due(max(moment(value, platform=True) for value in times)))
    return None


def inventory_project(data, root, cfg, force=False):
    """Import surviving work before admission, including pre-upgrade runs."""
    stamp = now()
    today = str(stamp.date())
    tag = str(root.resolve())
    if not force and stamp.timestamp() - data['roots'].get(tag, 0) < 600:
        return
    for path in (root / 'batches').glob('*/*/task.json'):
        task = path.parent
        receipt = lib.load_json(task / 'review/upload.json', default={})
        state = lib.load_json(task / 'state.json', default={}).get('stages', {}).get('run', {})
        current = lib.load_json(task / 'run/current.json', default={})
        if receipt and receipt.get('account') not in (None, lib.platform_account(cfg)):
            continue
        key = key_for(task)
        hold = data['holds'].get(key, {})
        if not (current or receipt or state.get('state') in ('running', 'ok', 'abandoned')):
            if (hold.get('host') == socket.gethostname() and hold.get('owner')
                    and not fbr_runtime.alive(hold['owner']) and not hold.get('end')):
                data['holds'].pop(key, None)
            continue
        if receipt.get('submission_id') and receipt.get('submitted_at'):
            created = moment(receipt['submitted_at'], platform=True)
            date = created.date()
            if (str(date) == today and receipt.get('account') == lib.platform_account(cfg)
                    and receipt.get('platform') == fbr_platform.BASE_URL):
                sample(data, receipt['submission_id'], created, state.get('started_at'))
                record_confirmation(data, receipt['submission_id'], created, (receipt.get('remote_data') or {}).get('session_id'))
            data['holds'].pop(key, None)
            continue
        uncertain = receipt.get('state') in ('creating', 'uncertain', 'verifying')
        attempts = lib.load_json(task / 'run/attempts.json', default=[]) if state.get('state') == 'failed' else []
        stopped = bool(attempts and attempts[-1].get('verdict') == 'stop')
        run_ended = (stopped or state.get('state') == 'abandoned' or state.get('reason') == 'max_attempts'
                     or state.get('reason') == '单题累计运行预算耗尽，恢复不会清零预算'
                     or '模型中断，已达“继续”次数上限' in state.get('problems', []))
        if not uncertain and (run_ended or (task / 'review/platform-deprecated.json').exists()):
            data['holds'].pop(key, None)
            continue
        ids = lib.load_json(task / 'trace/ids.json', default={})
        if not ids and not current and state.get('state') not in ('ok', 'running') and not uncertain:
            continue
        if not ids and not hold.get('due') and not (uncertain or hold.get('uncertain')):
            if hold.get('host') not in (None, socket.gethostname()):
                continue
            provisional_due = unfinished_run_due(task, current, state, hold)
            if provisional_due and provisional_due < today:
                owner = {'pid': current.get('owner_pid'), 'identity': current.get('owner_identity')}
                active = current.get('state') in ('starting', 'running', 'continue_ready') and (
                    fbr_runtime.alive(current) or fbr_runtime.alive(owner))
                if not active:
                    data['holds'].pop(key, None)
                    continue
                provisional_due = today
            if provisional_due:
                hold = data['holds'].setdefault(key, {'day': today, 'task': task.name})
                hold['run_due'] = provisional_due
                if hold['day'] < today:
                    hold['day'] = today
        hold = data['holds'].setdefault(key, {'day': today, 'task': task.name})
        if ids.get('session_file') and not hold.get('end'):
            try:
                info = trace_info(task / 'trace' / ids['session_file'], ids['session_id'])
            except lib.FbrError as exc:
                hold['problem'] = str(exc)
            else:
                bind(data, key, info, limit(cfg))
        attestation = lib.load_json(task / 'review/attestation.json', default={})
        hold['ready'] = bool(attestation.get('state') == 'accepted' and attestation.get('session_id') == hold.get('session'))
        if uncertain:
            hold['uncertain'] = True
            if receipt.get('requested_at'):
                hold['attempt_day'] = str(moment(receipt['requested_at'], platform=True).date())
        if hold.get('due', today) < today and not uncertain:
            hold['day'] = hold['due']
    data['roots'][tag] = stamp.timestamp()


def priority_order(root, cfg, tasks):
    today = str(now().date())
    with book(lib.platform_account(cfg)) as data:
        inventory(data, root, cfg, force=True)
        def priority(task):
            deadline = data['holds'].get(key_for(task), {}).get('due')
            if deadline == today:
                return 0
            return 1 if deadline and deadline > today else 2
        return sorted(tasks, key=priority)


def rollover(data, today):
    for h in data['holds'].values():
        deadline = h.get('due') or h.get('run_due') or h['day']
        if h['day'] < str(today) and (h.get('uncertain') or deadline >= str(today) or h.get('problem')):
            h['day'] = str(today)


def new_hold(data, task, day):
    return data['holds'].setdefault(key_for(task), {'day': str(day), 'task': task.name})


def retry(detail, seconds=60):
    return {'state': 'retry', 'detail': detail, 'retry_after': now().timestamp() + seconds}


def reserve(task, cfg):
    root = lib.containing_root(task)
    account = lib.platform_account(cfg)
    with book(account) as data:
        inventory(data, root, cfg)
        stamp, _ = current_row(data, account, limit(cfg))
        day = due(stamp)
        rollover(data, stamp.date())
        key = key_for(task)
        hold = data['holds'].get(key)
        if hold and hold['day'] >= str(stamp.date()):
            return
        target = day_row(data, day)
        remaining = limit(cfg) - used(target) - occupied(data, day, key)
        if remaining <= 0 or target.get('full'):
            return retry(f'{day} 的新增名额已用完，等待额度释放或跨时段')
        samples = data['samples']
        if day == stamp.date() and samples:
            # Submission gaps reflect the throughput of the whole downstream queue.
            times = sorted(s['at'] for s in samples if s['day'] == str(day))
            gaps = [b-a for a,b in zip(times, times[1:]) if 0 < b-a <= 3600]
            lead = median(s['delay'] for s in samples)
            backlog = occupied(data, day) * (median(gaps) if gaps else cfg['submit']['interval_seconds'])
            midnight = datetime.combine(day + DAY, time(), BJ)
            if (midnight - stamp).total_seconds() <= lead + backlog:
                return retry('按近期实际交付耗时和待交数量，今晚来不及消化新题')
        data['holds'][key] = {'day': str(day), 'task': task.name, 'started': stamp.isoformat(),
                              'host': socket.gethostname(), 'owner': {'pid': os.getpid(), 'identity': fbr_runtime.process_identity()}}


def release(task, cfg):
    account = lib.platform_account(cfg)
    receipt = lib.load_json(task / 'review/upload.json', default={})
    if receipt.get('state') in ('creating', 'uncertain', 'verifying') or receipt.get('submission_id'):
        return
    with book(account) as data:
        data['holds'].pop(key_for(task), None)


def collected(task, cfg, ids):
    info = trace_info(task / 'trace' / ids['session_file'], ids['session_id'])
    with book(lib.platform_account(cfg)) as data:
        rollover(data, now().date())
        new_hold(data, task, now().date())
        bind(data, key_for(task), info, limit(cfg))
    return info


class Wait(lib.FbrError):
    def __init__(self, reason, *, state='quota_wait'):
        super().__init__(reason)
        self.state = state
        self.retry_at = now().timestamp() + BLOCKED_RECHECK_SECONDS


def check_create(root, task, cfg, client, info, *, before_post=False):
    account = lib.platform_account(cfg)
    with book(account) as data:
        inventory(data, root, cfg)
        stamp, row = current_row(data, account, limit(cfg), client, force=before_post)
        day = stamp.date()
        rollover(data, day)
        if info['due'] < str(day):
            raise Wait('轨迹已超过提交期限', state='expired')
        if moment(info['end']) > stamp:
            raise Wait('轨迹最后运行时间晚于当前时间', state='submit_blocked')
        if info['session'] in row['ids'].values():
            raise Wait('该会话已有平台单，请按 SessionID 回读', state='needs_attention')
        key = key_for(task)
        new_hold(data, task, day)
        bind(data, key, info, limit(cfg))
        hold = data['holds'][key]
        if row.get('full'):
            raise Wait('平台已明确拒绝当日新增，等待平台今日提交数下降或日切')
        free = limit(cfg) - used(row)
        unresolved = sum(h.get('attempt_day') == str(day) for k, h in data['holds'].items() if k != key)
        if free > 0 and unresolved >= free:
            raise Wait('剩余名额有结果待核对的建单请求')
        if free <= 0:
            if row.get('pending_probe') not in (None, info['session']):
                raise Wait('上一条试投的建单结果待核对')
            if info['due'] <= str(day) or hold['day'] != info['due']:
                raise Wait('今日新增已达基准，只能试投可结转且已预留明日名额的题')
        elif info['due'] > str(day) and free <= sum(
                (h['day'] == str(day) and h.get('due', str(day)) <= str(day)) or h.get('attempt_day') == str(day)
                for k, h in data['holds'].items() if k != key):
            raise Wait('今日剩余额度已留给今晚到期的题')
        if before_post:
            hold.update(uncertain=True, attempt_day=str(day))
            if free <= 0:
                row.update(pending_probe=info['session'], probe_baseline=limit(cfg))
        return {'day': str(day), 'probe': free <= 0, 'overview': row['overview']}


def clear_attempt(account, task):
    with book(account) as data:
        hold = data['holds'].get(key_for(task), {})
        hold.pop('uncertain', None)
        day = hold.pop('attempt_day', None)
        if day and day_row(data, day).get('pending_probe') == hold.get('session'):
            day_row(data, day).pop('pending_probe', None)


def full(account, day, *, client=None, capacity=None, request_overview=None):
    with book(account) as data:
        row = day_row(data, day)
        row.update(full=True, rejected_overview=row['overview'] if request_overview is None else request_overview)
        if client is not None:
            try:
                refresh(data, account, client, force=True)
            except lib.FbrError as exc:
                row['recheck_error'] = str(exc)
        if not row.get('full'):
            return f'{account} 在 {day} 的平台今日提交数已回落至 {row["overview"]} 条，下一次调度重新核算。'
        detail = f"{account} 在 {day} 已核实新增 {len(row['ids'])} 条，平台已拒绝当天继续新增。"
        if capacity and len(row['ids']) < capacity:
            detail += f'低于当前 {capacity} 条基准。'
        row['rejection_notice'] = detail
        print(detail, flush=True)
        return detail


def record_confirmation(data, identifier, created, session):
    row = day_row(data, created.date())
    key = str(identifier)
    known = data.setdefault('created_days', {})
    if 'unlisted' not in row:
        row['ids'][key] = session
    elif key in row['ids']:
        row['ids'][key] = session or row['ids'][key]
    elif key not in known and created.timestamp() >= row['checked'] - BLOCKED_RECHECK_SECONDS:
        row['ids'][key] = session
        row['unlisted'][key] = created.timestamp()
    known[key] = str(created.date())
    return row


def confirmed(task, account, identifier, created, session, capacity):
    with book(account) as data:
        hold = data['holds'].pop(key_for(task), {})
        stamp = moment(created, platform=True)
        row = record_confirmation(data, identifier, stamp, session)
        if row.get('pending_probe') == session:
            row.pop('pending_probe', None)
        sample(data, identifier, stamp, hold.get('started') or hold.get('end'))
        return notify_increase(row, account, stamp.date(), capacity)


def summary(root, *, live=False, rescan=False):
    cfg = lib.settings(root)
    account = lib.platform_account(cfg)
    with book(account) as data:
        inventory(data, root, cfg, force=live or rescan)
        if live:
            refresh(data, account, force=True)
        day = now().date()
        rollover(data, day)
        row = day_row(data, day)
        holds = list(data['holds'].values())
        unlisted = len(row.get('unlisted', {}))
        limit_notice = row.get('notice') if len(row['ids']) > limit(cfg) else ''
        return dict(account=account, day=str(day), baseline=limit(cfg), overview=row['overview'],
                    older_repairs=row['old_repairs'], awaiting_list=unlisted, confirmed=len(row['ids']), charged=used(row),
                    checked=row.get('checked'), today_reserved=occupied(data, day),
                    today_available=0 if row.get('full') else max(0,limit(cfg)-used(row)-occupied(data, day)),
                    tomorrow_reserved=occupied(data, day+DAY), due_tonight=sum(h.get('due')==str(day) for h in holds),
                    late_ready=sum(h.get('due', '') > str(day) and bool(h.get('ready')) for h in holds),
                    unexplained_gap=row['overview'] - row['old_repairs'] - (len(row['ids']) - unlisted),
                    ready_tonight=sum(h.get('due')==str(day) and h.get('ready',False) for h in holds),
                    daily_limit_rejected=bool(row.get('full')),
                    notice=' '.join(filter(None, (limit_notice, row.get('rejection_notice')))))


def live_status(root):
    try:
        return summary(root, live=True)
    except lib.FbrError as exc:
        return {'error': str(exc)}


def status_text(row):
    if 'error' in row:
        return f"当前额度待核对：{row['error']}"
    if not row['checked']:
        return '今日额度尚待平台核对'
    return (f"{row['account']} {datetime.fromtimestamp(row['checked'], BJ):%Y-%m-%d %H:%M} 概览 {row['overview']}，新增 {row['confirmed']}，"
            f"待列表同步 {row['awaiting_list']}，今日占位 {row['today_reserved']}，可开 {row['today_available']}，明日预留 {row['tomorrow_reserved']}，"
            f"今晚到期 {row['due_tonight']}（已就绪 {row['ready_tonight']}），可结转已就绪 {row['late_ready']}，对账差额 {row['unexplained_gap']}。{row['notice']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='核对本账号概览、导入本项目存量并查看生产额度')
    parser.add_argument('--root')
    parser.add_argument('--sync', action='store_true', help='登录平台核对实时概览与本地任务')
    args = parser.parse_args()
    print(json.dumps(summary(lib.find_root(args.root), live=args.sync), ensure_ascii=False, indent=2))
