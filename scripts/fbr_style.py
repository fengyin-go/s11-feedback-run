#!/usr/bin/env python3
"""Deterministic public-Feedback language checks and policy-bound reports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from functools import lru_cache

import fbrlib as lib

VERSION = 1
POLICY_FILE = Path(__file__).resolve().parent.parent / "references/feedback-style-lexicon.json"
REPORT_FILE = "review/style-report.json"
DENSE_REPORT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z]{2,}-[0-9]+|[0-9]{2}:[0-9]{2}(?::[0-9]{2})?|[0-9]{3,})(?![A-Za-z0-9_])")
MACHINE_STYLE_RE = re.compile(r"\bTS\d+\b|python\s+-I|_enable\b|JSONDecodeError|Traceback")
BARE_TEST_COUNT_RE = re.compile(
    r"(?:\d+\s*/\s*\d+|Ran\s+\d+\s+tests|"
    r"\d+\s*项(?:后端|服务|回归|验收|边界|场景)?(?:测试|断言|回归|验收|场景)?\s*(?:通过|全过)?)",
    re.I,
)
COUNT_UNIT_RE = re.compile(r"[一二两三四五六七八九十百0-9]+\s*(?:条|次|项|个|段|份|轮)")
VAGUE_FAILURE_RE = re.compile(r"(?:验证中还出现过|在并发场景里|测试中|验证中)[^。；]{0,40}(?:问题|错误|失败|异常)")
CONCRETE_FAILURE_RE = re.compile(r"(?:[A-Za-z_][\w./-]+\.[A-Za-z]{2,5}|第\s*\d+\s*步|\d+\s*步)")
FIXED_SEQUENCE_RE = re.compile(r"模型先.{0,180}随后.{0,180}最后")
AVAILABILITY_OPENING_RE = re.compile(r"(?:^|[。；\n])[^。；\n]{2,50}(?:已可用|已经可用)[：:]")
ABSTRACT_FINISH_RE = re.compile(
    r"(?:均已|都已|全部)(?:实现|完成|落地|覆盖|打通|对齐|闭环)|"
    r"(?:已|已经)(?:实现|完成|落地|覆盖|打通|对齐|闭环)(?:[，,；。]|$)")
ACTION_RE = re.compile(
    r"提交|保存|返回|显示|拒绝|保留|覆盖|继续|删除|刷新|打开|点击|运行|报错|退出|"
    r"写入|读取|创建|更新|修改|重算|下载|上传|启动|停止|恢复|执行|切换|筛选|导出|导入"
)
NUMBER_RE = re.compile(r"(?<![\w.])\d{2,}(?:\.\d+)?(?![\w.])", re.A)


@lru_cache(maxsize=1)
def policy() -> dict:
    value = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise lib.FbrError("Feedback 语言策略词表格式不正确")
    return value


def policy_sha() -> str:
    return hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest()


def text_sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def units(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？；\n]+", str(text or "")) if item.strip()]


def _term_hits(text: str, terms: list[str]) -> list[dict]:
    found = []
    for term in terms:
        start = 0
        while term and (index := text.find(term, start)) >= 0:
            found.append({"term": term, "start": index, "end": index + len(term)})
            start = index + len(term)
    found.sort(key=lambda row: (row["start"], -(row["end"] - row["start"])))
    selected, covered = [], -1
    for row in found:
        if row["start"] < covered:
            continue
        selected.append(row)
        covered = row["end"]
    return selected


def _append(report: dict, *, code: str, message: str, severity: str = "error",
            phrase: str = "", suggestion: str = "", span: list[int] | None = None) -> None:
    row = {"code": code, "severity": severity, "message": message,
           "phrase": phrase, "suggestion": suggestion, "span": span or []}
    bucket = "errors" if severity == "error" else "warnings"
    if row not in report[bucket]:
        report[bucket].append(row)


def _abstract_problem(text: str, limits: dict) -> tuple[list[str], list[str]]:
    terms = policy().get("contextual_terms") or []
    errors, warnings = [], []
    for sentence in units(text):
        hits = list(dict.fromkeys(term for term in terms if term in sentence))
        action = bool(ACTION_RE.search(sentence))
        if len(hits) >= int(limits.get("abstract_terms_error", 5)) or (len(hits) >= 4 and not action):
            errors.append(
                f"句子把抽象词堆在一起（{'、'.join(hits)}），改成谁做了什么、结果在哪里看到")
        elif len(hits) >= int(limits.get("abstract_terms_warning", 3)):
            warnings.append(
                f"句子里的抽象词偏多（{'、'.join(hits)}），确认是否每个词都有具体对象")
    return errors, warnings


def review_text(text: str, score: int | None = None) -> dict:
    body = str(text or "")
    report = {
        "version": VERSION,
        "policy_version": policy().get("version"),
        "policy_sha": policy_sha(),
        "feedback_sha": text_sha(body),
        "state": "failed",
        "errors": [],
        "warnings": [],
        "hits": [],
        "metrics": {},
    }
    if re.search(r'(?:日期|配额日)(?:始终|一直|仍然|仍)?停当天', body):
        _append(report, code='missing_preposition', message='日期停当天缺少介词，核实后改为停在当天', phrase='停当天', suggestion='停在当天')
    for group in policy().get("hard_groups") or []:
        hits = _term_hits(body, list(group.get("terms") or []))
        for hit in hits:
            report["hits"].append({**hit, "kind": "hard", "group": group.get("id")})
        if hits:
            phrases = list(dict.fromkeys(row["term"] for row in hits))
            _append(report, code=str(group.get("id") or "hard"),
                    message=f"出现{group.get('label', '固定套话')}：{'、'.join(phrases)}。{group.get('guidance', '')}",
                    phrase="、".join(phrases), suggestion=str(group.get("guidance") or ""))

    for group in policy().get("rewrite_groups") or []:
        hits = _term_hits(body, list(group.get("terms") or []))
        for hit in hits:
            report["hits"].append({**hit, "kind": "rewrite", "group": group.get("id")})
        if hits:
            phrases = list(dict.fromkeys(row["term"] for row in hits))
            guidance = str(group.get("guidance") or "")
            _append(report, code=str(group.get("id") or "rewrite"),
                    message=f"出现企业黑话或抽象包装词：{'、'.join(phrases)}。{guidance}",
                    phrase="、".join(phrases), suggestion=guidance)

    machine_terms = [term for term in policy().get("machine_summary_phrases") or [] if term in body]
    if machine_terms:
        _append(report, code="machine_summary",
                message="Feedback 出现机器摘要式抽象串：" + "、".join(machine_terms),
                phrase="、".join(machine_terms), suggestion="改成具体动作和业务后果")

    dense = list(dict.fromkeys(DENSE_REPORT_TOKEN_RE.findall(body)))
    machine = list(dict.fromkeys(MACHINE_STYLE_RE.findall(body)))
    counts = COUNT_UNIT_RE.findall(body)
    values = list(dict.fromkeys(NUMBER_RE.findall(body)))
    limits = policy().get("thresholds") or {}
    report["metrics"] = {
        "characters": len(re.sub(r"\s+", "", body)),
        "dense_tokens": dense,
        "machine_fields": machine,
        "count_units": counts,
        "number_values": values,
    }
    if len(dense) >= int(limits.get("dense_unique_tokens", 6)):
        _append(report, code="dense_report",
                message="Feedback 的实体编号、时间点或大数字过密，改成用户能理解的关键场景",
                suggestion="只保留定位问题必需的一到两个值")
    if len(machine) >= int(limits.get("machine_fields", 3)):
        _append(report, code="tool_dump",
                message="Feedback 原样堆叠了过多工具字段，改写成失败后果和处理经过",
                suggestion="保留报错原文时只取定位所需片段")
    if BARE_TEST_COUNT_RE.search(body):
        _append(report, code="bare_test_count",
                message="Feedback 不要只报测试总数，要说明测试覆盖了哪些需求行为",
                suggestion="删除总数，直接写检查了哪些输入和结果")
    if len(counts) >= int(limits.get("count_units", 4)):
        _append(report, code="count_density",
                message="Feedback 的条数、次数和项数密度过高，改写成关键动作与用户可见后果",
                suggestion="只保留解释问题必需的数量")
    elif len(values) >= int(limits.get("exact_values_warning", 4)):
        _append(report, code="value_density", severity="warning",
                message="Feedback 的精确值偏多，确认每个值都用于定位问题",
                suggestion="删除不影响理解的编号、时间和计数")
    if ABSTRACT_FINISH_RE.search(body):
        _append(report, code="abstract_finish",
                message="Feedback 用“均已实现/完成/落地”这类状态词收口，改成具体动作和结果",
                suggestion="写清谁做了什么、用户在哪里看到结果")

    abstract_errors, abstract_warnings = _abstract_problem(body, limits)
    for message in abstract_errors:
        _append(report, code="nominalization", message=message,
                suggestion="一句话只保留一个抽象判断，补上动作、条件和结果")
    for message in abstract_warnings:
        _append(report, code="nominalization", severity="warning", message=message)
    if FIXED_SEQUENCE_RE.search(body):
        _append(report, code="fixed_sequence", severity="warning",
                message="过程句使用了“先、随后、最后”的固定顺序，确认是否只是状态播报",
                suggestion="只保留真实失败、修正和最终影响")
    if AVAILABILITY_OPENING_RE.search(body):
        _append(report, code="template_opening", severity="warning",
                message="结果句用了“X 已可用：”式固定开头，改成用户实际动作或看到的结果",
                suggestion="直接从最重要的可观察结果切入")

    for sentence in re.split(r"[。；\n]", body):
        if VAGUE_FAILURE_RE.search(sentence) and not CONCRETE_FAILURE_RE.search(sentence):
            _append(report, code="vague_failure",
                    message="过程问题只写了并发场景、验证中等泛称，必须指出具体文件、步骤或动作",
                    phrase=sentence.strip(), suggestion="补上出错的对象、动作和实际结果")
            break
    if (isinstance(score, int) and not isinstance(score, bool)
            and score <= 2 and not re.search(r"(?:问题|缺口|未实现|无法|不可用|失败)", body)):
        _append(report, code="low_score_gap",
                message="2 分及以下的 Feedback 必须明确写出具体问题、缺口或不可用后果",
                suggestion="直接写用户哪一步做不成")

    report["errors"] = list({json.dumps(row, ensure_ascii=False, sort_keys=True): row
                             for row in report["errors"]}.values())
    report["warnings"] = list({json.dumps(row, ensure_ascii=False, sort_keys=True): row
                               for row in report["warnings"]}.values())
    report["state"] = "passed" if not report["errors"] else "failed"
    return report


def error_messages(text: str, score: int | None = None) -> list[str]:
    return [row["message"] for row in review_text(text, score)["errors"]]


def generation_guidance() -> list[str]:
    return [
        "- 内部术语先翻成用户动作和结果：例如把“水位”写成“上次处理到哪条记录”，把“主链路”写成“页面请求实际落到哪个接口”。",
        "- 不写“首先、其次、此外、综上、整体来看”等作文连接词，也不写“均已落地、全部完成、未发现其他缺口”这类状态收口。",
        "- 每句至少落到一个具体动作、条件或用户看到的结果；不要用三个以上抽象名词并列代替事实。",
        "- 正文通常只保留一到两个任务编号、时间、次数或错误码；测试总数不能代替行为覆盖。",
        "- 用一句自然的话写清本轮最看重的业务约束，并把原句填进 `language_review.criterion_sentence`。",
        "- 写完后先运行 build/lint；命中词表和密度规则时按报告改写，不自动换词破坏事实。",
    ]


def language_review_errors(text: str, review: object) -> list[str]:
    if not isinstance(review, dict):
        return ["缺少 language_review，需登记最看重的业务约束和术语改写"]
    errors = []
    if review.get("reviewed") is not True:
        errors.append("language_review.reviewed 必须是 true")
    criterion = squash(review.get("criterion_sentence"))
    if len(criterion) < 6:
        errors.append("language_review.criterion_sentence 太短，需写清本轮最看重的业务约束")
    elif criterion not in squash(text):
        errors.append("language_review.criterion_sentence 必须逐字出现在 Feedback 正文")
    translated = review.get("translated_terms", [])
    if not isinstance(translated, list):
        errors.append("language_review.translated_terms 必须是列表")
    else:
        for index, row in enumerate(translated, 1):
            if (not isinstance(row, dict) or not str(row.get("source") or "").strip()
                    or not str(row.get("public") or "").strip()):
                errors.append(f"language_review.translated_terms 第 {index} 项必须有 source 和 public")
    retained = review.get("retained_terms", [])
    if not isinstance(retained, list):
        errors.append("language_review.retained_terms 必须是列表")
    else:
        for index, row in enumerate(retained, 1):
            if not isinstance(row, dict) or not str(row.get("term") or "").strip():
                errors.append(f"language_review.retained_terms 第 {index} 项缺少 term")
            elif str(row["term"]) not in text:
                errors.append(f"language_review.retained_terms 第 {index} 项的 term 不在正文中")
            elif len(str(row.get("reason") or "").strip()) < 8:
                errors.append(f"language_review.retained_terms 第 {index} 项缺少保留理由")
    return errors


def write_report(task: Path, text: str, score: int | None = None, language_review=None,
                 require_review: bool = False) -> dict:
    report = review_text(text, score)
    if require_review:
        extra = language_review_errors(text, language_review)
        for message in extra:
            _append(report, code="language_review", message=message,
                    suggestion="补全 language_review 后重新检查")
        report["state"] = "passed" if not report["errors"] else "failed"
    report["language_review"] = language_review
    report["checked_at"] = lib.now_iso()
    lib.save_json(Path(task) / REPORT_FILE, report)
    return report


def provenance_errors(task: Path) -> list[str]:
    path = Path(task) / REPORT_FILE
    if path.is_symlink() or not path.is_file():
        return ["缺少当前 Feedback 的语言检查报告"]
    report = lib.load_json(path, default={})
    feedback = lib.load_json(Path(task) / "review/feedback.json", default={})
    text = str(feedback.get("feedback", "")) if isinstance(feedback, dict) else ""
    if (not isinstance(report, dict) or report.get("version") != VERSION
            or report.get("policy_version") != policy().get("version")
            or report.get("policy_sha") != policy_sha()
            or report.get("feedback_sha") != text_sha(text)
            or report.get("state") != "passed"):
        return ["Feedback 语言检查报告缺失、未通过或已过期，须重新检查"]
    return []


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="检查 Feedback 公共正文的企业黑话、空评价和机器报告感")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--text", required=True)
    check.add_argument("--score", type=int)
    check.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = review_text(args.text, args.score)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["state"])
        for row in report["errors"]:
            print("- " + row["message"])
        for row in report["warnings"]:
            print("- 提示：" + row["message"])
    return 0 if report["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
