"""持久执行 docker exec；父调度退出后仍有绝对截止保护与可核验回执。"""
import json
from pathlib import Path
import subprocess
import sys
import time
import fbrlib as lib

if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text())
    timed_out = False
    with open(request['stdout'], 'wb') as out, open(request['stderr'], 'wb') as err:
        try:
            child = subprocess.Popen(request['command'], stdin=subprocess.PIPE, stdout=out, stderr=err)
            lib.save_json(Path(request['result']).with_suffix('.pid.json'),
                          {'id': request['id'], 'pid': child.pid})
            try:
                child.communicate(request['stdin'].encode(), timeout=max(.1,
                    request.get('guard_deadline', time.time() + 7200) - time.time()))
                rc = child.returncode
            except subprocess.TimeoutExpired:
                timed_out, rc = True, 124
                task = Path(request['task'])
                owner = lib.run(['docker', 'inspect', '--format',
                    '{{index .Config.Labels "solo11.owner"}}', task.name], timeout=30)
                if owner.returncode == 0 and owner.stdout.strip() == lib.content_hash(str(task.resolve())):
                    lib.run(['docker', 'kill', task.name], timeout=60)
                child.kill()
                child.communicate(timeout=10)
        except OSError:
            rc = 127
        lib.save_json(Path(request['result']), {'id': request['id'], 'rc': rc,
            'timed_out': timed_out, 'ended_epoch': time.time()})
