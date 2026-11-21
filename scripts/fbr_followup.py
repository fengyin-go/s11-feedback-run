"""Follow the confirmed SOLO2 submission belonging to one rolling task."""
import re
import time

import fbr_platform as platform
import fbr_quota as quota
import fbr_submit as submit
import fbrlib as lib

JOURNAL = 'review/platform-followup.json'
READY = 'review/platform-repair-ready.json'
WINDOW = 600


def enroll(task, cfg):
    path = task / JOURNAL
    if not path.exists() and not lib.load_json(task / 'review/upload.json', default={}).get('submission_id'):
        lib.save_json(path, {'state': 'submitting', 'task_id': task.name,
                             'account': lib.platform_account(cfg), 'queued_at': lib.now_iso()})


def repair_packet(task):
    state = lib.load_json(task / JOURNAL, default={})
    if state.get('state') != 'needs_platform_repair' or lib.platform_deprecated(task):
        return None
    receipt = lib.load_json(task / 'review/upload.json', default={})
    if receipt.get('last_repair_at') != state.get('last_repair_at'):
        return None
    ready = lib.load_json(task / READY, default={})
    if (ready and ready.get('revision') == state.get('revision') and ready.get('cycle') == state.get('cycle')
            and ready.get('feedback_digest') == lib.content_hash(lib.load_json(task / 'review/feedback.json', default={}))
            and ready != state.get('attempted_repair')):
        return None
    return {'task_id': task.name, 'task_path': str(task), 'kind': 'platform_repair',
            'checkpoint': str(task / JOURNAL), 'queued_at': state['queued_at'],
            'repair_errors': state['checks'], 'summary': state['summary'],
            'next_steps': ['根据原单质检意见修正可编辑字段，涉及 Feedback 时用 prepare/finalize --platform-repair。',
                           '通过 finalize --platform-repair 后由调度器复核并重新提交。仅修表单字段时，预览 fbr_batch.py repair 的变更后用 --execute --confirm REPAIR 写回原单。']}


def mark_ready(task, revision):
    active = lib.load_json(task / JOURNAL, default={})
    if revision and active.get('state') == 'needs_platform_repair' and revision == active.get('revision'):
        lib.save_json(task / READY, {'revision': revision, 'cycle': active['cycle'],
                      'feedback_digest': lib.content_hash(lib.load_json(task / 'review/feedback.json'))})


def poll(task, root, cfg, options, *, client=None, clock=time.time):
    path = task / JOURNAL
    journal = lib.load_json(path)
    receipt = lib.load_json(task / 'review/upload.json')
    account = lib.platform_account(cfg)
    identifier = receipt.get('submission_id')
    session = (receipt.get('binding', {}).get('data') or {}).get('session_id')
    if (not submit.valid_id(identifier) or not session or receipt.get('state') != 'confirmed'
            or receipt.get('task_id') != task.name or receipt.get('account') != account
            or receipt.get('platform') != platform.BASE_URL or journal.get('task_id') != task.name
            or journal.get('account') != account or journal.get('submission_id', identifier) != identifier
            or journal.get('session_id', session) != session):
        raise lib.FbrError('质检跟进回执与任务、平台账号或 SessionID 不一致')
    journal.update(submission_id=identifier, session_id=session)
    interval = max(1, getattr(options, 'cleanup_poll_seconds', 60))

    def result(state, detail, delay=None, **extra):
        journal.update(state=state, detail=detail, **extra)
        lib.save_json(path, journal)
        row = {'task_id': task.name, 'state': 'ok', 'detail': detail}
        if state == 'deleted':
            row['quota_overview'] = journal['overview']
        if delay is not None:
            row.update(state='retry', retry_after=clock() + delay)
        elif state in ('delete_guard_unavailable', 'needs_attention'):
            row['state'] = 'needs_attention'
        return row

    cycle = receipt.get('last_repair_at') or receipt['submitted_at']
    if journal.get('cycle') == cycle and journal['state'] in ('timed_out', 'deleted', 'unrepairable'):
        return result(journal['state'], journal['detail'])
    journal['cycle'] = cycle
    deadline = quota.moment(cycle, platform=True).timestamp() + WINDOW
    client = client or platform.Client(account=account, timeout=20)
    me = client.me()
    user = me.get('user', me)
    if user.get('username') != account or not user.get('id'):
        raise lib.FbrError('质检跟进账号身份不符')

    def check_identity(detail):
        if str(detail.get('id')) != str(identifier) or submit.record_session(detail) != session:
            raise lib.FbrError('平台详情的记录 ID 或 SessionID 不符')
        owner = detail.get('user_id')
        if owner is None:
            # Some detail projections omit the owner; the account list exposes user_id.
            matches = [row for row in submit.own_records(client) if str(row.get('id')) == str(identifier)]
            if len(matches) == 1 and submit.record_session(matches[0]) == session:
                owner = matches[0].get('user_id')
        if owner != user['id']:
            raise lib.FbrError('平台记录所有者与当前账号不符')

    try:
        detail = client.detail(identifier)
    except platform.PlatformHTTPError as exc:
        if exc.status != 404 or not journal.get('delete_attempted'):
            raise
        with quota.book(account) as data:
            row = quota.refresh(data, account, client, force=True)
            key = str(identifier)
            if key in row['ids'] and key not in row['unlisted']:
                return result(journal['state'], '详情已移除，等待账号列表同步', interval)
            row['unlisted'].pop(key, None)
            row['ids'].pop(key, None)
            data.setdefault('created_days', {})[key] = str(quota.moment(receipt['submitted_at'], platform=True).date())
            overview = row['overview']
        submit.discard_platform_task(task, {'status': 'DISCARDED'}, [], reason='discarded_record_removed')
        return result('deleted', f'已核对记录不存在，平台今日提交数 {overview}', overview=overview)
    check_identity(detail)
    status = detail.get('status')
    journal['status'] = status
    stage = detail.get('stage')
    if status == 'DISCARDED':
        if stage not in (None, '', 'DISCARDED') or detail.get('qc_running') is True:
            return result('needs_attention', '废弃状态与实时质检阶段不一致，保留原单')
        if journal['state'] == 'delete_acknowledged' and clock() < journal.get('ack_at', 0) + 60:
            return result('delete_acknowledged', '等待已受理的删除同步到详情', min(interval, 10))
        if journal.get('delete_attempted'):
            return result('needs_attention', '已有结果未知的删除请求，请先核对原请求')
        with submit.submit_lock(root):
            if not client.conditional_delete_supported():
                return result('delete_guard_unavailable', '平台 DELETE 同时接受待返修单，缺少服务端条件删除保护，已保留原单')
            etags = {}
            fresh = client.request('GET', f'/submissions/{identifier}', response_headers=etags)
            check_identity(fresh)
            if (fresh.get('status') != 'DISCARDED' or fresh.get('stage') not in (None, '', 'DISCARDED')
                    or fresh.get('qc_running') is True):
                return result('checking', '删除前状态已变化，重新分类', 0)
            etag = etags.get('ETag') or ''
            if not re.fullmatch(r'"[!#-~]+"', etag):
                return result('delete_guard_unavailable', '平台详情缺少可用于条件删除的强 ETag')
            result('deleting', '正在按已废弃记录的版本执行条件删除', delete_attempted=True)
            try:
                client.request('DELETE', f'/submissions/{identifier}', headers={'If-Match': etag})
            except platform.PlatformHTTPError as exc:
                if exc.status == 412:
                    return result('checking', '记录版本发生变化，重新检查', 0, delete_attempted=False)
                raise
            return result('delete_acknowledged', '平台已接受删除，等待详情和额度回读', 0, ack_at=clock())
    if status == 'QC_PASSED':
        return result('passed', '平台质检通过')
    if stage == 'QC_RUNNING' or status == 'QC_RUNNING' or detail.get('qc_running') is True and stage != 'QC_WAITING':
        return result('running_qc', '平台正在质检', interval)
    if (stage == 'QC_WAITING' or status == 'QC_WAITING'
            or status == 'SUBMITTED' and not stage and detail.get('qc_running') is False):
        remaining = deadline - clock()
        if remaining <= 0:
            return result('timed_out', '提交满 10 分钟，平台仍待质检，已结束本条跟进')
        return result('waiting', '等待平台质检', min(interval, remaining))
    if status == 'PENDING_FIX':
        lib.assert_platform_prompt_unchanged(task)
        blockers = submit.non_repairable_prompt_checks(detail)
        if blockers:
            marker = submit.discard_platform_task(task, detail, blockers)
            return result('unrepairable', marker['detail'])
        revision = submit.repair_revision(detail)
        ready = lib.load_json(task / READY, default={})
        if ready.get('revision') == revision and ready.get('cycle') == cycle:
            feedback = lib.load_json(task / 'review/feedback.json', default={})
            if ready.get('feedback_digest') == lib.content_hash(feedback):
                if journal.get('attempted_repair') == ready:
                    return result('needs_attention', '同一返修已尝试，平台仍待返修，请核对原单和写入回执')
                result('repair_sending', '正在重新提交原单', attempted_repair=ready)
                try:
                    response = submit.repair_batch(root, task.parent.name, tasks=task.name,
                        execute=True, confirm='REPAIR', client=client, expected_revision=revision,
                        comment='依据本次质检意见修正原单可编辑字段', interval=getattr(options, 'submit_interval', None))
                except lib.FbrError as exc:
                    if '另一个' in str(exc):
                        return result('checking', str(exc), interval, attempted_repair=None)
                    return result('needs_attention', f'原单返修未确认，请核对写入回执：{exc}')
                row = response['tasks'][0]
                if row['state'] in ('confirmed', 'changed', 'pending', 'ok'):
                    return result('checking', '已处理原单返修，重新核对质检状态', 0)
                if row['state'] == 'discarded':
                    return result('unrepairable', row['detail'])
        return result('needs_platform_repair', '待当前任务 AI 返修原单', interval, revision=revision,
                      checks=[c for c in detail.get('qc_checks', []) if c.get('passed') is False],
                      summary=detail.get('qc_summary', ''), last_repair_at=receipt.get('last_repair_at'))
    return result('needs_attention', f'未识别的平台状态 {status!r}')
