"""Reuse verified repository metadata; fields never inferred from prompt contents."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import fbrlib as lib

FIELDS = ('domain','subdirection','language','frameworks','harness','harness_version','os','repro_level')


def profile(root, baseline, values, source):
    identity = {k:baseline.get(k) for k in ('source_repo','initial_commit','image_id')}
    key = lib.content_hash(identity)
    path = Path(root)/'_registry/repo-profiles'/f'{key}.json'
    incoming = {k:values.get(k) for k in FIELDS}
    existing = lib.load_json(path,default={})
    if existing:
        incoming = {k: v if v not in (None, '') else existing.get('fields', {}).get(k) for k, v in incoming.items()}
        if existing.get('identity') != identity or existing.get('fields') != incoming:
            raise lib.FbrError('同一基线公共字段与已核实档案冲突；先核实来源，不逐题猜值')
        return path, existing
    if any(v is None or v == '' for v in incoming.values()):
        raise lib.FbrError('基础仓库公共字段未完整核实，不能生成复用档案')
    doc={'version':1,'identity':identity,'fields':incoming,'source':source,'verified_at':lib.now_iso()}
    lib.save_json(path,doc)
    return path,doc


def repair_from_peer(task, root):
    """Only exact frozen baseline peers with a current certificate are eligible."""
    import fbr_reviewer
    import fbr_fields
    target=lib.load_task(task)
    key=(target.get('repo_id'),target.get('initial_commit'),target.get('image_id'))
    for peer in (Path(root)/'batches').glob('*/*/task.json'):
        peer=peer.parent
        if peer==task: continue
        record=lib.load_task(peer)
        if (record.get('repo_id'),record.get('initial_commit'),record.get('image_id')) != key: continue
        if fbr_reviewer.provenance_errors(peer): continue
        record=fbr_fields.effective_record(peer)
        # Only an already evidence-bound repro_level revision is propagated. Other frozen fields must agree.
        if any(target.get(k)!=record.get(k) for k in FIELDS if k!='repro_level'): continue
        revision=lib.load_json(peer/fbr_fields.REVISIONS,default={})
        entry=revision.get('fields',{}).get('repro_level')
        if not entry or target.get('repro_level')==record.get('repro_level'): continue
        import copy,shutil
        new=copy.deepcopy(entry);new['old_value']=target.get('repro_level')
        for ref in [new['schema'],*new['sources']]:
            src=fbr_fields.source(peer,ref)
            dst=task/ref['path'];dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
        lib.save_json(task/fbr_fields.REVISIONS,{'version':1,'task_sha256':fbr_fields.sha(task/'task.json'), 'fields':{'repro_level':new}})
        return True
    return False
