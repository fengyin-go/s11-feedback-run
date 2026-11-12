#!/usr/bin/env python3
"""批次层面：看进度、出抽检预览页、登记抽检结论、导出及 SOLO2 平台提交。

    fbr_batch.py status    --batch b1110-01                 # 每道题走到哪一步，下一条该跑什么命令
    fbr_batch.py preview   --batch b1110-01 [--tasks …]     # 生成 preview.html：题面、改动、录屏、证据、分数与 Feedback 一页看完
    fbr_batch.py spotcheck --batch b1110-01 --task s11-000123 --state 通过|退回 --by 姓名 [--note …]
    fbr_batch.py export    --batch b1110-01                 # 白名单证据导出到 results，不复制代码
    fbr_batch.py upload    --batch b1110-01 [--tasks …] [--limit 1]  # 平台演练
    fbr_batch.py upload    --batch b1110-01 --execute --confirm SUBMIT  # 上传、建单、回读

抽检是人做的事：spotcheck 只登记人看完之后的结论，不替人下结论，所以必须写 --by。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib  # noqa: E402
import fbr_delivery as delivery  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
NEXT = {
    "run": "fbr_run.py run --batch {b} --execute",
    "collect": "fbr_run.py collect --batch {b}",
    "commit": "fbr_snapshot.py commit --batch {b}",
    "record": "fbr_record.py --batch {b}",
    "facts": "fbr_facts.py --batch {b}",
    "feedback": "fbr_feedback.py card --batch {b}，按 references/reviewer.md 调用指定验收模型后 fbr_feedback.py gate --batch {b}",
}
LABEL = {"run": "运行", "collect": "收集", "commit": "提交", "record": "录屏", "facts": "取证", "feedback": "Feedback"}
PAGE_HEAD = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>%s 抽检预览</title><style>
body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;margin:0;background:#f6f7f9;color:#1f2328}
main{max-width:1100px;margin:0 auto;padding:24px}
section{background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:18px 22px;margin:0 0 22px}
h1{font-size:20px}h2{font-size:17px;margin:0 0 8px}h3{font-size:14px;margin:16px 0 6px;color:#57606a}
pre{background:#f6f8fa;border:1px solid #d8dee4;border-radius:6px;padding:10px;overflow:auto;max-height:420px;
font:12px/1.5 Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
.score{display:inline-block;font-weight:700;padding:1px 10px;border-radius:12px;background:#ddf4ff;margin-left:8px}
.bad{color:#cf222e}.ok{color:#1a7f37}.fb{font-size:15px;background:#fffbe6;border:1px solid #f0e2a0;border-radius:6px;
padding:12px 14px}video{max-width:100%%;border:1px solid #d8dee4;border-radius:6px}
table{border-collapse:collapse;width:100%%}td,th{border:1px solid #d8dee4;padding:4px 8px;text-align:left;vertical-align:top}
nav a{margin-right:12px}</style><main><h1>%s 抽检预览</h1>"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def next_stage(task_dir: Path) -> str | None:
    for stage in lib.STAGES:
        if not lib.stage_ok(task_dir, stage):
            return stage
    return None


def status(root: Path, batch_id: str) -> list[dict]:
    rows = []
    for task_dir in lib.task_dirs(root, batch_id):
        stages = lib.load_state(task_dir)["stages"]
        stage = next_stage(task_dir)
        info = stages.get(stage, {}) if stage else {}
        detail = info.get("problems") or info.get("reason") or ""
        snapshot = lib.load_json(task_dir / "review" / "snapshot.json", default={})
        rows.append({
            "task_id": task_dir.name, "next": stage, "failed": info.get("state") == "failed",
            "detail": "；".join(detail) if isinstance(detail, list) else str(detail),
            "pushed": bool(snapshot.get("pushed")), "committed": lib.stage_ok(task_dir, "commit"),
        })
    return rows


def print_status(rows: list[dict], batch_id: str) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        label = "都做完了" if row["next"] is None else f"等{LABEL[row['next']]}"
        counts[label] = counts.get(label, 0) + 1
        line = f"{row['task_id']}  {label}"
        if row["failed"]:
            line += f"（上次没过：{lib.clip(row['detail'], 160)}）"
        if row["committed"] and not row["pushed"]:
            line += "  产物快照未推送"
        print(line)
    print("—— " + "，".join(f"{k} {v} 题" for k, v in counts.items()))
    for stage in lib.STAGES:
        if any(row["next"] == stage for row in rows):
            print(f"下一步（{LABEL[stage]}）：{NEXT[stage].format(b=batch_id)}")
    if any(row["committed"] and not row["pushed"] for row in rows):
        print(f"待推送：fbr_snapshot.py push --batch {batch_id}（演练；真推要用户明确同意后加 --execute --confirm PUBLISH）")
    if rows and all(row["next"] is None for row in rows):
        print(f"下一步：fbr_sheet.py --batch {batch_id}")


def task_section(root: Path, task_dir: Path, batch_path: Path, artifact_dir: Path | None = None) -> str:
    record = lib.load_json(artifact_dir / "task-summary.json") if artifact_dir else lib.load_task(task_dir)
    source = artifact_dir or task_dir
    facts = lib.load_json(source / "evidence" / "facts.json", default={})
    audit = lib.load_json(source / "evidence" / "audit.json", default={})
    doc = lib.load_json(source / "review" / "feedback.json", default={})
    verdict = lib.load_json(source / "review" / "gate.json", default={})
    spot = lib.load_json(source / "review" / "spotcheck.json", default={})
    name = record["task_id"]
    out = [f'<section id="{esc(name)}"><h2>{esc(name)}<span class="score">{esc(doc.get("score", "未打分"))} 分</span></h2>',
           f"<p>{esc(record.get('domain'))} / {esc(record.get('subdirection'))} / {esc(record.get('task_type'))} / "
           f"{esc(record.get('difficulty'))} / {esc(record.get('frameworks') or record.get('language'))}</p>"]
    state = "通过" if verdict.get("passed") else "没过" if verdict else "还没过闸门"
    out.append(f'<p>闸门：<b class="{"ok" if verdict.get("passed") else "bad"}">{state}</b>　抽检：'
               f"{esc(spot.get('state', '未抽检'))} {esc(spot.get('by', ''))} {esc(spot.get('note', ''))}</p>")
    for line in verdict.get("errors", []):
        out.append(f'<p class="bad">✗ {esc(line)}</p>')
    for line in verdict.get("warnings", []):
        out.append(f"<p>! {esc(line)}</p>")
    out += ["<h3>Feedback</h3>", f'<div class="fb">{esc(doc.get("feedback", "（还没写）"))}</div>',
            "<h3>任务原文</h3>", f"<pre>{esc(record['prompt'])}</pre>", "<h3>需求点与结论</h3>",
            "<table><tr><th>编号</th><th>要求</th><th>结论</th><th>说明</th></tr>"]
    judged = {str(r.get("id")): r for r in audit.get("requirements", []) if isinstance(r, dict)}
    for req in record["requirements"]:
        item = judged.get(str(req["id"]), {})
        mark = item.get("verdict", "") + (f"（{item['level']}）" if item.get("level") else "")
        out.append(f"<tr><td>{esc(req['id'])}</td><td>{esc(req.get('text'))}</td><td>{esc(mark)}</td>"
                   f"<td>{esc(item.get('note', ''))}</td></tr>")
    out.append("</table>")
    if audit.get("false_success") not in (None, "无"):
        out.append(f'<p class="bad">虚假成功（{esc(audit["false_success"])}）：{esc(audit.get("false_success_note"))}</p>')
    video = source / "record" / "recording.mp4"
    out.append("<h3>运行录屏</h3>")
    if video.is_file():
        out.append(f'<video controls preload="none" src="{esc(video.relative_to(batch_path).as_posix())}"></video>')
    else:
        out.append("<p>还没有录屏</p>")
    if facts:
        tests, check = facts["tests"], facts["verification"]
        out += ["<h3>运行结果</h3>",
                f"<p>产物测试：<b class=\"{'ok' if tests['after_ok'] else 'bad'}\">{'通过' if tests['after_ok'] else '不通过'}"
                f"</b>；新增失败 {esc('、'.join(tests['new_failures']) or '无')}；"
                f"模型最后一次改动后{'验证过' if check['verified_after_last_edit'] else '没有再验证'}</p>"]
        for index, step in enumerate(facts["demo"]["steps"], 1):
            out.append(f"<p>{index}. <code>{esc(step['cmd'])}</code> → 退出码 {esc(step.get('rc'))}</p>"
                       f"<pre>{esc((step.get('tail') or '')[-800:])}</pre>")
        if not tests["after_ok"]:
            out.append(f"<pre>{esc(tests['after'].get('test_tail', '')[-2500:])}</pre>")
        out += ["<h3>模型最后的回答</h3>", f"<pre>{esc(facts['claims']['final_answer'][:4000])}</pre>"]
    patch = source / "review" / "diff.patch"
    if artifact_dir and facts:
        changes = facts.get("changes", {})
        out += ["<h3>改动清单</h3>", "<pre>" + esc("\n".join(
            f"{f.get('status', '')} {f.get('path', '')} (+{f.get('added', 0)} / -{f.get('deleted', 0)})"
            for f in changes.get("files", []))) + "</pre>"]
    elif patch.is_file():
        text = patch.read_text(encoding="utf-8", errors="replace")
        more = f"\n…共 {len(text)} 字符，其余见 {patch.relative_to(root)}" if len(text) > 60000 else ""
        out += ["<h3>改动</h3>", f"<pre>{esc(text[:60000] + more)}</pre>"]
    out.append("</section>")
    return "\n".join(out)


def preview(root: Path, batch_id: str, wanted: str | None = None) -> Path:
    manifest = delivery.export_batch(root, batch_id)
    batch_path = delivery.delivery_dir(root, batch_id)
    items = [p for p in lib.task_dirs(root, batch_id, wanted)
             if delivery.exported_task(root, batch_id, p.name, manifest) is not None]
    if not items:
        raise lib.FbrError("还没有当前有效且已过 Feedback 闸门的任务，没什么可看的")
    parts = [PAGE_HEAD % (esc(batch_id), esc(batch_id)),
             "<nav>" + "".join(f'<a href="#{esc(p.name)}">{esc(p.name)}</a>' for p in items) + "</nav>"]
    parts += [task_section(root, task_dir, batch_path, batch_path / task_dir.name) for task_dir in items]
    parts.append("</main></html>")
    target = batch_path / "preview.html"
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


def spotcheck(root: Path, batch_id: str, task: str, state: str, by: str, note: str) -> dict:
    if state not in ("通过", "退回"):
        raise lib.FbrError("--state 只能是 通过 或 退回")
    if not by.strip():
        raise lib.FbrError("抽检是人看过之后的结论，要用 --by 写上是谁看的")
    if state == "退回" and not note.strip():
        raise lib.FbrError("退回要用 --note 写清楚原因")
    task_dir = lib.task_dirs(root, batch_id, task)[0]
    info = lib.require_stage(task_dir, "feedback")
    row = {"task_id": task_dir.name, "state": state, "by": by.strip(), "note": note.strip(),
           "feedback_sha": info.get("feedback_sha", ""), "at": lib.now_iso()}
    lib.save_json(task_dir / "review" / "spotcheck.json", row)
    if state == "退回":
        lib.set_stage(task_dir, "feedback", "failed", problems=[f"抽检退回：{note.strip()}"])
    return row


def upload_plan(root: Path, batch_id: str) -> list[dict]:
    """兼容旧调用：只列本地媒体；平台演练与提交由 fbr_submit.run_batch 执行。"""
    manifest = delivery.export_batch(root, batch_id)
    batch_path = delivery.delivery_dir(root, batch_id)
    rows = []
    for item in manifest["tasks"]:
        task_dir = batch_path / item["task_id"]
        ids = lib.load_json(task_dir / "trace" / "ids.json", default={})
        trace = task_dir / "trace" / ids.get("session_file", "-")
        video = task_dir / "record" / "recording.mp4"
        rows.append({"task_id": task_dir.name,
                     "trace": str(trace.relative_to(root)) if trace.is_file() else "",
                     "video": str(video.relative_to(root)) if video.is_file() else ""})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 批次进度、抽检预览、交付导出与平台提交")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "preview", "spotcheck", "upload", "export"):
        sub.add_parser(name).add_argument("--batch", required=True)
    sub.choices["preview"].add_argument("--tasks")
    upload = sub.choices["upload"]
    upload.add_argument("--tasks")
    upload.add_argument("--limit", type=int)
    upload.add_argument("--execute", action="store_true")
    upload.add_argument("--confirm", default="")
    check = sub.choices["spotcheck"]
    check.add_argument("--task", required=True)
    check.add_argument("--state", required=True)
    check.add_argument("--by", default="")
    check.add_argument("--note", default="")
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        if args.command == "status":
            print_status(status(root, args.batch), args.batch)
        elif args.command == "preview":
            print(preview(root, args.batch, args.tasks))
        elif args.command == "spotcheck":
            row = spotcheck(root, args.batch, args.task, args.state, args.by, args.note)
            print(f"{row['task_id']}  抽检{row['state']}  {row['by']}")
        elif args.command == "export":
            manifest = delivery.export_batch(root, args.batch)
            print(delivery.delivery_dir(root, args.batch) / "delivery-manifest.json")
            print(f"导出 {len(manifest['tasks'])} 题，排除 {len(manifest['excluded'])} 题；没有复制 workspace 或 .git")
            for row in manifest["excluded"]:
                print(f"{row['task_id']}  未导出：{row['reason']}")
        else:
            import fbr_submit
            result = fbr_submit.run_batch(root, args.batch, tasks=args.tasks, limit=args.limit,
                                          execute=args.execute, confirm=args.confirm)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
