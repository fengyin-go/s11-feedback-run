"""Evidence-bound metadata revisions; frozen task and model evidence stay intact."""
from __future__ import annotations
import hashlib
from pathlib import Path, PurePosixPath
import fbrlib as lib

REVISIONS = 'review/field-revisions.json'
SUPPORTED = {'repro_level'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source(task, item):
    if not isinstance(item, dict) or not isinstance(item.get('path'), str):
        raise lib.FbrError('字段修订缺少来源路径')
    relative = PurePosixPath(item['path'])
    if (relative.is_absolute() or '..' in relative.parts or '\\' in item['path']
            or len(relative.parts) < 3 or relative.parts[:2] != ('evidence', 'field-revisions')
            or relative.suffix != '.json'):
        raise lib.FbrError('字段修订来源必须在本题 evidence/field-revisions/*.json 内')
    path = task
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise lib.FbrError('字段修订来源不能是符号链接')
    if not path.is_file() or sha(path) != item.get('sha256'):
        raise lib.FbrError('字段修订来源缺失或摘要已变化：' + item['path'])
    return path


def effective_record(task):
    task = Path(task)
    record = lib.load_task(task)
    path = task / REVISIONS
    if path.is_symlink():
        raise lib.FbrError('字段修订文件不能是符号链接')
    if not path.exists():
        return record
    revision = lib.load_json(path)
    if (not isinstance(revision, dict) or revision.get('version') != 1
            or revision.get('task_sha256') != sha(task / 'task.json')):
        raise lib.FbrError('字段修订不匹配冻结 task.json')
    fields = revision.get('fields')
    if not isinstance(fields, dict) or not fields or set(fields) - SUPPORTED:
        raise lib.FbrError('字段修订包含未支持字段；不得覆盖题面、评分、会话或产物')
    record = dict(record)
    for key, entry in fields.items():
        if (not isinstance(entry, dict) or entry.get('old_value') != record.get(key)
                or not str(entry.get('reason', '')).strip() or not entry.get('verified_at')):
            raise lib.FbrError('字段修订缺少原值、原因或核实时间')
        schema = lib.load_json(source(task, entry.get('schema')))
        if (not isinstance(schema, dict) or not schema.get('fingerprint')
                or schema.get('platform') != 'https://solo2.jzxhnh.com'
                or not schema.get('checked_at')):
            raise lib.FbrError('字段修订缺少已核实的平台 schema')
        matches = [f for f in schema.get('fields', []) if isinstance(f, dict) and f.get('field_key') == key]
        if (len(matches) != 1 or matches[0].get('field_type') != 'select'
                or entry.get('value') not in matches[0].get('options', [])):
            raise lib.FbrError('字段修订新值不是来源 schema 的准确选项')
        evidence = entry.get('sources')
        if not isinstance(evidence, list) or not evidence:
            raise lib.FbrError('字段修订缺少实际环境核实证据')
        for item in evidence:
            source(task, item)
        record[key] = entry['value']
    return record
