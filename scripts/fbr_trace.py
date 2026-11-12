#!/usr/bin/env python3
"""Claude Code 轨迹（projects 目录下的 jsonl）与 print 模式流式输出的解析。

只读文件，不调外部命令。解析尽量容错：字段缺了就留空并记进 problems，由调用方决定拦不拦。
"""

from __future__ import annotations

import re
from pathlib import Path

import fbrlib as lib

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
TEST_RE = re.compile(
    r"(\bmake\s+(?:-\S+\s+)*(?:test|check)\b|\bpytest\b|-m\s+(?:pytest|unittest)\b|\bnpm\s+(?:run\s+)?test\b"
    r"|\byarn\s+test\b|\bnode\s+--test\b|\bvitest\b|\bjest\b|\bmvn\b[^\n|;&]*\b(?:test|verify)\b|\bctest\b)")
BUILD_RE = re.compile(
    r"(\bmake\b(?!\s+(?:-\S+\s+)*(?:test|check|setup|clean)\b)|\bnpm\s+run\s+build\b|\btsc\b|\bg(?:cc|\+\+)\s"
    r"|\bmvn\b[^\n|;&]*\b(?:package|compile)\b|\bpython3?\s+-m\s+(?:compileall|py_compile)\b)")
MUTATE_RE = re.compile(r"(\bsed\s+-i\b|\bgit\s+(?:apply|checkout|restore|stash|reset|revert)\b|\bpatch\s|(?<![<>0-9&])>{1,2}\s*[\w./~-]+"
                       r"|\btee\s|\brm\s|\bmv\s|\bcp\s)")
GATEWAY_RE = re.compile(
    r"(API Error|api_error|overloaded|rate.?limit|\b(?:429|500|502|503|504|529)\b|ECONN\w+|ETIMEDOUT|socket hang up"
    r"|fetch failed|Connection error|network error|insufficient|quota|balance|Unauthorized|\b401\b|\b403\b|timed? ?out)",
    re.I)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    parts.append(_text_of(block.get("content")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _blocks(row: dict) -> list[dict]:
    content = (row.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def session_files(projects_dir: Path) -> list[Path]:
    """主会话文件：projects/<工程目录>/<session>.jsonl。更深层的（子代理）不算。"""
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    return sorted(p for p in projects_dir.glob("*/*.jsonl") if p.is_file() and p.stat().st_size > 0)


def is_user_turn(row: dict) -> bool:
    if row.get("type") != "user" or row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    if row.get("toolUseResult") is not None:
        return False
    # CC 2.1.319 emits background task notifications as user rows with their
    # own promptId. Trust the provenance fields, never the content tag alone:
    # a human can legitimately submit text containing <task-notification>.
    origin = row.get("origin")
    if (row.get("promptSource") == "system"
            and row.get("turnOrigin") == "task_notification"
            and isinstance(origin, dict)
            and origin.get("kind") == "task-notification"
            and origin.get("producer") == "session-task"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    blocks = _blocks(row)
    if any(b.get("type") == "tool_result" for b in blocks):
        return False
    return any(b.get("type") == "text" and str(b.get("text", "")).strip() for b in blocks)


def main_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("isSidechain")]


def read_ids(projects_dir: Path, prompt: str | None = None, continuations: list[dict] | None = None) -> dict:
    """取 SessionID 与本轮 promptId，校验首轮题面和最多两次有记录的中断续写。"""
    files = session_files(projects_dir)
    out = {"session_id": "", "prompt_id": "", "session_file": "", "user_turns": 0, "rows": 0,
           "cli_version": "", "problems": []}
    if not files:
        out["problems"].append("导出的 projects 目录里没有会话文件")
        return out
    if len(files) > 1:
        out["problems"].append(f"会话文件有 {len(files)} 个，应当只有 1 个")
    path = max(files, key=lambda p: p.stat().st_size)
    rows = lib.read_jsonl(path)
    out.update(session_file=str(path.relative_to(Path(projects_dir).parent)), rows=len(rows))
    sessions = {r["sessionId"] for r in rows if r.get("sessionId")}
    if len(sessions) == 1:
        out["session_id"] = sessions.pop()
    else:
        out["problems"].append(f"会话文件里的 sessionId 有 {len(sessions)} 个")
    if out["session_id"] and path.stem != out["session_id"]:
        out["problems"].append("会话文件名和 sessionId 对不上")
    out["cli_version"] = next((str(r["version"]) for r in rows if r.get("version")), "")
    if not VERSION_RE.fullmatch(out["cli_version"]):
        out["problems"].append("轨迹里的 version 缺失或不是纯版本号")
    turns = [r for r in rows if is_user_turn(r)]
    out["user_turns"] = len(turns)
    continuations = continuations or []
    out["continuations"] = continuations
    prompt_ids = []
    for row in turns:
        if row.get("promptId") and row["promptId"] not in prompt_ids:
            prompt_ids.append(row["promptId"])
    if not turns:
        out["problems"].append("轨迹里找不到用户输入")
    elif len(turns) != 1 + len(continuations) or len(continuations) > 2:
        out["problems"].append(f"用户输入有 {len(turns)} 条、promptId {len(prompt_ids)} 个，不是单轮")
    elif not turns[0].get("promptId"):
        out["problems"].append("用户消息上没有 promptId")
    else:
        out["prompt_id"] = prompt_ids[0]
    for turn, entry in zip(turns[1:], continuations):
        if (_text_of((turn.get("message") or {}).get("content")) != "继续"
                or entry.get("prompt") != "继续" or entry.get("reason") != "无故中断"
                or entry.get("session_id") != out["session_id"]):
            out["problems"].append("续写必须是同一会话中有中断记录的“继续”")
    if turns and turns[0].get("promptId"):
        out["prompt_id"] = turns[0]["promptId"]  # 始终填第一轮
    if prompt is not None and turns:
        sent = _text_of((turns[0].get("message") or {}).get("content"))
        if sent != prompt:
            out["problems"].append("轨迹里的用户输入和题面不一致")
    if not any(r.get("type") == "assistant" for r in rows):
        out["problems"].append("轨迹里没有助手消息")
    return out


def valid_turn_count(ids: dict) -> bool:
    entries = ids.get("continuations") or []
    return (type(ids.get("user_turns")) is int and ids["user_turns"] == 1 + len(entries)
            and len(entries) <= 2 and all(
                e.get("prompt") == "继续" and e.get("reason") == "无故中断"
                and e.get("session_id") == ids.get("session_id") for e in entries))


def interruption_note(ids: dict) -> str:
    entries = ids.get("continuations") or []
    if not entries or not valid_turn_count(ids):
        return ""
    return (f"模型自行中断，已在同一会话发送{len(entries)}次“继续”；"
            "评价综合首轮及所有续写内容，SessionID、TurnID/PromptID和题面仍取第一轮数据")


def _target(tool: str, payload: dict) -> str:
    if tool == "Bash":
        return str(payload.get("command", ""))
    for key in ("file_path", "notebook_path", "path"):
        if payload.get(key):
            value = str(payload[key])
            return value[len("/workspace/"):] if value.startswith("/workspace/") else value
    if payload.get("pattern"):
        return str(payload["pattern"])
    return ""


def tool_steps(rows: list[dict]) -> list[dict]:
    """主链上的工具调用，按出现顺序从 1 编号。步骤号就是 Feedback 里引用轨迹的锚点。"""
    results = {}
    for row in main_rows(rows):
        if row.get("type") != "user":
            continue
        for block in _blocks(row):
            if block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[block["tool_use_id"]] = (bool(block.get("is_error")), _text_of(block.get("content")))
    steps = []
    for row in main_rows(rows):
        if row.get("type") != "assistant":
            continue
        for block in _blocks(row):
            if block.get("type") != "tool_use":
                continue
            tool = str(block.get("name", ""))
            payload = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = _target(tool, payload)
            done = block.get("id") in results
            is_error, output = results.get(block.get("id"), (False, ""))
            step = {
                "step": len(steps) + 1, "tool": tool, "target": lib.clip(target, 400), "answered": done,
                "is_error": is_error, "error": lib.clip(output, 600) if is_error else "",
                "output_tail": lib.clip("\n".join(output.splitlines()[-12:]), 800) if tool == "Bash" else "",
                "ts": row.get("timestamp", ""),
            }
            if tool == "Bash":
                step["kind"] = "test" if TEST_RE.search(target) else "build" if BUILD_RE.search(target) else "run"
                step["mutates"] = bool(MUTATE_RE.search(target))
            elif tool in EDIT_TOOLS:
                step["kind"] = "edit"
            else:
                step["kind"] = "read"
            steps.append(step)
    return steps


def final_answer(rows: list[dict]) -> str:
    """最后一条助手消息里的文字，就是模型交付时的自述。"""
    for row in reversed(main_rows(rows)):
        if row.get("type") != "assistant" or row.get("isApiErrorMessage"):
            continue
        text = "\n".join(str(b.get("text", "")) for b in _blocks(row) if b.get("type") == "text").strip()
        if text:
            return text
    return ""


def api_errors(rows: list[dict]) -> list[str]:
    found = []
    for row in rows:
        if row.get("isApiErrorMessage") or (row.get("type") == "system" and row.get("subtype") == "api_error"):
            text = _text_of((row.get("message") or {}).get("content")) or str(row.get("content", ""))
            found.append(lib.clip(text, 200))
    return found


def verification(steps: list[dict]) -> dict:
    """模型自己跑过的测试 / 构建，以及最后一次改动之后有没有再验证。"""
    edits = [s["step"] for s in steps if s["kind"] == "edit" or s.get("mutates")]
    last_edit = max(edits) if edits else 0
    checks = [{"step": s["step"], "kind": s["kind"], "cmd": s["target"], "passed": not s["is_error"],
               "error": s["error"]} for s in steps if s["kind"] in ("test", "build")]
    after = [c for c in checks if c["step"] > last_edit]
    return {
        "checks": checks, "last_edit_step": last_edit,
        "verified_after_last_edit": bool(after),
        "last_check_passed": checks[-1]["passed"] if checks else None,
        "tests_run": sum(1 for c in checks if c["kind"] == "test"),
        "tests_failed": sum(1 for c in checks if c["kind"] == "test" and not c["passed"]),
    }


def summarize(projects_dir: Path, session_file: str) -> dict:
    rows = lib.read_jsonl(Path(projects_dir).parent / session_file)
    steps = tool_steps(rows)
    stamps = sorted(str(r["timestamp"]) for r in rows if r.get("timestamp"))
    return {
        "steps": steps, "verification": verification(steps), "final_answer": final_answer(rows),
        "assistant_answers": [_text_of((r.get("message") or {}).get("content"))
                              for r in main_rows(rows) if r.get("type") == "assistant" and not r.get("isApiErrorMessage")],
        "api_errors": api_errors(rows), "started_at": stamps[0] if stamps else "",
        "ended_at": stamps[-1] if stamps else "",
        "counts": {"tools": len(steps), "errors": sum(1 for s in steps if s["is_error"]),
                   "edits": sum(1 for s in steps if s["kind"] == "edit"),
                   "unanswered": sum(1 for s in steps if not s["answered"])},
    }


# ---------- print 模式的流式输出 ----------

def read_stream(path: Path) -> dict:
    """从 stdout 的流式 JSON 里取 init 和 result 事件。"""
    out = {"events": 0, "session_id": "", "cli_version": "", "model": "", "result": None, "problems": []}
    init_seen = False
    init_session = ""
    for row in lib.read_jsonl(path):
        out["events"] += 1
        if row.get("type") == "system" and row.get("subtype") == "init":
            init_seen = True
            init_session = row.get("session_id", "")
            out["session_id"] = row.get("session_id", "") or out["session_id"]
            out["cli_version"] = str(row.get("claude_code_version", ""))
            out["model"] = str(row.get("model", ""))
            if not init_session:
                out["problems"].append("system/init 缺少 session_id")
            if not VERSION_RE.fullmatch(out["cli_version"]):
                out["problems"].append("system/init 的 claude_code_version 缺失或不是纯版本号")
        elif row.get("type") == "result":
            if not isinstance(row.get("is_error"), bool):
                out["problems"].append("result 缺少布尔字段 is_error")
            if not isinstance(row.get("result"), str):
                out["problems"].append("result 缺少字符串字段 result")
            if not row.get("session_id"):
                out["problems"].append("result 缺少 session_id")
            out["result"] = {"is_error": bool(row.get("is_error")), "subtype": str(row.get("subtype", "")),
                             "text": str(row.get("result", "") or ""), "num_turns": row.get("num_turns"),
                             "session_id": row.get("session_id", "")}
            out["session_id"] = row.get("session_id", "") or out["session_id"]
    if not init_seen:
        out["problems"].append("流式输出缺少 system/init 事件")
    if out["result"] and init_session and out["result"]["session_id"] != init_session:
        out["problems"].append("system/init 和 result 的 session_id 不一致")
    return out


def classify_attempt(rc: int, timed_out: bool, stream: dict, stderr_tail: str = "") -> dict:
    """判断这次运行算不算数。只有进程异常、网关报错、无故中断才重跑；

    模型正常结束但没改代码、改错了、答非所问，都是有效数据，不重跑。
    """
    result = stream.get("result")
    if timed_out:
        return {"verdict": "retry", "reason": "进程超时", "detail": ""}
    if result is None:
        why = "网关报错" if GATEWAY_RE.search(stderr_tail or "") else "进程异常退出" if rc != 0 else "无故中断"
        return {"verdict": "retry", "reason": why, "detail": lib.clip(stderr_tail, 300)}
    if stream.get("problems"):
        return {"verdict": "stop", "reason": "流式输出字段不符", "detail": "；".join(stream["problems"])}
    if result["is_error"]:
        why = "网关报错" if GATEWAY_RE.search(result["text"] + " " + (stderr_tail or "")) else "无故中断"
        return {"verdict": "retry", "reason": why, "detail": lib.clip(result["text"] or result["subtype"], 300)}
    if rc != 0:
        return {"verdict": "retry", "reason": "进程异常退出", "detail": lib.clip(stderr_tail, 300)}
    if not result["text"].strip():
        return {"verdict": "retry", "reason": "无故中断", "detail": "正常退出但没有最终回答"}
    if re.match(r"\s*API Error\b", result["text"]):
        return {"verdict": "retry", "reason": "网关报错", "detail": lib.clip(result["text"], 300)}
    return {"verdict": "accept", "reason": "", "detail": ""}
