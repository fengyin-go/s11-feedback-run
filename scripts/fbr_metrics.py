"""Append stage queue/execution measurements without storing prompts or credentials."""
from __future__ import annotations
import time
import fbrlib as lib


def stage(task, name, queued, fn, *args):
    start = time.time()
    try:
        result = fn(*args)
        state = result.get('state','ok') if isinstance(result,dict) else 'ok'
        return result
    except Exception:
        state = 'error'
        raise
    finally:
        lib.append_jsonl(task/'review/timings.jsonl', {'stage':name,'at':lib.now_iso(),
            'queue_seconds':round(start-queued,3),'execution_seconds':round(time.time()-start,3),'state':state})
