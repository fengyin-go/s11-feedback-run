#!/usr/bin/env python3
"""轨迹分析外部调用与当前任务 AI 验收的证据绑定入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib
import fbr_quality as quality

MODEL = "gpt-6.1-sol"
EFFORT = "high"
PROVIDER = "solo11_reviewer"
KEY_ENV = "SOLO11_REVIEW_API_KEY"
DEFAULT_ENV = "~/.codex/solo11/reviewer.env"


def credentials(path: Path) -> tuple[str, str]:
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise lib.FbrError("验收配置文件必须存在、不是符号链接、权限为 600")
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    base, key = values.get("base_url", "").rstrip("/"), values.get("apikey", "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise lib.FbrError("请在验收配置中填写 HTTPS base_url（不含密钥、查询参数或 fragment）")
    if not key or any(c.isspace() for c in key) or key[:1] in "\"'":
        raise lib.FbrError("请在验收配置的 apikey= 后填写独立验收 key（单行、无引号）")
    return base, key


def command(base_url: str, cwd: Path, output: Path, *, model=MODEL, effort=EFFORT) -> list[str]:
    overrides = {
        "model_provider": PROVIDER,
        "model_reasoning_effort": effort,
        f"model_providers.{PROVIDER}.name": "Solo11 acceptance reviewer",
        f"model_providers.{PROVIDER}.base_url": base_url,
        f"model_providers.{PROVIDER}.env_key": KEY_ENV,
        f"model_providers.{PROVIDER}.wire_api": "responses",
        f"model_providers.{PROVIDER}.requires_openai_auth": False,
        "project_doc_max_bytes": 0,
        "web_search": "disabled",
        "shell_environment_policy.exclude": [KEY_ENV, "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "apikey"],
    }
    args = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--sandbox", "read-only", "--skip-git-repo-check", "--model", model,
            "--cd", str(cwd), "--output-last-message", str(output), "--json"]
    for name, value in overrides.items():
        args += ["-c", f"{name}={json.dumps(value, ensure_ascii=False)}"]
    return args + ["-"]


def invoke(base: str, secret: str, cwd: Path, prompt: str, *, timeout: int = 1800, model=MODEL, effort=EFFORT) -> tuple[str, dict]:
    if not shutil.which("codex"):
        raise lib.FbrError("没有找到 codex CLI")
    env = dict(os.environ)
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_API_KEY", "apikey", "APIKEY", KEY_ENV):
        env.pop(name, None)
    env[KEY_ENV] = secret
    with tempfile.TemporaryDirectory(prefix="solo11-review-") as temp:
        # 独立 auth、缓存与会话目录，避免继承 CC Switch 的用户配置或登录态。
        isolated_home = Path(temp) / "codex-home"
        isolated_home.mkdir(mode=0o700)
        env["CODEX_HOME"] = str(isolated_home)
        output = Path(temp) / "response.txt"
        try:
            result = subprocess.run(command(base, cwd, output, model=model, effort=effort), input=prompt, text=True,
                                    capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise lib.FbrError("验收模型请求超时；停止，不回退其他模型") from None
        except OSError:
            raise lib.FbrError("无法启动验收模型进程；停止，不回退其他模型") from None
        text = output.read_text() if output.is_file() else ""
        if any(secret in value for value in (text, result.stdout, result.stderr)):
            raise lib.FbrError("验收模型输出出现验收 key，停止，不保存输出")
        if result.returncode != 0 or not text.strip():
            detail = lib.redact((result.stderr or result.stdout)[-1000:], [secret])
            raise lib.FbrError("验收模型调用失败，不回退其他模型：" + detail)
        return text, {"model": model, "reasoning_effort": effort, "provider": PROVIDER,
                      "base_url": base, "wire_api": "responses", "at": lib.now_iso()}


def trace_configuration(path: Path) -> tuple[str, str]:
    """Only the trace stage reads model settings from reviewer.env."""
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    model = values.get('model', MODEL)
    effort = values.get('reasoning_effort', values.get('model_reasoning_effort', values.get('effort', EFFORT)))
    if not model or any(c.isspace() for c in model) or effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise lib.FbrError('轨迹分析模型配置不合法')
    return model, effort


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def trace_index(task: Path) -> Path:
    """全量消息定位索引；短摘录只用于导航，不能作为事实证据。"""
    import fbr_trace
    target = task / "review/trace-index.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise lib.FbrError("轨迹索引不能是符号链接")
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        for path in sorted((task / "trace/projects").rglob("*.jsonl")):
            if path.is_symlink() or not path.resolve().is_relative_to(task.resolve()):
                raise lib.FbrError("轨迹路径越界或为符号链接")
            output.write(json.dumps({"source": str(path.relative_to(task))}, ensure_ascii=False) + "\n")
            with path.open(encoding="utf-8") as source:
                for line, raw in enumerate(source, 1):
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        raise lib.FbrError(f"轨迹 JSON 损坏：{path.name}:{line}") from None
                    if not isinstance(row, dict):
                        raise lib.FbrError(f"轨迹消息不是对象：{path.name}:{line}")
                    content = (row.get("message") or {}).get("content", row.get("content", ""))
                    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
                    nav = []
                    for number, block in enumerate(blocks):
                        if not isinstance(block, dict):
                            continue
                        kind = block.get("type", "")
                        item = {"block": number, "type": kind}
                        if kind == "tool_use":
                            payload = block.get("input")
                            item.update(id=block.get("id"), tool=block.get("name"),
                                        target=fbr_trace._target(str(block.get("name", "")), payload if isinstance(payload, dict) else {})[:240])
                        elif kind == "tool_result":
                            item.update(tool_use_id=block.get("tool_use_id"), is_error=block.get("is_error", False))
                        elif kind == "text":
                            item["preview"] = str(block.get("text", ""))[:100]
                        nav.append(item)
                    entry = {"line": line, "chars": len(raw), "type": row.get("type"), "blocks": nav}
                    output.write(json.dumps(entry, ensure_ascii=False) + "\n")
        output.seek(0)
        target.write_text(output.read(), encoding="utf-8")
    return target


def inputs(task: Path) -> dict:
    names = ["task.json", "trace/ids.json", "evidence/facts.json", "evidence/probes.jsonl",
             "review/card.md", "review/diff.patch", "review/trace-index.jsonl", 'record/recording.json', 'record/recording.mp4']
    names += [str(p.relative_to(task)) for p in sorted((task / "trace/projects").rglob("*.jsonl"))]
    names += [str(p.relative_to(task)) for p in sorted((task / 'evidence/command-logs').rglob('*.log'))]
    result = {}
    for name in names:
        p = task / name
        if p.is_symlink():
            raise lib.FbrError("验收输入不能是符号链接：" + name)
        if p.is_file():
            result[name] = file_sha(p)
    return result


def provenance_errors(task: Path) -> list[str]:
    receipt = lib.load_json(task / "review/model-review.json", default={})
    if (not isinstance(receipt, dict) or receipt.get('workflow') != 'task-agent-v1'
            or receipt.get('provider') != 'current_task_agent'
            or receipt.get('model') != 'current_task_agent' or receipt.get('reasoning_effort') != 'session'
            or receipt.get('state') != 'completed'):
        return ['缺少当前任务 AI 的有效验收回执；旧模型评价仅作历史记录']
    errors = analysis_errors(task, receipt.get('trace-analysis_sha'))
    if errors:
        return errors
    expected = {"audit_sha": task / "evidence/audit.json", "feedback_sha": task / "review/feedback.json"}
    if any(path.is_symlink() or not path.is_file() or file_sha(path) != receipt.get(key)
           for key, path in expected.items()):
        return ["验收正文或 audit 已变化，须由当前任务 AI 重新验收"]
    if receipt.get("inputs") != inputs(task):
        return ["轨迹或验收证据变化，须重新验收"]
    for name, digest in receipt.get('source_inputs', {}).items():
        path = task/name
        if path.is_symlink() or not path.is_file() or file_sha(path) != digest:
            return ['逐句复核的原始来源已变化：'+name]
    return quality.certificate_errors(task, receipt)


def analysis_errors(task: Path, expected_sha=None) -> list[str]:
    import fbr_trace_review
    from types import SimpleNamespace
    path = task/'review/trace-analysis.json'
    if path.is_symlink() or not path.is_file() or (expected_sha and file_sha(path) != expected_sha):
        return ['缺少绑定的轨迹分析报告']
    doc = lib.load_json(path, default={})
    if (doc.get('state') != 'accepted' or not doc.get('receipt', {}).get('model')
            or not doc.get('receipt', {}).get('reasoning_effort')
            or doc.get('receipt', {}).get('provider') != PROVIDER):
        return ['轨迹分析尚未完成或调用回执缺失']
    engine = SimpleNamespace(file_sha=file_sha, quality=quality)
    if doc.get('identity') != fbr_trace_review.identity(task, engine):
        return ['轨迹分析来源或规则已变化，须重新分析']
    try:
        fbr_trace_review.validate_sources(task, doc.get('statements'))
        fbr_trace_review.validate_coverage(task, doc)
    except lib.FbrError as exc:
        return [str(exc)]
    return []


def peer_feedback(task: Path) -> list[dict]:
    """同批与历史正文供复核比较；不把其他记录当成本题的完成证据。"""
    import fbr_feedback
    root = lib.find_root(str(task.parents[2]))
    rows = fbr_feedback.other_feedback(root, task.parent.name)
    for path in lib.task_dirs(root, task.parent.name):
        if path != task:
            doc = lib.load_json(path / "review/feedback.json", default={})
            if isinstance(doc, dict) and doc.get("feedback"):
                rows.append({"task_id": path.name, "feedback": str(doc["feedback"])})
    return rows


def draft_errors(task: Path, audit: dict, feedback: dict, peers: list[dict]) -> list[str]:
    import fbr_gate as gate
    import fbr_feedback
    record, facts = fbr_feedback.load_evidence(task)
    probes = lib.read_jsonl(task / "evidence/probes.jsonl")
    cfg = lib.settings(lib.find_root(str(task.parents[2])))["feedback_gate"]
    ev = gate.build_evidence(record, facts, audit, probes)
    errors = quality.check_contract(feedback, record, facts, audit, probes)
    for found, _ in (gate.check_audit(audit, record, ev),
                     gate.check_score(feedback.get("score"), facts, audit, ev, cfg),
                     gate.check_text(str(feedback.get("feedback", "")), record["prompt"], cfg),
                     gate.check_feedback(feedback, record, facts, audit, ev, cfg)):
        errors += found
    duplicates, _ = gate.check_duplicates({task.name: str(feedback.get("feedback", ""))}, peers, cfg)
    return errors + duplicates.get(task.name, [])


def decode_output(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise lib.FbrError("验收模型未返回合法 JSON；不采用本次结果，不回退其他模型") from None
    if not isinstance(value, dict):
        raise lib.FbrError("验收模型输出必须是 JSON 对象")
    return value


@lib.task_locked
def review(task: Path, base: str, secret: str, *, max_versions=3, model=MODEL, effort=EFFORT) -> dict:
    """Analyze traces only, then hand acceptance to the AI advancing this task."""
    lib.require_stage(task, 'facts')
    if not (task/'review/card.md').is_file():
        raise lib.FbrError('先执行 fbr_feedback.py card')
    trace_index(task)
    import fbr_trace_review
    from types import SimpleNamespace
    def trace_invoke(*args, **kwargs):
        return invoke(*args, **kwargs, model=model, effort=effort)
    engine = SimpleNamespace(invoke=trace_invoke, decode_output=decode_output, file_sha=file_sha,
                             quality=quality, MODEL=model, EFFORT=effort)
    import fbr_review_slots
    with fbr_review_slots.slot(lib.settings(lib.find_root(str(task.parents[2])))):
        fbr_trace_review.analyze(task, base, secret, engine)
    return prepare_task_agent(task)


def prepare_task_agent(task: Path) -> dict:
    lib.require_stage(task, 'facts')
    if lib.load_json(task/'review/upload.json', default={}).get('submission_id'):
        raise lib.FbrError('任务已有平台单，不能自动重写评价')
    errors = analysis_errors(task)
    if errors:
        raise lib.FbrError('；'.join(errors))
    import fbr_feedback
    binding = {'inputs': inputs(task), 'policy_sha': quality.policy_sha(),
               'trace-analysis_sha': file_sha(task/'review/trace-analysis.json')}
    checkpoint = {'workflow': 'task-agent-v1', 'state': 'needs_task_agent', 'task_id': task.name,
                  'at': lib.now_iso(), 'binding': binding,
                  'candidate_schema': {'audit': fbr_feedback.AUDIT_TEMPLATE, 'feedback': fbr_feedback.FEEDBACK_TEMPLATE,
                                       'needs_probes': []},
                  'verification_contract': {'task_agent': 'current_task_agent', 'binding': binding,
                      'candidate_sha': 'lib.content_hash(candidate)', 'checks': list(quality.CHECKS),
                      'claim_reviews': '按 public_units 完整逐句回查原文、断言与后续最终状态'},
                  'detail': '当前任务 AI 检查产物、评分、撰写及复核 Feedback；本阶段不调用 reviewer.env'}
    gate = lib.load_json(task/'review/gate.json', default={})
    last = lib.load_json(task/'review/task-agent-history.json', default={}).get('attempts', [])
    checkpoint['repair_errors'] = gate.get('errors', []) + (last[-1].get('errors', []) if last else [])
    checkpoint['next_steps'] = ['当前任务 AI 修复候选并逐句核对过程及产物断言', 'finalize 过闸并主动推送快照']
    lib.save_json(task/'review/task-agent-work.json', checkpoint)
    lib.set_stage(task, 'feedback', 'dry', reason=checkpoint['detail'])
    return {'state': 'needs_task_agent', 'task_id': task.name, 'detail': checkpoint['detail']}


@lib.task_locked
def finalize(task: Path, candidate: dict, verification: dict, *, publish=True) -> dict:
    """Deterministic validation only: never load credentials or call an external model."""
    lib.require_stage(task, 'facts')
    work = lib.load_json(task/'review/task-agent-work.json', default={})
    binding = {'inputs': inputs(task), 'policy_sha': quality.policy_sha(),
               'trace-analysis_sha': file_sha(task/'review/trace-analysis.json')}
    if work.get('binding') != binding or verification.get('binding') != binding:
        raise lib.FbrError('验收期间材料或规则变化，须刷新任务 AI 检查点并重新复核')
    if (verification.get('task_agent') != 'current_task_agent'
            or verification.get('candidate_sha') != lib.content_hash(candidate)):
        raise lib.FbrError('缺少当前任务 AI 与本次候选绑定的复核记录')
    errors = analysis_errors(task, binding['trace-analysis_sha'])
    requests = candidate.get('needs_probes')
    if not isinstance(requests, list):
        raise lib.FbrError('候选必须包含 needs_probes 列表')
    if requests:
        lib.save_json(task/'review/probe-requests.json', {'requests': requests, 'provider': 'current_task_agent'})
        return {'state': 'needs_probes', 'task_id': task.name}
    audit, feedback = candidate.get('audit'), candidate.get('feedback')
    if not isinstance(audit, dict) or not isinstance(feedback, dict):
        raise lib.FbrError('候选缺少 audit 或 feedback')
    errors += draft_errors(task, audit, feedback, peer_feedback(task))
    errors += quality.report_errors(verification) + quality.claim_report_errors(verification, feedback, task)
    history_path = task/'review/task-agent-history.json'
    history = lib.load_json(history_path, default={'attempts': []})
    history['attempts'].append({'at': lib.now_iso(), 'candidate': candidate,
                                'verification': verification, 'errors': errors})
    lib.save_json(history_path, history)
    if errors:
        return {'state': 'repairing', 'task_id': task.name, 'errors': errors,
                'detail': '当前任务 AI 按错误继续修复并复验；未采用失败候选'}
    if inputs(task) != binding['inputs'] or quality.policy_sha() != binding['policy_sha']:
        raise lib.FbrError('复核期间证据或规则变化，拒绝采用过期评价')
    # Preserve the previous accepted or legacy files as history before replacing them.
    old = task/'review/task-agent-history'/str(len(history['attempts']))
    for name in ('evidence/audit.json', 'review/feedback.json', 'review/model-review.json', 'review/quality.json'):
        path = task/name
        if path.is_file():
            target = old/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    source_inputs = {r['path']: file_sha(task/r['path'])
                     for row in verification['claim_reviews'] for r in row['sources']}
    lib.save_json(task/'evidence/audit.json', audit)
    lib.save_json(task/'review/feedback.json', feedback)
    receipt = {**binding, 'workflow': 'task-agent-v1', 'state': 'completed',
               'provider': 'current_task_agent', 'model': 'current_task_agent', 'reasoning_effort': 'session',
               'at': lib.now_iso(), 'source_inputs': source_inputs,
               'audit_sha': file_sha(task/'evidence/audit.json'), 'feedback_sha': file_sha(task/'review/feedback.json')}
    lib.save_json(task/'review/quality.json', {**receipt, 'version': quality.VERSION, 'state': 'accepted',
                  'checks': verification['checks'], 'claim_reviews': verification['claim_reviews']})
    lib.save_json(task/'review/model-review.json', receipt)
    (task/'review/probe-requests.json').unlink(missing_ok=True)
    # A successful candidate is immediately gated and persisted to the local workbook.
    import fbr_feedback
    import fbr_sheet
    root = lib.find_root(str(task.parents[2]))
    cfg = lib.settings(root)
    rows, _ = fbr_feedback.gate_batch(root, task.parent.name, cfg, task.name)
    fbr_sheet.write_sheet(root, task.parent.name, cfg)
    row = next(r for r in rows if r['task_id'] == task.name)
    if row['state'] == 'ok':
        import argparse, threading, fbr_flow
        options = argparse.Namespace(batch=task.parent.name, publish=publish, auto_submit=False, delivery_lock=threading.Lock())
        return fbr_flow.finish_review(task, root, cfg, options)
    return {'state': 'ok' if row['state'] == 'ok' else 'repairing', 'task_id': task.name,
            'errors': row.get('errors', []), 'detail': '已复核、过闸门并刷新本地表' if row['state'] == 'ok' else '继续修复闸门错误'}


def main() -> int:
    parser = argparse.ArgumentParser(description='仅轨迹分析使用 reviewer.env；过程核查及其余验收由当前任务 AI 完成')
    parser.add_argument('--root')
    parser.add_argument('--env-file', default=DEFAULT_ENV)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('probe', 'review', 'prepare', 'finalize'):
        p = subs.add_parser(name)
        p.add_argument('--execute', action='store_true')
        if name != 'probe':
            p.add_argument('--batch', required=True)
            p.add_argument('--task', required=True)
    subs.choices['finalize'].add_argument('--candidate', required=True)
    subs.choices['finalize'].add_argument('--verification', required=True)
    subs.choices['finalize'].add_argument('--no-publish', action='store_true')
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        if not args.execute:
            print(json.dumps({'state': 'dry', 'detail': '只有轨迹分析 review 及其连通性探测 probe 读取 reviewer.env；finalize 校验当前任务 AI 的验收材料并交付快照'}, ensure_ascii=False))
            return 0
        if args.command in ('probe', 'review'):
            base, secret = credentials(Path(args.env_file).expanduser())
            model, effort = trace_configuration(Path(args.env_file).expanduser())
        if args.command == 'probe':
            text, receipt = invoke(base, secret, root, '只回复 OK，不读取文件、不调用任何工具。', timeout=120, model=model, effort=effort)
            if text.strip() != 'OK':
                raise lib.FbrError('轨迹模型探测未返回约定的 OK')
            result = {'state': 'ok', **receipt, 'reply': 'OK'}
        else:
            task = lib.task_dirs(root, args.batch, args.task)[0]
            if args.command == 'review':
                result = review(task, base, secret, model=model, effort=effort)
            elif args.command == 'prepare':
                with lib.file_lock(task/'.operation.lock'):
                    result = prepare_task_agent(task)
            else:
                result = finalize(task, lib.load_json(Path(args.candidate)), lib.load_json(Path(args.verification)), publish=not args.no_publish)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['state'] != 'repairing' else 2
    except (lib.FbrError, OSError) as exc:
        print('错误：'+str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
