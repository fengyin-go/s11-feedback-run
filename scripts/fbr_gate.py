#!/usr/bin/env python3
"""打分与 Feedback 的闸门：纯检查函数，不读写任务目录，不调外部命令。

检查对象是 agent 写的两份文件：
- evidence/audit.json   逐条需求的结论（做到 / 有问题 / 看不出来）+ 锚点、实跑过的反例、虚假成功判断
- review/feedback.json  分数、Feedback 正文、过程句与问题句各自的锚点

每个检查返回 (errors, warnings)。errors 不清零就不进结果表。
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

import fbrlib as lib

VERDICTS = ("做到", "有问题", "看不出来")
LEVELS = ("未实现", "明显", "细节")
FALSE_SUCCESS = {"无": 5, "轻微": 3, "较大": 2, "严重": 1}
ANCHOR_TYPES = ("file", "tree", "missing", "step", "log", "value", "req", "test", "probe", "demo")

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
INTERNAL_WORDS = ["触发点在", "实际行为是", "后果是", "业务影响是", "反例", "逐条核对", "核对依据", "本侧", "该侧", "锚点",
                  "题面", "素材卡", "facts.json", "audit.json"]
TOPIC_WORDS = ["这道题", "该题", "本题", "此题"]
HARNESS_WORDS = ["容器", "镜像", "挂载", "docker", "harness", "录屏", "录制", "视频", "截图"]
SCOPE_WORDS = ["网关", "限流", "额度", "余额", "api error", "网络错误", "网络故障", "网络超时", "断网", "推理时长",
               "思考时间", "响应慢", "速度慢", "耗时过长", "戛然而止"]
OTHER_MACHINE_WORDS = ["其他机器", "别的机器", "换一台机器", "另一台机器", "其他环境下", "别人的电脑"]
BANNED_MARKS = ["“", "”", "‘", "’", "《", "》", "「", "」", "『", "』", "——", "—", "……", "…", "**", "`"]
SUBJECT_RE = re.compile(r"(?:^|[。；！？\n])\s*(这条|这一版|这份交付|本次交付|该交付|这次交付)")
SCORE_TALK_RE = re.compile(r"[给打评得判][了为到出]?\s*[1-5一二三四五]\s*分|扣分|满分|得分|评分|分数|档位")
SELF_RE = re.compile(r"我们?|本人|笔者")
FAIL_WORDS_RE = re.compile(r"不通过|没通过|未通过|没过|失败|报错|跑不起来|起不来|编译不过|无法运行|无法启动|无法编译")
# 「没有报错」「不会失败」是在说没出问题，不能当成写了失败
NEGATED_FAIL_RE = re.compile(r"(?:没有|不再|不会|没|未|无|不)(?:报错|失败)")
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
        "tree": set(facts.get("tree_files", [])),
        "steps": {s["step"] for s in facts["trace"]["steps"]},
        "req_ids": {str(r["id"]) for r in task["requirements"]},
        "failed_tests": set(tests["after"].get("failed_names", [])) | set(tests.get("new_failures", [])),
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
        path = str(anchor.get("path", "")).lstrip("./")
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


def valid_counterexamples(audit: dict, ev: dict) -> list[dict]:
    valid, seen = [], set()
    for item in audit.get("counterexamples") or []:
        if not isinstance(item, dict) or not isinstance(item.get("found_problem"), bool):
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
    examples = valid_counterexamples(audit, ev)
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
    if score <= 2 and not (not tests["after_runnable"] or "未实现" in levels or fake in ("较大", "严重")):
        errors.append(f"分数 {score} 偏低：没有跑不起来、未实现或较大的虚假成功")
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

def check_text(text: str, prompt: str, gate: dict) -> tuple[list[str], list[str]]:
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

    for label, found in (
        ("套话或 AI 腔的词", hits(AI_WORDS)),
        ("没法核验的评价", hits(VAGUE_WORDS)),
        ("内部口径词", hits(INTERNAL_WORDS)),
        ("指代题目的说法", hits(TOPIC_WORDS)),
        ("运行环境或录屏现场的词（任务本身没提到）", hits(HARNESS_WORDS, True)),
        ("网络、额度、耗时这类不该算在模型头上的因素", hits(SCOPE_WORDS, True)),
        ("跨机器的说法", hits(OTHER_MACHINE_WORDS)),
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

    for index, item in enumerate(issues, 1):
        where = f"第 {index} 个问题句"
        if not isinstance(item, dict):
            errors.append(f"{where}格式不对")
            continue
        sentence = squash(str(item.get("sentence") or ""))
        if len(sentence) < 6 or sentence not in flat:
            errors.append(f"{where}：sentence 要是正文里的原句")
        for key, label in (("trigger", "触发节点"), ("behavior", "模型实际行为"), ("impact", "业务影响")):
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
        conversation = isinstance(item, dict) and item.get("source") == "conversation" and not ev["steps"] and steps == []
        if (not isinstance(steps, list) or not steps) and not conversation:
            errors.append(f"{where}：要写它对应轨迹里的哪几步（steps）")
        else:
            errors += [f"{where}：轨迹里没有第 {s} 步" for s in steps if s not in ev["steps"]]
    if not process:
        errors.append("过程和产物都要写：正文里至少有一句讲过程，并在 process 里标出对应的轨迹步骤")

    if isinstance(score, int) and not isinstance(score, bool):
        if score == 5 and issues:
            errors.append("5 分不写缺口：issues 应当为空")
        verification_only = (score == 4 and isinstance(doc.get("score_basis"), dict)
                             and doc["score_basis"].get("category") == "verification_gap"
                             and all(r.get("verdict") in ("做到", "看不出来") for r in audit.get("requirements", []))
                             and audit.get("false_success") == "无"
                             and not facts["tests"]["regression"]
                             and not any(c.get("found_problem") for c in audit.get("counterexamples", [])))
        if score < 5 and not issues and not verification_only:
            errors.append(f"{score} 分至少要有一处问题句")
        if score >= 4 and len(re.findall(r"[A-Za-z_][\w./-]{2,}|\d+", text, flags=re.A)) < 2:
            errors.append("4 到 5 分也要写出核对过的要求和实际验证结果，正文里看不到具体的命令、文件或数值")
    if facts["tests"]["regression"] and not FAIL_WORDS_RE.search(NEGATED_FAIL_RE.sub("", text)):
        errors.append("产物上构建或测试不通过，正文必须明写")
    covered = {str(a.get("id")) for item in issues if isinstance(item, dict)
               for a in (item.get("anchors") or []) if isinstance(a, dict) and a.get("type") == "req"}
    for req in audit.get("requirements", []):
        if isinstance(req, dict) and req.get("verdict") == "有问题" and req.get("level") in ("未实现", "明显") \
                and str(req.get("id")) not in covered:
            errors.append(f"需求点 {req.get('id')} 判了{req.get('level')}，正文里要有对应的问题句（anchors 里带 req）")

    for found in PATH_RE.finditer(text):
        token = found.group(1).lstrip("./")
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
