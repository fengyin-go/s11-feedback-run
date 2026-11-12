"""One physical attempt per turn; retries rejoin after untouched tasks, without occupying a thread."""
from __future__ import annotations
import concurrent.futures as futures
import time


def rolling(tasks, call, *, parallel, deadline=float('inf'), stopped=lambda: False,
            on_result=lambda task, result: None, clock=time.time, sleeper=time.sleep):
    parallel = max(1, int(parallel))
    queue = [(0.0, i, task) for i, task in enumerate(tasks)]
    serial = len(queue)
    running, final = {}, []
    with futures.ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        while queue or running:
            if stopped() or clock() >= deadline:
                final.extend({'task_id': task.name, 'state': 'deferred', 'detail': '排空或批次截止，未派发'} for _, _, task in queue)
                queue.clear()
            ready = sorted((item for item in queue if item[0] <= clock()), key=lambda item: item[1])
            while ready and len(running) < parallel:
                item = ready.pop(0)
                queue.remove(item)
                running[pool.submit(call, item[2])] = item[2]
            if not running:
                if queue:
                    sleeper(min(1, max(0.01, min(v[0] for v in queue) - clock())))
                continue
            done, _ = futures.wait(running, timeout=1, return_when=futures.FIRST_COMPLETED)
            for future in done:
                task = running.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {'task_id': task.name, 'state': 'blocked', 'detail': str(exc)}
                on_result(task, result)
                if result.get('state') in ('retry', 'running') and not stopped() and clock() < deadline:
                    queue.append((result.get('retry_after', clock() + (5 if result.get('state') == 'running' else 0)), serial, task))
                    serial += 1
                else:
                    final.append(result)
    return final
