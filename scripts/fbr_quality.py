"""Feedback 的证据契约与语义复核协议；不生成评价、不猜测文本作者。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import fbr_gate as gate
import fbrlib as lib

VERSION = 11
CHECKS = ("result_and_process", "task_specific_voice", "test_relevance", "score_alignment",
          "evidence_fidelity", "scope_discipline", "non_template")
RULE_FILES = ("references/scoring.md", "references/feedback-writing.md", "references/feedback-calibration.md",
              "references/feedback-quality.md", "references/process-attribution-audit.md",
              "scripts/fbr_gate.py", "scripts/fbr_quality.py",
              "scripts/fbr_feedback.py", "scripts/fbr_trace_review.py", "scripts/fbr_sources.py")
TRACE_RULE_FILES = ("scripts/fbr_trace_review.py", "scripts/fbr_sources.py")
LEGACY_TRACE_POLICIES = {
    "a0eeac329d747bc52e6ee804ac30920511498b22344334cdb94a1910e328ca3c",
    "41e8cc299da551d1c8f22773ec0b0307262ddd13f5eb41cb2903b3bcc68d1e72",
}
LEGACY_FEEDBACK_POLICIES = {
    "6a50956ab20e9c7567e9397be7e7be8d3183d84981786b9899809a6c016111d4",
    "e7f4586b18419b0ae217edcfc1e3c6a2f8192810d77b032c11b2e27378882ff2",
}
TEST_CLAUSE = re.compile(r"[^。；！？\n]*(?:测试|用例|\btest\w*|\bpytest\b)[^。；！？\n]*", re.I)
COUNT_CLAIM = re.compile(r"(?:\d+\s*(?:条|个|项)\s*(?:测试|用例)|(?:测试|用例)[^。；]{0,8}\d+|\d+\s*(?:条|个|项)?\s*(?:全[部都]?通过|通过))")
NEGATIVE_TEST = re.compile(r"(?:未|没有|没|未曾|并未)[^。；]{0,24}(?:测试|用例|验证)|(?:测试|用例)[^。；]{0,16}(?:未执行|没跑|未跑|未重跑|缺失)")
PROCESS_AUDIT_CHECKS = ("full_trace", "baseline", "timeline", "source_boundary", "scope")
PROCESS_EXTERNAL_EVIDENCE = re.compile(r"(?:独立\s*)?(?:边界\s*)?probe|验收侧|录制侧", re.I)
PROCESS_PAGE_ACTION = re.compile(r"浏览器操作|页面操作|实际点击|点击[^。；]{0,16}按钮")
PROCESS_NEGATED_PAGE_ACTION = re.compile(
    r"(?:没有|未|没|并不)[^。；]{0,30}(?:浏览器|页面)?[^。；]{0,20}(?:点击|操作)[^。；]{0,20}(?:按钮|页面)"
)
PROCESS_BROWSER_TRACE = re.compile(r"playwright|puppeteer|browser|chromium|page\.click|locator\(", re.I)
PROCESS_TIMELINE = re.compile(r"最初|起初|最初|收尾|最后|随后|后来|最终")
PROCESS_CAUSAL = re.compile(r"让|使|造成|导致|引入|归因")
PROCESS_SCOPE = re.compile(r"只|仅|从未|没有|未实际|唯一")
PROCESS_EVIDENCE_FIELDS = {
    "tool": "tool",
    "target": "target",
    "output": "output_tail",
    "error": "error",
}


def policy_sha() -> str:
    skill = Path(__file__).resolve().parent.parent
    values = {name: hashlib.sha256((skill / name).read_bytes()).hexdigest() for name in RULE_FILES}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def trace_policy_sha() -> str:
    """轨迹分析规则与 Feedback 写作规则分开绑定，改文字规范不重跑轨迹模型。"""
    skill = Path(__file__).resolve().parent.parent
    values = {name: hashlib.sha256((skill / name).read_bytes()).hexdigest() for name in TRACE_RULE_FILES}
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


def trace_has_browser_action(facts: dict) -> bool:
    for step in facts.get("trace", {}).get("steps", []):
        haystack = f"{step.get('tool', '')} {step.get('target', '')}"
        if PROCESS_BROWSER_TRACE.search(haystack):
            return True
    return False


def process_audit_errors(doc: dict, facts: dict) -> list[str]:
    """Ensure process prose describes verified model behaviour, not evaluator evidence."""
    process = doc.get("process")
    audit = doc.get("process_audit")
    if not isinstance(process, list) or not process:
        return []
    if not isinstance(audit, list) or len(audit) != len(process):
        return ["process_audit 必须按顺序覆盖全部过程句"]
    browser_seen = trace_has_browser_action(facts)
    steps = {step.get("step"): step for step in facts.get("trace", {}).get("steps", [])
             if isinstance(step, dict) and type(step.get("step")) is int}
    answers = facts.get("trace", {}).get("assistant_answers") or []
    errors = []
    for index, (item, checked) in enumerate(zip(process, audit), 1):
        label = f"第 {index} 个过程句归因"
        raw_sentence = str(item.get("sentence") or "") if isinstance(item, dict) else ""
        sentence = gate.squash(raw_sentence)
        evidence = item.get("evidence") if isinstance(item, dict) else None
        declared_steps = item.get("steps") if isinstance(item, dict) else None
        evidenced_steps = set()
        evidence_quotes = []
        if not isinstance(evidence, list) or not evidence:
            errors.append(label + "缺少逐句轨迹证据 evidence")
        else:
            for evidence_index, source in enumerate(evidence, 1):
                source_label = f"{label}第 {evidence_index} 条证据"
                if not isinstance(source, dict):
                    errors.append(source_label + "格式不对")
                    continue
                quote = str(source.get("excerpt") or "").strip()
                if len(quote) < 4:
                    errors.append(source_label + "摘录太短")
                    continue
                kind = source.get("kind")
                if kind == "assistant":
                    answer_index = source.get("index")
                    if type(answer_index) is not int or not 1 <= answer_index <= len(answers):
                        errors.append(source_label + "没有对应的助手文本")
                    elif gate.squash(quote) not in gate.squash(str(answers[answer_index - 1])):
                        errors.append(source_label + "摘录不在对应助手文本中")
                    else:
                        evidence_quotes.append(gate.squash(quote))
                    continue
                step_no, field = source.get("step"), source.get("field")
                if kind != "tool" or type(step_no) is not int or field not in PROCESS_EVIDENCE_FIELDS:
                    errors.append(source_label + "必须使用 kind=tool 并给出 step 与合法 field")
                    continue
                row = steps.get(step_no)
                if not row:
                    errors.append(source_label + f"指向不存在的第 {step_no} 步")
                    continue
                actual = str(row.get(PROCESS_EVIDENCE_FIELDS[field]) or "")
                if gate.squash(quote) not in gate.squash(actual):
                    errors.append(source_label + f"摘录不在第 {step_no} 步的 {field} 中")
                    continue
                evidenced_steps.add(step_no)
                evidence_quotes.append(gate.squash(quote))
        if isinstance(declared_steps, list) and declared_steps:
            missing = {s for s in declared_steps if type(s) is int and s in steps} - evidenced_steps
            if missing:
                errors.append(label + "列出的轨迹步骤缺少对应 evidence：" + "、".join(map(str, sorted(missing))))
            extra = evidenced_steps - {s for s in declared_steps if type(s) is int}
            if extra:
                errors.append(label + "evidence 引用了未列入 steps 的步骤：" + "、".join(map(str, sorted(extra))))
        else:
            if evidenced_steps:
                errors.append(label + "有工具证据但没有登记 steps")
            elif not evidence_quotes:
                errors.append(label + "没有可核对的过程证据")
        if not isinstance(checked, dict):
            errors.append(label + "格式不对")
            continue
        if checked.get("sentence") != raw_sentence:
            errors.append(label + "必须对应同一条过程句原文")
        if checked.get("subject") != "model":
            errors.append(label + "的 subject 必须是 model；验收 probe、演示和页面验收结果不能写成模型动作")
        if checked.get("verdict") != "supported":
            errors.append(label + "必须已完成原文、基线、时间线和后续状态核对")
        checks = checked.get("checks")
        if not isinstance(checks, dict) or any(checks.get(name) is not True for name in PROCESS_AUDIT_CHECKS):
            errors.append(label + "必须完成 full_trace、baseline、timeline、source_boundary、scope 五项检查")
        if len(str(checked.get("note") or "").strip()) < 8:
            errors.append(label + "缺少具体归因核对说明")
        if PROCESS_EXTERNAL_EVIDENCE.search(sentence):
            errors.append(label + "混入了独立 probe 或验收侧证据；模型过程只能写模型轨迹中的行为")
        if PROCESS_PAGE_ACTION.search(sentence) and not browser_seen and not PROCESS_NEGATED_PAGE_ACTION.search(sentence):
            errors.append(label + "写了页面或浏览器操作，但轨迹中没有对应浏览器动作；应改写为模型实现或最终产物现象")
        if PROCESS_TIMELINE.search(sentence) and isinstance(checks, dict) and checks.get("timeline") is not True:
            errors.append(label + "含时间判断，必须核对完整前置和后续步骤")
        if PROCESS_CAUSAL.search(sentence) and isinstance(checks, dict) and checks.get("baseline") is not True:
            errors.append(label + "含因果或归属判断，必须对照初始提交，区分基线已有行为与模型引入")
        if PROCESS_SCOPE.search(sentence) and isinstance(checks, dict) and checks.get("scope") is not True:
            errors.append(label + "含只、仅、从未、没有等范围判断，必须扫描完整轨迹后再写")
    return errors


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
    if not isinstance(basis, dict) or basis.get("category") not in ("delivery_issue", "verification_gap", "process_issue", "complete"):
        errors.append("score_basis.category 必须是 delivery_issue、verification_gap、process_issue 或 complete")
    elif (doc.get("score") == 5) != (basis["category"] == "complete"):
        errors.append("complete 仅对应 5 分，其余档必须说明交付问题、验证边界或过程问题")
    elif basis["category"] in ("verification_gap", "process_issue") and doc.get("score") != 4:
        errors.append("verification_gap 和 process_issue 只用于没有已证实交付缺陷的 4 分")
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

    errors += process_audit_errors(doc, facts)

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
    process_sentences = {
        gate.squash(str(item.get('sentence') or '')): {
            gate.squash(str(source.get('excerpt') or ''))
            for source in item.get('evidence', [])
            if isinstance(source, dict) and str(source.get('excerpt') or '').strip()
        }
        for item in doc.get('process', []) if isinstance(item, dict)
    }
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
        if gate.squash(unit) in process_sentences and not any(
                isinstance(source, dict)
                and str(source.get('path', '')).startswith('trace/projects/')
                and str(source.get('path', '')).endswith('.jsonl')
                for source in sources):
            errors.append(label + '是模型过程句，来源中必须包含原始轨迹 JSONL，不能只靠 probe、录屏或最终源码')
        if gate.squash(unit) in process_sentences:
            required_quotes = process_sentences[gate.squash(unit)]
            trace_quotes = {
                gate.squash(str(source.get('excerpt') or ''))
                for source in sources
                if isinstance(source, dict)
                and str(source.get('path', '')).startswith('trace/projects/')
                and str(source.get('path', '')).endswith('.jsonl')
            }
            missing_quotes = required_quotes - trace_quotes
            if missing_quotes:
                errors.append(label + '没有逐条引用过程句 evidence 对应的原始轨迹摘录')
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
    allowed = {policy_sha()} | LEGACY_FEEDBACK_POLICIES
    if receipt.get("policy_sha") not in allowed or cert.get("policy_sha") not in allowed:
        return ["Feedback 规则已更新，须按当前规则重新生成并复核"]
    for key in ("workflow", "trace-analysis_sha", "source_inputs", "model", "reasoning_effort", "provider", "inputs", "audit_sha", "feedback_sha"):
        if cert.get(key) != receipt.get(key):
            return ["Feedback 语义复核与当前模型回执、证据或正文不一致"]
    return report_errors(cert) + claim_report_errors(cert, lib.load_json(task / 'review/feedback.json'), task)
