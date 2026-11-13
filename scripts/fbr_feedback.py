#!/usr/bin/env python3
"""R6 打分与 Feedback：给当前任务 AI 备料、实跑反例、核对模型回执与证据闸门。

    fbr_feedback.py card  --batch b1110-01 [--tasks …]                      # 生成 review/card.md
    fbr_feedback.py probe --batch b1110-01 --task s11-000123 --cmd '…'      # 在产物提交上实跑一条命令（起容器）
    fbr_feedback.py gate  --batch b1110-01 [--tasks …]                      # 检查 audit.json 和 feedback.json

当前任务 AI 生成候选并逐句复核，fbr_reviewer.py finalize 校验后保存：
- evidence/audit.json   逐条需求的结论 + 锚点、实跑过的反例、虚假成功判断
- review/feedback.json  score、feedback 正文、issues（问题句）、process（过程句）、note（进备注列）
闸门通过才把 feedback 阶段记成 ok，结果表只收通过的。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib  # noqa: E402
import fbr_gate as gate  # noqa: E402
import fbr_reviewer as reviewer  # noqa: E402

AUDIT_TEMPLATE = {
    "requirements": [{"id": "R1", "verdict": "做到 | 有问题 | 看不出来", "level": "未实现 | 明显 | 细节（只在有问题时写）",
                      "note": "不是做到时写清楚是什么情况", "anchors": [{"type": "file", "path": "src/x.py"}]}],
    "counterexamples": [{"what": "试了什么边界", "probe": "P1", "found_problem": False}],
    "false_success": "无 | 轻微 | 较大 | 严重", "false_success_note": "", "false_success_anchors": [],
}
FEEDBACK_TEMPLATE = {
    "score": 4, "feedback": "正文",
    "results": [{"sentence": "正文中描述最终行为的原句", "requirement_ids": ["R1"],
                 "anchors": [{"type": "demo", "index": 1}]}],
    "score_basis": {"category": "delivery_issue | verification_gap | complete",
                    "delivered": "哪些主要要求实际做到", "remaining": "真实缺口、验证边界或已验证完整范围",
                    "why_this_score": "结合最终交付影响解释本档，不能按过程出错次数扣分",
                    "lowest_supported_score": 4,
                    "lower_score_rejected": "说明再低一档为什么缺少确凿证据；1 分写已到最低档",
                    "anchors": [{"type": "demo", "index": 1}]},
    "test_claims": [{"sentence": "正文涉及测试的完整分句，没有则整个列表为空", "requirement_ids": ["R1"],
                     "behavior": "该测试实际检查的本题行为", "source": {"kind": "probe", "id": "P1"},
                     "output_excerpt": "该次执行的真实输出摘录",
                     "count": None, "count_quote": "仅当正文提到数量时给出原始计数行"}],
    "issues": [{"sentence": "正文里的原句", "trigger": "在哪一步、什么条件下出的问题", "behavior": "模型实际做了什么",
                "impact": "对使用的人意味着什么", "anchors": [{"type": "req", "id": "R2"}, {"type": "step", "step": 31}]}],
    "process": [{"sentence": "正文里讲过程的原句", "steps": [12, 13]}],
    "process_audit": [{"sentence": "与 process 完全一致的过程句", "subject": "model", "verdict": "supported",
                       "checks": {"full_trace": True, "baseline": True, "timeline": True,
                                  "source_boundary": True, "scope": True},
                       "note": "说明已回查哪些原始步骤、基线及后续最终状态"}],
    "note": "",
}
ANCHOR_HELP = """锚点写法（每个都会被核对，对不上就过不了闸门）：
  {"type": "file", "path": "src/a.py"}        本次改动里的文件
  {"type": "tree", "path": "src/b.py"}        产物里存在的文件（没改也行）
  {"type": "missing", "path": "docs/x.md"}    产物里不存在的文件
  {"type": "step", "step": 31}                轨迹的第 31 步
  {"type": "log", "text": "报错原文"}          测试、演示、轨迹、实跑记录里的原文片段
  {"type": "value", "text": "37"}             证据里出现过的数值或字符串
  {"type": "req", "id": "R2"}                 需求点
  {"type": "test", "name": "失败用例名"}       产物上失败的用例
  {"type": "probe", "id": "P1"}               实跑记录
  {"type": "demo", "index": 2}                演示的第 2 条命令"""


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_probes(task_dir: Path) -> list[dict]:
    return lib.read_jsonl(Path(task_dir) / "evidence" / "probes.jsonl")


def load_evidence(task_dir: Path) -> tuple[dict, dict]:
    record = lib.load_task(task_dir)
    lib.require_stage(task_dir, "facts")
    facts = lib.load_json(Path(task_dir) / "evidence" / "facts.json")
    if facts.get('source_identity'):
        import fbr_facts
        if facts['source_identity'] != fbr_facts.source_identity(Path(task_dir)):
            raise lib.FbrError('事实卡来源已变化，先重新取证，不能使用过期材料')
    import fbr_quality
    return record, fbr_quality.enrich_test_logs(Path(task_dir), facts)


# ---------- card ----------

def build_card(record: dict, facts: dict, probes: list[dict]) -> str:
    tests, changes, check, claims = facts["tests"], facts["changes"], facts["verification"], facts["claims"]
    out = [f"# {record['task_id']} 素材卡", "",
           "只是材料，不是结论。分数和正文要自己看过改动、轨迹和运行结果再写；这里没有的事不要写。有中断时必须综合首轮及所有续写的轨迹、回答和最终产物，填表标识仍取第一轮。", "",
           "## 任务原文", "", record["prompt"].strip(), "", "## 需求点", ""]
    for req in record["requirements"]:
        out.append(f"- {req['id']}：{req.get('text', '')}（怎么验：{req.get('check', '') or '未写'}）")

    out += ["", "## 构建与测试（任务镜像新容器，干净克隆）", "",
            f"- 命令：`{tests['setup_cmd']}` 然后 `{tests['test_cmd']}`"]
    for label, side in (("初始提交", tests["before"]), ("产物提交", tests["after"])):
        out.append(f"- {label}：setup 退出码 {side['setup_rc']}，test 退出码 {side['test_rc']}，"
                   f"通过 {side.get('passed')}，失败 {side.get('failed')}")
    out.append(f"- 新增失败：{'、'.join(tests['new_failures']) or '无'}；新修好：{'、'.join(tests['fixed']) or '无'}")
    out.append(f"- 结论口径：产物测试{'通过' if tests['after_ok'] else '不通过'}，"
               f"{'跑得起来' if tests['after_runnable'] else '跑不起来'}，"
               f"{'算在这次改动头上' if tests['regression'] else '不算这次改动造成的失败'}")
    if not tests["after_ok"]:
        out += ["", "产物上测试输出的末尾：", "", "```", lib.clip(tests["after"].get("test_tail", "")[-1800:], 1800)
                or lib.clip(tests["after"].get("setup_tail", ""), 1200), "```"]

    out += ["", "## 演示命令（录屏时的实际结果）", ""]
    if not facts["demo"]["setup_ok"]:
        out.append("- 准备命令没成功：" + lib.clip(facts["demo"].get("setup_tail", ""), 300))
    for index, step in enumerate(facts["demo"]["steps"], 1):
        state = "没跑到" if step.get("skipped") else f"退出码 {step.get('rc')}" + ("，被截断" if step.get("cut") else "")
        out += [f"{index}. `{step['cmd']}` → {state}；要看的：{step.get('show') or '未写'}", "", "```",
                lib.clip((step.get("tail") or "")[-600:], 600), "```"]

    out += ["", "## 改动", "",
            f"- {changes['totals']['files']} 个文件，+{changes['totals']['added']} / -{changes['totals']['deleted']}；"
            f"模型自己提交 {changes.get('model_commits', 0)} 次"
            + ("；历史被改写过" if changes.get("history_rewritten") else "")
            + ("；相对初始提交没有任何改动" if changes["no_change"] else "")]
    for item in changes["files"][:60]:
        out.append(f"  - {item['status']} {item['path']} (+{item['added']} / -{item['deleted']})"
                   + (" [测试]" if item["is_test"] else ""))
    for label, key in (("被删的测试", "tests_deleted"), ("删得比加得多的测试", "tests_weakened")):
        if changes.get(key):
            out.append(f"- {label}：{'、'.join(changes[key])}")
    for label, key in (("新加的跳过标记", "skips_added"), ("新增的 TODO 或占位", "placeholders")):
        for row in changes.get(key, [])[:10]:
            out.append(f"- {label}：{row['file']}：{row['line']}")
    out.append("- 完整 diff 在 review/diff.patch")

    out += ["", "## 过程（轨迹）", "",
            f"- 共 {facts['trace']['counts']['tools']} 步，其中报错 {facts['trace']['counts']['errors']} 步，"
            f"改文件 {facts['trace']['counts']['edits']} 步",
            f"- 模型自己跑过测试：{'是' if check['tests_run'] else '否'}；最后一次改动在第 {check['last_edit_step']} 步，"
            f"之后{'又验证过' if check['verified_after_last_edit'] else '没有再验证'}；"
            f"最后一次检查{'通过' if check['last_check_passed'] else '没通过' if check['checks'] else '不存在'}"]
    for row in check["checks"][-12:]:
        out.append(f"  - 第 {row['step']} 步 {row['kind']}：`{lib.clip(row['cmd'], 100)}` → "
                   f"{'通过' if row['passed'] else '没通过'}")
    out += ["", "步骤索引（编号就是 step 锚点用的号）：", ""]
    steps = facts["trace"]["steps"]
    for step in steps[:400]:
        flag = "  ✗ " + lib.clip(step.get("error", ""), 120) if step.get("is_error") else ""
        out.append(f"  {step['step']:>3} {step['tool']:<6} {lib.clip(step.get('target', ''), 110)}{flag}")
    if len(steps) > 400:
        out.append(f"  …其余 {len(steps) - 400} 步见 evidence/facts.json")

    if facts["trace"].get("continuations"):
        out += ["", "## 中断续写（综合以下全部回答与完整轨迹评价，表中 ID 仍取首轮）", ""]
        for index, answer in enumerate(facts["trace"].get("assistant_answers", []), 1):
            out += [f"### 助手文本 {index}", "", "```", answer, "```", ""]
    out += ["", "## 模型最后的回答", "", "```", lib.clip(claims["final_answer"], 3000), "```"]
    if claims["mentioned_missing"]:
        out.append("- 回答里提到、产物里没有的文件：" + "、".join(claims["mentioned_missing"]))
    if claims["mentioned_unchanged"]:
        out.append("- 回答里提到、这次没改的文件：" + "、".join(claims["mentioned_unchanged"]))

    out += ["", "## 实跑记录", ""]
    if not probes:
        out.append("还没有。验证需求点和试边界用 `fbr_feedback.py probe`，每跑一条这里多一行。")
    for row in probes:
        out += [f"- {row['id']}：`{lib.clip(row['cmd'], 160)}` → 退出码 {row['rc']}", "", "```",
                lib.clip((row.get("tail") or "")[-500:], 500), "```"]

    out += ["", "## 要写的两份文件", "", "evidence/audit.json：", "", "```json",
            json.dumps(AUDIT_TEMPLATE, ensure_ascii=False, indent=2), "```", "", "review/feedback.json：", "", "```json",
            json.dumps(FEEDBACK_TEMPLATE, ensure_ascii=False, indent=2), "```", "", ANCHOR_HELP, "",
            "打分口径见 references/scoring.md，正文写法见 references/feedback-writing.md。"]
    return "\n".join(out) + "\n"


@lib.task_locked
def card_one(task_dir: Path) -> dict:
    record, facts = load_evidence(task_dir)
    text = build_card(record, facts, load_probes(task_dir))
    (task_dir / "review").mkdir(exist_ok=True)
    (task_dir / "review" / "card.md").write_text(text, encoding="utf-8")
    return {"task_id": record["task_id"], "state": "ok", "detail": "review/card.md"}


# ---------- probe ----------

@lib.task_locked
def probe_one(task_dir: Path, cfg: dict, cmd: str, timeout: int, with_setup: bool = True) -> dict:
    """在产物提交的干净克隆上跑一条命令，结果追加到 evidence/probes.jsonl。会起一个任务镜像的新容器。"""
    return probe_many(task_dir, cfg, [{'cmd':cmd,'timeout':timeout}], with_setup=with_setup)[0]


def probe_many(task_dir, cfg, requests, with_setup=True):
    record, facts = load_evidence(task_dir)
    if not requests or any(not r.get('cmd','').strip() for r in requests):
        raise lib.FbrError('probe 请求缺少明确命令')
    lib.restore_git_config(task_dir)
    # Commands share setup only when they explicitly declare that they do not mutate the environment.
    if len(requests) > 1 and not all(r.get('read_only') is True for r in requests):
        return [probe_many(task_dir,cfg,[r],with_setup)[0] for r in requests]
    clone = lib.clean_clone(task_dir/'workspace', facts['commit'], f"s11-probe-{record['task_id']}")
    steps = [{'name':'probe' if len(requests)==1 else f'probe-{i}', 'cmd':r['cmd'], 'timeout':r.get('timeout',180)} for i,r in enumerate(requests)]
    if with_setup:
        steps.insert(0, {'name':'setup','cmd':cfg['facts']['setup_cmd'],'timeout':cfg['facts']['setup_timeout']})
    # stop_on_fail retains setup failure semantics; independent probes must not suppress one another.
    rows = lib.run_steps_in_image(record['image'],clone,steps,tail=80)
    setup_rc = rows[0]['rc'] if with_setup else None
    outputs=[]
    for request,done in zip(requests,rows[1:] if with_setup else rows):
        row = {'id':f'P{len(load_probes(task_dir))+1}','cmd':request['cmd'],'rc':done['rc'],'seconds':done['seconds'],
               'tail':lib.redact(done['tail'][-3000:],[lib.read_key(cfg)]),'setup_rc':setup_rc,'commit':facts['commit'],
               'at':lib.now_iso(),'container_name':done.get('container_name',''),'clone':str(clone)}
        if done.get('output_file'):
            import shutil
            target=task_dir/'evidence/command-logs/probes'/f"{row['id']}.log"
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(done['output_file'],target)
            row.update(output_file=str(target.relative_to(task_dir)),output_sha256=done.get('output_sha256'))
        lib.append_jsonl(task_dir/'evidence/probes.jsonl',row);outputs.append(row)
    return outputs


# ---------- gate ----------

@lib.task_locked
def gate_task(task_dir: Path, cfg: dict) -> dict:
    """单题检查（不含查重）。返回 {task_id, ready, errors, warnings, score, text}。"""
    record, facts = load_evidence(task_dir)
    out = {"task_id": record["task_id"], "ready": False, "errors": [], "warnings": [], "score": None, "text": "",
           "commit": facts["commit"]}
    if facts['demo'].get('failure_kind') in ('environment', 'unresolved'):
        out['errors'].append('准备失败原因属于环境或未查清，先补证/恢复环境，不据此认定功能失败')
    audit_path, doc_path = task_dir / "evidence" / "audit.json", task_dir / "review" / "feedback.json"
    missing = [str(p.relative_to(task_dir)) for p in (audit_path, doc_path) if not p.is_file()]
    if missing:
        out["errors"].append("还没写：" + "、".join(missing))
        return out
    audit, doc = lib.load_json(audit_path), lib.load_json(doc_path)
    if not isinstance(audit, dict) or not isinstance(doc, dict):
        out["errors"].append("audit.json 和 feedback.json 都要是 JSON 对象")
        return out
    out.update(ready=True, score=doc.get("score"), text=str(doc.get("feedback", "")))
    out["errors"] += reviewer.provenance_errors(task_dir)
    import fbr_quality
    out["errors"] += fbr_quality.check_contract(doc, record, facts, audit, load_probes(task_dir))
    conf = cfg["feedback_gate"]
    ev = gate.build_evidence(record, facts, audit, load_probes(task_dir))
    for errors, warnings in (
        gate.check_audit(audit, record, ev),
        gate.check_score(doc.get("score"), facts, audit, ev, conf),
        gate.check_text(out["text"], record["prompt"], conf),
        gate.check_feedback(doc, record, facts, audit, ev, conf),
    ):
        out["errors"] += errors
        out["warnings"] += warnings
    return out


def other_feedback(root: Path, batch_id: str) -> list[dict]:
    """查重要比到的历史数据：其他批次里已过闸门的，加上 _registry/delivered-feedback.jsonl 里登记的。"""
    rows = [r for r in lib.read_jsonl(root / "_registry" / "delivered-feedback.jsonl") if r.get("feedback")]
    for path in sorted((root / "batches").glob("b*/*/review/feedback.json")):
        task_dir = path.parent.parent
        if not lib.TASK_ID_RE.fullmatch(task_dir.name) or task_dir.parent.name == batch_id or not lib.stage_ok(task_dir, "feedback"):
            continue
        doc = lib.load_json(path, default={})
        if isinstance(doc, dict) and doc.get("feedback"):
            rows.append({"task_id": task_dir.name, "feedback": str(doc["feedback"])})
    return rows


def gate_batch(root: Path, batch_id: str, cfg: dict, wanted: str | None = None) -> tuple[list[dict], list[str]]:
    from contextlib import ExitStack
    with lib.file_lock(root / '_registry/gate.lock'), ExitStack() as locks:
        for task in sorted(lib.task_dirs(root, batch_id, wanted)):
            locks.enter_context(lib.file_lock(task / '.operation.lock'))
        return _gate_batch(root, batch_id, cfg, wanted)


def _gate_batch(root: Path, batch_id: str, cfg: dict, wanted: str | None = None) -> tuple[list[dict], list[str]]:
    todo = [p for p in lib.task_dirs(root, batch_id, wanted) if lib.stage_ok(p, "facts")]
    results = {p.name: (p, gate_task(p, cfg)) for p in todo}
    # 查重看整批：这次没点到的任务，只要写了正文也参与比对
    records = {}
    for path in lib.task_dirs(root, batch_id):
        doc = lib.load_json(path / "review" / "feedback.json", default={})
        if isinstance(doc, dict) and str(doc.get("feedback", "")).strip():
            records[path.name] = str(doc["feedback"])
    conf = cfg["feedback_gate"]
    duplicates, _ = gate.check_duplicates(records, other_feedback(root, batch_id), conf)
    rows = []
    for name, (task_dir, result) in results.items():
        if result["ready"]:
            result["errors"] += duplicates.get(name, [])
            spot = lib.load_json(task_dir / "review" / "spotcheck.json", default={})
            if spot.get("state") == "退回" and spot.get("feedback_sha") == sha(result["text"]):
                result["errors"].append(f"抽检退回过（{spot.get('note', '')}），正文还没改")
        passed = result["ready"] and not result["errors"]
        lib.save_json(task_dir / "review" / "gate.json", {
            "task_id": name, "passed": passed, "score": result["score"], "errors": result["errors"],
            "warnings": result["warnings"], "feedback_sha": sha(result["text"]), "commit": result["commit"],
            "checked_at": lib.now_iso()})
        if passed:
            lib.set_stage(task_dir, "feedback", "ok", score=result["score"], feedback_sha=sha(result["text"]))
        elif result["ready"] or lib.load_state(task_dir)["stages"].get("feedback"):
            lib.set_stage(task_dir, "feedback", "failed", problems=result["errors"][:20])
        rows.append({"task_id": name, "state": "ok" if passed else ("failed" if result["ready"] else "todo"),
                     "score": result["score"], "errors": result["errors"], "warnings": result["warnings"]})
    scores = []
    for path in lib.task_dirs(root, batch_id):
        info = lib.load_state(path)["stages"].get("feedback", {})
        if info.get("state") == "ok" and isinstance(info.get("score"), int):
            scores.append(info["score"])
    note = gate.five_ratio_note(scores, conf)
    return rows, [note] if note else []


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 打分与 Feedback")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("card", "gate"):
        item = sub.add_parser(name)
        item.add_argument("--batch", required=True)
        item.add_argument("--tasks")
    probe = sub.add_parser("probe")
    probe.add_argument("--batch", required=True)
    probe.add_argument("--task", required=True)
    probe.add_argument("--cmd", required=True)
    probe.add_argument("--timeout", type=int, default=180)
    probe.add_argument("--no-setup", action="store_true")
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        cfg = lib.settings(root)
        if args.command == "probe":
            task_dir = lib.task_dirs(root, args.batch, args.task)[0]
            row = probe_one(task_dir, cfg, args.cmd, args.timeout, not args.no_setup)
            print(f"{row['id']}  退出码 {row['rc']}  {row['seconds']} 秒")
            print(row["tail"])
            return 0
        if args.command == "card":
            failed = 0
            for task_dir in [p for p in lib.task_dirs(root, args.batch, args.tasks) if lib.stage_ok(p, "facts")]:
                try:
                    row = card_one(task_dir)
                except lib.FbrError as exc:
                    row, failed = {"task_id": task_dir.name, "state": "failed", "detail": str(exc)}, failed + 1
                print(f"{row['task_id']}  {row['state']}  {row['detail']}", flush=True)
            return 1 if failed else 0
        rows, notes = gate_batch(root, args.batch, cfg, args.tasks)
        for row in rows:
            label = {"ok": "通过", "failed": "没过", "todo": "待写"}[row["state"]]
            print(f"{row['task_id']}  {label}  分数 {row['score']}")
            for line in row["errors"]:
                print(f"    ✗ {line}")
            for line in row["warnings"]:
                print(f"    ! {line}")
        for line in notes:
            print(f"提示：{line}")
        done = sum(1 for r in rows if r["state"] == "ok")
        print(f"共 {len(rows)} 题，通过 {done} 题")
        return 0 if done == len(rows) else 1
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
