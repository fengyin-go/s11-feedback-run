#!/usr/bin/env python3
"""打分与 Feedback 的闸门：纯检查函数，不读写任务目录，不调外部命令。

检查对象是 agent 写的两份文件：
- evidence/audit.json   逐条需求的结论（做到 / 有问题 / 看不出来）+ 锚点、实跑过的反例、虚假成功判断
- review/feedback.json  分数、Feedback 正文、过程句与问题句各自的锚点
- 4 分还必须在最终交付上写出验证缺口或轻微交付问题，纯过程返工不能提交平台

每个检查返回 (errors, warnings)。errors 不清零就不进结果表。
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import fbrlib as lib
import fbr_style

VERDICTS = ("做到", "有问题", "看不出来")
LEVELS = ("未实现", "明显", "细节")
FALSE_SUCCESS = {"无": 5, "轻微": 3, "较大": 2, "严重": 1}
ANCHOR_TYPES = ("file", "tree", "missing", "step", "log", "value", "req", "test", "probe", "demo")
CODE_REVIEW_POLICY_VERSION = 2
CURRENT_FEEDBACK_POLICY = 3
CODE_REVIEW_STATUSES = {"pass", "defect", "blocked"}
CODE_REVIEW_FILE_RESULTS = {"reviewed", "not_applicable"}
CODE_REVIEW_CHECK_STATUSES = {"pass", "violation", "not_applicable"}
CODE_REVIEW_EVIDENCE_LEVELS = {"reproduced", "code_reachable"}
CODE_REVIEW_KINDS = {
    "logic_error", "integration_gap", "contract_mismatch", "data_integrity",
    "transaction", "concurrency_race", "idempotency", "recovery",
    "compatibility_regression", "validation_gap", "false_success", "minor_format",
}
CODE_REVIEW_BLOCKED_REASONS = {
    "nondeterministic_unobservable",
    "required_external_service_unavailable",
    "artifact_cannot_expose_result",
    "execution_environment_unsupported",
}
CODE_REVIEW_SOURCE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".vue", ".svelte", ".sql",
    ".json", ".yml", ".yaml", ".toml", ".xml", ".sh", ".html", ".css",
}

RUBRIC = [
    "一次性完美跑通。不仅实现了所有明示需求，还主动补全了隐性需求（如边界处理），零虚假成功",
    "基本完美。达成所有主要需求，代码可运行，但有极少量的细节遗漏，无虚假成功",
    "勉强达标。核心功能可跑，但有明显 Bug 需人工微调；或存在轻微的虚假成功（如某非核心文件未保存）",
    "未达成主要需求。代码因逻辑或依赖问题无法运行；或存在较大比例的虚假成功（如宣称改了 5 个文件，实际只改了 2 个）",
    "完全失败。代码严重编译报错完全不可运行；答非所问；或出现极其恶劣的虚假成功（满口答应但一行代码没写）",
]
AI_WORDS = ["综上所述", "综上", "总的来说", "总体而言", "整体来看", "可以看出",
            "值得一提", "值得注意的是", "需要指出的是", "极大地", "显著提升", "充分体现"]
VAGUE_WORDS = ["完美完成", "效果很好", "基本可用", "表现出色", "整体不错", "质量较高", "完成度高"]
EMPTY_COMPLETENESS_RE = re.compile(
    r"(?:未发现|没有发现|没有|无)(?:其他)?(?:明确)?(?:未完成功能|未完成项|功能缺口|未完成要求)"
)
INTERNAL_WORDS = ["触发点在", "实际行为是", "后果是", "业务影响是", "反例", "逐条核对", "核对依据", "本侧", "该侧", "锚点",
                  "题面", "素材卡", "facts.json", "audit.json"]
TOPIC_WORDS = ["这道题", "该题", "本题", "此题"]
HARNESS_WORDS = ["容器", "镜像", "挂载", "docker", "harness", "录屏", "录制", "视频", "截图"]
SCOPE_WORDS = ["网关", "限流", "额度", "余额", "api error", "网络错误", "网络故障", "网络超时", "断网", "推理时长",
               "思考时间", "响应慢", "速度慢", "耗时过长", "戛然而止"]
OTHER_MACHINE_WORDS = ["其他机器", "别的机器", "换一台机器", "另一台机器", "其他环境下", "别人的电脑"]
EVIDENCE_METHOD_WORDS = ["轨迹", "素材卡", "事实卡", "原始会话", "工具返回",
                         "证据显示", "证据表明", "证据中", "证据里"]
BANNED_MARKS = ["“", "”", "‘", "’", "《", "》", "「", "」", "『", "』", "——", "—", "……", "…", "**", "`"]
SUBJECT_RE = re.compile(r"(?:^|[。；！？\n])\s*(这条|这一版|这份交付|本次交付|该交付|这次交付)")
SCORE_TALK_RE = re.compile(r"[给打评得判][了为到出]?\s*[1-5一二三四五]\s*分|扣分|满分|得分|评分|分数|档位")
SELF_RE = re.compile(r"我们?|本人|笔者")
MODEL_PROCESS_RE = re.compile(
    r"(?:^|[。；！？\n])[^。；！？\n]{0,24}模型[^。；！？\n]{0,160}"
    r"(?:先|最初|随后|最后|尝试|实现|修改|重写|验证|修正|重跑|完成|遗漏|没有|未|误判|判断)"
)
PROCESS_GAP_ONLY_RE = re.compile(r"未覆盖|没覆盖|没有覆盖|仍未验证|还没有验证|没有验证|未验证")
PROCESS_ONLY_FINAL_GAP_RE = re.compile(
    r"(?:模型|它).{0,30}(?:没有|未|没).{0,20}(?:验证|核对|验收|检查|测试)"
    r"|(?:没有|未|没)(?:有)?(?:做|进行|完成|执行)?(?:验证|核对|验收|检查|测试)"
)
PROCESS_PROBLEM_RE = re.compile(
    r"失败|报错|误判|错判|判断错|错误|异常|中断|不通过|没过|冲突|超时|无法|返工|JSONDecodeError|Error|Traceback|TS\d+",
    re.I,
)
PROCESS_REPAIR_RE = re.compile(r"修正|修复|改为|改成|补上|补齐|重跑|重试|重新验证|换用|回查")
PROCESS_CONSEQUENCE_RE = re.compile(r"导致|因此|结果|最终|最后|仍|仍然|只|没有|无法|不能|保留|通过|成功")
FAIL_WORDS_RE = re.compile(r"不通过|没通过|未通过|没过|失败|报错|跑不起来|起不来|编译不过|无法运行|无法启动|无法编译")
# 「没有报错」「不会失败」是在说没出问题，不能当成写了失败
NEGATED_FAIL_RE = re.compile(r"(?:没有|不再|不会|没|未|无|不)(?:报错|失败)")
PLATFORM_REMAINING_GAP_RE = re.compile(
    r"(?:最终|当前|目前)[^。；\n]{0,60}(?:仍|尚未|没有|未|无法|缺少|不能)|"
    r"(?:最终交付|交付结果|当前产物|页面|接口|入口|导出|恢复|记录|文件|任务)"
    r"[^。；\n]{0,50}(?:仍|尚未|没有|未|无法|缺少|不可用|不能)|"
    r"(?:用户|使用的人|操作者)[^。；\n]{0,60}(?:看不到|无法|不能|没有)|"
    r"仍缺少|仍无法|仍不能|仍未|仍返回|仍显示|仍存在|"
    r"尚未覆盖|没有覆盖|无法确认|尚未验证|仍未验证|"
    r"有一处没处理|没处理|未处理|没有给出|未给出|没有保存|未保存|没有实现|未实现"
    r"|走不通|求和会失败|打不开|不可用|无法使用|无法完成|"
    r"(?:报|返回)\s*(?:Invalid requirement|[45]\d\d)"
)
STEP_REF_RE = re.compile(r"第\s*(\d+)\s*步")
REQ_REF_RE = re.compile(r"(?<![A-Za-z0-9_])R\d+(?![A-Za-z0-9_])")
# re.A：\w 只认 ASCII，文件名紧挨着中文也能切出来（和 fbr_facts.PATH_RE 一致）
PATH_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.([A-Za-z]{1,5}))(?![\w/-])", re.A)
PATH_EXT = {"py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "c", "cc", "cpp", "h", "hpp", "md", "json", "yml",
            "yaml", "toml", "xml", "sh", "sql", "txt", "cfg", "ini", "html", "css", "csv", "lock", "mk", "glsl"}


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。！？；\n]+", text or "") if s.strip()]


def remaining_gap_sentences(text: str) -> list[str]:
    """Return public clauses that state a final delivery or verification gap."""
    return [unit for unit in sentences(text) if PLATFORM_REMAINING_GAP_RE.search(unit)]


def public_score_basis(doc: dict) -> dict | None:
    """Resolve an archived paraphrase to its evidenced public sentence without changing the stored review."""
    basis = doc.get('score_basis')
    if not isinstance(basis, dict):
        return None
    if doc.get('feedback_policy_version') or doc.get('score') != 4 or not basis.get('anchors'):
        return basis
    remaining = squash(str(basis.get('remaining') or '')).rstrip('。')
    text = str(doc.get('feedback') or '')
    if not remaining or remaining in squash(text):
        return basis
    if basis.get('category') == 'delivery_issue':
        candidates = [str(issue.get('sentence') or '').strip().rstrip('。')
                      for issue in doc.get('issues') or [] if isinstance(issue, dict)
                      and issue.get('anchors') and issue.get('trigger') and issue.get('impact')]
    elif basis.get('category') == 'verification_gap':
        candidates = [unit for unit in sentences(text) if '无法确认' in unit or '尚未验证' in unit]
    else:
        return basis
    matches = set()
    for unit in candidates:
        if not unit or unit not in text:
            continue
        matcher = SequenceMatcher(None, remaining, unit, autojunk=False)
        shared = [block.size for block in matcher.get_matching_blocks() if block.size >= 3]
        if max(shared, default=0) >= 7 or (len(shared) >= 2 and sum(shared) >= 8):
            matches.add(unit)
    if len(matches) == 1:
        return {**basis, 'remaining': matches.pop()}
    return basis


def platform_consistency_errors(score, text: str, basis: dict | None = None) -> list[str]:
    """Reject process-only 4-point feedback before platform consistency review."""
    if score != 4:
        return []
    errors = []
    gaps = remaining_gap_sentences(str(text or ""))
    # A platform payload contains no findings. Missing a regex match is weak evidence;
    # an explicit process-only or fully-complete conclusion is a real contradiction.
    process_only = (str(text).strip().startswith('模型') and len(sentences(text)) == 1
                    and bool(PROCESS_REPAIR_RE.search(text)))
    explicit_complete = bool(re.search(r'最终(?:产物|交付)(?:无问题|没有问题|全部完成|没有缺口)', text))
    if not isinstance(basis, dict):
        if not gaps and (process_only or explicit_complete):
            return ["4 分必须写出最终交付扣分点，已修正的过程返工没有说明残余影响"]
        return []
    category = str(basis.get("category") or "")
    if category == "process_issue":
        errors.append(
            "4 分的 process_issue 不能作为平台交付扣分点："
            "须重新确认最终交付是否完整；若仍有缺口，改为 verification_gap 或 delivery_issue 并在正文明确说明"
        )
    elif category and category not in ("verification_gap", "delivery_issue"):
        errors.append("4 分的 score_basis.category 必须是 verification_gap 或 delivery_issue")
    remaining = squash(str(basis.get("remaining") or ""))
    stated_delivery = (category in ('delivery_issue', 'verification_gap') and bool(remaining)
                       and remaining in squash(text) and len(remaining) >= 6)
    if not gaps and not stated_delivery:
        errors.append(
            "4 分必须写出最终交付扣分点：没有最终缺口、验证边界或用户可见残留后果时，"
            "不能只写过程返工；请重新核实交付并考虑 5 分或补出真实 verification_gap/delivery_issue"
        )
    if not remaining_gap_sentences(remaining) and not stated_delivery:
        errors.append(
            "score_basis.remaining 必须写明同一最终交付缺口，不能只写过程失败、"
            "没有已证实功能缺口或全部通过"
        )
    elif not stated_delivery and gaps and not any(squash(gap) in remaining or remaining in squash(gap) for gap in gaps):
        errors.append("score_basis.remaining 必须对应正文中的同一最终缺口句")
    if category == "verification_gap" and PROCESS_ONLY_FINAL_GAP_RE.search(remaining):
        errors.append(
            "verification_gap 不能只写模型没有验证或没有核对；"
            "必须写最终产物中仍无法观察的用户结果，并绑定已执行 probe"
        )
    return list(dict.fromkeys(errors))


def longest_common(a: str, b: str) -> str:
    """两段文本（已归一化）的最长公共子串。文本都很短，直接动态规划。"""
    best, end = 0, 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best, end = current[j], i
        previous = current
    return a[end - best:end]


# ---------- 证据索引 ----------

def build_evidence(task: dict, facts: dict, audit: dict | None, probes: list[dict]) -> dict:
    tests, logs = facts["tests"], []
    for side in ("before", "after"):
        logs += [tests[side].get("test_tail", ""), tests[side].get("setup_tail", ""),
                 tests[side].get("full_test_output", ""), tests[side].get("full_setup_output", "")]
    logs.append(facts["demo"].get("setup_tail", ""))
    logs += [step.get("tail") or "" for step in facts["demo"]["steps"]]
    for step in facts["trace"]["steps"]:
        logs += [step.get("error", ""), step.get("output_tail", ""), step.get("target", "")]
    logs += [(row.get("tail") or "") + "\n" + (row.get("cmd") or "") for row in probes]
    logs.append(facts["claims"].get("final_answer", ""))
    blob = json.dumps([facts, audit or {}, probes, task.get("prompt", ""), task.get("requirements", [])],
                      ensure_ascii=False)
    return {
        "diff_files": {f["path"] for f in facts["changes"]["files"]},
        "review_files": sorted(
            f["path"] for f in facts["changes"]["files"]
            if Path(str(f.get("path") or "")).suffix.lower() in CODE_REVIEW_SOURCE_SUFFIXES
        ),
        "tree": set(facts.get("tree_files", [])),
        "steps": {s["step"] for s in facts["trace"]["steps"]},
        "req_ids": {str(r["id"]) for r in task["requirements"]},
        "failed_tests": set(tests["after"].get("failed_names", [])) | set(tests.get("new_failures", [])),
        "tests_regression": bool(tests.get("regression")),
        "probes": {row.get("id"): row for row in probes},
        "demo_steps": len(facts["demo"]["steps"]),
        "logs": squash("\n".join(logs)),
        "blob": squash(blob.replace("\\n", " ").replace('\\"', '"')),
        "claims_missing": set(facts["claims"].get("mentioned_missing", [])),
        "task_text": squash(task.get("prompt", "") + " " + json.dumps(task.get("requirements", []), ensure_ascii=False)),
    }


def verify_anchor(anchor, ev: dict) -> str | None:
    """锚点必须真实存在。返回问题描述，没问题返回 None。"""
    if not isinstance(anchor, dict) or anchor.get("type") not in ANCHOR_TYPES:
        return f"锚点格式不对：{json.dumps(anchor, ensure_ascii=False)[:80]}"
    kind = anchor["type"]
    if kind in ("file", "tree", "missing"):
        path = str(anchor.get("path", "")).removeprefix("./")
        if not path:
            return "文件锚点没写 path"
        if kind == "file" and path not in ev["diff_files"]:
            return f"{path} 不在改动清单里"
        if kind == "tree" and path not in ev["tree"]:
            return f"{path} 不在产物里"
        if kind == "missing" and path in ev["tree"]:
            return f"{path} 在产物里是存在的"
    elif kind == "step":
        if anchor.get("step") not in ev["steps"]:
            return f"轨迹里没有第 {anchor.get('step')} 步"
    elif kind == "log":
        text = squash(str(anchor.get("text", "")))
        if len(text) < 4:
            return "报错原文太短（至少 4 个字符）"
        if text not in ev["logs"]:
            return f"报错原文在日志里找不到：{text[:40]}"
    elif kind == "value":
        text = squash(str(anchor.get("text", "")))
        if not text or text not in ev["blob"]:
            return f"数值在证据里找不到：{text[:40]}"
    elif kind == "req":
        if str(anchor.get("id")) not in ev["req_ids"]:
            return f"没有需求点 {anchor.get('id')}"
    elif kind == "test":
        if anchor.get("name") not in ev["failed_tests"]:
            return f"失败用例里没有 {anchor.get('name')}"
    elif kind == "probe":
        if anchor.get("id") not in ev["probes"]:
            return f"没有编号 {anchor.get('id')} 的实跑记录"
    elif kind == "demo":
        index = anchor.get("index")
        if not isinstance(index, int) or not 1 <= index <= ev["demo_steps"]:
            return f"演示里没有第 {index} 条命令"
    return None


def _anchor_problems(anchors, ev: dict, where: str) -> list[str]:
    if not isinstance(anchors, list):
        return [f"{where}：anchors 要是列表"]
    return [f"{where}：{p}" for p in (verify_anchor(a, ev) for a in anchors) if p]


def _text_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def code_review_errors(doc: dict, task: dict, audit: dict, ev: dict) -> list[str]:
    """Require a completed implementation review before a v2 score is accepted.

    Legacy feedback without ``feedback_policy_version`` remains readable. New
    candidates carry version 2 and must prove that relevant changed code was
    reviewed, every requirement has a code-level conclusion, and any claimed
    verification gap followed an actual probe attempt.
    """
    try:
        policy_version = int(doc.get("feedback_policy_version") or 0)
    except (TypeError, ValueError):
        policy_version = 0
    if policy_version < CODE_REVIEW_POLICY_VERSION:
        return []
    review = doc.get("code_review")
    if not isinstance(review, dict):
        return ["新 Feedback 必须填写 code_review，先完成代码审查再评分"]

    errors: list[str] = []
    if review.get("version") != 1:
        errors.append("code_review.version 必须是 1")
    if review.get("baseline_compared") is not True:
        errors.append("code_review.baseline_compared 必须为 true，先区分基线已有与本次引入的问题")

    files = review.get("changed_files_reviewed")
    if not isinstance(files, list):
        errors.append("code_review.changed_files_reviewed 必须是列表")
        files = []
    reviewed: dict[str, dict] = {}
    reviewed_finding_ids: set[str] = set()
    for index, row in enumerate(files, 1):
        label = f"code_review.changed_files_reviewed[{index}]"
        if not isinstance(row, dict):
            errors.append(label + " 必须是对象")
            continue
        path = str(row.get("path") or "").removeprefix("./")
        if not path:
            errors.append(label + ".path 缺失")
            continue
        if path not in ev["diff_files"]:
            errors.append(f"{label}.path 不在本次改动中：{path}")
            continue
        if path in reviewed:
            errors.append(f"{label}.path 重复：{path}")
        reviewed[path] = row
        if row.get("result") not in CODE_REVIEW_FILE_RESULTS:
            errors.append(f"{label}.result 必须是 reviewed 或 not_applicable")
        ids = _text_list(row.get("requirement_ids"))
        if not ids or any(req not in ev["req_ids"] for req in ids):
            errors.append(f"{label}.requirement_ids 必须引用真实需求")
        if len(str(row.get("note") or "").strip()) < 12:
            errors.append(f"{label}.note 至少 12 字，说明实际看了哪些入口、分支、数据落点和测试")
        if (policy_version >= CURRENT_FEEDBACK_POLICY and row.get('result') == 'reviewed'
                and not str(row.get('consumer_check') or '').strip()):
            errors.append(f'{label}.consumer_check 缺失：沿改动追到真实调用方或最终输出，说明触发输入和核对结果')
        finding_ids = _text_list(row.get("finding_ids"))
        reviewed_finding_ids.update(finding_ids)
        if row.get("result") == "not_applicable" and finding_ids:
            errors.append(f"{label} 标为 not_applicable 时不能关联缺陷")
    missing_files = sorted(set(ev["review_files"]) - set(reviewed))
    if missing_files:
        errors.append("code_review 未覆盖相关改动文件：" + "、".join(missing_files))

    checks = review.get("requirement_checks")
    if not isinstance(checks, list):
        errors.append("code_review.requirement_checks 必须是列表")
        checks = []
    by_requirement: dict[str, dict] = {}
    for index, row in enumerate(checks, 1):
        label = f"code_review.requirement_checks[{index}]"
        if not isinstance(row, dict):
            errors.append(label + " 必须是对象")
            continue
        requirement_id = str(row.get("requirement_id") or "")
        if requirement_id not in ev["req_ids"]:
            errors.append(f"{label}.requirement_id 不是真实需求：{requirement_id or '空'}")
            continue
        if requirement_id in by_requirement:
            errors.append(f"{label}.requirement_id 重复：{requirement_id}")
        by_requirement[requirement_id] = row
        if row.get("status") not in CODE_REVIEW_STATUSES:
            errors.append(f"{label}.status 必须是 pass、defect 或 blocked")
        chain = _text_list(row.get("call_chain"))
        if len(chain) < 2 or any(len(item) < 3 for item in chain):
            errors.append(f"{label}.call_chain 至少写入口和处理/数据落点两个真实节点")
        code_checks = row.get("checks")
        if not isinstance(code_checks, list) or len(code_checks) < 2:
            errors.append(f"{label}.checks 至少登记两个代码检查项")
            code_checks = []
        for check_index, item in enumerate(code_checks, 1):
            check_label = f"{label}.checks[{check_index}]"
            if not isinstance(item, dict):
                errors.append(check_label + " 必须是对象")
                continue
            if len(str(item.get("id") or "").strip()) < 3:
                errors.append(check_label + ".id 缺失")
            if item.get("status") not in CODE_REVIEW_CHECK_STATUSES:
                errors.append(check_label + ".status 必须是 pass、violation 或 not_applicable")
            if len(str(item.get("evidence") or "").strip()) < 12:
                errors.append(check_label + ".evidence 至少 12 字，写清代码事实和结果")
        check_statuses = {
            item.get("status") for item in code_checks if isinstance(item, dict)
        }
        if check_statuses and not check_statuses & {"pass", "violation"}:
            errors.append(f"{label}.checks 不能全部标为 not_applicable")
        if row.get("status") != "defect" and "violation" in check_statuses:
            errors.append(f"{label} 有 violation，但需求状态没有标为 defect")
        finding_ids = _text_list(row.get("finding_ids"))
        if row.get("status") == "defect" and not finding_ids:
            errors.append(f"{label} 判了 defect，但 finding_ids 为空")
        if row.get("status") in ("pass", "blocked") and finding_ids:
            errors.append(f"{label} 不是 defect，不能关联 finding_ids")
    missing_requirements = sorted(ev["req_ids"] - set(by_requirement))
    if missing_requirements:
        errors.append("code_review 未覆盖需求：" + "、".join(missing_requirements))

    findings = review.get("findings")
    if not isinstance(findings, list):
        errors.append("code_review.findings 必须是列表")
        findings = []
    by_finding: dict[str, dict] = {}
    for index, row in enumerate(findings, 1):
        label = f"code_review.findings[{index}]"
        if not isinstance(row, dict):
            errors.append(label + " 必须是对象")
            continue
        finding_id = str(row.get("id") or "")
        if not finding_id:
            errors.append(label + ".id 缺失")
            continue
        if finding_id in by_finding:
            errors.append(f"{label}.id 重复：{finding_id}")
        by_finding[finding_id] = row
        ids = _text_list(row.get("requirement_ids"))
        if not ids or any(req not in ev["req_ids"] for req in ids):
            errors.append(f"{label}.requirement_ids 必须引用真实需求")
        if row.get("kind") not in CODE_REVIEW_KINDS:
            errors.append(f"{label}.kind 不在允许的代码缺陷类型中")
        if row.get("evidence_level") not in CODE_REVIEW_EVIDENCE_LEVELS:
            errors.append(f"{label}.evidence_level 必须是 reproduced 或 code_reachable")
        if policy_version >= CURRENT_FEEDBACK_POLICY and squash(str(row.get('expected') or '')) == squash(str(row.get('actual') or '')):
            errors.append(f'{label} 的实际行为和预期一致，请撤回疑点或补上不同的实际结果')
        for key, minimum in (("trigger", 8), ("expected", 8), ("actual", 8), ("impact", 8)):
            if len(str(row.get(key) or "").strip()) < minimum:
                errors.append(f"{label}.{key} 必须写清楚")
        anchors = row.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            errors.append(f"{label} 缺少证据 anchors")
            anchors = []
        else:
            errors += _anchor_problems(anchors, ev, label)
        kinds = {item.get("type") for item in anchors if isinstance(item, dict)}
        level = row.get("evidence_level")
        if level == "reproduced" and not kinds & {"probe", "test", "log", "demo"}:
            errors.append(f"{label} 的 reproduced 缺陷必须绑定 probe、失败测试、日志或演示")
        if level == "code_reachable" and "file" not in kinds:
            errors.append(f"{label} 的 code_reachable 缺陷必须绑定真实代码文件")
        if level == "code_reachable":
            for requirement_id in ids:
                requirement_check = by_requirement.get(requirement_id) or {}
                if len(_text_list(requirement_check.get("call_chain"))) < 2:
                    errors.append(
                        f"{label} 声称代码可达，但 {requirement_id} 的 call_chain 少于两个节点"
                    )
        for requirement_id in ids:
            requirement_check = by_requirement.get(requirement_id) or {}
            if finding_id not in _text_list(requirement_check.get("finding_ids")):
                errors.append(f"{label} 未被 {requirement_id} 的 requirement_check 引用")
        audit_requirements = {
            str(item.get("id")): item for item in audit.get("requirements", [])
            if isinstance(item, dict)
        }
        for requirement_id in ids:
            audit_row = audit_requirements.get(requirement_id) or {}
            reflected = audit_row.get("verdict") == "有问题"
            if row.get("kind") == "false_success" and audit.get("false_success") != "无":
                reflected = True
            if not reflected:
                errors.append(f"{label} 所属需求 {requirement_id} 未在 audit 中判为有问题")
    for requirement_id, row in by_requirement.items():
        for finding_id in _text_list(row.get("finding_ids")):
            if finding_id not in by_finding:
                errors.append(f"{requirement_id} 引用了不存在的 finding：{finding_id}")
    unknown_file_findings = reviewed_finding_ids - set(by_finding)
    if unknown_file_findings:
        errors.append("changed_files_reviewed 引用了不存在的 finding：" + "、".join(sorted(unknown_file_findings)))
    detached_findings = set(by_finding) - reviewed_finding_ids
    if detached_findings:
        errors.append("finding 必须关联至少一个 reviewed 改动文件：" + "、".join(sorted(detached_findings)))
    audit_requirements = {
        str(item.get("id")): item for item in audit.get("requirements", [])
        if isinstance(item, dict)
    }
    for requirement_id, audit_row in audit_requirements.items():
        if audit_row.get("verdict") == "有问题":
            review_row = by_requirement.get(requirement_id) or {}
            if review_row.get("status") != "defect" or not _text_list(review_row.get("finding_ids")):
                errors.append(f"audit 中 {requirement_id} 已判有问题，code_review 必须对应 defect finding")
        elif audit_row.get("verdict") == "看不出来":
            review_row = by_requirement.get(requirement_id) or {}
            if review_row.get("status") != "blocked":
                errors.append(f"audit 中 {requirement_id} 判了看不出来，code_review.status 必须为 blocked")
    for requirement_id, review_row in by_requirement.items():
        if review_row.get("status") == "blocked":
            audit_row = audit_requirements.get(requirement_id) or {}
            if audit_row.get("verdict") != "看不出来":
                errors.append(f"code_review 中 {requirement_id} 标为 blocked，audit 必须判为看不出来")
    if ev.get("tests_regression") and not findings:
        errors.append("产物测试出现本次回归，code_review 必须给出对应代码缺陷或说明具体 finding")

    basis = doc.get("score_basis") if isinstance(doc.get("score_basis"), dict) else {}
    category = str(basis.get("category") or "")
    if category in ("complete", "verification_gap") and findings:
        errors.append(f"{category} 不能带有 code_review.findings；已发现缺陷必须按 delivery_issue 处理")
    if category == "delivery_issue" and not findings:
        errors.append("delivery_issue 必须给出至少一个 code_review.findings 缺陷")
    if category == "complete":
        blocked = [rid for rid, row in by_requirement.items() if row.get("status") == "blocked"]
        if blocked:
            errors.append("complete 不能保留 blocked 的代码审查结论：" + "、".join(sorted(blocked)))

    issues = doc.get("issues") if isinstance(doc.get("issues"), list) else []
    issue_findings = {
        str(item.get("finding_id")) for item in issues
        if isinstance(item, dict) and item.get("finding_id")
    }
    unknown_issue_findings = issue_findings - set(by_finding)
    if unknown_issue_findings:
        errors.append("issues 引用了不存在的 finding：" + "、".join(sorted(unknown_issue_findings)))
    if category == "delivery_issue":
        unmapped = set(by_finding) - issue_findings
        if unmapped:
            errors.append("delivery_issue 的 finding 必须对应的 issues 问题句：" + "、".join(sorted(unmapped)))

    if policy_version >= CURRENT_FEEDBACK_POLICY and doc.get('score') == 4:
        remaining = squash(str(basis.get('remaining') or ''))
        if category == 'delivery_issue':
            supported = [squash(str(item.get('sentence') or '')) for item in issues
                         if isinstance(item, dict) and item.get('finding_id') in by_finding]
        else:
            gap = review.get('verification_gap')
            supported = [squash(str(gap.get('unresolved_observable') or ''))] if isinstance(gap, dict) else []
        if not any(sentence and remaining and (sentence in remaining or remaining in sentence) for sentence in supported):
            errors.append('score_basis.remaining 须引用有 finding 的产物问题句或经 probe 核实的验证边界')
    if category == "verification_gap":
        defects = [rid for rid, row in by_requirement.items() if row.get("status") == "defect"]
        if defects:
            errors.append("已有代码缺陷时不能用 verification_gap：" + "、".join(sorted(defects)))
        gap = review.get("verification_gap")
        if not isinstance(gap, dict):
            errors.append("verification_gap 必须提供 code_review.verification_gap 证据")
        else:
            unresolved = str(gap.get("unresolved_observable") or "").strip()
            if len(unresolved) < 6:
                errors.append("code_review.verification_gap.unresolved_observable 必须写出未观察到的用户结果")
            elif squash(unresolved) not in squash(str(doc.get("feedback") or "")):
                errors.append("code_review.verification_gap.unresolved_observable 必须逐字出现在正文")
            attempted = _text_list(gap.get("attempted_probes"))
            if not attempted:
                errors.append("verification_gap 必须列出已尝试的 probe，不能只因模型没有验证就判 4 分")
            for probe_id in attempted:
                probe = ev["probes"].get(probe_id)
                if not probe:
                    errors.append(f"verification_gap 引用了不存在的 probe：{probe_id}")
                elif probe.get("setup_rc") != 0 or not isinstance(probe.get("rc"), int) \
                        or isinstance(probe.get("rc"), bool) or probe["rc"] in (124, 126, 127, 137):
                    errors.append(f"probe {probe_id} 未有效执行，不能证明验证缺口")
            if gap.get("blocked_reason") not in CODE_REVIEW_BLOCKED_REASONS:
                errors.append(
                    "code_review.verification_gap.blocked_reason 必须是客观阻塞原因，"
                    "不能用模型未测试或未核对"
                )
    return list(dict.fromkeys(errors))


# ---------- audit.json ----------

def check_audit(audit: dict, task: dict, ev: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not isinstance(audit, dict) or not isinstance(audit.get("requirements"), list):
        return ["audit.json 缺 requirements 列表"], warnings
    seen = {}
    for item in audit["requirements"]:
        if isinstance(item, dict) and item.get("id") is not None:
            seen[str(item["id"])] = item
    for req in task["requirements"]:
        rid = str(req["id"])
        item = seen.get(rid)
        if item is None:
            errors.append(f"需求点 {rid} 没有结论")
            continue
        verdict, anchors = item.get("verdict"), item.get("anchors") or []
        if verdict not in VERDICTS:
            errors.append(f"需求点 {rid} 的结论要写 做到 / 有问题 / 看不出来")
            continue
        errors += _anchor_problems(anchors, ev, f"需求点 {rid}")
        if verdict != "看不出来" and not anchors:
            errors.append(f"需求点 {rid} 判了{verdict}，但没有锚点")
        if verdict != "做到" and not str(item.get("note", "")).strip():
            errors.append(f"需求点 {rid} 判了{verdict}，要在 note 里写清楚是什么情况")
        if verdict == "有问题" and item.get("level") not in LEVELS:
            errors.append(f"需求点 {rid} 判了有问题，level 要写 未实现 / 明显 / 细节")
    extra = set(seen) - ev["req_ids"]
    if extra:
        errors.append("audit.json 里有任务里没有的需求点：" + "、".join(sorted(extra)))
    for index, item in enumerate(audit.get("counterexamples") or [], 1):
        if not isinstance(item, dict) or not str(item.get("what", "")).strip():
            errors.append(f"第 {index} 个反例没写 what")
        elif item.get("probe") not in ev["probes"]:
            errors.append(f"第 {index} 个反例没有对应的实跑记录（probe）")
        elif not isinstance(item.get("found_problem"), bool):
            errors.append(f"第 {index} 个反例要写 found_problem: true / false")
    level = audit.get("false_success")
    if level not in FALSE_SUCCESS:
        errors.append("false_success 要写 无 / 轻微 / 较大 / 严重")
    elif level != "无":
        if not str(audit.get("false_success_note", "")).strip():
            errors.append("判了虚假成功，要在 false_success_note 里写清楚模型说了什么、实际是什么")
        anchors = audit.get("false_success_anchors") or []
        if not anchors:
            errors.append("判了虚假成功，要给锚点（false_success_anchors）")
        errors += _anchor_problems(anchors, ev, "虚假成功")
    return errors, warnings


def valid_counterexamples(audit: dict, ev: dict, *, require_novel: bool = False) -> list[dict]:
    valid, seen = [], set()
    for item in audit.get("counterexamples") or []:
        if not isinstance(item, dict) or not isinstance(item.get("found_problem"), bool):
            continue
        if require_novel and item.get("prompt_implied") is not False:
            continue
        pid = item.get("probe")
        probe = ev["probes"].get(pid)
        if not probe or pid in seen or not str(item.get("what", "")).strip():
            continue
        if probe.get("setup_rc") != 0 or not isinstance(probe.get("rc"), int) \
                or isinstance(probe.get("rc"), bool) or probe["rc"] in (124, 126, 127, 137):
            continue
        seen.add(pid)
        valid.append(item)
    return valid


# ---------- 分数 ----------

def score_cap(facts: dict, audit: dict, ev: dict, gate: dict) -> tuple[int, list[str]]:
    """按证据算分数上限。默认上限 4 分，5 分要每条需求都验过、反例实跑没找出问题。"""
    cap, why = 5, []

    def lower(value: int, reason: str) -> None:
        nonlocal cap
        cap = min(cap, value)
        why.append(f"{reason}，最高 {value} 分")

    if facts["tests"]["regression"]:
        lower(3, "产物上构建或测试不通过")
    elif not facts["tests"]["after_ok"]:
        lower(4, "产物测试尚未通过，不能给 5 分")
    reqs = [r for r in audit.get("requirements", []) if isinstance(r, dict)]
    levels = [r.get("level") for r in reqs if r.get("verdict") == "有问题"]
    if "未实现" in levels:
        lower(3, "有明确要求没实现")
    if "明显" in levels:
        lower(3, "有明显问题")
    if "细节" in levels:
        lower(4, "有细节遗漏")
    if any(r.get("verdict") == "看不出来" for r in reqs):
        lower(4, "有需求点验证不了")
    level = audit.get("false_success")
    if level in FALSE_SUCCESS and level != "无":
        lower(FALSE_SUCCESS[level], f"虚假成功（{level}）")
    examples = valid_counterexamples(audit, ev, require_novel=True)
    if len(examples) < int(gate["min_counterexamples"]):
        lower(4, f"实跑过的反例不到 {gate['min_counterexamples']} 个")
    if any(c["found_problem"] for c in examples):
        lower(4, "反例找出了问题")
    return cap, why


def check_score(score, facts: dict, audit: dict, ev: dict, gate: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
        return ["score 要是 1 到 5 的整数"], warnings
    cap, why = score_cap(facts, audit, ev, gate)
    if score > cap:
        errors.append(f"分数 {score} 超过证据允许的上限 {cap}：" + "；".join(why))
    reqs = [r for r in audit.get("requirements", []) if isinstance(r, dict)]
    levels = [r.get("level") for r in reqs if r.get("verdict") == "有问题"]
    fake = audit.get("false_success")
    tests = facts["tests"]
    if audit.get("false_success") == "严重" and score != 1:
        errors.append("判了严重的虚假成功，只能是 1 分")
    if score <= 3 and not (tests["regression"] or "未实现" in levels or "明显" in levels or fake in ("轻微", "较大", "严重")):
        errors.append(f"分数 {score} 偏低：证据里没有测试不过、未实现、明显问题或虚假成功中的任何一项")
    if score <= 2 and not (not tests["after_runnable"] or "未实现" in levels or fake in ("较大", "严重") or (score == 2 and (levels.count("明显") >= 2 or any(r.get("core_path") is True and r.get("level") == "明显" and r.get("verdict") == "有问题" for r in reqs)))):
        errors.append(f"分数 {score} 偏低：缺少运行失败、未实现、虚假成功或明确核心/多项明显问题的证据")
    demo = facts["demo"]["steps"]
    dead = not tests["after_runnable"] and demo and all(s.get("rc") not in (0, None) for s in demo)
    if dead and score > 2:
        warnings.append("测试和演示都跑不起来，没有可用结果，一般是 2 分")
    missing = sum(1 for v in levels if v == "未实现")
    if reqs and missing * 2 > len(reqs) and score > 2:
        warnings.append("过半的需求点没实现，一般不超过 2 分")
    if facts["changes"]["no_change"] and score > 2:
        warnings.append("产物相对初始提交没有任何改动，确认这个分数")
    return errors, warnings


# ---------- Feedback 正文 ----------

def check_text(text: str, prompt: str, gate: dict, *, enforce_language: bool = True) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    body = (text or "").strip()
    if not body:
        return ["Feedback 是空的"], warnings
    size = len(re.sub(r"\s+", "", body))
    if size < int(gate["min_chars"]):
        errors.append(f"Feedback 只有 {size} 字，至少 {gate['min_chars']} 字")
    if size > int(gate["max_chars"]):
        errors.append(f"Feedback 有 {size} 字，最多 {gate['max_chars']} 字")
    lowered, prompt_low = body.lower(), (prompt or "").lower()

    def hits(words: list[str], skip_if_in_prompt: bool = False) -> list[str]:
        return [w for w in words if w in lowered and not (skip_if_in_prompt and w in prompt_low)]

    if enforce_language:
        style = fbr_style.review_text(body)
        errors += [row["message"] for row in style["errors"]]
        warnings += [row["message"] for row in style["warnings"]]
    else:
        for label, found in (
            ("套话或 AI 腔的词", hits(AI_WORDS)),
            ("没法核验的评价", hits(VAGUE_WORDS)),
        ):
            if found:
                errors.append(f"出现{label}：" + " ".join(dict.fromkeys(found)))
        if EMPTY_COMPLETENESS_RE.search(body):
            errors.append("删除“未发现其他明确未完成功能”这类空泛完成度句；直接写完成了什么、没完成什么或仍有哪项问题")

    for label, found in (
        ("内部口径词", hits(INTERNAL_WORDS)),
        ("指代题目的说法", hits(TOPIC_WORDS)),
        ("运行环境或录屏现场的词（任务本身没提到）", hits(HARNESS_WORDS, True)),
        ("网络、额度、耗时这类不该算在模型头上的因素", hits(SCOPE_WORDS, True)),
        ("跨机器的说法", hits(OTHER_MACHINE_WORDS)),
        ("取证来源口吻", hits(EVIDENCE_METHOD_WORDS)),
        ("禁用的标点或标记", [m for m in BANNED_MARKS if m in body]),
    ):
        if found:
            errors.append(f"出现{label}：" + " ".join(dict.fromkeys(found)))
    if SUBJECT_RE.search(body):
        errors.append("用了「这条 / 这一版 / 这份交付」当主语")
    if SCORE_TALK_RE.search(body):
        errors.append("正文里不写给分结论（几分、扣分、档位）")
    if SELF_RE.search(body):
        errors.append("正文里不写验收人自己（我、我们、本人）")
    if body.count("模型") > 1:
        errors.append("正文里“模型”最多出现一次，第一次点明主体后改用“它”承接或省略主语")
    if not MODEL_PROCESS_RE.search(body):
        errors.append("正文缺少模型实现或验证过程的具体行为句")
    model_sentences = [sentence for sentence in sentences(body) if "模型" in sentence]
    concrete_process = [
        sentence for sentence in model_sentences
        if PROCESS_PROBLEM_RE.search(sentence)
        and (PROCESS_REPAIR_RE.search(sentence) or PROCESS_CONSEQUENCE_RE.search(sentence))
    ]
    if any(PROCESS_GAP_ONLY_RE.search(sentence) for sentence in model_sentences) and not concrete_process:
        errors.append("模型过程只写了未覆盖或未验证，缺少实际过程问题、处理动作和后果")
    if REQ_REF_RE.search(body):
        errors.append("正文里不写需求点编号（R1 这类），直接说是哪条要求")
    norm = lib.normalize(body)
    for line in RUBRIC:
        for part in re.split(r"[。；]", line):
            block = longest_common(norm, lib.normalize(part))
            if len(block) >= 8 and lib.han_count(block) >= 6:
                errors.append(f"照抄了分数档位的描述：{block}")
    block = longest_common(norm, lib.normalize(prompt or ""))
    if len(block) >= 20:
        errors.append(f"复述了任务原文：{block[:30]}")
    heads = [lib.normalize(s)[:4] for s in sentences(body)]
    repeated = {h for h in heads if len(h) == 4 and heads.count(h) >= 3}
    if repeated:
        warnings.append("多句用同一个开头：" + " ".join(sorted(repeated)))
    return errors, warnings


def check_feedback(doc: dict, task: dict, facts: dict, audit: dict, ev: dict, gate: dict) -> tuple[list[str], list[str]]:
    """review/feedback.json 的结构、锚点，以及分数和正文是否对得上。"""
    errors, warnings = [], []
    text, score = str(doc.get("feedback", "")), doc.get("score")
    flat = squash(text)
    issues = doc.get("issues") if isinstance(doc.get("issues"), list) else []
    process = doc.get("process") if isinstance(doc.get("process"), list) else []
    if task.get("feedback_policy_version") == CURRENT_FEEDBACK_POLICY and doc.get("feedback_policy_version") != CURRENT_FEEDBACK_POLICY:
        errors.append("新交接题需按当前版本重建 Feedback 和代码审查材料")
    errors += code_review_errors(doc, task, audit, ev)
    if doc.get('feedback_policy_version') == CURRENT_FEEDBACK_POLICY:
        paragraphs = [part.strip() for part in re.split(r'\n[ \t]*\n', text.strip().replace('\r\n', '\n')) if part.strip()]
        if len(paragraphs) != 2 or not paragraphs[1].startswith('模型'):
            errors.append('按两段重写：第一段先说实际交付，再说已证实的问题；第二段以模型开头，先说操作，有过程问题再说明。')
        else:
            product_sentences = [unit for unit in re.split(r'[。！？]+', paragraphs[0]) if unit.strip()]
            basis = doc.get('score_basis') or {}
            if not issues and isinstance(basis, dict) and basis.get('category') == 'complete' and len(product_sentences) > 1:
                errors.append('产物段用一句概括已完成的功能；各项详细验证保存在内部结果映射')
            if issues and product_sentences and all(
                isinstance(item, dict) and squash(str(item.get('sentence') or '')).rstrip('。') in squash(product_sentences[0])
                for item in issues
            ) and len(product_sentences) > 1:
                errors.append('产物段首句先概括实际交付，再说明已证实的瑕疵')
            for item in issues:
                if isinstance(item, dict) and squash(str(item.get('sentence') or '')) not in squash(paragraphs[0]):
                    errors.append('将最终产物问题写入第一段并重新绑定 issues')
            for item in process:
                if isinstance(item, dict) and squash(str(item.get('sentence') or '')) not in squash(paragraphs[1]):
                    errors.append('模型实际动作及其后续状态必须落在第二段')
            if len(re.findall(r'[。！？]+', paragraphs[1].rstrip('。！？'))) > 1:
                errors.append('过程段先用一句说明模型操作，确有问题再补一句说明后果')

    for index, item in enumerate(issues, 1):
        where = f"第 {index} 个问题句"
        if not isinstance(item, dict):
            errors.append(f"{where}格式不对")
            continue
        sentence = squash(str(item.get("sentence") or ""))
        if len(sentence) < 6 or sentence not in flat:
            errors.append(f"{where}：sentence 要是正文里的原句")
        for key, label in (("trigger", "触发条件"), ("behavior", "最终实现行为"), ("impact", "业务影响")):
            if len(str(item.get(key) or "").strip()) < 4:
                errors.append(f"{where}：缺{label}（{key}）")
        anchors = item.get("anchors") or []
        located = [a for a in anchors if isinstance(a, dict) and a.get("type") in
                   ("file", "tree", "missing", "step", "log", "test", "probe", "demo")]
        if not located:
            errors.append(f"{where}：至少要有一个能定位的锚点（文件、步骤、报错原文、失败用例、实跑记录、演示命令）")
        errors += _anchor_problems(anchors, ev, where)
    for index, item in enumerate(process, 1):
        where = f"第 {index} 个过程句"
        sentence = squash(str(item.get("sentence") or "")) if isinstance(item, dict) else ""
        steps = item.get("steps") if isinstance(item, dict) else None
        if len(sentence) < 6 or sentence not in flat:
            errors.append(f"{where}：sentence 要是正文里的原句")
        elif sentence not in {squash(unit) for unit in sentences(text)}:
            errors.append(f"{where}：必须对应正文中的一个完整句段，不能跨分号、句号或换行拼接多个动作")
        conversation = isinstance(item, dict) and item.get("source") == "conversation" and not ev["steps"] and steps == []
        if (not isinstance(steps, list) or not steps) and not conversation:
            errors.append(f"{where}：要写它对应轨迹里的哪几步（steps）")
        else:
            errors += [f"{where}：轨迹里没有第 {s} 步" for s in steps if s not in ev["steps"]]
    if not process:
        errors.append("过程和产物都要写：正文里至少有一句讲过程，并在 process 里标出对应的轨迹步骤")
    elif len(process) > 3:
        errors.append("过程段最多写 3 句，只保留最能解释交付结果的事件")

    if isinstance(score, int) and not isinstance(score, bool):
        if score == 5 and issues:
            errors.append("5 分不写缺口：issues 应当为空")
        process_or_verification_gap = (score == 4 and isinstance(doc.get("score_basis"), dict)
                             and doc["score_basis"].get("category") == "verification_gap"
                             and all(r.get("verdict") in ("做到", "看不出来") for r in audit.get("requirements", []))
                             and audit.get("false_success") == "无"
                             and not facts["tests"]["regression"]
                             and not any(c.get("found_problem") for c in audit.get("counterexamples", [])))
        if score < 5 and not issues and not process_or_verification_gap:
            errors.append(f"{score} 分至少要有一处问题句")
        errors += platform_consistency_errors(
            score, text, public_score_basis(doc),
        )
        if score >= 4 and len(re.findall(r"[A-Za-z_][\w./-]{2,}|\d+", text, flags=re.A)) < 2:
            errors.append("4 到 5 分也要写出核对过的要求和实际验证结果，正文里看不到具体的命令、文件或数值")
    if facts["tests"]["regression"] and not FAIL_WORDS_RE.search(NEGATED_FAIL_RE.sub("", text)):
        errors.append("产物上构建或测试不通过，正文必须明写")
    process_sentences = [squash(str(item.get("sentence") or "")) for item in process if isinstance(item, dict)]
    process_problem = any(PROCESS_PROBLEM_RE.search(sentence) or PROCESS_GAP_ONLY_RE.search(sentence)
                          for sentence in process_sentences)
    process_repair = any(PROCESS_REPAIR_RE.search(sentence) for sentence in process_sentences)
    if score == 5 and process_problem and process_repair and doc.get('feedback_policy_version') != CURRENT_FEEDBACK_POLICY:
        errors.append("5 分要求过程接近一次性完美跑通；正文已写真实错误验证、返工或关键缺口，应降到 4 分并说明过程问题")
    covered = {str(a.get("id")) for item in issues if isinstance(item, dict)
               for a in (item.get("anchors") or []) if isinstance(a, dict) and a.get("type") == "req"}
    for req in audit.get("requirements", []):
        if isinstance(req, dict) and req.get("verdict") == "有问题" and req.get("level") in ("未实现", "明显") \
                and str(req.get("id")) not in covered:
            errors.append(f"需求点 {req.get('id')} 判了{req.get('level')}，正文里要有对应的问题句（anchors 里带 req）")

    for found in PATH_RE.finditer(text):
        token = found.group(1).removeprefix("./")
        if found.group(2).lower() not in PATH_EXT or not re.search(r"[A-Za-z]", token.rsplit(".", 1)[0]):
            continue
        # 运行时才生成的文件（任务原文点了名的输出、日志里打出来的路径）也算有出处
        known = token in ev["diff_files"] or token in ev["tree"] or token in ev["claims_missing"] or any(
            p.endswith("/" + token) for p in ev["diff_files"] | ev["tree"]) \
            or token in ev["task_text"] or token in ev["logs"]
        if not known:
            errors.append(f"正文提到的文件 {token} 在产物、改动清单、任务原文和日志里都找不到")
    errors += [f"正文提到第 {n} 步，轨迹里没有这一步" for n in STEP_REF_RE.findall(text) if int(n) not in ev["steps"]]
    for number in dict.fromkeys(re.findall(r"(?<![\w.])\d{2,}(?:\.\d+)?(?![\w.])", text, flags=re.A)):
        if number not in ev["blob"]:
            warnings.append(f"数值 {number} 在证据里找不到，确认不是凭印象写的")
    return errors, warnings


# ---------- 查重 ----------

def check_duplicates(records: dict[str, str], delivered: list[dict], gate: dict) -> tuple[dict[str, list[str]], list[str]]:
    """records: {任务编号: Feedback}；delivered: 已交付的 [{task_id, feedback}]。返回 (每题的问题, 批次级提示)。"""
    size, han = int(gate["dup_block"]), int(gate["dup_block_han"])
    problems: dict[str, list[str]] = {name: [] for name in records}
    norm = {name: lib.normalize(text) for name, text in records.items()}
    old = [(str(row.get("task_id", "")), lib.normalize(str(row.get("feedback", "")))) for row in delivered
           if str(row.get("task_id", "")) not in records]

    def grams(text: str) -> set[str]:
        return {text[i:i + size] for i in range(len(text) - size + 1) if lib.han_count(text[i:i + size]) >= han}

    index: dict[str, list[str]] = {}
    for name, text in norm.items():
        for gram in grams(text):
            index.setdefault(gram, []).append(name)
    pairs = {}
    for gram, names in index.items():
        for a in names:
            for b in names:
                if a < b:
                    pairs.setdefault((a, b), gram)
    for (a, b), gram in sorted(pairs.items()):
        block = longest_common(norm[a], norm[b])
        problems[a].append(f"和 {b} 有 {len(block)} 字的相同片段：{block[:24]}")
        problems[b].append(f"和 {a} 有 {len(block)} 字的相同片段：{block[:24]}")
    old_index: dict[str, str] = {}
    for other, text in old:
        for gram in grams(text):
            old_index.setdefault(gram, other)
    for name, text in norm.items():
        hit = next(((gram, old_index[gram]) for gram in grams(text) if gram in old_index), None)
        if hit:
            problems[name].append(f"和已交付的 {hit[1] or '历史数据'} 有相同片段：{hit[0]}")

    # 文件名、命令与数值换掉之后仍几乎一样，是换槽位的模板；不靠随机同义词修饰。
    def skeleton(text: str) -> str:
        return lib.normalize(re.sub(r"[A-Za-z_][A-Za-z0-9_./:=<>-]*|\d+(?:\.\d+)?", "◇", text))

    shapes = {name: skeleton(text) for name, text in records.items()}
    history = [(str(r.get("task_id", "")), skeleton(str(r.get("feedback", "")))) for r in delivered
               if str(r.get("task_id", "")) not in records]
    for name, shape in shapes.items():
        candidates = [(other, value) for other, value in shapes.items() if other > name] + history
        for other, value in candidates:
            if min(len(shape), len(value)) < 90 or min(lib.han_count(shape), lib.han_count(value)) < 40:
                continue
            if SequenceMatcher(None, shape, value, autojunk=False).ratio() >= 0.88:
                problems[name].append(f"和 {other or '历史数据'} 的句式骨架高度重复，不能只替换文件名、命令和数量")
                if other in problems:
                    problems[other].append(f"和 {name} 的句式骨架高度重复，须从本题事实重写")

    seen: dict[str, str] = {}
    for name, text in records.items():
        for sentence in sentences(text):
            key = lib.normalize(sentence)
            if len(key) < 10:
                continue
            if key in seen and seen[key] != name:
                problems[name].append(f"整句和 {seen[key]} 重复：{sentence[:24]}")
            seen.setdefault(key, name)

    notes = []
    head_len, total = int(gate["head_len"]), len(records)
    heads: dict[str, list[str]] = {}
    for name, text in norm.items():
        heads.setdefault(text[:head_len], []).append(name)
    for head, names in heads.items():
        if total >= 5 and len(names) >= 2 and len(names) / total > float(gate["head_ratio"]):
            for name in names:
                problems[name].append(f"开头 {head_len} 个字和另外 {len(names) - 1} 条一样：{head}")
    return problems, notes


def five_ratio_note(scores: list[int], gate: dict) -> str:
    if len(scores) >= 5:
        ratio = sum(1 for s in scores if s == 5) / len(scores)
        if ratio > float(gate["five_ratio"]):
            return f"这一批 5 分占 {ratio:.0%}，超过 {float(gate['five_ratio']):.0%}，回头重看 5 分的那几条"
    return ""
