#!/usr/bin/env python3
"""R7 结果表：把过了闸门的任务汇成 results/<批次>/result.xlsx。

    fbr_sheet.py --batch b1110-01

- 「结果」页：保留本地 21 列兼容格式，后面是内部列。只收 Feedback 过了闸门、且过闸门之后正文没再改过的任务。
- 「进度」页：批次里每道题走到哪个阶段、卡在什么地方。
- 表是从各任务目录的 JSON 重新生成的，直接改表里的内容下次会被覆盖；要改就改源文件再生成。
- 在线平台可通过 fbr_submit.py 直接提交；有效回执回填远端轨迹、录屏及实际提交账号和时间。
- 未提交或回执失效时「轨迹文件」「运行录屏」填本地相对路径（相对工作目录）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbr_trace
import fbrlib as lib  # noqa: E402
import fbr_delivery as delivery  # noqa: E402
import fbr_reviewer as reviewer  # noqa: E402

PUBLIC_COLUMNS = ["User Prompt", "所属领域", "子方向", "任务类型", "任务难度", "语言/框架", "Harness", "Harness 版本",
                  "操作系统", "环境可复现等级", "初始环境快照", "SessionID", "TurnID/PromptID", "轨迹文件", "产物快照",
                  "运行录屏", "交付完整性", "Feedback", "备注", "提交人", "提交时间"]
INTERNAL_COLUMNS = ["任务编号", "批次", "底座仓库", "任务仓库", "运行开始", "运行结束", "提交截止", "尝试次数", "证据路径",
                    "闸门状态", "抽检状态", "数据状态", "上传状态"]
OPTIONAL = {"备注", "提交人", "提交时间"}
OPTIONS = {
    "所属领域": ["安全", "游戏、图形与多媒体", "数据工程"],
    "子方向": ["身份认证", "网络与通信安全", "系统安全", "漏洞检测与攻防", "其他", "游戏开发", "图形渲染",
            "音视频处理", "图像处理", "数据采集、爬虫与 RPA", "数仓与数据湖", "数据分析与数据应用"],
    "任务类型": ["功能开发", "Bugfix", "代码重构", "功能优化", "依赖升级与迁移", "质量保障", "项目维护", "技术讨论",
              "问题排查与诊断", "部署运维", "泛Agent需求"],
    "任务难度": ["困难", "地狱"],
    "环境可复现等级": ["无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境"],
    "操作系统": ["MacOS/Linux"],
}
SNAPSHOT_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/commit/[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
WIDTHS = {"User Prompt": 60, "Feedback": 70, "备注": 30, "初始环境快照": 36, "产物快照": 36, "轨迹文件": 36,
          "运行录屏": 36, "SessionID": 24, "TurnID/PromptID": 24, "证据路径": 30, "数据状态": 30}
STAGE_LABEL = {"run": "运行", "collect": "收集", "commit": "提交", "record": "录屏", "facts": "取证", "feedback": "Feedback"}


def parse_time(text: str) -> datetime | None:
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text or "", pattern)
        except ValueError:
            continue
    return None


def show_time(text: str) -> str:
    moment = parse_time(text)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else ""


def deadline(ended: str, cutoff: str) -> str:
    """提交截止：运行结束当天的截止时间；已经过了就顺延到第二天。"""
    moment = parse_time(ended)
    if not moment:
        return ""
    hour, minute = (int(v) for v in cutoff.split(":"))
    limit = moment.replace(hour=hour, minute=minute, second=0)
    if moment > limit:
        limit += timedelta(days=1)
    return limit.strftime("%Y-%m-%d %H:%M")


def canonical_field(name: str, value):
    """仅映射实时平台已证实的等价值；不猜测未知枚举。"""
    aliases = {
        "操作系统": {"Linux arm64": "MacOS/Linux", "Linux amd64": "MacOS/Linux",
                   "Linux": "MacOS/Linux", "MacOS": "MacOS/Linux"},
        "子方向": {"数据采集、爬虫与RPA": "数据采集、爬虫与 RPA"},
    }
    return aliases.get(name, {}).get(value, value)


def build_note(doc: dict, snapshot: dict, recording: dict, attempts: list[dict]) -> str:
    """工程说明保留在内部证据，不能进入对外备注。"""
    return ""


def build_row(root: Path, task_dir: Path, cfg: dict, *, delivery_manifest: dict | None = None) -> tuple[dict | None, str]:
    """返回 (一行数据, 没进结果页的原因)。"""
    record = lib.load_task(task_dir)
    stages = lib.load_state(task_dir)["stages"]
    info = stages.get("feedback", {})
    if info.get("state") != "ok":
        return None, "Feedback 还没过闸门"
    doc = lib.load_json(task_dir / "review" / "feedback.json", default={})
    text = str(doc.get("feedback", "")).strip()
    if hashlib.sha256(str(doc.get("feedback", "")).encode("utf-8")).hexdigest() != info.get("feedback_sha"):
        return None, "过闸门之后正文改过，要重新过一遍"
    if doc.get("score") != info.get("score"):
        return None, "过闸门之后分数改过，要重新过一遍"
    problems = reviewer.provenance_errors(task_dir)
    if problems:
        return None, problems[0]
    artifact_dir = task_dir
    if delivery_manifest is not None:
        artifact_dir = delivery.exported_task(root, task_dir.parent.name, task_dir.name, delivery_manifest)
        if artifact_dir is None:
            excluded = {r["task_id"]: r["reason"] for r in delivery_manifest.get("excluded", [])}
            return None, excluded.get(task_dir.name, "任务未进入当前交付清单")
    ids = lib.load_json(task_dir / "trace" / "ids.json", default={})
    snapshot = lib.load_json(task_dir / "review" / "snapshot.json", default={})
    recording = lib.load_json(task_dir / "record" / "recording.json", default={})
    attempts = lib.load_json(task_dir / "run" / "attempts.json", default=[])
    spot = lib.load_json(task_dir / "review" / "spotcheck.json", default={})
    if spot.get("feedback_sha") != info.get("feedback_sha"):
        spot = {}                       # 抽检看的不是现在这一版正文，不算数
    run = stages.get("run", {})
    rel = artifact_dir.relative_to(root)
    trace_file = rel / "trace" / ids.get("session_file", "") if ids.get("session_file") else None
    video = rel / "record" / "recording.mp4"
    row = {
        "User Prompt": record["prompt"], "所属领域": record.get("domain", ""), "子方向": canonical_field("子方向", record.get("subdirection", "")),
        "任务类型": record.get("task_type", ""), "任务难度": record.get("difficulty", ""),
        "语言/框架": record.get("frameworks") or record.get("language", ""),
        "Harness": record.get("harness") or cfg["harness"],
        "Harness 版本": str(ids.get("cli_version") or record.get("harness_version") or cfg["harness_version"]),
        "操作系统": canonical_field("操作系统", record.get("os") or cfg["os"]), "环境可复现等级": record.get("repro_level", ""),
        "初始环境快照": (record.get("publish") or {}).get("initial_snapshot") or "",
        "SessionID": ids.get("session_id", ""), "TurnID/PromptID": ids.get("prompt_id", ""),
        "轨迹文件": str(trace_file) if trace_file and (root / trace_file).is_file() else "",
        "产物快照": snapshot.get("artifact_snapshot", "") if snapshot.get("pushed") else "",
        "运行录屏": str(video) if (root / video).is_file() else "",
        "交付完整性": doc.get("score"), "Feedback": text,
        "备注": fbr_trace.interruption_note(ids),
        "提交人": "", "提交时间": "",
        "任务编号": record["task_id"], "批次": record.get("batch") or task_dir.parent.name,
        "底座仓库": record.get("repo_id", ""), "任务仓库": record.get("task_repo_full") or record.get("task_repo", ""),
        "运行开始": show_time(run.get("started_at", "")), "运行结束": show_time(run.get("ended_at", "")),
        "提交截止": deadline(run.get("ended_at", ""), cfg["daily_cutoff"]),
        "尝试次数": run.get("attempts", len(attempts) or ""), "证据路径": str(rel / "evidence"),
        "闸门状态": "通过", "抽检状态": spot.get("state", "未抽检"), "上传状态": "未提交",
    }
    # fbr_submit 也会用本地行生成平台字段；延迟导入避免模块循环。
    from fbr_submit import receipt_status

    receipt_manifest = delivery_manifest
    if receipt_manifest is None:
        receipt_manifest = lib.load_json(
            delivery.delivery_dir(root, task_dir.parent.name) / "delivery-manifest.json", default={})
    status = receipt_status(root, task_dir, row, receipt_manifest)
    row["上传状态"] = status["state"]
    if status.get("valid"):
        row.update({"轨迹文件": status["trace"], "运行录屏": status["video"],
                    "提交人": status["submitter"], "提交时间": show_time(status["submitted_at"])})
    row["数据状态"] = "；".join(row_problems(row, ids, snapshot)) or "可提交"
    row["闸门状态"] = "通过" if row["数据状态"] == "可提交" else "未通过：" + row["数据状态"]
    return row, ""


def prepared_fields(root: Path, task_dir: Path, cfg: dict) -> dict:
    """可核对的原始字段；不把未通过当前验收的评价或占位值当作准备完成。"""
    task = lib.load_task(task_dir)
    ids = lib.load_json(task_dir / "trace/ids.json", default={})
    snapshot = lib.load_json(task_dir / "review/snapshot.json", default={})
    video = task_dir / "record/recording.mp4"
    session = task_dir / "trace" / ids.get("session_file", "")
    trace_ok = bool(ids.get("session_file")) and session.is_file() and session.resolve().is_relative_to((task_dir / "trace/projects").resolve())
    return {"User Prompt": task["prompt"], "所属领域": task.get("domain", ""),
            "子方向": canonical_field("子方向", task.get("subdirection", "")),
            "任务类型": task.get("task_type", ""), "任务难度": task.get("difficulty", ""),
            "语言/框架": task.get("frameworks") or task.get("language", ""),
            "Harness": task.get("harness") or cfg["harness"],
            "Harness 版本": str(ids.get("cli_version") or task.get("harness_version") or cfg["harness_version"]),
            "操作系统": canonical_field("操作系统", task.get("os") or cfg["os"]),
            "环境可复现等级": task.get("repro_level", ""),
            "初始环境快照": (task.get("publish") or {}).get("initial_snapshot", ""),
            "SessionID": ids.get("session_id", ""), "TurnID/PromptID": ids.get("prompt_id", ""),
            "轨迹文件": str(session.relative_to(root)) if trace_ok else "",
            "产物快照": snapshot.get("artifact_snapshot", "") if snapshot.get("pushed") else "",
            "运行录屏": str(video.relative_to(root)) if lib.stage_ok(task_dir, "record") and video.is_file() else "",
            "备注": fbr_trace.interruption_note(ids)}


def row_problems(row: dict, ids: dict, snapshot: dict) -> list[str]:
    problems = [f"缺{name}" for name in PUBLIC_COLUMNS if name not in OPTIONAL and (row.get(name) is None or not str(row.get(name)).strip())]
    for name, allowed in OPTIONS.items():
        if row.get(name) and row[name] not in allowed:
            problems.append(f"{name}的取值不在选项里：{row[name]}")
    for name in ("初始环境快照", "产物快照"):
        if row.get(name) and not SNAPSHOT_RE.match(row[name]):
            problems.append(f"{name}不是提交链接")
    if row.get("Harness 版本") and not VERSION_RE.match(row["Harness 版本"]):
        problems.append("Harness 版本只填纯版本号")
    if ids.get("user_turns") is not None and not fbr_trace.valid_turn_count(ids):
        problems.append(f"轨迹里有 {ids.get('user_turns')} 轮用户输入")
    if snapshot and not snapshot.get("pushed"):
        problems.append("产物快照还没推送")
    if row.get("抽检状态") == "退回":
        problems.append("抽检被退回")
    return list(dict.fromkeys(problems))


def progress_row(task_dir: Path, reason: str) -> list:
    stages = lib.load_state(task_dir)["stages"]
    cells, stuck = [task_dir.name], ""
    for stage in lib.STAGES:
        info = stages.get(stage, {})
        cells.append({"ok": "✓", "failed": "✗", "dry": "演练"}.get(info.get("state"), ""))
        if info.get("state") == "failed" and not stuck:
            detail = info.get("problems") or info.get("reason") or ""
            detail = "；".join(detail) if isinstance(detail, list) else str(detail)
            stuck = f"{STAGE_LABEL[stage]}：{lib.clip(detail, 200)}"
    snapshot = lib.load_json(task_dir / "review" / "snapshot.json", default={})
    cells.append("✓" if snapshot.get("pushed") else "")
    cells.append(stuck or reason)
    return cells


def write_sheet(root: Path, batch_id: str, cfg: dict) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    manifest = delivery.export_batch(root, batch_id, cfg)
    rows, progress = [], []
    for task_dir in lib.task_dirs(root, batch_id):
        try:
            row, reason = build_row(root, task_dir, cfg, delivery_manifest=manifest)
        except lib.FbrError as exc:
            row, reason = None, str(exc)
        if row:
            rows.append(row)
        progress.append(progress_row(task_dir, row["数据状态"] if row and row["数据状态"] != "可提交" else ("" if row else reason)))

    book = Workbook()
    sheet = book.active
    sheet.title = "结果"
    columns = PUBLIC_COLUMNS + INTERNAL_COLUMNS
    sheet.append(columns)
    for row in rows:
        sheet.append([row.get(name, "") for name in columns])
    head_font, wrap = Font(bold=True), Alignment(wrap_text=True, vertical="top")
    for index, name in enumerate(columns, 1):
        cell = sheet.cell(row=1, column=index)
        cell.font = head_font
        cell.fill = PatternFill("solid", fgColor="DDEBF7" if index <= len(PUBLIC_COLUMNS) else "EDEDED")
        sheet.column_dimensions[get_column_letter(index)].width = WIDTHS.get(name, 16)
    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = wrap
    sheet.freeze_panes = "B2"

    board = book.create_sheet("进度")
    board.append(["任务编号", *[STAGE_LABEL[s] for s in lib.STAGES], "已推送", "卡在哪"])
    for line in progress:
        board.append(line)
    for cell in board[1]:
        cell.font = head_font
    board.column_dimensions["A"].width = 14
    board.column_dimensions[get_column_letter(len(lib.STAGES) + 3)].width = 80
    board.freeze_panes = "B2"

    audit = book.create_sheet("必填字段核查")
    audit.append(["任务编号", "字段", "状态", "依据或缺失原因"])
    required = [c for c in PUBLIC_COLUMNS if c not in OPTIONAL]
    by_id = {row["任务编号"]: row for row in rows}
    entries = []
    for task_dir in lib.task_dirs(root, batch_id):
        row = by_id.get(task_dir.name)
        try:
            prepared = row or prepared_fields(root, task_dir, cfg)
        except lib.FbrError:
            prepared = {}
        for field in required:
            value = prepared.get(field)
            state = ("已准备" if row else "有原始依据，待当前验收") if value not in (None, "") else "缺失/未通过验收"
            if value and field in OPTIONS and value not in OPTIONS[field]:
                state = "枚举无效，阻止交付"
            source = {"User Prompt": "task.json.prompt（冻结交接）",
                      "所属领域": "task.json.domain", "任务类型": "task.json.task_type",
                      "任务难度": "task.json.difficulty", "语言/框架": "task.json.frameworks/language",
                      "Harness": "task.json.harness/settings.harness",
                      "Harness 版本": "trace/ids.json.cli_version，缺失才回退交接配置",
                      "环境可复现等级": "task.json.repro_level",
                      "初始环境快照": "task.json.publish.initial_snapshot",
                      "SessionID": "trace/ids.json.session_id（原始主会话）",
                      "TurnID/PromptID": "trace/ids.json.prompt_id（首轮）",
                      "轨迹文件": "当前 delivery-manifest.json 核对过摘要的原始主会话",
                      "产物快照": "review/snapshot.json：仅推送并回读成功后填写",
                      "操作系统": "task.json/settings，按已核实平台枚举映射，架构保留原证据",
                      "子方向": "task.json，按已核实平台枚举映射",
                      "运行录屏": "record/recording.json 与实际 MP4",
                      "交付完整性": "review/feedback.json 与独立验收回执",
                      "Feedback": "review/feedback.json 与独立验收回执"}.get(field,
                          "task.json、trace/ids.json 及当前交付清单")
            detail = source if value not in (None, "") else "缺少当前有效依据；不得填入推测值"
            entry = {"task_id": task_dir.name, "field": field, "state": state, "source": detail}
            entries.append(entry)
            audit.append([task_dir.name, field, state, detail])
    audit.freeze_panes = "C2"
    for column, width in (("A", 18), ("B", 24), ("C", 22), ("D", 90)):
        audit.column_dimensions[column].width = width
    lib.save_json(delivery.delivery_dir(root, batch_id) / "field-audit.json",
                  {"batch_id": batch_id, "checked_at": lib.now_iso(), "fields": entries,
                   "platform_auto_fields": ["提交人", "提交时间"],
                   "ready": all(r["数据状态"] == "可提交" for r in rows) and len(rows) == len(progress)})

    corrections = book.create_sheet("字段修订核对")
    correction_columns = ["任务编号", "操作系统", "子方向", "产物快照", "运行录屏", "备注", "验收状态", "依据"]
    corrections.append(correction_columns)
    for task_dir in lib.task_dirs(root, batch_id):
        try:
            task = lib.load_task(task_dir)
        except lib.FbrError as exc:
            corrections.append([task_dir.name, "", "", "", "", "", "交接无效", str(exc)])
            continue
        snapshot = lib.load_json(task_dir / "review/snapshot.json", default={})
        ids = lib.load_json(task_dir / "trace/ids.json", default={})
        meta = lib.load_json(task_dir / "record/recording.json", default={})
        video = task_dir / "record/recording.mp4"
        status = "当前验收通过" if task_dir.name in by_id else "未通过当前验收，不能提交"
        corrections.append([task_dir.name,
            canonical_field("操作系统", task.get("os") or cfg["os"]),
            canonical_field("子方向", task.get("subdirection", "")),
            snapshot.get("artifact_snapshot", "") if snapshot.get("pushed") else "",
            str(video.relative_to(root)) if meta and video.is_file() else "",
            fbr_trace.interruption_note(ids), status,
            "冻结 task.json + 已核实平台枚举；snapshot.json 推送回读；record/recording.json；trace/ids.json。仅核对相关字段，不代替完整交付闸门。"])
    corrections.freeze_panes = "B2"
    for index, column in enumerate(correction_columns, 1):
        corrections.column_dimensions[get_column_letter(index)].width = 20 if index == 1 else 38
    for line in corrections.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = wrap
    for cell in corrections[1]:
        cell.font = head_font

    target = delivery.delivery_dir(root, batch_id) / "result.xlsx"
    book.save(target)
    ready = sum(1 for row in rows if row["数据状态"] == "可提交")
    return {"path": target, "tasks": len(progress), "rows": len(rows), "ready": ready,
            "pending": [(row["任务编号"], row["数据状态"]) for row in rows if row["数据状态"] != "可提交"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 结果表")
    parser.add_argument("--root")
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        out = write_sheet(root, args.batch, lib.settings(root))
        print(f"{out['path']}")
        print(f"批次共 {out['tasks']} 题，进结果页 {out['rows']} 题，其中可提交 {out['ready']} 题")
        for name, state in out["pending"]:
            print(f"  {name}  {state}")
        return 0
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
