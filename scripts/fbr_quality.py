"""Feedback 的证据契约与语义复核协议；不生成评价、不猜测文本作者。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import fbr_gate as gate
import fbrlib as lib

VERSION = 4
CHECKS = ("result_and_process", "task_specific_voice", "test_relevance", "score_alignment",
          "evidence_fidelity", "scope_discipline", "non_template")
RULE_FILES = ("references/scoring.md", "references/feedback-writing.md", "references/feedback-calibration.md",
              "references/feedback-quality.md", "scripts/fbr_gate.py", "scripts/fbr_quality.py",
              "scripts/fbr_reviewer.py", "scripts/fbr_feedback.py", "scripts/fbr_trace_review.py", "scripts/fbr_sources.py")
TEST_CLAUSE = re.compile(r"[^。；！？\n]*(?:测试|用例|\btest\w*|\bpytest\b)[^。；！？\n]*", re.I)
COUNT_CLAIM = re.compile(r"(?:\d+\s*(?:条|个|项)\s*(?:测试|用例)|(?:测试|用例)[^。；]{0,8}\d+|\d+\s*(?:条|个|项)?\s*(?:全[部都]?通过|通过))")
NEGATIVE_TEST = re.compile(r"(?:未|没有|没|未曾|并未)[^。；]{0,24}(?:测试|用例|验证)|(?:测试|用例)[^。；]{0,16}(?:未执行|没跑|未跑|未重跑|缺失)")


def policy_sha() -> str:
    skill = Path(__file__).resolve().parent.parent
    values = {name: hashlib.sha256((skill / name).read_bytes()).hexdigest() for name in RULE_FILES}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def enrich_test_logs(task: Path, facts: dict) -> dict:
    """仅在内存加载完整原始日志；路径和摘要必须与事实卡绑定。"""
    for side in ('before', 'after'):
        result = facts['tests'][side]
        for output in result.get('outputs', []):
            if not output.get('output_file'):
                continue
            path = task / output['output_file']
            if path.is_symlink() or not path.resolve().is_relative_to((task / 'evidence/command-logs').resolve()):
                raise lib.FbrError('完整命令日志路径不属于本题证据目录')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != output.get('output_sha256'):
                raise lib.FbrError('完整命令日志缺失或摘要不符，先重新取证')
            name = output.get('name')
            if name in ('setup', 'test'):
                result['full_' + name + '_output'] = path.read_text(errors='replace')
    return facts


def source_text(claim: dict, facts: dict, probes: list[dict]) -> str:
    source = claim.get("source", {})
    if not isinstance(source, dict):
        return ""
    kind = source.get("kind")
    if kind == "trace":
        row = next((s for s in facts["trace"]["steps"] if s["step"] == source.get("step")), {})
        return row.get("output_tail", "") + "\n" + row.get("error", "")
    if kind == "probe":
        row = next((p for p in probes if p["id"] == source.get("id")), {})
        return row.get("tail", "")
    if kind == "tests_after":
        return facts["tests"]["after"].get("full_test_output", facts["tests"]["after"].get("test_tail", ""))
    if kind == "verification":
        return json.dumps(facts["verification"], ensure_ascii=False)
    if kind == "demo":
        index = source.get("index")
        rows = facts["demo"]["steps"]
        if type(index) is int and 1 <= index <= len(rows):
            return rows[index - 1].get("tail", "") or ""
    return ""


def check_contract(doc: dict, task: dict, facts: dict, audit: dict, probes: list[dict]) -> list[str]:
    """摘要真实存在不等于语义真实；此处检查可确定的出处，含义交给另一次指定模型复核。"""
    errors = []
    text = gate.squash(str(doc.get("feedback", "")))
    ev = gate.build_evidence(task, facts, audit, probes)
    results = doc.get("results")
    if not isinstance(results, list) or not results:
        errors.append("结果反馈缺失：results 至少登记一处正文中的最终行为及证据")
    else:
        for i, item in enumerate(results, 1):
            label = f"第 {i} 个结果句"
            if not isinstance(item, dict):
                errors.append(label + "格式不对")
                continue
            sentence = gate.squash(str(item.get("sentence", "")))
            if len(sentence) < 6 or sentence not in text:
                errors.append(label + "必须是正文原句")
            ids = item.get("requirement_ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(r, str) or r not in ev["req_ids"] for r in ids):
                errors.append(label + "必须对应真实需求点（requirement_ids）")
            anchors = item.get("anchors", [])
            if not isinstance(anchors, list) or not any(isinstance(a, dict) and a.get("type") in
                    ("file", "tree", "missing", "log", "test", "probe", "demo") for a in anchors):
                errors.append(label + "缺少产物或运行证据，不能只引用需求点或过程步骤")
            else:
                errors += gate._anchor_problems(anchors, ev, label)

    basis = doc.get("score_basis")
    if not isinstance(basis, dict) or any(not isinstance(basis.get(k), str) or len(basis[k].strip()) < 6
            for k in ("delivered", "remaining", "why_this_score")):
        errors.append("score_basis 必须说明已交付、剩余缺口及本档理由；不进入正文")
    if not isinstance(basis, dict) or basis.get("category") not in ("delivery_issue", "verification_gap", "complete"):
        errors.append("score_basis.category 必须是 delivery_issue、verification_gap 或 complete")
    elif (doc.get("score") == 5) != (basis["category"] == "complete"):
        errors.append("complete 仅对应 5 分，其余档必须说明交付问题或验证缺口")
    elif basis["category"] == "verification_gap" and doc.get("score") != 4:
        errors.append("verification_gap 只用于没有已证实交付缺陷但验证不足的 4 分")
    if isinstance(basis, dict):
        if type(basis.get('lowest_supported_score')) is not int or basis.get('lowest_supported_score') != doc.get('score'):
            errors.append('score_basis.lowest_supported_score 必须等于确凿证据支持的最低合理档位及本次分数')
        if len(str(basis.get('lower_score_rejected', '')).strip()) < 8:
            errors.append('必须说明为何再低一档缺少确凿证据，不得为压档编造缺陷')
        anchors = basis.get('anchors', [])
        if not isinstance(anchors, list) or not anchors:
            errors.append('评分依据必须绑定最终交付证据 anchors')
        else:
            errors += gate._anchor_problems(anchors, ev, '评分依据')

    claims = doc.get("test_claims")
    if not isinstance(claims, list):
        errors.append("test_claims 必须为列表，没有测试描述时为空")
        claims = []
    valid_sentences = []
    for i, item in enumerate(claims, 1):
        label = f"第 {i} 条测试描述"
        if not isinstance(item, dict):
            errors.append(label + "格式不对")
            continue
        sentence = gate.squash(str(item.get("sentence", "")))
        if len(sentence) < 6 or sentence not in text:
            errors.append(label + "必须是正文中的连续原文")
        else:
            valid_sentences.append(sentence)
        ids = item.get("requirement_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(r, str) or r not in ev["req_ids"] for r in ids):
            errors.append(label + "没有关联本题真实需求点")
        if len(str(item.get("behavior", "")).strip()) < 6:
            errors.append(label + "须说明测试验证的具体行为，不能只报总数或通过状态")
        output = source_text(item, facts, probes)
        source = item.get("source")
        if isinstance(source, dict) and source.get("kind") == "verification" and (
                not NEGATIVE_TEST.search(sentence) or COUNT_CLAIM.search(sentence)):
            errors.append(label + "的 verification 来源仅用于未执行或未重测的事实，不能证明通过状态或数量")
        excerpt = str(item.get("output_excerpt", "")).strip()
        if len(excerpt) < 4 or gate.squash(excerpt) not in gate.squash(output):
            errors.append(label + "的输出摘录不在指定运行日志中，必要时补充定向 probe")
        count = item.get("count")
        if COUNT_CLAIM.search(sentence):
            quote = str(item.get("count_quote", "")).strip()
            if type(count) is not int or count < 0 or len(quote) < 4 or gate.squash(quote) not in gate.squash(output) \
                    or not re.search(rf"(?<!\d){count}(?!\d)", quote):
                errors.append(label + "提到通过数量，必须给出同一日志中的 count 与 count_quote；不能推算或挪用全仓总数")
    # 覆盖整段相关分句，防止只登记“make test”几个字符却遗漏后面的夸大结论。
    for raw in re.split(r"[。；！？\n]", str(doc.get("feedback", ""))):
        if not TEST_CLAUSE.search(raw) and not COUNT_CLAIM.search(raw):
            continue
        clause = gate.squash(raw)
        if not any(clause in s for s in valid_sentences):
            errors.append("测试描述未登记完整分句及来源：" + clause[:60])
    return errors


def report_errors(report: object) -> list[str]:
    if not isinstance(report, dict) or not isinstance(report.get("checks"), list):
        return ["语义复核未返回 checks 列表"]
    checks = report["checks"]
    ids = [c.get("id") for c in checks if isinstance(c, dict)]
    if len(checks) != len(CHECKS) or any(not isinstance(i, str) for i in ids) or set(ids) != set(CHECKS):
        return ["语义复核项目缺失、重复或未知"]
    errors = []
    for c in checks:
        if type(c.get("passed")) is not bool or not isinstance(c.get("reason"), str) or len(c["reason"].strip()) < 6:
            errors.append("语义复核项目缺少明确判断及具体依据：" + str(c.get("id")))
        elif not c["passed"]:
            errors.append(str(c["id"]) + "：" + c["reason"])
    return errors


def public_units(text: str) -> list[str]:
    """按句末及分号分段；不按小数点、文件扩展名或报错中的英文标点切分。"""
    return [s.strip() for s in re.split(r'[。；！？\n]+', text) if s.strip()]


def claim_report_errors(report: dict, doc: dict, task: Path) -> list[str]:
    """强制逐句复查及原文摘录；摘录能否支持含义仍由当前任务 AI 逐句复核。"""
    rows = report.get('claim_reviews')
    units = public_units(doc.get('feedback', ''))
    if not isinstance(rows, list) or len(rows) != len(units):
        return ['逐句复查缺失：claim_reviews 必须按顺序覆盖全部正文句段']
    errors = []
    for index, (unit, row) in enumerate(zip(units, rows), 1):
        label = f'第 {index} 句复查'
        if not isinstance(row, dict) or row.get('sentence') != unit:
            errors.append(label + '未对应完整正文原句')
            continue
        if row.get('verdict') != 'supported':
            errors.append(label + '存在矛盾、证据不足或未复核的断言')
        for key in ('reason', 'later_check'):
            if not isinstance(row.get(key), str) or len(row[key].strip()) < 6:
                errors.append(label + '缺少具体依据或后续修正/最终状态检查')
        assertions = row.get('assertions')
        if not isinstance(assertions, list) or not assertions or any(
                not isinstance(a, str) or len(a.strip()) < 2 for a in assertions):
            errors.append(label + '未拆出需要核对的事实断言')
        sources = row.get('sources')
        if not isinstance(sources, list) or not sources:
            errors.append(label + '缺少回查原文')
            continue
        for source in sources:
            if not isinstance(source, dict):
                errors.append(label + '原文来源格式错误')
                continue
            name, quote = source.get('path'), source.get('excerpt')
            if not isinstance(name, str) or not isinstance(quote, str) or len(quote.strip()) < 4:
                errors.append(label + '缺少原始文件及连续摘录')
                continue
            rel = Path(name)
            allowed = (name in ('task.json', 'review/diff.patch', 'evidence/probes.jsonl', 'record/recording.json')
                       or (name.startswith('trace/projects/') and name.endswith('.jsonl'))
                       or (name.startswith('evidence/command-logs/') and name.endswith('.log'))
                       or name.startswith('workspace/'))
            path = task / rel
            if (not allowed or rel.is_absolute() or '..' in rel.parts
                    or not path.resolve().is_relative_to(task.resolve())
                    or any((task / Path(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts) + 1))
                    or not path.is_file()):
                errors.append(label + '来源不是本题可核对的原始材料')
                continue
            if quote not in path.read_text(errors='replace'):
                errors.append(label + '摘录不在原始文件中')
    return errors


def certificate_errors(task: Path, receipt: dict) -> list[str]:
    path = task / "review/quality.json"
    if path.is_symlink() or not path.is_file():
        return ["缺少当前任务 AI 的 Feedback 语义复核回执"]
    cert = lib.load_json(path, default={})
    if not isinstance(cert, dict) or cert.get("version") != VERSION or cert.get("state") != "accepted":
        return ["Feedback 语义复核尚未通过"]
    if receipt.get("policy_sha") != policy_sha() or cert.get("policy_sha") != policy_sha():
        return ["Feedback 规则已更新，须按当前规则重新生成并复核"]
    for key in ("workflow", "trace-analysis_sha", "source_inputs", "model", "reasoning_effort", "provider", "inputs", "audit_sha", "feedback_sha"):
        if cert.get(key) != receipt.get(key):
            return ["Feedback 语义复核与当前模型回执、证据或正文不一致"]
    return report_errors(cert) + claim_report_errors(cert, lib.load_json(task / 'review/feedback.json'), task)
