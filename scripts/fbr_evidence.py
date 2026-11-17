#!/usr/bin/env python3
"""Index trace evidence once and serve exact source ranges without full rereads."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import fbrlib as lib

VERSION = 2
TRACE_PREFIX = "trace/projects/"
OFFSET_DIR = "trace/offsets"
MANIFEST = "trace/manifest.json"
EVENTS = "trace/events.jsonl"
CHECK = "trace/trace-check.json"
PACK = "trace/review-pack.json"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(task: Path, relative: str) -> Path:
    task = Path(task).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise lib.FbrError("证据路径越界")
    path = task / rel
    current = task
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise lib.FbrError("证据路径不能包含符号链接")
    if not path.resolve().is_relative_to(task):
        raise lib.FbrError("证据路径越界")
    return path


def trace_sources(task: Path) -> list[Path]:
    projects = Path(task) / "trace/projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.rglob("*.jsonl") if p.is_file())


def _relative(task: Path, path: Path) -> str:
    return path.relative_to(Path(task)).as_posix()


def _quick(path: Path) -> dict:
    stat = path.stat()
    return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns}


def _offset_name(relative: str) -> str:
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20] + ".json"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _block_content(row: dict) -> Any:
    return (row.get("message") or {}).get("content", row.get("content", ""))


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    parts.append(_text_of(block.get("content")))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        if value.get("type") == "tool_result":
            return _text_of(value.get("content"))
    return ""


def _target(tool: str, payload: dict) -> str:
    import fbr_trace
    return fbr_trace._target(tool, payload)


def _kind(tool: str, target: str) -> tuple[str, bool]:
    import fbr_trace
    if tool == "Bash":
        if fbr_trace.TEST_RE.search(target):
            return "test", bool(fbr_trace.MUTATE_RE.search(target))
        if fbr_trace.BUILD_RE.search(target):
            return "build", bool(fbr_trace.MUTATE_RE.search(target))
        return "run", bool(fbr_trace.MUTATE_RE.search(target))
    if tool in fbr_trace.EDIT_TOOLS:
        return "edit", True
    return "read", False


def _event_id(relative: str, line: int, block: int | None) -> str:
    suffix = "row" if block is None else str(block)
    return hashlib.sha256(f"{relative}:{line}:{suffix}".encode()).hexdigest()[:16]


def _event_from_block(relative: str, line: int, block: int | None, row: dict, value: Any) -> dict:
    event = {
        "id": _event_id(relative, line, block),
        "source": relative,
        "line": line,
        "block": block,
        "row_type": row.get("type", ""),
        "timestamp": row.get("timestamp", ""),
        "is_sidechain": bool(row.get("isSidechain")),
        "is_api_error": bool(row.get("isApiErrorMessage")),
        "type": "row",
    }
    if isinstance(value, str):
        value = {"type": "text", "text": value}
    if not isinstance(value, dict):
        event["type"] = "unknown"
        return event
    kind = str(value.get("type", "unknown"))
    event["type"] = kind
    if kind == "tool_use":
        tool = str(value.get("name", ""))
        payload = value.get("input") if isinstance(value.get("input"), dict) else {}
        target = _target(tool, payload)
        action, mutates = _kind(tool, target)
        event.update(tool=tool, tool_use_id=str(value.get("id", "")),
                     target=lib.clip(target, 1200), kind=action, mutates=mutates,
                     input_preview=lib.clip(json.dumps(payload, ensure_ascii=False), 1600))
    elif kind == "tool_result":
        output = _text_of(value.get("content"))
        event.update(tool_use_id=str(value.get("tool_use_id", "")),
                     is_error=bool(value.get("is_error")),
                     output_chars=len(output),
                     output_preview=lib.clip(output, 2000))
    elif kind == "text":
        text = str(value.get("text", ""))
        event.update(text=text, text_chars=len(text))
    return event


def _read_source(task: Path, path: Path, source: dict) -> tuple[list[dict], list[dict]]:
    relative = _relative(task, path)
    quick_before = _quick(path)
    digest = hashlib.sha256()
    starts = [0]
    events = []
    lines = 0
    blocks = 0
    with path.open("rb") as stream:
        while True:
            raw = stream.readline()
            if not raw:
                break
            lines += 1
            digest.update(raw)
            if not raw.strip():
                events.append({
                    "id": _event_id(relative, lines, None), "source": relative,
                    "line": lines, "block": None, "row_type": "blank",
                    "timestamp": "", "is_sidechain": False, "is_api_error": False,
                    "type": "blank",
                })
                starts.append(stream.tell())
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise lib.FbrError(f"轨迹 JSON 损坏：{path.name}:{lines}") from None
            if not isinstance(row, dict):
                raise lib.FbrError(f"轨迹消息不是对象：{path.name}:{lines}")
            content = _block_content(row)
            if isinstance(content, list):
                if not content:
                    events.append(_event_from_block(relative, lines, None, row, None))
                for number, value in enumerate(content):
                    blocks += 1
                    events.append(_event_from_block(relative, lines, number, row, value))
            else:
                events.append(_event_from_block(relative, lines, None, row, content))
            starts.append(stream.tell())
    source["sha256"] = digest.hexdigest()
    source["bytes"] = path.stat().st_size
    source["lines"] = lines
    source["blocks"] = blocks
    if _quick(path) != quick_before:
        raise lib.FbrError(f"轨迹在摄取期间发生变化：{relative}")
    return events, starts


def _pair_tools(events: list[dict]) -> None:
    uses = {}
    results = {}
    for event in events:
        if event.get("type") == "tool_use":
            uses.setdefault(event.get("tool_use_id"), []).append(event)
        elif event.get("type") == "tool_result":
            results.setdefault(event.get("tool_use_id"), []).append(event)
    for tool_id, group in uses.items():
        outcome = results.get(tool_id, [])
        claimed = set()
        for result in outcome:
            candidates = [use for use in group
                          if use.get("line", 0) <= result.get("line", 0) and use["id"] not in claimed]
            if not candidates:
                candidates = [use for use in group if use["id"] not in claimed]
            if not candidates:
                break
            use = max(candidates, key=lambda row: row.get("line", 0))
            claimed.add(use["id"])
            use.update(answered=True, result_event_id=result["id"], result_line=result["line"],
                       result_block=result["block"], result_is_error=result.get("is_error", False),
                       result_preview=result.get("output_preview", ""))
            result.update(pair_event_id=use["id"], pair_line=use["line"], pair_block=use.get("block"),
                          tool=use.get("tool", ""), target=use.get("target", ""),
                          kind=use.get("kind", ""))
        for use in group:
            use.setdefault("answered", False)
            use["logical_answered"] = bool(claimed)
            use["duplicate_tool_use"] = use["id"] not in claimed and bool(claimed)
    for tool_id, group in results.items():
        for result in group:
            result.setdefault("logical_answered", bool(uses.get(tool_id)))


def _logical_tools(events: list[dict]) -> list[dict]:
    grouped = {}
    for event in events:
        if event.get("type") == "tool_use":
            grouped.setdefault(event.get("tool_use_id"), []).append(event)
    chosen = []
    for group in grouped.values():
        answered = [event for event in group if event.get("answered")]
        chosen.append(answered[-1] if answered else group[-1])
    return sorted(chosen, key=lambda row: (row.get("source", ""), row.get("line", 0), row.get("block") or 0))


def _logical_results(events: list[dict]) -> list[dict]:
    grouped = {}
    for event in events:
        if event.get("type") == "tool_result":
            grouped[event.get("tool_use_id")] = event
    return sorted(grouped.values(), key=lambda row: (row.get("source", ""), row.get("line", 0),
                                                     row.get("block") or 0))


def _assistant_text(events: list[dict]) -> list[dict]:
    return [event for event in events
            if event.get("type") == "text" and event.get("row_type") == "assistant"
            and not event.get("is_api_error") and str(event.get("text", "")).strip()]


def _build_pack(task: Path, events: list[dict], sources: list[dict]) -> dict:
    text_events = _assistant_text(events)
    tools = _logical_tools(events)
    results = _logical_results(events)
    errors = [event for event in results if event.get("is_error")]
    checks = [event for event in tools if event.get("kind") in ("test", "build")]
    edits = [event for event in tools if event.get("kind") == "edit" or event.get("mutates")]
    final_answer = text_events[-1].get("text", "") if text_events else ""
    corrections = []
    import fbr_trace
    pattern = __import__("re").compile(
        r"(failed|failure|error|fix|fixed|retry|again|wrong|actually|但是|失败|报错|修正|改为|重跑)",
        __import__("re").I)
    for event in text_events:
        if pattern.search(str(event.get("text", ""))):
            corrections.append({"event_id": event["id"], "source": event["source"],
                                "line": event["line"], "block": event["block"],
                                "text": lib.clip(event["text"], 4000)})
    last_edit = max((event["line"] for event in edits), default=0)
    checks_after = [event for event in checks if event["line"] > last_edit]
    high_risk = []
    if len(sources) > 1:
        high_risk.append("multi_source")
    if errors or any(event.get("result_is_error") for event in tools):
        high_risk.append("tool_errors")
    if edits and not checks_after:
        high_risk.append("no_check_after_last_edit")
    if corrections:
        high_risk.append("assistant_corrections")
    if any(not event.get("answered") for event in tools):
        high_risk.append("unpaired_tool_use")
    return {
        "version": VERSION,
        "task_id": Path(task).name,
        "sources": sources,
        "summary": {
            "assistant_texts": len(text_events),
            "last_edit_line": last_edit,
            "checked_after_last_edit": bool(checks_after),
            "high_risk": high_risk,
        },
        "assistant_text": [
            {"event_id": event["id"], "source": event["source"], "line": event["line"],
             "block": event["block"], "text": event.get("text", "")}
            for event in text_events
        ],
        "tool_events": [
            {"event_id": event["id"], "source": event["source"], "line": event["line"],
             "block": event["block"], "tool": event.get("tool"), "kind": event.get("kind"),
             "target": event.get("target"), "mutates": event.get("mutates", False),
             "answered": event.get("answered", False),
             "is_error": event.get("result_is_error", False),
             "result_line": event.get("result_line"), "result_block": event.get("result_block"),
             "result_preview": lib.clip(event.get("result_preview", ""), 1200)}
            for event in tools
        ],
        "checks": [
            {"event_id": event["id"], "source": event["source"], "line": event["line"],
             "block": event["block"], "tool": event.get("tool"), "kind": event.get("kind"),
             "target": event.get("target"), "is_error": event.get("result_is_error", False),
             "result_line": event.get("result_line"), "result_block": event.get("result_block"),
             "result_preview": lib.clip(event.get("result_preview", ""), 1600)}
            for event in checks
        ],
        "errors": [
            {"event_id": event["id"], "source": event["source"], "line": event["line"],
             "block": event["block"], "tool": event.get("tool"), "target": event.get("target"),
             "pair_line": event.get("pair_line"), "pair_block": event.get("pair_block"),
             "output_preview": lib.clip(event.get("output_preview", ""), 2000)}
            for event in errors
        ],
        "corrections": corrections,
        "final_answer": {
            "event_id": text_events[-1]["id"], "source": text_events[-1]["source"],
            "line": text_events[-1]["line"], "block": text_events[-1]["block"],
            "text": final_answer,
        } if text_events else {},
    }


def ingest(task: Path, *, force: bool = False) -> dict:
    task = Path(task).resolve()
    paths = trace_sources(task)
    if not paths:
        raise lib.FbrError("没有可摄取的原始轨迹")
    manifest_path = task / MANIFEST
    old = lib.load_json(manifest_path, default={})
    old_rows = {row.get("path"): row for row in old.get("files", []) if isinstance(row, dict)}
    current = {_relative(task, path): _quick(path) for path in paths}
    reusable = (not force and old.get("version") == VERSION
                and old.get("files")
                and all(row.get("path") in current
                        and row.get("bytes") == current[row["path"]]["bytes"]
                        and row.get("mtime_ns") == current[row["path"]]["mtime_ns"]
                        and row.get("ctime_ns") == current[row["path"]]["ctime_ns"]
                        for row in old.get("files"))
                and set(old_rows) == set(current)
                and (task / EVENTS).is_file() and (task / CHECK).is_file() and (task / PACK).is_file())
    if reusable:
        return lib.load_json(task / CHECK)

    sources, events = [], []
    for path in paths:
        relative = _relative(task, path)
        source = {"path": relative, "offset_file": f"{OFFSET_DIR}/{_offset_name(relative)}", **_quick(path)}
        rows, starts = _read_source(task, path, source)
        offset_path = task / source["offset_file"]
        lib.save_json(offset_path, {"version": VERSION, "path": relative,
                                    "sha256": source["sha256"], "starts": starts})
        source["event_count"] = len(rows)
        sources.append(source)
        events.extend(rows)
    _pair_tools(events)
    event_ids = {event["id"] for event in events}
    source_lines = {(event["source"], event["line"]) for event in events}
    check = {
        "version": VERSION,
        "task_id": task.name,
        "generated_at": lib.now_iso(),
        "full_scan_complete": True,
        "all_source_files_processed": len(sources) == len(paths),
        "all_lines_accounted": len(source_lines) == sum(source["lines"] for source in sources),
        "sources": sources,
        "counts": {
            "source_files": len(sources),
            "lines": sum(source["lines"] for source in sources),
            "blocks": sum(source["blocks"] for source in sources),
            "events": len(events),
            "unique_events": len(event_ids),
        },
        "problems": [],
    }
    if len(event_ids) != len(events):
        check["problems"].append("事件 ID 不唯一")
    if not check["all_lines_accounted"]:
        check["problems"].append("存在未计入的原始轨迹行")
    _write_jsonl(task / EVENTS, events)
    pack = _build_pack(task, events, sources)
    lib.save_json(task / PACK, pack)
    check["event_sha256"] = _sha_bytes((task / EVENTS).read_bytes())
    check["pack_sha256"] = _sha_bytes((task / PACK).read_bytes())
    lib.save_json(manifest_path, {"version": VERSION, "generated_at": check["generated_at"],
                                  "files": sources})
    lib.save_json(task / CHECK, check)
    return check


def _identity_map(task: Path) -> dict:
    check = ingest(task)
    return {row["path"]: row["sha256"] for row in check["sources"]}


def current_identity(task: Path, generated: bool = True) -> dict:
    task = Path(task).resolve()
    check = ingest(task)
    for source in check["sources"]:
        path = _safe_path(task, source["path"])
        if _quick(path) != {"bytes": source["bytes"], "mtime_ns": source["mtime_ns"],
                            "ctime_ns": source["ctime_ns"]}:
            raise lib.FbrError("原始轨迹在摄取后发生变化，须重新摄取")
    identity = {"version": VERSION, "sources": _identity_map(task)}
    if generated:
        for name in (EVENTS, CHECK, PACK):
            identity[name] = _sha_bytes((task / name).read_bytes())
    return identity


def _load_manifest(task: Path) -> dict:
    ingest(task)
    manifest = lib.load_json(task / MANIFEST)
    return {row["path"]: row for row in manifest.get("files", [])}


def _decode_exact(stream, path: Path, source: dict, offsets: dict, ref: dict) -> dict:
    name = str(ref.get("path") or "")
    start = ref.get("start_line")
    end = ref.get("end_line", start)
    block = ref.get("block")
    if type(start) is not int or type(end) is not int or not (1 <= start <= end <= source["lines"]):
        raise lib.FbrError(f"轨迹行号越界或缺失：{name}")
    if type(block) is not int or block < 0 or end != start:
        raise lib.FbrError("block 必须是非负整数，且只用于单行原文")
    starts = offsets["starts"]
    stream.seek(starts[start - 1])
    raw = stream.read(starts[end] - starts[start - 1])
    text = raw.decode("utf-8", errors="replace")
    row = json.loads(text)
    content = _block_content(row)
    if isinstance(content, list):
        if block >= len(content):
            raise lib.FbrError(f"block 越界：{name}:{start}")
        decoded = content[block]
    elif block == 0:
        decoded = content
    else:
        raise lib.FbrError(f"block 越界：{name}:{start}")
    return {"path": name, "start_line": start, "end_line": end, "block": block,
            "sha256": source["sha256"], "raw": text, "decoded": decoded}


def _contains_exact(found: dict, excerpt: str) -> bool:
    if not isinstance(excerpt, str) or not excerpt:
        return False
    candidates = [found["raw"]]
    decoded = found.get("decoded")
    if decoded is not None:
        candidates.append(json.dumps(decoded, ensure_ascii=False))
        candidates.append(_text_of(decoded))
        if isinstance(decoded, dict):
            candidates.extend(str(value) for value in decoded.values()
                               if isinstance(value, (str, int, float, bool)))
    return any(excerpt in candidate for candidate in candidates if candidate)


def _locate_excerpt(
    stream,
    source: dict,
    offsets: dict,
    excerpt: str,
) -> tuple[int, int] | None:
    """Find one unique line/block containing the exact excerpt.

    Reviewer models occasionally provide a nearby line number for otherwise
    valid evidence.  Re-anchoring is safe only when the excerpt occurs in one
    location in the bound source file; ambiguous or missing text still fails.
    """
    starts = offsets["starts"]
    locations: set[tuple[int, int]] = set()
    for line_no in range(1, source["lines"] + 1):
        stream.seek(starts[line_no - 1])
        raw = stream.read(starts[line_no] - starts[line_no - 1])
        try:
            row = json.loads(raw.decode("utf-8", errors="replace"))
        except (TypeError, ValueError):
            continue
        content = _block_content(row)
        blocks = list(enumerate(content)) if isinstance(content, list) else [(0, content)]
        for block_no, decoded in blocks:
            candidate = {"raw": raw.decode("utf-8", errors="replace"), "decoded": decoded}
            if _contains_exact(candidate, excerpt):
                locations.add((line_no, block_no))
                if len(locations) > 1:
                    return None
    return next(iter(locations)) if len(locations) == 1 else None


def _record_access(task: Path, operation: str, refs: list[dict], *,
                   bytes_returned: int = 0, full_reads: int = 0, file_opens: int = 0) -> None:
    if os.environ.get("FBR_EVIDENCE_ACCESS_LOG") == "0":
        return
    row = {
        "at": lib.now_iso(),
        "operation": operation,
        "refs": len(refs),
        "sources": sorted({str(ref.get("path", "")) for ref in refs}),
        "bytes_returned": bytes_returned,
        "full_reads": full_reads,
        "file_opens": file_opens,
    }
    lib.append_jsonl(_access_path(Path(task)), row)


def _access_path(task: Path) -> Path:
    """Keep diagnostics outside the immutable task projection."""
    try:
        root = lib.find_root(str(task))
        return root / "_verification" / "evidence-access" / (Path(task).name + ".jsonl")
    except lib.FbrError:
        return Path(task).parent / "_evidence-access" / (Path(task).name + ".jsonl")


def fetch_refs(task: Path, refs: list[dict], *, operation: str = "fetch",
               require_excerpt: bool = True, allow_rebind: bool = True) -> dict:
    task = Path(task).resolve()
    unique = {}
    for ref in refs:
        if not isinstance(ref, dict):
            raise lib.FbrError("证据引用必须是对象")
        key = (ref.get("path"), ref.get("start_line"), ref.get("end_line", ref.get("start_line")),
               ref.get("block"))
        unique.setdefault(key, ref)
    manifest = _load_manifest(task)
    grouped = {}
    found, errors = [], []
    for ref in unique.values():
        name = str(ref.get("path") or "")
        grouped.setdefault(name, []).append(ref)
    for name, group in grouped.items():
        source = manifest.get(name)
        if not source:
            errors.extend({"ref": ref, "error": f"轨迹文件不在摄取清单中：{name}"} for ref in group)
            continue
        try:
            path = _safe_path(task, name)
            if not name.startswith(TRACE_PREFIX) or not name.endswith(".jsonl") or not path.is_file():
                raise lib.FbrError("轨迹证据必须指向 trace/projects/*.jsonl")
            if _quick(path) != {"bytes": source["bytes"], "mtime_ns": source["mtime_ns"],
                                "ctime_ns": source["ctime_ns"]}:
                raise lib.FbrError("原始轨迹在摄取后发生变化，须重新摄取")
            offsets = lib.load_json(_safe_path(task, source["offset_file"]))
            if (offsets.get("sha256") != source["sha256"]
                    or len(offsets.get("starts", [])) != source["lines"] + 1):
                raise lib.FbrError("轨迹偏移索引与清单不一致")
        except lib.FbrError as exc:
            errors.extend({"ref": ref, "error": str(exc)} for ref in group)
            continue
        # One open per source file for the whole batch.
        with path.open("rb") as stream:
            for ref in group:
                try:
                    row = _decode_exact(stream, path, source, offsets, ref)
                    excerpt = ref.get("excerpt")
                    if require_excerpt and not _contains_exact(row, str(excerpt or "")):
                        if not allow_rebind:
                            raise lib.FbrError(
                                f"摘录不在指定原始区间：{name}:{ref.get('start_line')} block {ref.get('block')}")
                        located = _locate_excerpt(stream, source, offsets, str(excerpt or ""))
                        if located is None:
                            raise lib.FbrError(
                                f"摘录不在指定原始区间：{name}:{ref.get('start_line')} block {ref.get('block')}")
                        original = {
                            "start_line": ref.get("start_line"),
                            "end_line": ref.get("end_line", ref.get("start_line")),
                            "block": ref.get("block"),
                        }
                        ref["start_line"], ref["end_line"] = located[0], located[0]
                        ref["block"] = located[1]
                        ref["rebound_from"] = original
                        row = _decode_exact(stream, path, source, offsets, ref)
                        if not _contains_exact(row, str(excerpt or "")):
                            raise lib.FbrError(
                                f"摘录无法重新锚定：{name}:{ref.get('start_line')} block {ref.get('block')}")
                        row["rebound_from"] = original
                    row["excerpt"] = str(excerpt or "")
                    found.append(row)
                except (lib.FbrError, ValueError) as exc:
                    errors.append({"ref": ref, "error": str(exc)})
    returned = sum(len(row["raw"]) for row in found)
    _record_access(task, operation, list(unique.values()), bytes_returned=returned,
                   file_opens=len(grouped))
    return {"state": "ok" if not errors else "failed", "evidence": found, "errors": errors,
            "stats": {"requested": len(refs), "deduplicated": len(unique), "returned": len(found),
                      "bytes_returned": returned, "full_reads": 0, "file_opens": len(grouped)}}


def verify_refs(task: Path, refs: list[dict], *, operation: str = "verify") -> dict:
    return fetch_refs(task, refs, operation=operation, require_excerpt=True, allow_rebind=False)


def resolve_excerpt(task: Path, relative: str, excerpt: str) -> dict:
    """Find one unique line/block for an exact quote without guessing ambiguities."""
    task = Path(task).resolve()
    if not relative.startswith(TRACE_PREFIX) or not relative.endswith('.jsonl'):
        raise lib.FbrError('只有原始轨迹摘录可以自动定位')
    manifest = _load_manifest(task)
    source = manifest.get(relative)
    if not source:
        raise lib.FbrError(f'轨迹文件不在摄取清单中：{relative}')
    path = _safe_path(task, relative)
    if _quick(path) != {'bytes': source['bytes'], 'mtime_ns': source['mtime_ns'],
                        'ctime_ns': source['ctime_ns']}:
        raise lib.FbrError('原始轨迹在摄取之后发生变化，须重新摄取')
    offsets = lib.load_json(_safe_path(task, source['offset_file']))
    with path.open('rb') as stream:
        located = _locate_excerpt(stream, source, offsets, excerpt)
    _record_access(task, 'resolve-excerpt', [{'path': relative}],
                   full_reads=1, file_opens=1)
    if located is None:
        raise lib.FbrError(
            f'摘录没有唯一位置：{relative}；请检查是否不存在或出现多次')
    return {'path': relative, 'start_line': located[0], 'end_line': located[0],
            'block': located[1], 'excerpt': excerpt}


def access_summary(task: Path) -> dict:
    rows = lib.read_jsonl(_access_path(Path(task)))
    return {
        "calls": len(rows),
        "full_reads": sum(int(row.get("full_reads", 0)) for row in rows),
        "indexed_reads": len(rows),
        "file_opens": sum(int(row.get("file_opens", 0)) for row in rows),
        "bytes_returned": sum(int(row.get("bytes_returned", 0)) for row in rows),
        "refs": sum(int(row.get("refs", 0)) for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "status", "pack"):
        command = subs.add_parser(name)
        command.add_argument("--task", required=True)
        if name == "ingest":
            command.add_argument("--force", action="store_true")
    for name in ("fetch", "verify"):
        command = subs.add_parser(name)
        command.add_argument("--task", required=True)
        command.add_argument("--ref-file")
        command.add_argument("--ref", action="append", default=[])
    args = parser.parse_args()
    try:
        task = Path(args.task).expanduser().resolve()
        if not (task / "task.json").is_file():
            raise lib.FbrError("task 目录缺少 task.json")
        if args.command == "ingest":
            result = ingest(task, force=args.force)
        elif args.command == "status":
            result = {**ingest(task), "access": access_summary(task)}
        elif args.command == "pack":
            ingest(task)
            result = lib.load_json(task / PACK)
        else:
            refs = []
            if args.ref_file:
                loaded = lib.load_json(Path(args.ref_file))
                if isinstance(loaded, dict):
                    loaded = loaded.get("refs") or loaded.get("sources") or [loaded]
                refs.extend(loaded)
            for raw in args.ref:
                refs.append(json.loads(raw))
            if not refs:
                raise lib.FbrError("至少提供一个 --ref 或 --ref-file")
            result = fetch_refs(task, refs, operation=args.command)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("state", "ok") != "failed" else 2
    except (lib.FbrError, OSError, ValueError) as exc:
        print("错误：" + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
