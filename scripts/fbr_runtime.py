"""共享额度、压力控制、持久执行与进展监控。无平台写操作。"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time
import uuid

import fbrlib as lib

STREAM = contextvars.ContextVar('fbr_stream', default=None)
LIMIT = re.compile(r'max_parallel_requests|rate.?limit|\b429\b|\bTPM\b', re.I)
SOFT = re.compile(r'\b(?:500|502|503|504|529)\b|overloaded', re.I)
AUTH = re.compile(r'\b(?:401|403)\b|unauthorized|invalid.?api.?key', re.I)
NETWORK = re.compile(r'TLS|SSL|ECONN|ENOTFOUND|EAI_AGAIN|DNS|connection refused', re.I)


def gateway_signal(row):
    """Only API error payloads carry pressure; token counts and IDs are metrics."""
    kind = row.get('type')
    retry = kind == 'system' and row.get('subtype') == 'api_retry'
    if not (kind == 'error' or retry or
            (kind == 'system' and row.get('subtype') in ('api_error', 'error')) or
            (kind == 'result' and row.get('is_error'))):
        return ''
    text = json.dumps({key: row[key] for key in
        ('error_status', 'status', 'status_code', 'error', 'message', 'result') if key in row}, ensure_ascii=False)
    if LIMIT.search(text):
        return 'limit'
    if SOFT.search(text):
        return 'soft'
    if NETWORK.search(text) or retry:
        return 'network'
    return ''


def process_identity(pid=None):
    pid = pid or os.getpid()
    p = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def alive(info):
    return bool(info.get('pid') and info.get('identity') and
                process_identity(info['pid']) == info['identity'])


def runtime_home(cfg):
    return Path(os.environ.get('FBR_RUNTIME_HOME') or cfg.get('runtime_home') or
                '~/.codex/solo11/runtime').expanduser()


def auth_error(segment, stderr):
    result = segment.get('result') or {}
    text = str(result.get('text', ''))
    api_text = text if result.get('is_error') or re.match(r'\s*API Error\b', text) else ''
    return bool(AUTH.search(stderr + ' ' + api_text))


def batch_deadline(root, batch, cfg):
    directory = lib.batch_dir(root, batch)
    path = directory / 'runtime-budget.json'
    with lib.file_lock(directory / '.runtime-budget.lock'):
        candidates = [time.time() + cfg['run']['batch_budget']]
        for source in (path, directory / 'flow/plan.json', directory / 'runner/budget.json'):
            value = lib.load_json(source, default={}).get('deadline')
            if value is not None:
                candidates.append(float(value))
        deadline = min(candidates)
        lib.save_json(path, {'deadline': deadline})
        return deadline


def pressure_update(state, event, options, now):
    """纯状态转换，时间由调用者注入，便于验证阈值和冷却。"""
    ceiling = int(state['ceiling'])
    cap = min(ceiling, int(state.get('cap', ceiling)))
    if event in ('limit', 'soft', 'network'):
        if state.get('spike_until', 0) > now:
            cap = min(cap, state.get('pre_spike', cap))
            state.update(spike_until=0, next_spike=now + options['spike_backoff'],
                         last_ramp=now, successes=0, probe_ok=0)
        elif now - state.get('last_drop', -1e20) >= options['downgrade_dedup']:
            floor = 1 if event == 'limit' else min(ceiling, options['soft_floor'])
            cap = min(cap, max(floor, cap - 1))
            state['last_drop'] = now
            state.setdefault('initial_drop', now)
            state.setdefault('next_spike', now + options['spike_interval'])
        if event == 'limit':
            state.update(last_limit=now, successes=0, probe_ok=0)
    elif event == 'success':
        state['successes'] = state.get('successes', 0) + 1
    elif event in ('probe_ok', 'probe_fail'):
        state['probe_ok'] = state.get('probe_ok', 0) + 1 if event == 'probe_ok' else 0
        state['last_probe'] = now
    stable = now - state.get('last_limit', state.get('initial_drop', 0)) >= options['stable_seconds']
    cooled = now - state.get('last_ramp', 0) >= options['stable_seconds']
    if cap < ceiling and stable and cooled and (state.get('successes', 0) >= 1 or
                                               state.get('probe_ok', 0) >= options['probe_successes']):
        cap += 1
        state.update(last_ramp=now, successes=0, probe_ok=0)
    elif cap < ceiling and cooled and now >= state.get('next_spike', float('inf')):
        state.update(pre_spike=cap, spike_until=now + options['spike_watch'],
                     next_spike=now + options['spike_interval'])
        cap = ceiling
    if state.get('spike_until', 0) and now >= state['spike_until']:
        state['spike_until'] = 0
    state['cap'] = min(ceiling, cap)
    return state


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        # 额度按网关+实际 key 指纹共享，不按 key 文件路径分裂。不保存原文。
        digest = hashlib.sha256(('https://llm2.jzxhnh.com\0' + lib.read_key(cfg)).encode()).hexdigest()
        self.path = runtime_home(cfg) / ('gateway-' + digest[:24])
        self.options = cfg['gateway']
        self.token = ''

    @contextlib.contextmanager
    def transaction(self):
        # 短临界区只做文件操作；锁竞争可重试，不在锁内睡眠/探网关。
        while True:
            lock = lib.file_lock(self.path / 'quota.lock')
            try:
                lock.__enter__()
                break
            except lib.FbrError as exc:
                if '另一个进程' not in str(exc):
                    raise
                time.sleep(.05)
        try:
            data = lib.load_json(self.path / 'pressure.json', default={})
            ceiling = int(self.options['machine_parallel'])
            if data.get('ceiling', ceiling) != ceiling:
                raise lib.FbrError('同一网关凭据的 machine_parallel 配置不一致，先统一各运行入口的额度配置')
            data.update(ceiling=ceiling, cap=min(ceiling, data.get('cap', ceiling)))
            yield data
            lib.save_json(self.path / 'pressure.json', data)
        finally:
            lock.__exit__(None, None, None)

    def event(self, kind):
        with self.transaction() as data:
            if kind == 'auth':
                data['auth_blocked'] = True
            pressure_update(data, kind, self.options, time.time())

    def acquire(self, deadline, *, once=False):
        deadline = min(deadline, self.cfg.get('_deadline', float('inf')))
        while time.time() < deadline:
            if self.cfg.get('_drain') and Path(self.cfg['_drain']).exists():
                return False
            with self.transaction() as data:
                if data.get('auth_blocked'):
                    raise lib.FbrError('同一网关凭据已出现鉴权失败，暂停新轮，检查凭据')
                slots = {k: v for k, v in data.get('slots', {}).items() if alive(v)}
                data['slots'] = slots
                pressure_update(data, 'tick', self.options, time.time())
                if len(slots) < data['cap']:
                    self.token = uuid.uuid4().hex
                    slots[self.token] = {'pid': os.getpid(), 'identity': process_identity(), 'at': time.time()}
                    return True
            if once:
                return False
            time.sleep(min(1, max(0, deadline - time.time())))
        return False

    def release(self):
        if self.token:
            with self.transaction() as data:
                data.setdefault('slots', {}).pop(self.token, None)
            self.token = ''

    def transfer(self, pid):
        if self.token:
            with self.transaction() as data:
                if self.token in data.get('slots', {}):
                    data['slots'][self.token].update(pid=pid, identity=process_identity(pid))

    def track_existing(self, info, *, claim=False):
        """Count a verified surviving worker after its original monitor exited."""
        if not alive(info):
            return False
        with self.transaction() as data:
            slots = {key: value for key, value in data.get('slots', {}).items() if alive(value)}
            token = next((key for key, value in slots.items() if
                value['pid'] == info['pid'] and value['identity'] == info['identity']), '')
            if not token:
                token = uuid.uuid4().hex
                slots[token] = {'pid': info['pid'], 'identity': info['identity'],
                    'at': time.time(), 'recovered': True}
            data['slots'] = slots
            if claim:
                self.token = token
        return True

    def probe_due(self):
        with self.transaction() as data:
            now = time.time()
            if data['cap'] >= data['ceiling'] or now - data.get('last_probe', 0) < self.options['probe_interval']:
                return False
            # 跨进程领探针；占共享槽位，失败只清连续计数。
            data['last_probe'] = now
            return True

    def health_probe(self):
        if not self.probe_due() or not self.acquire(time.time() + 1, once=True):
            return
        name = 's11-health-' + uuid.uuid4().hex[:12]
        try:
            result = lib.run(['docker', 'run', '-i', *lib.docker_hardening(), '--name', name,
                              '--env-file', str(lib.key_path(self.cfg)), '--entrypoint',
                              '/usr/local/bin/entrypoint.sh', self.cfg['image'], 'print'],
                             input_text='只回复 OK，不读文件、不调用工具。', timeout=90)
            rows = []
            for line in result.stdout.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
            ok = result.returncode == 0 and any(isinstance(r, dict) and r.get('type') == 'result' and
                r.get('is_error') is False and str(r.get('result', '')).strip() == 'OK' for r in rows)
            self.event('probe_ok' if ok else 'probe_fail')
        finally:
            lib.run(['docker', 'stop', name], timeout=30)
            lib.run(['docker', 'rm', name], timeout=30)
            self.release()


class Watcher:
    def __init__(self, started):
        self.offset = 0
        self.pending = b''
        self.progress = self.output = started
        self.retry_at = self.previous_retry = 0
        self.retry_interval = 0
        self.kind = ''
        self.session_id = self.model = ''
        self.events = self.tools = 0
        self.active_tools = set()
        self.last_type = ''

    def poll(self, path, now):
        if not Path(path).exists():
            return
        with open(path, 'rb') as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        if chunk:
            self.output = now
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()
        for raw in lines:
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            self.events += 1
            kind = row.get('type')
            self.last_type = kind
            if kind == 'system' and row.get('subtype') == 'init':
                self.session_id = row.get('session_id', '')
                self.model = row.get('model', '')
            # system 还包含 thinking_tokens 等正常指标，不能将其数值当 HTTP 状态。
            signal = gateway_signal(row)
            if signal:
                self.retry_interval = now - self.retry_at if self.retry_at else 0
                self.previous_retry, self.retry_at = self.retry_at, now
                self.kind = signal
            # The harness emits these only from API thinking_delta/signature_delta,
            # not from an elapsed-time spinner. Positive increments mean the API
            # is producing content again, even before a full assistant message.
            delta, total = row.get('estimated_tokens_delta'), row.get('estimated_tokens')
            if (kind == 'system' and row.get('subtype') == 'thinking_tokens'
                    and type(delta) is int and type(total) is int and total >= delta > 0):
                self.progress = now
                self.retry_at = 0
            if kind in ('assistant', 'user', 'result'):
                self.progress = now
                self.retry_at = 0
                self.tools += sum(b.get('type') == 'tool_use' for b in row.get('message', {}).get('content', [])
                                  if isinstance(b, dict)) if isinstance(row.get('message', {}).get('content'), list) else 0
                content = row.get('message', {}).get('content', [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') == 'tool_use' and block.get('id'):
                            self.active_tools.add(block['id'])
                        elif block.get('type') == 'tool_result':
                            self.active_tools.discard(block.get('tool_use_id'))

    def protected(self, now, cfg):
        grace = min(cfg['gateway_grace_max'], max(cfg['gateway_grace'], self.retry_interval * 3))
        return self.retry_at > 0 and now - self.retry_at <= grace


@contextlib.contextmanager
def stream_context(task, cfg, attempt, segment, gateway):
    token = STREAM.set({'task': task, 'cfg': cfg, 'attempt': attempt, 'segment': segment, 'gateway': gateway})
    try:
        yield
    finally:
        STREAM.reset(token)


def monitored_stream(command, stdin_text, stdout_path, stderr_path, timeout, context):
    """小 worker 脱离父进程留存结果；父进程断开后可核对 PID 和回执接管。"""
    task, cfg, gateway = context['task'], context['cfg']['run'], context['gateway']
    request_id = uuid.uuid4().hex
    request_path = task / 'run' / f'exec-{request_id}.json'
    result_path = task / 'run' / f'exit-{request_id}.json'
    starting_budget = lib.load_json(task / 'run/budget.json', default={'active_seconds': 0, 'waiting_seconds': 0})
    current = lib.load_json(task / 'run/current.json', default={})
    attempt_wait_base = current.get('attempt_waiting_base', 0 if current.get('segment', 0) else starting_budget.get('waiting_seconds', 0))
    attempt_wait_used = max(0, starting_budget.get('waiting_seconds', 0) - attempt_wait_base)
    wait_remaining = max(0, cfg['gateway_wait_total'] - attempt_wait_used)
    if cfg.get('gateway_wait_task_total') is not None:
        wait_remaining = min(wait_remaining, max(0, cfg['gateway_wait_task_total'] - starting_budget.get('waiting_seconds', 0)))
    guard = min(context['cfg'].get('_deadline', float('inf')), time.time() +
        min(timeout + cfg['grace_seconds'], max(0, cfg['task_budget'] - starting_budget['active_seconds'])) + wait_remaining)
    request = {'id': request_id, 'command': command, 'stdin': stdin_text,
               'task': str(task), 'guard_deadline': guard, 'started_epoch': time.time(), 'base_budget': starting_budget,
               'attempt_waiting_base': attempt_wait_base,
               'stdout': str(stdout_path), 'stderr': str(stderr_path), 'result': str(result_path), 'timeout': timeout}
    lib.save_json(request_path, request)
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('fbr_worker.py')), str(request_path)],
                               start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.time()
    base = lib.load_json(task / 'run/current.json', default={})
    base.update(pid=process.pid, identity=process_identity(process.pid), request_id=request_id,
                result=str(result_path), stdout=str(stdout_path), stderr=str(stderr_path),
                segment=context['segment'], state='running')
    lib.save_json(task / 'run/current.json', base)
    gateway.transfer(process.pid)
    return monitor_process(process, request, base, context, started=started)


class ExistingProcess:
    """Observe a surviving worker by PID identity; never launch another command."""
    def __init__(self, current):
        self.current = current
        self.pid = current['pid']

    def poll(self):
        return None if alive(self.current) else 0

    def wait(self, timeout):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            receipt = lib.load_json(Path(self.current['result']), default={})
            if receipt.get('id') == self.current['request_id'] and 'rc' in receipt:
                return receipt['rc']
            if not alive(self.current):
                return 0
            time.sleep(.1)
        raise subprocess.TimeoutExpired('existing worker', timeout)

    def kill(self):
        if alive(self.current):
            os.kill(self.pid, signal.SIGKILL)


def monitor_existing(task, cfg, current):
    request = lib.load_json(task / 'run' / ('exec-' + current['request_id'] + '.json'), default={})
    if request.get('id') != current['request_id'] or request.get('task') != str(task):
        raise lib.FbrError('存活 worker 的原始派发记录不匹配，不能恢复监控')
    for key in ('stdout', 'stderr', 'result'):
        if request.get(key) != current.get(key) or not Path(request[key]).resolve().is_relative_to((task / 'run').resolve()):
            raise lib.FbrError('存活 worker 的派发输出路径不匹配，不能恢复监控')
    if not isinstance(request.get('started_epoch'), (int, float)) or request['started_epoch'] > time.time():
        raise lib.FbrError('存活 worker 的派发时间不完整，不能恢复监控')
    gateway = Gateway(cfg)
    if not gateway.track_existing(current, claim=True):
        return
    current.update(owner_pid=os.getpid(), owner_identity=process_identity(), monitor_recovered_at=lib.now_iso())
    lib.save_json(task / 'run/current.json', current)
    try:
        return monitor_process(ExistingProcess(current), request, current,
            {'task': task, 'cfg': cfg, 'gateway': gateway}, adopted=True)
    finally:
        gateway.release()


def monitor_process(process, request, base, context, *, started=None, adopted=False):
    task, cfg, gateway = context['task'], context['cfg']['run'], context['gateway']
    request_id, result_path, stdout_path = request['id'], Path(request['result']), Path(request['stdout'])
    started = request['started_epoch'] if started is None else started
    base_budget = request['base_budget']
    previous = lib.load_json(task / 'run/budget.json', default=base_budget)
    timeout = request.get('timeout', min(cfg['timeout'], max(1, cfg['task_budget'] - base_budget['active_seconds'])))
    watcher = Watcher(time.time())
    if adopted:
        # Reconstruct tool/session state, but historical retries are not new gateway events.
        now = time.time()
        watcher.poll(stdout_path, now)
        watcher.retry_at = watcher.previous_retry = watcher.retry_interval = 0
        watcher.kind = ''
        watcher.output = min(now, stdout_path.stat().st_mtime) if stdout_path.exists() else now
    extended = False
    waiting_started = 0
    waiting_total = max(0, previous.get('waiting_seconds', 0) - base_budget.get('waiting_seconds', 0)) if adopted else 0
    last_pressure = 0
    deadline = started + timeout + waiting_total

    def save_budget(now, spent_wait):
        previous = lib.load_json(task / 'run/budget.json', default=base_budget)
        lib.save_json(task / 'run/budget.json', {
            'active_seconds': max(previous['active_seconds'], base_budget['active_seconds'] + now - started - spent_wait),
            'waiting_seconds': max(previous.get('waiting_seconds', 0), base_budget.get('waiting_seconds', 0) + spent_wait)})
    while True:
        now = time.time()
        watcher.poll(stdout_path, now)
        done = lib.load_json(result_path, default={})
        if done.get('id') == request_id and 'rc' in done:
            process.wait(timeout=10)
            base.update(state='exited', rc=done['rc'], ended_at=lib.now_iso())
            lib.save_json(task / 'run/current.json', base)
            spent_wait = waiting_total + (now - waiting_started if waiting_started else 0)
            save_budget(now, spent_wait)
            return done['rc'], bool(done.get('timed_out'))
        # A completed print result can precede a CLI shutdown hang. Export its
        # matching main trace, then stop the owned container; retain the real
        # worker exit code and a separate verified cleanup proof.
        if not base.get('completion_cleanup') and watcher.last_type == 'result' and not watcher.active_tools:
            import fbr_completion
            if fbr_completion.terminal(stdout_path, context['cfg'].get('model', 'auto_model/urm')):
                proof = fbr_completion.cleanup(task, context['cfg'], base, request)
                if proof:
                    base['completion_cleanup'] = proof
                    lib.save_json(task / 'run/current.json', base)
                    continue
        protected = watcher.protected(now, cfg)
        if protected and not waiting_started:
            # The worker retries autonomously and can resume without admission.
            # Keep its reservation until it exits so retry recovery cannot exceed cap.
            waiting_started = now
        elif waiting_started and not protected:
            duration = now - waiting_started
            waiting_total += duration
            deadline += duration
            waiting_started = 0
        if watcher.retry_at > last_pressure:
            gateway.event(watcher.kind)
            last_pressure = watcher.retry_at
        heartbeat = {'at': lib.now_iso(), 'elapsed': now - started, 'idle': now - watcher.progress,
                     'output_idle': now - watcher.output, 'events': watcher.events, 'tools': watcher.tools,
                     'session_id': watcher.session_id, 'model': watcher.model,
                     'gateway_wait': bool(waiting_started), 'waiting_seconds': waiting_total +
                         (now - waiting_started if waiting_started else 0), 'pid': process.pid,
                     'attempt_waiting_base': request.get('attempt_waiting_base', base_budget.get('waiting_seconds', 0)),
                     'attempt_waiting_seconds': max(0, base_budget.get('waiting_seconds', 0) + waiting_total + (now - waiting_started if waiting_started else 0) - request.get('attempt_waiting_base', base_budget.get('waiting_seconds', 0))),
                     'waiting_limit_attempt': cfg['gateway_wait_total'], 'waiting_limit_task': cfg.get('gateway_wait_task_total')}
        lib.save_json(task / 'run/heartbeat.json', heartbeat)
        spent_wait = waiting_total + (now - waiting_started if waiting_started else 0)
        save_budget(now, spent_wait)
        reason = ''
        if waiting_started:
            if (now - waiting_started >= cfg['gateway_wait_limit']
                    or base_budget.get('waiting_seconds', 0) + spent_wait - request.get('attempt_waiting_base', base_budget.get('waiting_seconds', 0)) >= cfg['gateway_wait_total']
                    or (cfg.get('gateway_wait_task_total') is not None and base_budget.get('waiting_seconds', 0) + spent_wait >= cfg['gateway_wait_task_total'])):
                reason = 'gateway_wait_exhausted'
        elif now >= deadline:
            if not extended and now - watcher.progress <= 180:
                deadline += cfg['grace_seconds']
                extended = True
            else:
                reason = 'timeout'
        # 无输出且无近期网关信号才判卡住；长工具等待仍有绝对预算限制。
        elif now - max(watcher.progress, watcher.output) >= cfg['stall_seconds'] and not protected and not watcher.active_tools:
            reason = 'stalled'
        active = base_budget['active_seconds'] + now - started - spent_wait
        if active >= cfg['task_budget']:
            reason = 'task_budget'
        if now >= context['cfg'].get('_deadline', float('inf')):
            reason = 'batch_budget'
        if now >= request.get('guard_deadline', float('inf')):
            reason = 'worker_guard'
        if reason:
            before_stop_bytes = stdout_path.stat().st_size if stdout_path.exists() else 0
            # 先停止容器内进程，再等 worker 写出退出码。原始证据仍在容器中。
            lib.run(['docker', 'kill', task.name], timeout=60)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            diagnosis = {'at': lib.now_iso(), 'reason': reason, 'attempt': base.get('attempt'), 'stream_bytes_before_stop': before_stop_bytes,
                         'request_id': base.get('request_id'), 'monitor_rc': 124,
                         'elapsed': now - started, 'active_seconds': active,
                         'waiting_seconds': base_budget.get('waiting_seconds', 0) + spent_wait,
                         'heartbeat': heartbeat,
                         'limits': {k: cfg.get(k) for k in ('gateway_wait_limit', 'gateway_wait_total', 'task_budget', 'timeout', 'stall_seconds')},
                         'batch_deadline': context['cfg'].get('_deadline'),
                         'worker_exit': lib.load_json(Path(base.get('result', '/nonexistent')), default={})}
            lib.save_json(task / 'run' / ('termination-' + str(base.get('request_id', 'unknown')) + '.json'), diagnosis)
            base.update(state='exited', rc=124, timed_out=True, reason=reason, stream_bytes_before_stop=before_stop_bytes)
            lib.save_json(task / 'run/current.json', base)
            return 124, True
        if process.poll() is not None:
            raise lib.FbrError('执行 worker 退出但缺少退出回执，保留容器，先 recover 核对')
        time.sleep(min(cfg['poll_seconds'], 5))
