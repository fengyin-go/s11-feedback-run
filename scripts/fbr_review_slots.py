"""Cross-process review cap; blocked/probe work holds no model slot."""
import contextlib
import os
from pathlib import Path
import time
import fbrlib as lib


@contextlib.contextmanager
def slot(cfg):
    root=Path(os.environ.get('FBR_RUNTIME_HOME','~/.codex/solo11/runtime')).expanduser()/'review-slots'
    root.mkdir(parents=True,exist_ok=True)
    deadline=cfg.get('_deadline',float('inf'))
    limit=min(5,max(1,int(cfg.get('review_parallel',5))))
    held=None
    while held is None:
        for i in range(limit):
            candidate=lib.file_lock(root/f'{i}.lock')
            try:candidate.__enter__()
            except lib.FbrError:continue
            held=candidate;break
        if held is None:
            if time.time()>=deadline:raise lib.FbrError('等待评价槽达到批次预算，保留检查点')
            time.sleep(0.25)
    try:yield
    finally:held.__exit__(None,None,None)
