#!/usr/bin/env python3
"""Local preflight checks for SOLO2 rules that cannot be repaired through Feedback."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fbrlib as lib


MIN_EFFECTIVE_TOOL_CALLS = 50
MIN_PROMPT_EXPECTED_TOOL_CALLS = 55
H3_FALSE_GUARDS = {
    "one_branch_violates_constraints",
    "fixed_resolution_in_prompt",
    "implementation_only",
    "retry_or_error_message_only",
    "display_only",
    "single_result_invariant_only",
    "source_selection_only",
    "equivalent_user_outcome",
}
MEMORY_PATH_RE = re.compile(
    r"(?:^|[\s\"'])(?:/home/[^/\s]+/|/root/|~/)?"
    r"\.claude/(?:projects|todos|sessions|history\.jsonl|memory)(?:/|[\s\"']|$)",
    re.IGNORECASE,
)
MEMORY_CONTENT_RE = re.compile(
    r"(?:queue-operation|sessionId|promptId)",
    re.IGNORECASE,
)

NON_REPAIRABLE_QC_CHECKS = {
    "dedup",
    "dedup_semantic",
    "trace_complexity_floor",
    "trace_tool_call_floor",
    "trace_memory",
    "trace_skill",
    "trace_standard_leak",
    "trace_docker_call",
}


def _problem(
    code: str,
    message: str,
    *,
    owner: str,
    action: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "owner": owner,
        "action": action,
        "evidence": evidence or {},
    }


def prompt_contract_problems(task_dir: Path) -> tuple[list[dict], list[dict]]:
    """Check the frozen prompt-generation contract before spending quota."""
    task_dir = Path(task_dir)
    parent = task_dir.parent
    topic_map = lib.load_json(parent / "repo-topic-map.json", default={})
    generation = lib.load_json(parent / "prompt-generation-manifest.json", default={})
    errors: list[dict] = []
    warnings: list[dict] = []
    if not topic_map:
        warnings.append(_problem(
            "prompt_topic_map_missing",
            "缺少 repo-topic-map.json，无法在运行前核对 P5/H3 和 workload_design",
            owner="s11-gsb-prompt-create",
            action="refresh_prompt_handoff",
        ))
        return errors, warnings

    version = topic_map.get("difficulty_contract_version")
    if type(version) is not int or version < 3:
        errors.append(_problem(
            "p5_contract_legacy",
            f"题目难度契约版本为 {version!r}，低于 H3 v3，提交后存在 P5 废弃风险",
            owner="s11-gsb-prompt-create",
            action="rebuild_prompt_with_h3_v3",
        ))
    try:
        sequence = int(task_dir.name.split("-")[1])
    except (IndexError, ValueError):
        sequence = -1
    plan = next(
        (
            item for item in topic_map.get("task_plan") or []
            if isinstance(item, dict) and item.get("sequence") == sequence
        ),
        None,
    )
    if not isinstance(plan, dict):
        errors.append(_problem(
            "prompt_plan_missing",
            "task_plan 中找不到当前题位，不能冻结难度和查重结论",
            owner="s11-gsb-prompt-create",
            action="refresh_prompt_handoff",
        ))
    else:
        design = plan.get("difficulty_design")
        if not isinstance(design, dict) or design.get("version") != 3:
            errors.append(_problem(
                "p5_h3_design_missing",
                "当前题位没有 difficulty_design v3，P5 平台检查风险不可接受",
                owner="s11-gsb-prompt-create",
                action="rebuild_prompt_with_h3_v3",
            ))
        else:
            tradeoff = design.get("design_tradeoff")
            guards = (
                tradeoff.get("false_h3_guards")
                if isinstance(tradeoff, dict)
                else None
            )
            missing = sorted(
                name for name in H3_FALSE_GUARDS
                if not isinstance(guards, dict) or guards.get(name) is not False
            )
            if missing:
                errors.append(_problem(
                    "p5_h3_guard_missing",
                    "H3 假取舍守卫未全部关闭：" + "、".join(missing),
                    owner="s11-gsb-prompt-create",
                    action="redesign_h3_choice",
                    evidence={"missing_guards": missing},
                ))
            workload = design.get("workload_design")
            expected = (
                workload.get("expected_effective_tool_calls")
                if isinstance(workload, dict)
                else None
            )
            slices = workload.get("work_slices") if isinstance(workload, dict) else None
            paths = (
                workload.get("verification_paths")
                if isinstance(workload, dict)
                else None
            )
            if (
                type(expected) is not int
                or expected < MIN_PROMPT_EXPECTED_TOOL_CALLS
                or not isinstance(slices, list)
                or len(slices) < 4
                or not isinstance(paths, list)
                or len(set(paths)) < 2
            ):
                errors.append(_problem(
                    "p6_workload_design_weak",
                    "workload_design 未达到 55 次预期调用、4 个工作切片和 2 条验证路径",
                    owner="s11-gsb-prompt-create",
                    action="expand_workload_before_run",
                    evidence={
                        "expected_effective_tool_calls": expected,
                        "work_slices": len(slices) if isinstance(slices, list) else 0,
                        "verification_paths": len(set(paths)) if isinstance(paths, list) else 0,
                    },
                ))

    if not generation:
        warnings.append(_problem(
            "prompt_generation_manifest_missing",
            "缺少 prompt-generation-manifest.json，无法复核 A-5 和跨项目查重结论",
            owner="s11-gsb-prompt-create",
            action="refresh_generation_manifest",
        ))
    else:
        if generation.get("raw_a5_ok") is False:
            errors.append(_problem(
                "raw_a5_failed",
                "提示词生成清单显示 A-5 公共长片段未通过",
                owner="s11-gsb-prompt-create",
                action="rewrite_prompt_story_shape",
            ))
        cross = generation.get("cross_project")
        if isinstance(cross, dict) and cross.get("platform_dedup_ok") is False:
            errors.append(_problem(
                "platform_dedup_failed",
                "提示词生成清单显示平台历史语料查重未通过",
                owner="s11-gsb-prompt-create",
                action="rewrite_prompt_story_shape",
            ))
        if generation.get("gate_ok") is False:
            errors.append(_problem(
                "prompt_gate_failed",
                "提示词生成清单的整批硬闸未通过",
                owner="s11-gsb-prompt-create",
                action="rerun_prompt_generation_gates",
            ))
    return errors, warnings


def _memory_hit(event: dict[str, Any]) -> dict[str, Any] | None:
    tool = str(event.get("tool") or "")
    target = str(event.get("target") or "")
    preview = str(event.get("result_preview") or "")
    if tool not in {"Read", "Bash", "Grep", "Glob"}:
        return None
    # Oversized tool output is persisted under projects/*/tool-results. Reading
    # that file is a normal continuation of the tool call, not transcript access.
    if "/tool-results/" in target and not target.endswith(".jsonl"):
        return None
    # Claude persists oversized tool output under .claude/projects. That path
    # appearing in a result preview is a harness artifact, not a model read.
    path_match = MEMORY_PATH_RE.search(target)
    persisted_output = (
        preview.lstrip().startswith("<persisted-output>")
        or "Full output saved to:" in preview
    )
    content_match = None
    if not persisted_output and ".claude/" in target.lower():
        content_match = MEMORY_CONTENT_RE.search(preview)
    if not path_match and not content_match:
        return None
    matched = path_match or content_match
    assert matched is not None
    return {
        "tool": tool,
        "target": lib.clip(target, 500),
        "result_preview": lib.clip(preview, 500),
        "matched": matched.group(0),
        "severity": (
            "hard"
            if re.search(
                r"\.claude/(?:projects/[^\"'\s]*\.jsonl|todos|history\.jsonl|memory)",
                target,
                re.IGNORECASE,
            )
            else "soft"
        ),
    }


def trace_rule_problems(
    task_dir: Path,
    *,
    enforce_call_floor: bool = True,
) -> tuple[list[dict], list[dict], dict]:
    """Check P6 call floor and private transcript access from the bounded review pack."""
    task_dir = Path(task_dir)
    pack = lib.load_json(task_dir / "trace/review-pack.json", default={})
    errors: list[dict] = []
    warnings: list[dict] = []
    summary = pack.get("summary") if isinstance(pack, dict) else {}
    if not isinstance(summary, dict) or not summary:
        warnings.append(_problem(
            "trace_policy_pack_missing",
            "缺少轨迹 review-pack，提交前无法核对 P6 和私有轨迹读取",
            owner="s11-feedback-run",
            action="reingest_trace",
        ))
        return errors, warnings, {}

    effective = summary.get("paired_tools")
    if type(effective) is not int:
        effective = summary.get("logical_tool_uses")
    if (
        enforce_call_floor
        and type(effective) is int
        and effective < MIN_EFFECTIVE_TOOL_CALLS
    ):
        errors.append(_problem(
            "p6_tool_call_floor",
            f"有效工具调用 {effective} 次，低于平台下限 {MIN_EFFECTIVE_TOOL_CALLS}",
            owner="s11-gsb-prompt-create",
            action="prompt_rebuild_required",
            evidence={"valid_tool_calls": effective, "floor": MIN_EFFECTIVE_TOOL_CALLS},
        ))

    hits = []
    for index, event in enumerate(pack.get("tool_events") or [], start=1):
        if not isinstance(event, dict):
            continue
        hit = _memory_hit(event)
        if hit:
            hits.append({"step": index, **hit})
    hard_hits = [hit for hit in hits if hit.get("severity") == "hard"]
    soft_hits = [hit for hit in hits if hit.get("severity") != "hard"]
    if hard_hits:
        errors.append(_problem(
            "trace_memory_read",
            "轨迹读取了 .claude 下的私有会话文件，平台将按 memory 作弊处理",
            owner="s11-feedback-run",
            action="runtime_rerun_required",
            evidence={"hits": hard_hits[:5]},
        ))
    if soft_hits:
        warnings.append(_problem(
            "trace_private_state_probe",
            "轨迹探测了 .claude 私有运行状态或会话元数据；未读取答案正文但需换干净会话",
            owner="s11-feedback-run",
            action="inspect_runtime_isolation",
            evidence={"hits": soft_hits[:5]},
        ))
    return errors, warnings, {
        "effective_tool_calls": effective,
        "memory_read": bool(hard_hits),
        "memory_probe": bool(soft_hits),
        "memory_hits": hard_hits[:5],
        "private_state_hits": soft_hits[:5],
    }


def evaluate(task_dir: Path) -> dict[str, Any]:
    task_dir = Path(task_dir)
    prompt_errors, prompt_warnings = prompt_contract_problems(task_dir)
    generation_managed = (
        (task_dir.parent / "repo-topic-map.json").is_file()
        or (task_dir.parent / "prompt-generation-manifest.json").is_file()
    )
    trace_errors, trace_warnings, trace_summary = trace_rule_problems(
        task_dir,
        enforce_call_floor=generation_managed,
    )
    errors = prompt_errors + trace_errors
    warnings = prompt_warnings + trace_warnings
    return {
        "version": 1,
        "state": "blocked" if errors else "ready",
        "errors": errors,
        "warnings": warnings,
        "trace": trace_summary,
        "checked_at": lib.now_iso(),
    }
