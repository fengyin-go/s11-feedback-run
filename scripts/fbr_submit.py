#!/usr/bin/env python3
"""SOLO2 单轮数据：计划、上传、建单、回读；默认不写平台。"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib
import fbr_trace
import fbr_delivery as delivery

BASE_URL = "https://solo2.jzxhnh.com"
ACCOUNT = os.environ.get("SOLO11_PLATFORM_ACCOUNT", "zouyonghong")
FIELD_MAP = {
    "domain": "所属领域", "sub_direction": "子方向", "question_type": "任务类型",
    "difficulty": "任务难度", "languages": "语言/框架", "harness": "Harness",
    "harness_version": "Harness 版本", "os_platform": "操作系统", "repro_level": "环境可复现等级",
    "env_snapshot": "初始环境快照", "user_prompt": "User Prompt", "session_id": "SessionID",
    "turn_id": "TurnID/PromptID", "artifact_snapshot": "产物快照", "score_delivery": "交付完整性",
    "feedback": "Feedback", "other_issues": "备注",
}
MEDIA_TYPES = {"trace_file": "attachment", "screencast": "video"}
DENSE_REPORT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z]{2,}-[0-9]+|[0-9]{2}:[0-9]{2}(?::[0-9]{2})?|[0-9]{3,})(?![A-Za-z0-9_])")
MACHINE_STYLE_RE = re.compile(r"\bTS\d+\b|python\s+-I|_enable\b|JSONDecodeError|Traceback")
BARE_TEST_COUNT_RE = re.compile(
    r"(?:\d+\s*/\s*\d+|Ran\s+\d+\s+tests|"
    r"\d+\s*项(?:后端|服务|回归|验收|边界|场景)?(?:测试|断言|回归|验收|场景)?\s*(?:通过|全过)?)",
    re.I,
)
AI_STYLE_COUNT_RE = re.compile(r"[一二两三四五六七八九十百0-9]+\s*(?:条|次|项|个|段|份|轮)")
ABSTRACT_REPORT_RE = re.compile(r"复用已有版本锁定|没有依赖临时状态|抽象名词串")
VAGUE_FAILURE_RE = re.compile(r"(?:验证中还出现过|在并发场景里|测试中|验证中)[^。；]{0,40}(?:问题|错误|失败|异常)")
CONCRETE_FAILURE_RE = re.compile(r"(?:[A-Za-z_][\w./-]+\.[A-Za-z]{2,5}|第\s*\d+\s*步|\d+\s*步)")
FIELD_TYPES = dict.fromkeys(FIELD_MAP, "text") | {
    **dict.fromkeys(("domain", "sub_direction", "question_type", "difficulty", "harness", "os_platform", "repro_level"), "select"),
    **dict.fromkeys(("user_prompt", "feedback", "other_issues"), "textarea"),
    "env_snapshot": "url", "artifact_snapshot": "url", "score_delivery": "number", **MEDIA_TYPES,
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def schema_fields(schema: dict) -> list[dict]:
    fields = schema.get("fields")
    if not schema.get("fingerprint") or not isinstance(fields, list):
        raise lib.FbrError("平台表单缺少 fields 或 fingerprint，停止提交")
    if any(not isinstance(f, dict) for f in fields):
        raise lib.FbrError("平台字段结构已变化，停止提交")
    enabled = [f for f in fields if f.get("is_enabled") is not False and not f.get("is_auto")]
    keys = [f.get("field_key") for f in enabled]
    if len(keys) != len(set(keys)) or any(not k for k in keys):
        raise lib.FbrError("平台字段缺失或重复，停止提交")
    supported = set(FIELD_MAP) | set(MEDIA_TYPES)
    if set(keys) != supported:
        raise lib.FbrError("平台字段已变化，需更新映射：缺少 " + ",".join(sorted(supported - set(keys)))
                           + "；新增 " + ",".join(sorted(set(keys) - supported)))
    for f in enabled:
        key, kind = f["field_key"], f.get("field_type")
        if kind != FIELD_TYPES[key]:
            raise lib.FbrError(f"平台 {key} 的字段类型已变化，停止提交")
        validation = f.get("validation") or {}
        if (not isinstance(validation, dict)
                or set(validation) - {"options_by", "pattern", "pattern_message", "min", "max", "integer"}
                or f.get("required_when")):
            raise lib.FbrError(f"平台 {key} 出现未支持的校验规则，停止提交")
        dependent = validation.get("options_by")
        if dependent and (not isinstance(dependent, dict) or not isinstance(dependent.get("map"), dict)
                          or dependent.get("field") not in supported
                          or any(not isinstance(v, list) for v in dependent["map"].values())):
            raise lib.FbrError(f"平台 {key} 的联动选项结构已变化，停止提交")
    return enabled


def scalar_payload(row: dict) -> dict:
    data = {key: row.get(label, "") for key, label in FIELD_MAP.items()}
    data["other_issues"] = ""  # 新交及返修均清空；中断说明只允许由真实轨迹生成
    if data["question_type"] == "泛Agent需求":
        data["question_type"] = "泛 Agent 需求"
    return data


def checked_file(root: Path, task_id: str, name: str, manifest: dict) -> dict:
    entry = next((r for r in manifest.get("tasks", []) if r.get("task_id") == task_id), None)
    if not entry:
        raise lib.FbrError(f"{task_id}：未进入当前交付清单")
    rel = f"results/{manifest['batch_id']}/{task_id}/{name}"
    matches = [f for f in entry.get("files", []) if f.get("path") == rel]
    if len(matches) != 1:
        raise lib.FbrError(f"{task_id}：交付清单缺少 {name}")
    info, path = matches[0], root / rel
    base = root / "results" / manifest["batch_id"] / task_id
    if not delivery.safe_file(base, path):
        raise lib.FbrError(f"{task_id}：附件缺失 {name}")
    if path.stat().st_size != info.get("bytes") or delivery.digest(path) != info.get("sha256"):
        raise lib.FbrError(f"{task_id}：附件与交付清单摘要不一致 {name}")
    return {"path": str(path), "relative": rel, "name": path.name,
            "bytes": info["bytes"], "sha256": info["sha256"]}


def plan_from_row(root: Path, task_dir: Path, row: dict, manifest: dict) -> dict:
    entry = next((r for r in manifest.get("tasks", []) if r.get("task_id") == task_dir.name), None)
    if not entry or manifest.get("batch_id") != task_dir.parent.name:
        raise lib.FbrError("任务未在当前交付清单内")
    # A caller may read a sheet row before refreshing results. Check the actual
    # source inventory too, otherwise a stale projection can validate an old receipt.
    source_files = delivery._source_files(task_dir, entry.get("feedback_sha", ""))
    inputs = {str(p.relative_to(task_dir)): delivery.digest(p) for p in source_files}
    for name in ("state.json", "task.json", "review/snapshot.json", "run/attempts.json"):
        path = task_dir / name
        if delivery.safe_file(task_dir, path):
            inputs[name] = delivery.digest(path)
    source_digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    if source_digest != entry.get("source_digest"):
        raise lib.FbrError("原始交付记录已变化，旧清单和提交回执失效；请重新导出并核对")
    ids_file = checked_file(root, task_dir.name, "trace/ids.json", manifest)
    ids = lib.load_json(Path(ids_file["path"]))
    session = delivery._session_file(task_dir)
    if not session or ids.get("session_file") != session:
        raise lib.FbrError("主会话路径与交付输入不一致")
    trace = checked_file(root, task_dir.name, "trace/" + session, manifest)
    video = checked_file(root, task_dir.name, "record/recording.mp4", manifest)
    data = scalar_payload(row)
    if ids.get("continuations"):
        actual = fbr_trace.read_ids(task_dir / "trace/projects", lib.load_task(task_dir)["prompt"],
                                    lib.require_stage(task_dir, "run").get("continuations", []))
        if actual["problems"] or any(actual.get(k) != ids.get(k) for k in
                                      ("session_id", "prompt_id", "user_turns", "continuations")):
            raise lib.FbrError("中断续写记录与原始轨迹不一致")
    cfg = lib.settings(root)
    api_key_read = fbr_trace.detect_api_key_read(task_dir, cfg)
    expected_note = fbr_trace.run_note(ids, api_key_read=api_key_read)
    data["other_issues"] = expected_note
    if str(row.get("备注") or "") != expected_note:
        raise lib.FbrError("备注与当前轨迹事实不一致，先重新生成结果表")
    if (not fbr_trace.valid_turn_count(ids) or data["session_id"] != ids.get("session_id")
            or data["turn_id"] != ids.get("prompt_id")):
        raise lib.FbrError("平台提交必须有真实单轮会话及匹配的 SessionID、PromptID")
    binding = {"platform": BASE_URL, "account": ACCOUNT, "task_id": task_dir.name,
               "source_digest": entry.get("source_digest"), "data": data,
               "files": {k: {f: v[f] for f in ("relative", "bytes", "sha256")} for k, v in (("trace_file", trace), ("screencast", video))}}
    return {"task_id": task_dir.name, "batch": task_dir.parent.name, "data": data,
            "files": {"trace_file": trace, "screencast": video}, "binding": binding, "content_digest": digest(binding)}


def validate_plan(plan: dict, schema: dict) -> list[str]:
    errors, data = [], plan["data"]
    for field in schema_fields(schema):
        key, label = field["field_key"], field.get("label", field["field_key"])
        if key in MEDIA_TYPES:
            info = plan["files"][key]
            limit = schema.get("attachment_max_mb", 27) if key == "trace_file" else schema.get("video_max_mb", 500)
            if not isinstance(limit, (int, float)) or limit <= 0:
                errors.append(f"{label}上传大小限制无效")
            elif not 0 < info["bytes"] <= limit * 1024 * 1024:
                errors.append(f"{label}须非空且不超过 {limit} MB")
            continue
        value = data.get(key)
        if field.get("is_required") and (value is None or str(value).strip() == ""):
            errors.append(f"缺{label}")
            continue
        if value is None or str(value).strip() == "":
            continue
        validation = field.get("validation") or {}
        if field.get("options") and value not in field["options"]:
            errors.append(f"{label}不在当前平台选项内")
        dependent = validation.get("options_by")
        if dependent:
            options = dependent.get("map", {}).get(data.get(dependent.get("field")), [])
            if value not in options:
                errors.append(f"{label}与所属领域不匹配")
        # The frontend counts JavaScript UTF-16 code units.
        if field.get("max_length") and len(str(value).encode("utf-16-le")) // 2 > field["max_length"]:
            errors.append(f"{label}超过平台长度限制")
        if validation.get("pattern"):
            try:
                if not re.search(validation["pattern"], str(value)):
                    errors.append(f"{label}格式不符")
            except re.error:
                errors.append(f"{label}平台正则不可识别")
        if key == "score_delivery" and (type(value) is not int or not 1 <= value <= 5):
            errors.append("交付完整性必须为1到5的整数")
        if field.get("field_type") == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{label}须为数字")
            elif (("min" in validation and value < validation["min"])
                  or ("max" in validation and value > validation["max"])
                  or (validation.get("integer") and type(value) is not int)):
                errors.append(f"{label}不符合当前平台数值范围")
        if set(validation) - {"options_by", "pattern", "pattern_message", "min", "max", "integer"}:
            errors.append(f"{label}出现未支持的平台校验规则")
        if field.get("required_when"):
            errors.append(f"{label}出现未支持的条件必填规则")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", str(data.get("harness_version", ""))):
        errors.append("Harness版本必须为纯版本号")
    snapshots = [urlsplit(str(data.get(k, ""))).path.split("/") for k in ("env_snapshot", "artifact_snapshot")]
    if len(snapshots[0]) < 5 or snapshots[0][1:3] != snapshots[1][1:3]:
        errors.append("初始与产物快照必须属于同一GitHub仓库")
    return list(dict.fromkeys(errors))


def media_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in ("\n", "\r", "\\")):
        raise lib.FbrError("平台返回的媒体地址无效")
    parsed = urlsplit(value)
    if value.startswith("/") and not value.startswith("//"):
        return urljoin(BASE_URL, value)
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return value
    # The live attachment upload API returns a storage key, not a URL.
    # Keep that key unchanged in the payload; accept only the observed namespace.
    if (not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment
            and re.fullmatch(r"uploads/[A-Za-z0-9_./-]+", value)
            and all(part not in (".", "..", "") for part in value.split("/"))):
        return urljoin(BASE_URL + "/", value)
    raise lib.FbrError("平台返回的媒体地址必须为HTTPS、站内绝对路径或uploads存储键")


def validate_uploaded(key: str, info: dict, local: dict) -> dict:
    if not isinstance(info, dict):
        raise lib.FbrError("平台上传响应必须是附件信息对象")
    if key == "trace_file":
        if (not isinstance(info, dict) or not isinstance(info.get("name"), str)
                or not info["name"].endswith(".jsonl") or info.get("size") != local["bytes"]):
            raise lib.FbrError("轨迹上传响应缺少有效name/path/size")
        media_url(info.get("path", ""))
        return {k: info[k] for k in ("name", "path", "size")}
    media_url(info.get("url", ""))
    if not info.get("name"):
        raise lib.FbrError("视频上传响应缺少name")
    return {k: info[k] for k in ("url", "name")}


def platform_style_problems(text: str, score: int | None = None) -> list[str]:
    dense = list(dict.fromkeys(DENSE_REPORT_TOKEN_RE.findall(text or "")))
    machine = list(dict.fromkeys(MACHINE_STYLE_RE.findall(text or "")))
    errors = []
    if len(dense) >= 6:
        errors.append("Feedback 的实体编号、时间点或大数字过密，改成用户能理解的关键场景")
    if len(machine) >= 3:
        errors.append("Feedback 原样堆叠了过多工具字段，改写成失败后果和处理经过")
    if BARE_TEST_COUNT_RE.search(text or ""):
        errors.append("Feedback 不要只报测试总数，要说明测试覆盖了哪些需求行为")
    style_counts = AI_STYLE_COUNT_RE.findall(text or "")
    if len(style_counts) >= 4:
        errors.append("Feedback 的条数、次数和项数密度过高，改写成关键动作与用户可见后果")
    if ABSTRACT_REPORT_RE.search(text or ""):
        errors.append("Feedback 出现机器摘要式抽象串，改成具体动作和业务后果")
    for sentence in re.split(r"[。；\n]", text or ""):
        if VAGUE_FAILURE_RE.search(sentence) and not CONCRETE_FAILURE_RE.search(sentence):
            errors.append("过程问题只写了并发场景、验证中等泛称，必须指出具体文件、步骤或动作")
            break
    if score is not None and score <= 2 and not re.search(r"(?:问题|缺口|未实现|无法|不可用|失败)", text or ""):
        errors.append("2 分及以下的 Feedback 必须明确写出具体问题、缺口或不可用后果")
    return errors


def payload_with_media(plan: dict, uploads: dict) -> dict:
    data = copy.deepcopy(plan["data"])
    data["trace_file"] = [validate_uploaded("trace_file", uploads["trace_file"], plan["files"]["trace_file"])]
    data["screencast"] = validate_uploaded("screencast", uploads["screencast"], plan["files"]["screencast"])["url"]
    return data


def recheck_binding(root: Path, plan: dict, task_dir: Path) -> None:
    manifest = lib.load_json(root / "results" / plan["batch"] / "delivery-manifest.json")
    row = {label: plan["data"][key] for key, label in FIELD_MAP.items()}
    current = plan_from_row(root, task_dir, row, manifest)
    if current["content_digest"] != plan["content_digest"]:
        raise lib.FbrError("提交期间本地字段或附件已变化，停止建单；重新导出并演练")


def receipt_status(root: Path, task_dir: Path, row: dict, delivery_manifest: dict | None) -> dict:
    """只相信已回读、绑定当前字段和附件的回执；不触网、不重建交付投影。"""
    path = task_dir / "review/upload.json"
    if not path.exists():
        return {"valid": False, "state": "未提交"}
    bad = {"valid": False, "state": "回执已失效"}
    try:
        receipt = lib.load_json(path)
        if not isinstance(receipt, dict):
            return bad
        if not delivery_manifest:
            return bad
        plan = plan_from_row(root, task_dir, row, delivery_manifest)
        if (receipt.get("platform") != BASE_URL or receipt.get("account") != ACCOUNT
                or receipt.get("content_digest") != plan["content_digest"]):
            return bad
        if receipt.get("state") != "confirmed":
            return {"valid": False, "state": {"prepared": "待提交", "uploaded": "附件已上传", "creating": "提交待核对", "verifying": "提交待核对",
                                                  "uncertain": "提交待核对"}.get(receipt.get("state"), "回执已失效")}
        if not receipt.get("verified_at") or not valid_id(receipt.get("submission_id")):
            return bad
        expected = payload_with_media(plan, receipt.get("uploads", {}))
        if receipt.get("remote_data") != expected:
            return bad
        remote_trace = receipt.get('remote_trace')
        trace_url = media_url(expected['trace_file'][0]['path'])
        if remote_trace:
            local = plan['files']['trace_file']
            rows = receipt.get('remote_response_data', {}).get('trace_file')
            if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get('url') != remote_trace.get('url')
                    or remote_trace.get('sha256') != local['sha256'] or remote_trace.get('bytes') != local['bytes']
                    or remote_trace.get('name') != expected['trace_file'][0]['name']):
                return bad
            trace_url = media_url(remote_trace['url'])
        elif expected['trace_file'][0]['path'].startswith('uploads/'):
            return bad
        return {"valid": True, "state": "已提交 / " + str(receipt.get("remote_status", "待质检")),
                "trace": trace_url, "video": media_url(expected["screencast"]),
                "submitter": ACCOUNT, "submitted_at": receipt.get("submitted_at", ""),
                "submission_id": receipt["submission_id"]}
    except (lib.FbrError, KeyError, TypeError, ValueError, OSError):
        return bad


def valid_id(value) -> bool:
    return (type(value) is int and value > 0) or (isinstance(value, str) and value.isdigit() and int(value) > 0)


def remote_data(record: dict) -> dict:
    data = record.get("data")
    if isinstance(data, dict):
        return data
    if all(key in record for key in (*FIELD_MAP, *MEDIA_TYPES)):
        return {key: record[key] for key in (*FIELD_MAP, *MEDIA_TYPES)}
    raise lib.FbrError("平台详情缺少完整字段对象，不能确认提交成功")


def verify_remote_trace(actual: dict, expected: dict, local: dict) -> dict:
    """The live detail API replaces an uploaded storage key with a COS URL."""
    rows = actual.get('trace_file')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise lib.FbrError('平台回读轨迹附件数量或格式不一致')
    info, uploaded = rows[0], expected['trace_file'][0]
    if info.get('name') != uploaded['name'] or info.get('size') != local['bytes']:
        raise lib.FbrError('平台回读轨迹名称或大小不一致')
    url = info.get('url')
    if not isinstance(url, str) or not url.startswith('https://') or media_url(url) != url:
        raise lib.FbrError('平台回读轨迹缺少有效HTTPS下载地址')
    checksum, count = hashlib.sha256(), 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=60) as response:
            if urlsplit(response.geturl()).scheme != 'https':
                raise lib.FbrError('轨迹下载发生非HTTPS跳转')
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > local['bytes']:
                    raise lib.FbrError('平台轨迹字节数超出交付文件')
                checksum.update(block)
    except (OSError, ValueError):
        raise lib.FbrError('平台轨迹下载核对失败，保留记录ID待回读') from None
    if count != local['bytes'] or checksum.hexdigest() != local['sha256']:
        raise lib.FbrError('平台轨迹内容摘要与交付文件不一致')
    return {'url': url, 'name': info['name'], 'bytes': count, 'sha256': checksum.hexdigest()}


def own_records(client) -> list[dict]:
    items, seen = [], set()
    for page in range(1, 10001):
        result = client.list_submissions(page=page, page_size=100)
        rows, meta = result.get("items"), result.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise lib.FbrError("平台列表分页结构变化，停止提交")
        if (type(meta.get("total_pages")) is not int or meta["total_pages"] < 0
                or type(meta.get("total")) is not int or meta["total"] < 0):
            raise lib.FbrError("平台列表分页计数无效，停止提交")
        for row in rows:
            if not isinstance(row, dict) or not valid_id(row.get("id")) or str(row["id"]) in seen:
                raise lib.FbrError("平台列表ID缺失或重复，停止提交")
            seen.add(str(row["id"]))
            items.append(row)
        if page >= meta.get("total_pages", 0):
            if meta.get("total") != len(items):
                raise lib.FbrError("平台列表总数不一致，停止提交")
            return items
        if not rows:
            raise lib.FbrError("平台分页中断，停止提交")
    raise lib.FbrError("平台列表超出支持页数，停止提交")


def record_session(record: dict) -> str:
    data = record.get("data") or {}
    if not isinstance(data, dict):
        raise lib.FbrError("平台列表data结构变化，停止提交")
    return str(data.get("session_id") or record.get("session_id") or "")


def locked_domain(me: dict, records: list[dict]) -> str:
    values = {str(me[key]) for key in ("locked_domain", "domain") if me.get(key)}
    for row in records:
        data = row.get("data") or {}
        if not isinstance(data, dict):
            raise lib.FbrError("平台列表data结构变化，停止提交")
        value = data.get("domain") or row.get("domain")
        if not value:
            raise lib.FbrError("平台已有记录缺少领域，不能确认账号锁定领域")
        values.add(value)
    if len(values) > 1:
        raise lib.FbrError("账号已有多个领域记录，需人工确认平台锁定领域")
    return next(iter(values), "")


def prepare(root: Path, batch_id: str, schema: dict, tasks: str | None = None, limit: int | None = None) -> list[dict]:
    import fbr_sheet
    schema_fields(schema)
    cfg = lib.settings(root)
    manifest = delivery.export_batch(root, batch_id, cfg)
    selected = lib.task_dirs(root, batch_id, tasks)[:limit]
    if not selected:
        raise lib.FbrError("没有选中的待提交任务")
    plans, errors = [], []
    for task_dir in selected:
        try:
            row, reason = fbr_sheet.build_row(root, task_dir, cfg, delivery_manifest=manifest)
            if not row:
                raise lib.FbrError(reason)
            ids = lib.load_json(task_dir / "trace/ids.json", default={})
            snapshot = lib.load_json(task_dir / "review/snapshot.json", default={})
            # Online enum validation belongs to the live schema. The offline
            # workbook list may lag a newly enabled platform option.
            online_options = {f"{FIELD_MAP[f['field_key']]}的取值不在选项里："
                              for f in schema_fields(schema) if f.get("field_type") == "select"}
            problems = [p for p in fbr_sheet.row_problems(row, ids, snapshot)
                        if p not in ("缺提交人", "缺提交时间") and not any(p.startswith(prefix) for prefix in online_options)]
            plan = plan_from_row(root, task_dir, row, manifest)
            problems += validate_plan(plan, schema)
            if problems:
                raise lib.FbrError("；".join(dict.fromkeys(problems)))
            plans.append(plan)
        except (lib.FbrError, OSError) as exc:
            errors.append(f"{task_dir.name}：{exc}")
    if errors:
        raise lib.FbrError("所选任务未全部准备好，不上传任何文件：\n" + "\n".join(errors))
    sessions = [p["data"]["session_id"] for p in plans]
    if len(sessions) != len(set(sessions)):
        raise lib.FbrError("所选任务存在重复SessionID")
    if len({p["data"]["domain"] for p in plans}) != 1:
        raise lib.FbrError("一个账号只能提交一个领域，所选任务跨领域")
    return plans


@contextlib.contextmanager
def submit_lock(root: Path):
    path = root / "_registry/platform-submit.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise lib.FbrError("平台提交锁不能是符号链接")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise lib.FbrError("另一个平台提交进程正在运行，请等待它结束后再核对") from None
        import fbr_runtime
        shared = fbr_runtime.runtime_home({}) / ('platform-' + lib.content_hash([BASE_URL, ACCOUNT])[:24] + '.lock')
        with lib.file_lock(shared):
            yield


def confirm_remote(client, plan: dict, receipt: dict, task_dir: Path, submission_id) -> dict:
    # Invalidate the prior confirmation before asking the server again. A failed
    # read or a changed remote record must not keep looking confirmed in Excel.
    receipt.update(state="verifying", submission_id=submission_id)
    lib.save_json(task_dir / "review/upload.json", receipt)
    detail = client.detail(submission_id)
    expected = payload_with_media(plan, receipt["uploads"])
    actual = remote_data(detail)
    remote_trace = None
    original_actual = copy.deepcopy(actual)
    # Live API canonicalizes the languages separator to its ASCII equivalent.
    if actual.get('languages') == expected['languages'].replace('；', ';'):
        actual = {**actual, 'languages': expected['languages']}
    if actual.get('trace_file') != expected['trace_file']:
        remote_trace = verify_remote_trace(actual, expected, plan['files']['trace_file'])
        actual = {**actual, 'trace_file': expected['trace_file']}
    if any(actual.get(key) != value for key, value in expected.items()):
        raise lib.FbrError("平台回读字段与本次提交不一致，保留回执待核对")
    if str(detail.get("id")) != str(submission_id):
        raise lib.FbrError("平台回读ID不一致")
    created_at, remote_status = detail.get("created_at"), detail.get("status")
    try:
        datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        raise lib.FbrError("平台回读缺少有效created_at，不能用本地时间冒充提交时间") from None
    if not isinstance(remote_status, str) or not remote_status:
        raise lib.FbrError("平台回读缺少状态，不能确认提交")
    receipt.update(remote_response_data=original_actual, remote_trace=remote_trace)
    receipt.update(state="confirmed", submission_id=submission_id, remote_data={k: actual[k] for k in expected},
                   remote_status=remote_status, verified_at=lib.now_iso(), submitted_at=created_at)
    lib.save_json(task_dir / "review/upload.json", receipt)
    return {"task_id": task_dir.name, "state": "confirmed", "submission_id": submission_id,
            "status": receipt["remote_status"], "url": f"{BASE_URL}/app/submissions/{submission_id}"}


def submit_one(root: Path, plan: dict, client, schema: dict, records: list[dict], *, execute: bool) -> dict:
    task_dir = root / "batches" / plan["batch"] / plan["task_id"]
    path = task_dir / "review/upload.json"
    receipt = lib.load_json(path, default={})
    if not isinstance(receipt, dict):
        raise lib.FbrError("提交回执必须为JSON对象，旧格式需先核对，不能自动覆盖")
    same = (receipt.get("content_digest") == plan["content_digest"] and receipt.get("account") == ACCOUNT
            and receipt.get("platform") == BASE_URL)
    if not (same and valid_id(receipt.get("submission_id"))):
        style_errors = platform_style_problems(str(plan["data"].get("feedback", "")),
                                               plan["data"].get("score_delivery"))
        if style_errors:
            raise lib.FbrError("；".join(style_errors))
    if same:
        if not isinstance(receipt.get("uploads"), dict) or set(receipt["uploads"]) - set(MEDIA_TYPES):
            raise lib.FbrError("回执中的附件清单格式无效，先核对再继续")
        for key, uploaded in receipt["uploads"].items():
            validate_uploaded(key, uploaded, plan["files"][key])
    matches = [r for r in records if record_session(r) == plan["data"]["session_id"]]
    if len(matches) > 1:
        raise lib.FbrError("平台存在多个相同SessionID记录，停止自动提交")
    if same and valid_id(receipt.get("submission_id")):
        if set(receipt["uploads"]) != set(MEDIA_TYPES):
            raise lib.FbrError("已有记录ID但附件回执不完整，停止核对")
        return confirm_remote(client, plan, receipt, task_dir, receipt["submission_id"])
    if matches:
        if same and set(receipt["uploads"]) == set(MEDIA_TYPES):
            return confirm_remote(client, plan, receipt, task_dir, matches[0]["id"])
        raise lib.FbrError(f"SessionID已存在于平台记录 {matches[0]['id']} 中，不能重复建单或自动覆盖")
    if receipt.get("state") in ("creating", "verifying", "uncertain", "confirmed"):
        raise lib.FbrError("已有提交回执但平台未能确认，禁止自动重复建单；先核对原记录")
    if not execute:
        return {"task_id": plan["task_id"], "state": "dry", "domain": plan["data"]["domain"],
                "fields": list(plan["data"]) + list(MEDIA_TYPES),
                "files": {k: {"path": f["relative"], "bytes": f["bytes"], "sha256": f["sha256"]}
                          for k, f in plan["files"].items()}, "content_digest": plan["content_digest"]}
    recheck_binding(root, plan, task_dir)
    if not same:
        if receipt:
            archive = task_dir / "review/platform-history" / (lib.now_iso().replace(":", "-") + ".json")
            lib.save_json(archive, receipt)
        receipt = {"platform": BASE_URL, "account": ACCOUNT, "task_id": plan["task_id"],
                   "content_digest": plan["content_digest"], "binding": plan["binding"], "state": "prepared", "uploads": {}}
        lib.save_json(path, receipt)
    for key, kind in MEDIA_TYPES.items():
        if key in receipt["uploads"]:
            validate_uploaded(key, receipt["uploads"][key], plan["files"][key])
            continue
        # Recheck exact projected bytes immediately before sending the attachment.
        local = plan["files"][key]
        if Path(local["path"]).stat().st_size != local["bytes"] or delivery.digest(Path(local["path"])) != local["sha256"]:
            raise lib.FbrError("实际上传附件与演练摘要不一致")
        uploaded = client.upload(Path(local["path"]), kind=kind, expected_size=local["bytes"], expected_sha256=local["sha256"])
        receipt["uploads"][key] = validate_uploaded(key, uploaded, local)
        receipt["state"] = "uploaded"
        lib.save_json(path, receipt)
    # Re-fetch schema before create: changed fingerprint requires a new plan, never silently adapt.
    current_schema = client.schema()
    schema_fields(current_schema)
    if current_schema.get("fingerprint") != schema["fingerprint"]:
        raise lib.FbrError("上传期间平台表单已变化，附件已保留，重新演练后再提交")
    recheck_binding(root, plan, task_dir)
    data = payload_with_media(plan, receipt["uploads"])
    video_field = next(f for f in schema["fields"] if f.get("field_key") == "screencast")
    if video_field.get("max_length") and len(data["screencast"].encode("utf-16-le")) // 2 > video_field["max_length"]:
        raise lib.FbrError("上传视频地址超过平台字段长度限制，停止建单")
    receipt.update(state="creating", requested_at=lib.now_iso(), schema_fingerprint=schema["fingerprint"])
    lib.save_json(path, receipt)
    try:
        created = client.create(data, schema["fingerprint"])
        if not valid_id(created.get("id")):
            raise lib.FbrError("建单响应没有有效ID，提交结果待核对")
        receipt["submission_id"] = created["id"]
        lib.save_json(path, receipt)
        return confirm_remote(client, plan, receipt, task_dir, created["id"])
    except (lib.FbrError, OSError) as exc:
        from fbr_platform import RequestNotSent
        not_sent = isinstance(exc, RequestNotSent) and not valid_id(receipt.get("submission_id"))
        receipt["state"] = "uploaded" if not_sent else "uncertain"
        if not_sent:
            receipt["last_create_not_sent"] = lib.now_iso()
        lib.save_json(path, receipt)
        raise


def repair_one(root: Path, plan: dict, client, schema: dict, *, execute: bool, comment: str) -> dict:
    task_dir = root / "batches" / plan["batch"] / plan["task_id"]
    path = task_dir / "review/upload.json"
    receipt = lib.load_json(path, default={})
    if not isinstance(receipt, dict) or not valid_id(receipt.get("submission_id")):
        raise lib.FbrError(f"{plan['task_id']}：没有可返修的平台记录")
    style_errors = platform_style_problems(str(plan["data"].get("feedback", "")))
    if style_errors:
        raise lib.FbrError("；".join(style_errors))
    submission_id = receipt["submission_id"]
    detail = client.detail(submission_id)
    if str(detail.get("session_id")) != str(plan["data"]["session_id"]):
        raise lib.FbrError(f"{plan['task_id']}：平台记录 SessionID 与当前交付不一致")
    current = remote_data(detail)
    current_trace = detail.get("trace_file")
    if current_trace:
        current["trace_file"] = current_trace
    locked = set(detail.get("locked_fields") or [])
    desired = copy.deepcopy(current)
    for key in plan["data"]:
        if key in MEDIA_TYPES or key in locked:
            desired[key] = current[key]
        else:
            desired[key] = plan["data"][key]
    changes = {key: {"from": current.get(key), "to": desired.get(key)}
               for key in desired if current.get(key) != desired.get(key)}

    def save_receipt(updated: dict, actual: dict) -> dict:
        expected = payload_with_media(plan, receipt.get("uploads", {}))
        receipt.update({
            "content_digest": plan["content_digest"],
            "binding": plan["binding"],
            "schema_fingerprint": schema["fingerprint"],
            "state": "confirmed",
            "remote_data": expected,
            "remote_response_data": actual,
            "remote_status": updated.get("status"),
            "verified_at": lib.now_iso(),
            "last_repair_at": lib.now_iso(),
            "last_repair_comment": comment,
            "version_no": updated.get("current_version"),
        })
        lib.save_json(path, receipt)

    qc_active = detail.get("qc_running") is True or detail.get("stage") in ("QC_WAITING", "QC_RUNNING")
    if not changes:
        if not execute:
            return {"task_id": plan["task_id"], "state": "dry", "submission_id": submission_id,
                    "status": detail.get("status"), "changed_fields": [], "already_applied": True}
        save_receipt(detail, current)
        return {"task_id": plan["task_id"], "state": "confirmed", "submission_id": submission_id,
                "status": detail.get("status"), "changed_fields": [], "already_applied": True,
                "url": f"{BASE_URL}/app/submissions/{submission_id}"}
    if detail.get("status") != "PENDING_FIX" or detail.get("editable") is not True:
        raise lib.FbrError(f"{plan['task_id']}：当前状态不是可编辑的待返修")
    if not execute:
        return {"task_id": plan["task_id"], "state": "dry", "submission_id": submission_id,
                "status": detail.get("status"), "changed_fields": sorted(changes), "changes": changes}

    history = task_dir / "review/platform-history" / (lib.now_iso().replace(":", "-") + "-before-repair.json")
    lib.save_json(history, receipt)
    client.update(submission_id, desired, schema["fingerprint"], comment)
    current_schema = client.schema()
    schema_fields(current_schema)
    if current_schema.get("fingerprint") != schema["fingerprint"]:
        raise lib.FbrError("返修期间平台表单已变化，结果需核对")
    updated = client.detail(submission_id)
    actual = remote_data(updated)
    if actual.get("trace_file") != desired.get("trace_file"):
        raise lib.FbrError("返修回读的轨迹附件与提交值不一致")
    for key, value in desired.items():
        if actual.get(key) != value and not (key == "languages" and actual.get(key) == value.replace("；", ";")):
            raise lib.FbrError(f"返修回读字段不一致：{key}")
    if (updated.get("qc_running") is not True
            and updated.get("stage") not in ("QC_WAITING", "QC_RUNNING")
            and updated.get("status") not in ("QC_WAITING", "QC_RUNNING")):
        raise lib.FbrError("返修后平台未进入重新质检状态")
    save_receipt(updated, actual)
    return {"task_id": plan["task_id"], "state": "confirmed", "submission_id": submission_id,
            "status": updated.get("status"), "changed_fields": sorted(changes),
            "url": f"{BASE_URL}/app/submissions/{submission_id}"}


def repair_batch(root: Path, batch_id: str, *, tasks: str | None = None,
                 execute: bool = False, confirm: str = "", comment: str = "", client=None) -> dict:
    if execute and confirm != "REPAIR":
        raise lib.FbrError("真实平台返修需要 --execute --confirm REPAIR")
    if client is None:
        from fbr_platform import Client
        client = Client(account=ACCOUNT)
    with submit_lock(root):
        client.login()
        me = client.me()
        user = me.get("user", me)
        if user.get("username") != ACCOUNT or user.get("must_change_password"):
            raise lib.FbrError("登录账号不符或需要人工修改初始密码")
        schema = client.schema()
        plans = prepare(root, batch_id, schema, tasks, None)
        rows = [repair_one(root, plan, client, schema, execute=execute, comment=comment) for plan in plans]
        return {"platform": BASE_URL, "account": ACCOUNT, "execute": execute, "tasks": rows}


def run_batch(root: Path, batch_id: str, *, tasks: str | None = None, limit: int | None = None,
              execute: bool = False, confirm: str = "", client=None) -> dict:
    if execute and confirm != "SUBMIT":
        raise lib.FbrError("真实平台提交需要 --execute --confirm SUBMIT")
    if limit is not None and limit < 1:
        raise lib.FbrError("--limit必须为正整数")
    if client is None:
        from fbr_platform import Client
        client = Client(account=ACCOUNT)
    with submit_lock(root):
        client.login()
        me = client.me()
        user = me.get("user", me)
        if user.get("username") != ACCOUNT or user.get("must_change_password"):
            raise lib.FbrError("登录账号不符或需要人工修改初始密码")
        schema, records = client.schema(), own_records(client)
        plans = prepare(root, batch_id, schema, tasks, limit)
        domain = locked_domain(user, records)
        if domain and any(p["data"]["domain"] != domain for p in plans):
            raise lib.FbrError(f"账号已锁定领域 {domain}，本批领域不符，不上传")
        # Run the *same* receipt/duplicate/readback checks for every task before
        # the first mutation. A weaker separate precheck can miss later errors.
        previews = [submit_one(root, plan, client, schema, records, execute=False) for plan in plans]
        rows = previews if not execute else [preview if preview["state"] == "confirmed" else
            submit_one(root, plan, client, schema, records, execute=True)
            for plan, preview in zip(plans, previews)]
        return {"platform": BASE_URL, "account": ACCOUNT, "execute": execute,
                "locked_domain": domain or "尚未锁定；首次提交后锁定", "tasks": rows}


def probe(client=None) -> dict:
    if client is None:
        from fbr_platform import Client
        client = Client(account=ACCOUNT)
    client.login()
    result = client.me()
    user = result.get("user", result)
    if user.get("username") != ACCOUNT:
        raise lib.FbrError("登录账号不符")
    schema = client.schema()
    fields = schema_fields(schema)
    records = own_records(client)
    return {"platform": BASE_URL, "account": ACCOUNT, "fingerprint": schema["fingerprint"],
            "fields": [{k: f.get(k) for k in ("field_key", "label", "field_type", "is_required", "options")}
                       for f in fields], "existing_records": len(records), "locked_domain": locked_domain(user, records),
            "attachment_max_mb": schema.get("attachment_max_mb"), "video_max_mb": schema.get("video_max_mb", 500)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="登录并只读核对账号、字段、领域和历史记录")
    p = sub.add_parser("submit", help="默认演练；上传附件、建单和回读需明确执行")
    p.add_argument("--batch", required=True)
    p.add_argument("--tasks")
    p.add_argument("--limit", type=int)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm", default="")
    r = sub.add_parser("repair", help="对待返修记录保存修正字段并重新质检")
    r.add_argument("--batch", required=True)
    r.add_argument("--tasks")
    r.add_argument("--execute", action="store_true")
    r.add_argument("--confirm", default="")
    r.add_argument("--comment", default="")
    args = parser.parse_args()
    try:
        if args.command == "probe":
            result = probe()
        elif args.command == "repair":
            result = repair_batch(lib.find_root(args.root), args.batch, tasks=args.tasks,
                                  execute=args.execute, confirm=args.confirm, comment=args.comment)
        else:
            result = run_batch(lib.find_root(args.root), args.batch, tasks=args.tasks,
                               limit=args.limit, execute=args.execute, confirm=args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
