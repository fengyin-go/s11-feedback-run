"""Persist execution receipts; progressing model work has no fixed wall-clock kill."""
from pathlib import Path
import json
import subprocess
import sys
import time
import fbrlib as lib


def execute(request):
    import fbr_runtime as runtime
    policy = request.get('progress_policy')
    watcher = runtime.Watcher(time.time()) if policy else None
    timed_out, reason = False, ''
    waiting_started, waiting_total = 0, 0
    with open(request['stdout'], 'wb') as out, open(request['stderr'], 'wb') as err:
        try:
            child = subprocess.Popen(request['command'], stdin=subprocess.PIPE, stdout=out, stderr=err)
            lib.save_json(Path(request['result']).with_suffix('.pid.json'),
                          {'id': request['id'], 'pid': child.pid, 'identity': runtime.process_identity(child.pid)})
            first = True
            while True:
                try:
                    interval = min(1, max(.02, policy.get('poll_seconds', 1))) if policy else max(.1,
                        request.get('guard_deadline', time.time() + 7200) - time.time())
                    child.communicate(request['stdin'].encode() if first else None, timeout=interval)
                    rc = child.returncode
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    if policy:
                        now = time.time()
                        watcher.poll(Path(request['stdout']), now)
                        protected = watcher.protected(now, policy)
                        if protected and not waiting_started:
                            waiting_started = now
                        elif waiting_started and not protected:
                            waiting_total += now - waiting_started
                            waiting_started = 0
                        spent = waiting_total + (now - waiting_started if waiting_started else 0)
                        base_wait = request.get('base_budget', {}).get('waiting_seconds', 0)
                        attempt_wait = base_wait + spent - request.get('attempt_waiting_base', base_wait)
                        if waiting_started and (now - waiting_started >= policy['gateway_wait_limit']
                                or attempt_wait >= policy['gateway_wait_total']
                                or (policy.get('gateway_wait_task_total') is not None
                                    and base_wait + spent >= policy['gateway_wait_task_total'])):
                            reason = 'gateway_wait_exhausted'
                        elif runtime.progress_alive(watcher, now, policy):
                            continue
                        else:
                            reason = 'stalled'
                    else:
                        reason = 'worker_guard'
                    timed_out, rc = True, 124
                    task = Path(request['task'])
                    owner = lib.run(['docker', 'inspect', '--format',
                        '{{index .Config.Labels "solo11.owner"}}', task.name], timeout=30)
                    if owner.returncode == 0 and owner.stdout.strip() == lib.content_hash(str(task.resolve())):
                        lib.run(['docker', 'kill', task.name], timeout=60)
                    child.kill()
                    child.communicate(timeout=10)
                    break
        except OSError:
            rc = 127
    return {'id': request['id'], 'rc': rc, 'timed_out': timed_out,
            'reason': reason, 'ended_epoch': time.time()}


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text())
    lib.save_json(Path(request['result']), execute(request))
