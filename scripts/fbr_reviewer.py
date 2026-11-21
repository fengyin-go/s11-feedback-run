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
import fbr_evidence
import fbr_quality as quality
import fbr_style

MODEL = "gpt-6.2-sol"
EFFORT = "high"
PROVIDER = "solo11_reviewer"
KEY_ENV = "SOLO11_REVIEW_API_KEY"
DEFAULT_ENV = "~/.codex/solo11/reviewer.env"
FINALIZE_IDEMPOTENCY_VERSION = 1


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
        # The reviewer sandbox is read-only; access diagnostics are recorded by
        # the parent/current-task-AI process instead of mutating the task tree.
        env["FBR_EVIDENCE_ACCESS_LOG"] = "0"
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
                      "base_url": base, "wire_api": "responses", "at": lib.now_iso(),
                      "prompt_bytes": len(prompt.encode("utf-8")),
                      "response_bytes": len(text.encode("utf-8")),
                      "usage": token_usage(result.stdout)}


def token_usage(events: str) -> dict:
    """Extract only token counters from codex --json; never retain the event stream."""
    usage = {}
    for line in (events or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        stack = [event]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("input_tokens", "output_tokens", "reasoning_output_tokens",
                               "cached_input_tokens", "total_tokens") and type(item) is int:
                        usage[key] = item
                    elif key == "reasoning_tokens" and type(item) is int:
                        usage["reasoning_output_tokens"] = item
                    elif isinstance(item, (dict, list)):
                        stack.append(item)
            elif isinstance(value, list):
                stack.extend(value)
    return usage


def write_metrics(task: Path, stage: str, values: dict) -> dict:
    path = Path(task)/'review/metrics.json'
    doc = lib.load_json(path, default={'version': 1, 'stages': {}})
    doc['version'] = 1
    doc['stages'][stage] = {'at': lib.now_iso(), **values}
    lib.save_json(path, doc)
    return doc


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


def _load_attempt_history(task: Path) -> tuple[Path, dict]:
    """Load current history and normalize the legacy top-level list format."""
    path = Path(task) / "review/task-agent-history.json"
    raw = lib.load_json(path, default={})
    if isinstance(raw, list):
        return path, {"version": 2, "attempts": raw, "accepted_versions": []}
    if not isinstance(raw, dict):
        raise lib.FbrError("task-agent-history.json 必须是对象或旧版数组")
    attempts = raw.get("attempts")
    if attempts is None:
        attempts = []
    if not isinstance(attempts, list):
        raise lib.FbrError("task-agent-history.json.attempts 必须是数组")
    history = dict(raw)
    history["version"] = 2
    history["attempts"] = attempts
    accepted = history.get("accepted_versions")
    if not isinstance(accepted, list):
        accepted = []
    history["accepted_versions"] = accepted
    return path, history


def _validate_frozen_prompt_inputs(task: Path) -> None:
    """Finalize must not reuse or accept output after the frozen prompt changed."""
    record = lib.load_json(task / "task.json")
    prompt_path = task / "prompt.txt"
    if (not prompt_path.is_file() or prompt_path.is_symlink()
            or prompt_path.read_text(encoding="utf-8") != str(record.get("prompt") or "")):
        raise lib.FbrError(f"{task.name}：prompt.txt 与 task.json 题面不一致")
    contract = record.get("review_contract")
    if contract is not None:
        contract_path = task / "review-contract.json"
        if (not contract_path.is_file() or contract_path.is_symlink()
                or lib.load_json(contract_path, default={}) != contract):
            raise lib.FbrError(f"{task.name}：review-contract.json 与 task.json 不一致")


def _finalize_attempt_key(task: Path, candidate: dict, verification: dict, binding: dict,
                          *, publish: bool, defer_delivery: bool,
                          allow_platform_repair: bool, peers: list[dict]) -> tuple[str, dict]:
    """Hash only semantic inputs; timestamps and repeated execution are excluded."""
    root = lib.find_root(str(task.parents[2]))
    cfg = lib.settings(root)
    spot = task / "review/spotcheck.json"
    mode = {
        "publish": bool(publish),
        "defer_delivery": bool(defer_delivery),
        "allow_platform_repair": bool(allow_platform_repair),
    }
    signature = {
        "version": FINALIZE_IDEMPOTENCY_VERSION,
        "candidate_sha": lib.content_hash(candidate),
        "verification_sha": lib.content_hash(verification),
        "binding": binding,
        "quality_version": quality.VERSION,
        "style_version": fbr_style.VERSION,
        "style_policy_sha": fbr_style.policy_sha(),
        "gate_context": {
            "feedback_gate": cfg.get("feedback_gate", {}),
            "peer_feedback_sha": lib.content_hash(peers),
            "spotcheck_sha": file_sha(spot) if spot.is_file() and not spot.is_symlink() else "",
        },
        "mode": mode,
    }
    return lib.content_hash(signature), {"signature": signature, "mode": mode}


def _accepted_state_current(task: Path, candidate: dict, *, publish: bool,
                            defer_delivery: bool) -> bool:
    """Return true only when a previously accepted state is still complete and valid."""
    try:
        lib.load_task_readonly(task)
    except lib.FbrError:
        return False
    audit, feedback = candidate.get("audit"), candidate.get("feedback")
    if not isinstance(audit, dict) or not isinstance(feedback, dict):
        return False
    if lib.load_json(task / "evidence/audit.json", default={}) != audit:
        return False
    if lib.load_json(task / "review/feedback.json", default={}) != feedback:
        return False
    stage = lib.load_state(task)["stages"].get("feedback", {})
    gate = lib.load_json(task / "review/gate.json", default={})
    model_review = lib.load_json(task / "review/model-review.json", default={})
    quality_receipt = lib.load_json(task / "review/quality.json", default={})
    code_review_path = task / "review/code-review.json"
    if feedback.get("feedback_policy_version", 0) >= 2:
        if (code_review_path.is_symlink() or not code_review_path.is_file()
                or lib.load_json(code_review_path) != feedback.get("code_review")):
            return False
    text_sha = hashlib.sha256(str(feedback.get("feedback", "")).encode("utf-8")).hexdigest()
    if (stage.get("state") != "ok" or stage.get("feedback_sha") != text_sha
            or gate.get("passed") is not True or gate.get("feedback_sha") != text_sha
            or model_review.get("state") != "completed"
            or quality_receipt.get("state") != "accepted"):
        return False
    spot = lib.load_json(task / "review/spotcheck.json", default={})
    if spot.get("state") == "退回" and spot.get("feedback_sha") == text_sha:
        return False
    delivery = lib.load_json(task / "review/delivery-repair.json", default={})
    if not publish and delivery.get("state") == "local_accepted":
        return not provenance_errors(task)
    if delivery.get("state") != "passed":
        return False
    if publish and not defer_delivery:
        snapshot = lib.load_json(task / "review/snapshot.json", default={})
        if snapshot.get("pushed") is not True:
            return False
    if not defer_delivery:
        import fbr_attestation
        root = lib.find_root(str(task.parents[2]))
        if not fbr_attestation.validate(task, root, lib.settings(root))["valid"]:
            return False
    return True


def _reuse_finalize_attempt(task: Path, history_path: Path, history: dict, attempt: dict,
                            candidate: dict, *, publish: bool, defer_delivery: bool) -> dict | None:
    """Return a previously recorded result when no semantic input has changed."""
    state = str(attempt.get("result_state") or "")
    errors = attempt.get("errors") if isinstance(attempt.get("errors"), list) else []
    accepted = not errors and _accepted_state_current(
        task, candidate, publish=publish, defer_delivery=defer_delivery)
    if accepted:
        attempt["result_state"] = "ok"
        attempt["result_detail"] = "相同 candidate 和 verification 已完整验收，复用现有回执"
        result_state, result_errors, detail = (
            "ok", [], "相同 candidate 和 verification 已完整验收，复用现有回执"
        )
    elif state == "repairing" or errors or attempt.get("gate_errors"):
        gate_errors = attempt.get("gate_errors")
        result_errors = list(dict.fromkeys(
            [str(item) for item in errors] +
            ([str(item) for item in gate_errors] if isinstance(gate_errors, list) else [])
        ))
        result_state = "repairing"
        detail = (
            "相同 candidate 和 verification 已经处理过；返回已记录错误，"
            "不要重复 finalize"
        )
        if attempt.get("result_detail"):
            detail += "；" + str(attempt["result_detail"])
    else:
        return None
    attempt["reuse_count"] = int(attempt.get("reuse_count") or 0) + 1
    attempt["last_reused_at"] = lib.now_iso()
    lib.save_json(history_path, history)
    return {
        "task_id": task.name,
        "state": result_state,
        "errors": result_errors,
        "detail": detail,
        "idempotent_reuse": True,
        "attempt_sha": attempt.get("attempt_sha"),
    }


def _remember_finalize_result(history_path: Path, history: dict, attempt: dict,
                              result: dict, *, gate_errors: list[str] | None = None) -> dict:
    attempt.update({
        "result_state": result.get("state", "unknown"),
        "result_detail": result.get("detail", ""),
        "result_errors": list(result.get("errors") or []),
    })
    if gate_errors is not None:
        attempt["gate_errors"] = list(gate_errors)
    lib.save_json(history_path, history)
    return result


def trace_index(task: Path) -> Path:
    """Compatibility navigation index projected from the single-ingest event graph."""
    target = task / "review/trace-index.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise lib.FbrError("轨迹索引不能是符号链接")
    check = fbr_evidence.ingest(task)
    events = lib.read_jsonl(task / fbr_evidence.EVENTS)
    output = []
    for source in check["sources"]:
        output.append({"source": source["path"]})
        for event in events:
            if event.get("source") != source["path"]:
                continue
            kind = event.get("type", "")
            ref = {"block": event.get("block"), "type": kind}
            if kind == "tool_use":
                ref.update(id=event.get("tool_use_id"), tool=event.get("tool"),
                           target=str(event.get("target", ""))[:240])
            elif kind == "tool_result":
                ref.update(tool_use_id=event.get("tool_use_id"), is_error=event.get("is_error", False))
            elif kind == "text":
                ref["preview"] = str(event.get("text", ""))[:100]
            output.append({"line": event.get("line"), "type": event.get("row_type"),
                           "event_id": event.get("id"), "blocks": [ref]})
    target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output),
                      encoding="utf-8")
    return target


def inputs(task: Path) -> dict:
    fbr_evidence.ingest(task)
    names = ["task.json", "trace/ids.json", "evidence/facts.json", "evidence/probes.jsonl",
             "review/card.md", "review/review-capsule.json", "review/task-agent-brief.md",
             "review/diff.patch",
             'record/recording.json', 'record/recording.mp4']
    names += [fbr_evidence.MANIFEST, fbr_evidence.EVENTS, fbr_evidence.CHECK]
    names += [str(p.relative_to(task)) for p in sorted((task / 'evidence/command-logs').rglob('*.log'))]
    result = {}
    for name in names:
        p = task / name
        if p.is_symlink():
            raise lib.FbrError("验收输入不能是符号链接：" + name)
        if p.is_file():
            result[name] = (
                fbr_evidence.generated_digest(p)
                if name in (fbr_evidence.MANIFEST, fbr_evidence.CHECK)
                else file_sha(p)
            )
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
    recorded_inputs = receipt.get("inputs")
    if not isinstance(recorded_inputs, dict):
        return ["缺少验收输入摘要"]
    current_inputs = inputs(task)
    generated_inputs = {
        fbr_evidence.MANIFEST, fbr_evidence.CHECK, "trace/review-pack.json",
    }
    recorded_keys = set(recorded_inputs)
    current_keys = set(current_inputs)
    missing = recorded_keys - current_keys
    added = current_keys - recorded_keys
    changed = {
        name for name in recorded_keys & current_keys
        if recorded_inputs.get(name) != current_inputs.get(name)
    }
    source_changes = (missing | added | changed) - generated_inputs
    generated_changes = (missing | added | changed) & generated_inputs
    if source_changes:
        return ["轨迹或验收证据变化，须重新验收"]
    if generated_changes:
        closeout = lib.load_json(task / "review/task-closeout.json", default={})
        stages = closeout.get("stages") if isinstance(closeout.get("stages"), dict) else {}
        frozen = (
            closeout.get("task_id") == task.name
            and closeout.get("gate_state") == "passed"
            and all(stages.get(name) == "ok"
                    for name in ("run", "collect", "commit", "record", "facts", "feedback"))
        )
        if not frozen:
            return ["轨迹或验收证据变化，须重新验收"]
    for name, digest in receipt.get('source_inputs', {}).items():
        path = task/name
        if path.is_symlink():
            return ['逐句复核的原始来源已变化：'+name]
        if not path.is_file():
            if lib.compacted_source_removed_ok(task, name):
                continue
            return ['逐句复核的原始来源已变化：'+name]
        if file_sha(path) != digest:
            return ['逐句复核的原始来源已变化：'+name]
    errors = quality.certificate_errors(task, receipt)
    if errors:
        return errors
    cert = lib.load_json(task / "review/quality.json", default={})
    if isinstance(cert, dict) and cert.get("language_policy_version"):
        errors = fbr_style.provenance_errors(task)
        if errors:
            return errors
        report = task / fbr_style.REPORT_FILE
        if cert.get("style_report_sha") != file_sha(report):
            return ["Feedback 语言检查报告在验收后被改动，须重新 lint/finalize"]
    return []


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
    identity = doc.get('identity') or {}
    current = fbr_trace_review.identity(task, engine)

    def stable_evidence(value):
        evidence = (value or {}).get('evidence')
        if not isinstance(evidence, dict):
            return evidence
        return {
            key: digest
            for key, digest in evidence.items()
            if key not in (fbr_evidence.CHECK, fbr_evidence.PACK)
        }

    if (identity.get('inputs') != current.get('inputs')
            or stable_evidence(identity) != stable_evidence(current)
            or identity.get('policy_sha') not in ({current.get('policy_sha')} | quality.LEGACY_TRACE_POLICIES)):
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


def downgrade_errors(task: Path, feedback: dict) -> list[str]:
    basis = feedback.get("score_basis") if isinstance(feedback, dict) else {}
    if not isinstance(basis, dict) or basis.get("downgrade_from") != 4:
        return []
    if feedback.get("score") != 3:
        return ["声明从 4 分降到 3 分时，最终 score 必须为 3"]
    validation = lib.load_json(task / "review/downgrade-validation.json", default={})
    errors = []
    if not validation.get("eligible"):
        errors.append("4 -> 3 证据审计尚未通过，不能采用降分候选")
    for key, path in (
        ("evidence_sha256", task / "review/downgrade-review.json"),
        ("probes_sha256", task / "evidence/probes.jsonl"),
        ("downgrade_probes_sha256", task / "evidence/downgrade-probes.jsonl"),
    ):
        digest = file_sha(path) if path.is_file() else ""
        if not digest or validation.get(key) != digest:
            errors.append(f"4 -> 3 证据已变化，须重新运行 fbr_score_audit.py validate：{path.name}")
    return errors


def draft_errors(task: Path, audit: dict, feedback: dict, peers: list[dict]) -> list[str]:
    import fbr_gate as gate
    import fbr_feedback
    record, facts = fbr_feedback.load_evidence(task)
    probes = lib.read_jsonl(task / "evidence/probes.jsonl")
    cfg = lib.settings(lib.find_root(str(task.parents[2])))["feedback_gate"]
    ev = gate.build_evidence(record, facts, audit, probes)
    errors = quality.check_contract(feedback, record, facts, audit, probes)
    errors += quality.requirements_status_errors(feedback, record, audit, ev)
    errors += downgrade_errors(task, feedback)
    for found, _ in (gate.check_audit(audit, record, ev),
                     gate.check_score(feedback.get("score"), facts, audit, ev, cfg),
                     gate.check_text(str(feedback.get("feedback", "")), record["prompt"], cfg),
                     gate.check_feedback(feedback, record, facts, audit, ev, cfg)):
        errors += found
    duplicates, _ = gate.check_duplicates({task.name: str(feedback.get("feedback", ""))}, peers, cfg)
    return errors + duplicates.get(task.name, [])


def lint_candidate(task: Path, candidate: dict, verification: dict | None = None) -> dict:
    """Run every deterministic pre-finalize check without writing accepted evidence."""
    audit, feedback = candidate.get("audit"), candidate.get("feedback")
    if not isinstance(audit, dict) or not isinstance(feedback, dict):
        raise lib.FbrError("候选缺少 audit 或 feedback")
    errors = analysis_errors(task)
    requests = candidate.get("needs_probes")
    if not isinstance(requests, list):
        errors.append("候选必须包含 needs_probes 列表")
    elif requests:
        errors.append("候选仍请求补证，先执行 probe 并刷新材料")
    errors += draft_errors(task, audit, feedback, peer_feedback(task))
    if verification is not None:
        report = fbr_style.write_report(
            task, str(feedback.get("feedback", "")), feedback.get("score"),
            verification.get("language_review"), require_review=True)
        errors += [row["message"] for row in report["errors"]]
        errors += quality.report_errors(verification)
        errors += quality.claim_report_errors(verification, feedback, task)
    else:
        fbr_style.write_report(
            task, str(feedback.get("feedback", "")), feedback.get("score"),
            require_review=False)
    return {"task_id": task.name, "state": "ok" if not errors else "repairing",
            "errors": list(dict.fromkeys(errors)), "detail": "Feedback 候选本地预检通过" if not errors else
            "按 errors 修复后重新 lint；平台提交预检仍会实时核对 schema、附件和账号"}


def repair_actions(errors: list[str]) -> list[dict]:
    """Convert mechanical validation errors into targeted patch locations."""
    actions = []
    for error in errors:
        path, fix = "feedback", "按错误缩小或补齐正文和结构化证据后重新 build/lint"
        if "逐条引用过程句 evidence" in error or "过程句 evidence" in error:
            path = "process[].evidence"
            fix = "保留 process.evidence，并让同句 claims.sources 引用同一摘录；重新 build 会自动合并"
        elif "摘录不在原始文件" in error or "摘录不在指定" in error or "摘录没有唯一位置" in error:
            path = "claims[].sources"
            fix = "从指定 step 的 target/output/error 复制连续原文，交给 build 自动定位行号和 block"
        elif "缺少原始文件及连续摘录" in error or "来源不是本题可核对" in error:
            path = "claims[].sources"
            fix = "改为 trace/projects、command-logs、probes、recording.json 或 task.json 的连续摘录"
        elif "完整句段" in error or "正文原句" in error or "完整正文原句" in error:
            path = "results/process/issues[].sentence"
            fix = "引用一个完整句号或分号段；若只写局部短语，build 仅在唯一包含句时自动扩展"
        elif "consumer_check" in error:
            path = "code_review.changed_files_reviewed[].consumer_check"
            fix = "沿本次改动追到消费者，写入实际输入、调用方和最终观察结果，再核对 finding"
        elif any(key in error for key in ("按两段重写", "写入第一段", "落在第二段", "过程段先用一句")):
            path = "feedback"
            fix = "按产物和过程整理两段，先概括完成项和操作，再写有证据的问题；重绑原句"
        elif "public_behavior" in error or "use=delivery" in error:
            path = "test_claims"
            fix = "检查该次断言；交付结论引用正文业务结果，纯模型操作绑定过程句，重跑 lint"
        elif "process_audit" in error or "过程句归因" in error:
            path = "process[].audit"
            fix = "补齐五项 checks 和 note；process_audit 由 build 从该对象生成"
        elif "requirements_status" in error or "需求状态" in error:
            path = "requirements[]"
            fix = "补齐需求 verdict、user_text 和 anchors；build 会生成 requirements_status"
        elif "code_review" in error or "代码审查" in error or "finding" in error:
            path = "code_review"
            fix = "先审查最终 diff 和调用链，补齐文件覆盖、逐需求检查、finding 与 issues 的对应关系"
        elif "score_basis" in error or "分数" in error or "评分依据" in error:
            path = "score_basis"
            fix = "补齐 category、已交付、剩余、降档理由和 anchors；可省略项由 build 生成"
        elif "language_review" in error or "正文里" in error or "Feedback" in error:
            path = "feedback/language_review"
            fix = "按 style-report 命中位置改写正文，并确保 criterion_sentence 逐字存在于正文"
        elif "测试描述" in error or "test_claims" in error:
            path = "test_claims"
            fix = "用同一次执行的真实输出绑定完整测试分句、行为和计数"
        actions.append({"path": path, "error": error, "fix": fix})
    return actions


def validation_report(task: Path, stage: str, result: dict) -> Path:
    errors = [str(item) for item in result.get("errors") or []]
    groups = {}
    for error in errors:
        if any(word in error for word in ("正文原句", "完整分句", "连续原文", "对应完整正文")):
            key = "sentence_alignment"
        elif any(word in error for word in ("轨迹里没有第", "步骤", "process", "过程句", "evidence")):
            key = "trace_steps"
        elif any(word in error for word in ("requirements_status", "没有覆盖全部需求", "results 没有按需求")):
            key = "requirements_coverage"
        elif any(word in error for word in ("code_review", "代码审查", "finding", "call_chain")):
            key = "code_review"
        elif any(word in error for word in ("锚点", "anchors", "来源", "摘录不在")):
            key = "anchors_sources"
        elif any(word in error for word in ("测试描述", "通过数量", "count", "输出摘录")):
            key = "test_claims"
        elif any(word in error for word in ("score", "评分", "分数", "category")):
            key = "score_basis"
        elif any(word in error for word in ("查重", "模板", "骨架", "相同片段")):
            key = "duplicates"
        elif any(word in error for word in ("黑话", "抽象", "语言", "数字", "精确值", "完成度", "AI 腔", "套话")):
            key = "language_style"
        else:
            key = "structure"
        groups.setdefault(key, []).append(error)
    target = task/f"review/{stage}-errors.json"
    lib.save_json(target, {
        "version": 1, "stage": stage, "task_id": task.name,
        "state": result.get("state"), "error_count": len(errors),
        "groups": groups, "errors": errors,
        "repair_actions": repair_actions(errors), "at": lib.now_iso(),
    })
    return target


def compact_result(task: Path, stage: str, result: dict, *, full: bool = False) -> str:
    if full:
        return json.dumps(result, ensure_ascii=False, indent=2)
    errors = list(dict.fromkeys(str(item) for item in result.get("errors") or []))
    lines = [f"{task.name}\t{result.get('state', 'ok')}\t{result.get('detail', '')}".rstrip()]
    if errors:
        report = validation_report(task, stage, result)
        lines.append(f"错误 {len(errors)} 项；完整报告 {report}")
        lines.extend(
            f"- {lib.clip(row['path'], 80)}：{lib.clip(row['error'], 200)}"
            for row in repair_actions(errors)[:5]
        )
        if len(errors) > 5:
            lines.append(f"- 其余 {len(errors)-5} 项见报告")
    return "\n".join(lines)


def decode_output(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise lib.FbrError("验收模型未返回合法 JSON；不采用本次结果，不回退其他模型") from None
    if not isinstance(value, dict):
        raise lib.FbrError("验收模型输出必须是 JSON 对象")
    return value


@lib.task_locked
def review(task: Path, base: str, secret: str, *, max_versions=3, model=MODEL, effort=EFFORT,
           allow_platform_repair: bool = False) -> dict:
    """Analyze traces only, then hand acceptance to the AI advancing this task."""
    if allow_platform_repair:
        lib.assert_platform_prompt_unchanged(task)
    lib.require_stage(task, 'facts')
    if not (task/'review/card.md').is_file() or not (task/'review/task-agent-brief.md').is_file():
        raise lib.FbrError('先执行 fbr_feedback.py card')
    fbr_evidence.ingest(task)
    import fbr_trace_review
    from types import SimpleNamespace
    def trace_invoke(*args, **kwargs):
        return invoke(*args, **kwargs, model=model, effort=effort)
    engine = SimpleNamespace(invoke=trace_invoke, decode_output=decode_output, file_sha=file_sha,
                             quality=quality, MODEL=model, EFFORT=effort)
    import fbr_review_slots
    with fbr_review_slots.slot(lib.settings(lib.find_root(str(task.parents[2])))):
        fbr_trace_review.analyze(task, base, secret, engine)
    analysis = lib.load_json(task/'review/trace-analysis.json')
    write_metrics(task, 'trace_analysis', {
        'model': analysis.get('receipt', {}).get('model'),
        'reasoning_effort': analysis.get('receipt', {}).get('reasoning_effort'),
        'prompt_bytes': analysis.get('receipt', {}).get('prompt_bytes', 0),
        'response_bytes': analysis.get('receipt', {}).get('response_bytes', 0),
        'usage': analysis.get('receipt', {}).get('usage', {}),
        'pack_bytes': (task/fbr_evidence.PACK).stat().st_size,
        'raw_trace_bytes': sum(row.get('bytes', 0) for row in
                               lib.load_json(task/fbr_evidence.CHECK).get('sources', [])),
        'access': fbr_evidence.access_summary(task),
    })
    return prepare_task_agent(task, allow_platform_repair=allow_platform_repair)


def prepare_task_agent(task: Path, *, allow_platform_repair: bool = False) -> dict:
    lib.require_stage(task, 'facts')
    if (not allow_platform_repair
            and lib.load_json(task/'review/upload.json', default={}).get('submission_id')):
        raise lib.FbrError('任务已有平台单，不能自动重写评价')
    if allow_platform_repair:
        lib.assert_platform_prompt_unchanged(task)
    errors = analysis_errors(task)
    if errors:
        raise lib.FbrError('；'.join(errors))
    import fbr_feedback
    fbr_evidence.ingest(task)
    binding = {'inputs': inputs(task), 'policy_sha': quality.policy_sha(),
               'trace-analysis_sha': file_sha(task/'review/trace-analysis.json')}
    evidence_check = lib.load_json(task / fbr_evidence.CHECK)
    schema_path = task/'review/candidate-schema.json'
    template = fbr_feedback.decision_template(task)
    lib.save_json(schema_path, template)
    decision_path = task/'review/decision-input.json'
    if not decision_path.exists():
        lib.save_json(decision_path, template)
    checkpoint = {'workflow': 'task-agent-v1', 'state': 'needs_task_agent', 'task_id': task.name,
                  'at': lib.now_iso(), 'binding': binding,
                  'candidate_schema_ref': str(schema_path.relative_to(task)),
                  'evidence_contract': {
                      'brief': 'review/task-agent-brief.md',
                      'trace_pack': fbr_evidence.PACK,
                      'trace_check': fbr_evidence.CHECK,
                      'trace_check_sha256': file_sha(task / fbr_evidence.CHECK),
                      'counts': evidence_check.get('counts', {}),
                      'access': fbr_evidence.access_summary(task),
                      'rule': '禁止直接读取原始 trace JSONL；每条过程结论必须通过精确行号和 block 回查原始轨迹',
                  },
                  'verification_contract': {'task_agent': 'current_task_agent', 'binding': binding,
                      'candidate_sha': 'lib.content_hash(candidate)', 'checks': list(quality.CHECKS),
                      'code_review': 'policy v2 必须覆盖相关改动文件、全部需求调用链和代码检查；finding 优先进入 delivery_issue',
                      'language_review': '填写 criterion_sentence、translated_terms、retained_terms；criterion_sentence 必须逐字出现在正文',
                      'process_evidence': '每个 process 句绑定真实 steps 及 tool/assistant evidence；复核引用同一原始摘录',
                      'claim_reviews': '按 public_units 完整逐句回查；trace 来源必须带 start_line、end_line、block 和原文'},
                  'detail': '当前任务 AI 检查产物、评分、撰写及复核 Feedback；本阶段不调用 reviewer.env'}
    if allow_platform_repair:
        import fbr_followup
        ticket = lib.load_json(task / fbr_followup.JOURNAL, default={})
        if ticket.get('state') == 'needs_platform_repair':
            checkpoint['platform_repair_revision'] = ticket['revision']
    gate = lib.load_json(task/'review/gate.json', default={})
    _, history = _load_attempt_history(task)
    last = history.get('attempts', [])
    checkpoint['repair_errors'] = gate.get('errors', []) + (last[-1].get('errors', []) if last else [])
    checkpoint['next_steps'] = ['当前任务 AI 修复候选并逐句核对过程及产物断言', 'finalize 过闸并主动推送快照']
    lib.save_json(task/'review/task-agent-work.json', checkpoint)
    lib.set_stage(task, 'feedback', 'dry', reason=checkpoint['detail'])
    return {'state': 'needs_task_agent', 'task_id': task.name, 'detail': checkpoint['detail']}


@lib.task_locked
def finalize(task: Path, candidate: dict, verification: dict, *, publish=True,
             allow_platform_repair: bool = False, defer_delivery: bool = False) -> dict:
    """Deterministic validation only: never load credentials or call an external model."""
    if allow_platform_repair:
        lib.assert_platform_prompt_unchanged(task)
    if not isinstance(candidate, dict) or not isinstance(verification, dict):
        raise lib.FbrError("candidate 和 verification 都必须是 JSON 对象")
    _validate_frozen_prompt_inputs(task)
    lib.require_stage(task, 'facts')
    work = lib.load_json(task/'review/task-agent-work.json', default={})
    binding = {'inputs': inputs(task), 'policy_sha': quality.policy_sha(),
               'trace-analysis_sha': file_sha(task/'review/trace-analysis.json')}
    if work.get('binding') != binding or verification.get('binding') != binding:
        raise lib.FbrError('验收期间材料或规则变化，须刷新任务 AI 检查点并重新复核')
    if (verification.get('task_agent') != 'current_task_agent'
            or verification.get('candidate_sha') != lib.content_hash(candidate)):
        raise lib.FbrError('缺少当前任务 AI 与本次候选绑定的复核记录')
    peers = peer_feedback(task)
    attempt_key, attempt_meta = _finalize_attempt_key(
        task, candidate, verification, binding,
        publish=publish, defer_delivery=defer_delivery,
        allow_platform_repair=allow_platform_repair, peers=peers,
    )
    history_path, history = _load_attempt_history(task)
    existing = next(
        (row for row in history["attempts"]
         if isinstance(row, dict)
         and attempt_key in (row.get("attempt_key"), row.get("attempt_sha"))),
        None,
    )
    if existing:
        reused = _reuse_finalize_attempt(
            task, history_path, history, existing, candidate,
            publish=publish, defer_delivery=defer_delivery,
        )
        if reused:
            return reused
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
    errors += draft_errors(task, audit, feedback, peers)
    style_report = fbr_style.write_report(
        task, str(feedback.get("feedback", "")), feedback.get("score"),
        verification.get("language_review"), require_review=True)
    errors += [row["message"] for row in style_report["errors"]]
    errors += quality.report_errors(verification) + quality.claim_report_errors(verification, feedback, task)
    errors = list(dict.fromkeys(errors))
    objects = task/'review/task-agent-history/objects'
    objects.mkdir(parents=True, exist_ok=True)
    candidate_sha = lib.content_hash(candidate)
    verification_sha = lib.content_hash(verification)
    candidate_obj = objects/(candidate_sha + '.json')
    verification_obj = objects/(verification_sha + '.json')
    if not candidate_obj.is_file():
        lib.save_json(candidate_obj, candidate)
    if not verification_obj.is_file():
        lib.save_json(verification_obj, verification)
    attempt = existing
    if attempt is None:
        attempt = {
            'at': lib.now_iso(),
            'attempt_sha': attempt_key,
            'attempt_key': attempt_key,
            'candidate_sha': candidate_sha,
            'verification_sha': verification_sha,
            'binding_sha': lib.content_hash(binding),
            'idempotency_version': FINALIZE_IDEMPOTENCY_VERSION,
            'mode': attempt_meta['mode'],
            'errors': errors,
            'candidate_ref': str(candidate_obj.relative_to(task)),
            'verification_ref': str(verification_obj.relative_to(task)),
            'reuse_count': 0,
        }
        history['attempts'].append(attempt)
    else:
        attempt.update({
            'candidate_ref': str(candidate_obj.relative_to(task)),
            'verification_ref': str(verification_obj.relative_to(task)),
            'errors': errors,
        })
    lib.save_json(history_path, history)
    write_metrics(task, 'feedback_attempt', {
        'result': 'accepted' if not errors else 'repairing',
        'candidate_bytes': len(json.dumps(candidate, ensure_ascii=False).encode('utf-8')),
        'verification_bytes': len(json.dumps(verification, ensure_ascii=False).encode('utf-8')),
        'errors': len(errors), 'access': fbr_evidence.access_summary(task),
        'history_attempts': len(history['attempts']),
    })
    if errors:
        return _remember_finalize_result(
            history_path, history, attempt,
            {'state': 'repairing', 'task_id': task.name, 'errors': errors,
             'detail': '当前任务 AI 按错误继续修复并复验；未采用失败候选'},
        )
    if inputs(task) != binding['inputs'] or quality.policy_sha() != binding['policy_sha']:
        raise lib.FbrError('复核期间证据或规则变化，拒绝采用过期评价')
    # Preserve an accepted version as one content-addressed object, not four copied files.
    previous = {}
    for name in ('evidence/audit.json', 'review/feedback.json', 'review/model-review.json',
                 'review/quality.json', 'review/code-review.json'):
        path = task/name
        if path.is_file():
            previous[name] = lib.load_json(path)
    if previous:
        snapshot_sha = lib.content_hash(previous)
        snapshot_path = objects/(snapshot_sha + '.json')
        if not snapshot_path.is_file():
            lib.save_json(snapshot_path, previous)
        accepted_versions = history.setdefault('accepted_versions', [])
        if not any(row.get('snapshot_sha') == snapshot_sha
                   for row in accepted_versions if isinstance(row, dict)):
            accepted_versions.append({
                'at': lib.now_iso(), 'snapshot_sha': snapshot_sha,
                'snapshot_ref': str(snapshot_path.relative_to(task)),
            })
            lib.save_json(history_path, history)
    trace_hashes = {row['path']: row['sha256']
                    for row in lib.load_json(task/fbr_evidence.CHECK).get('sources', [])}
    source_inputs = {}
    for row in verification['claim_reviews']:
        for ref in row['sources']:
            name = ref['path']
            source_inputs[name] = trace_hashes.get(name) or file_sha(task/name)
    lib.save_json(task/'evidence/audit.json', audit)
    lib.save_json(task/'review/feedback.json', feedback)
    receipt = {**binding, 'workflow': 'task-agent-v1', 'state': 'completed',
               'provider': 'current_task_agent', 'model': 'current_task_agent', 'reasoning_effort': 'session',
               'at': lib.now_iso(), 'source_inputs': source_inputs,
               'trace_check_sha': file_sha(task / fbr_evidence.CHECK),
               'language_policy_version': fbr_style.VERSION,
               'language_policy_sha': fbr_style.policy_sha(),
               'language_review': verification.get('language_review'),
               'style_report_sha': file_sha(task / fbr_style.REPORT_FILE),
               'audit_sha': file_sha(task/'evidence/audit.json'), 'feedback_sha': file_sha(task/'review/feedback.json')}
    lib.save_json(task/'review/quality.json', {**receipt, 'version': quality.VERSION, 'state': 'accepted',
                  'checks': verification['checks'], 'claim_reviews': verification['claim_reviews']})
    lib.save_json(task/'review/model-review.json', receipt)
    (task/'review/probe-requests.json').unlink(missing_ok=True)
    # A successful candidate is immediately gated and persisted to the local workbook.
    import fbr_feedback
    root = lib.find_root(str(task.parents[2]))
    cfg = lib.settings(root)
    rows, _ = fbr_feedback.gate_batch(root, task.parent.name, cfg, task.name)
    row = next(r for r in rows if r['task_id'] == task.name)
    if row['state'] == 'ok':
        if isinstance(feedback.get('code_review'), dict):
            lib.save_json(task/'review/code-review.json', feedback['code_review'])
        import fbr_work
        fbr_work.closeout(root, task.parent.name, task.name)
        if defer_delivery:
            delivery_path = task/'review/delivery-repair.json'
            delivery_history = lib.load_json(delivery_path, default={'attempts': []})
            delivery_history['state'] = 'passed'
            lib.save_json(delivery_path, delivery_history)
            lib.set_stage(task, 'feedback', 'ok', score=feedback.get('score'),
                          feedback_sha=fbr_feedback.sha(str(feedback.get('feedback', ''))))
            return _remember_finalize_result(
                history_path, history, attempt,
                {'task_id': task.name, 'state': 'ok',
                 'detail': '已复核并过闸门；交付表和导出延后统一生成'},
            )
        import fbr_sheet
        fbr_sheet.write_sheet(root, task.parent.name, cfg, task_id=task.name)
        import argparse, threading, fbr_flow
        options = argparse.Namespace(batch=task.parent.name, publish=publish, auto_submit=False, delivery_lock=threading.Lock())
        result = fbr_flow.finish_review(task, root, cfg, options)
        return _remember_finalize_result(history_path, history, attempt, result)
    result = {'state': 'repairing', 'task_id': task.name,
              'errors': row.get('errors', []), 'detail': '继续修复闸门错误'}
    return _remember_finalize_result(
        history_path, history, attempt, result, gate_errors=row.get('errors', []),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description='仅轨迹分析使用 reviewer.env；过程核查及其余验收由当前任务 AI 完成')
    parser.add_argument('--root')
    parser.add_argument('--env-file', default=DEFAULT_ENV)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('probe', 'review', 'prepare', 'finalize', 'lint', 'build'):
        p = subs.add_parser(name)
        p.add_argument('--execute', action='store_true')
        p.add_argument('--json', action='store_true')
        if name != 'probe':
            p.add_argument('--batch', required=True)
            p.add_argument('--task', required=True)
    subs.choices['finalize'].add_argument('--candidate', required=True)
    subs.choices['finalize'].add_argument('--verification', required=True)
    subs.choices['lint'].add_argument('--candidate', required=True)
    subs.choices['lint'].add_argument('--verification')
    subs.choices['build'].add_argument('--decision', required=True)
    subs.choices['finalize'].add_argument('--no-publish', action='store_true')
    subs.choices['finalize'].add_argument('--defer-delivery', action='store_true')
    subs.choices['prepare'].add_argument('--platform-repair', action='store_true')
    subs.choices['review'].add_argument('--platform-repair', action='store_true')
    subs.choices['finalize'].add_argument('--platform-repair', action='store_true')
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        if args.command == 'lint':
            task = lib.task_dirs(root, args.batch, args.task)[0]
            verification = lib.load_json(Path(args.verification)) if args.verification else None
            result = lint_candidate(task, lib.load_json(Path(args.candidate)), verification)
            print(compact_result(task, 'lint', result, full=args.json))
            return 0 if result['state'] == 'ok' else 2
        if args.command == 'build':
            import fbr_feedback
            task = lib.task_dirs(root, args.batch, args.task)[0]
            decision = lib.load_json(Path(args.decision))
            candidate, verification = fbr_feedback.expand_decision(task, decision)
            candidate_path = task/'review/candidate.json'
            verification_path = task/'review/verification.json'
            lib.save_json(candidate_path, candidate)
            lib.save_json(verification_path, verification)
            lib.save_json(
                task/'review/build-report.json',
                verification.get('build_derivations') or {},
            )
            result = lint_candidate(task, candidate, verification)
            result.update(
                candidate=str(candidate_path),
                verification=str(verification_path),
                build_report=str(task/'review/build-report.json'),
            )
            print(compact_result(task, 'build', result, full=args.json))
            return 0 if result['state'] == 'ok' else 2
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
                result = review(task, base, secret, model=model, effort=effort,
                                allow_platform_repair=args.platform_repair)
            elif args.command == 'prepare':
                with lib.file_lock(task/'.operation.lock'):
                    result = prepare_task_agent(task, allow_platform_repair=args.platform_repair)
            else:
                import fbr_followup
                ticket = lib.load_json(task / 'review/task-agent-work.json', default={}) if args.platform_repair else {}
                result = finalize(task, lib.load_json(Path(args.candidate)), lib.load_json(Path(args.verification)),
                                  publish=not args.no_publish,
                                  allow_platform_repair=args.platform_repair,
                                  defer_delivery=args.defer_delivery)
                if result.get('state') == 'ok' and not args.defer_delivery:
                    fbr_followup.mark_ready(task, ticket.get('platform_repair_revision'))
        if args.command == 'probe':
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else f"probe\t{result['state']}\t{result.get('model','')}")
        else:
            task = lib.task_dirs(root, args.batch, args.task)[0]
            print(compact_result(task, args.command, result, full=args.json))
        return 0 if result['state'] != 'repairing' else 2
    except (lib.FbrError, OSError) as exc:
        print('错误：'+str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
