"""Verify terminal success before cleaning up a CLI that failed to exit."""
from pathlib import Path
import hashlib
import json
import time

import fbrlib as lib
import fbr_trace as trace


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def terminal(path, model):
    try:
        data = Path(path).read_bytes()
        if not data.endswith(b'\n'):
            return None
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        if not rows or not all(isinstance(row, dict) for row in rows):
            return None
        row = rows[-1]
        parsed = trace.read_stream(Path(path))
        if (parsed['problems'] or parsed['model'] != model or row.get('type') != 'result'
                or row.get('subtype') != 'success' or row.get('is_error') is not False
                or row.get('terminal_reason') != 'completed' or row.get('stop_reason') != 'end_turn'
                or not row.get('uuid') or not str(row.get('result', '')).strip()
                or row.get('parent_tool_use_id') or parsed['session_id'] != row.get('session_id')):
            return None
        assistants = [r for r in rows if r.get('type') == 'assistant'
                      and r.get('session_id') == row['session_id'] and not r.get('parent_tool_use_id')
                      and any(b.get('type') == 'text' for b in (r.get('message') or {}).get('content', [])
                              if isinstance(b, dict))]
        if not assistants:
            return None
        text = '\n'.join(b.get('text', '') for b in assistants[-1]['message']['content']
                         if isinstance(b, dict) and b.get('type') == 'text')
        return row if text == row['result'] else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def cleanup(task, cfg, current, request):
    """Export and verify trace before stopping only the owned completed container.

    The worker writes its real process exit status. This proof permits the run
    classifier to distinguish post-completion cleanup from an interrupted run.
    """
    path = Path(request['stdout'])
    result = terminal(path, cfg.get('model', 'auto_model/urm'))
    if not result or time.time() - path.stat().st_mtime < 30:
        return None
    inspected = lib.run(['docker', 'inspect', '--format', '{{json .}}', task.name], timeout=30)
    if inspected.returncode:
        return None
    info = json.loads(inspected.stdout)
    if (info.get('Id') != current.get('container_id') or
            info.get('Config', {}).get('Labels', {}).get('solo11.owner') != lib.content_hash(str(task.resolve()))):
        raise lib.FbrError('完成收尾的容器归属不符，保留现场')
    directory = task / 'run' / ('completed-' + request['id'])
    directory.mkdir(exist_ok=True)
    projects = directory / 'projects'
    if projects.exists():
        raise lib.FbrError('完成收尾已有检查点，需核对后恢复，不覆盖原证据')
    copied = lib.run(['docker', 'cp', f'{task.name}:{lib.TRACE_SRC}', str(projects)], timeout=300)
    if copied.returncode:
        raise lib.FbrError('完成事件后的轨迹导出失败，不停止容器')
    record = lib.load_json(task / 'task.json')
    ids = trace.read_ids(projects, record['prompt'], current.get('continuations', []))
    rows = lib.read_jsonl(projects.parent / ids.get('session_file', 'missing')) if ids.get('session_file') else []
    if (ids['problems'] or ids['session_id'] != result['session_id']
            or trace.final_answer(rows) != result['result']):
        raise lib.FbrError('完成事件与导出主轨迹不一致，不停止容器')
    proof = {'version': 1, 'request_id': request['id'], 'container_id': info['Id'],
             'session_id': ids['session_id'], 'result_uuid': result['uuid'],
             'stdout_sha256': sha(path), 'model': cfg.get('model', 'auto_model/urm'),
             'trace': {p.relative_to(task).as_posix(): sha(p) for p in projects.rglob('*.jsonl')},
             'exported_at': lib.now_iso(), 'state': 'exported'}
    proofpath = directory / 'cleanup.json'
    lib.save_json(proofpath, proof)
    if terminal(path, proof['model']) != result or sha(path) != proof['stdout_sha256']:
        raise lib.FbrError('完成事件后又出现输出，保留现场，不执行收尾')
    stopped = lib.run(['docker', 'stop', task.name], timeout=60)
    proof.update(state='stopped' if stopped.returncode == 0 else 'stop_failed',
                 stop_rc=stopped.returncode, stopped_at=lib.now_iso())
    lib.save_json(proofpath, proof)
    if stopped.returncode:
        raise lib.FbrError('完成收尾停止失败，保留检查点')
    return proofpath.relative_to(task).as_posix()


def valid(task, current, done):
    relative = current.get('completion_cleanup')
    expected = 'run/completed-' + str(current.get('request_id')) + '/cleanup.json'
    if relative != expected or done.get('id') != current.get('request_id') or done.get('timed_out'):
        return False
    try:
        proof = lib.load_json(task / relative)
        result = terminal(Path(current['stdout']), proof['model'])
        return bool(proof.get('state') == 'stopped' and proof.get('stop_rc') == 0
                    and proof.get('request_id') == done['id'] and proof.get('container_id') == current.get('container_id')
                    and result and proof.get('session_id') == result['session_id']
                    and proof.get('result_uuid') == result['uuid']
                    and sha(current['stdout']) == proof.get('stdout_sha256')
                    and proof.get('trace') and all(sha(task / p) == digest for p, digest in proof['trace'].items()))
    except (OSError, KeyError, TypeError, ValueError):
        return False
