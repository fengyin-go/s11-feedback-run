#!/usr/bin/env python3
"""Local preflight checks for SOLO2 rules that cannot be repaired through Feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fbrlib as lib


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
NON_REPAIRABLE_QC_CHECKS = {
    "dedup",
    "dedup_semantic",
    "trace_complexity_floor",
    "trace_tool_call_floor",
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


def prompt_contract_problems(
    task_dir: Path,
    *,
    strict: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Check H3 and A-5 independently; legacy batches may omit both source files.

    Missing source files are blocking for new S11 batches. Legacy batches are
    allowed to continue in compatibility mode, but a manifest that exists and
    reports a failed gate is always blocking.
    """
    task_dir = Path(task_dir)
    parent = task_dir.parent
    topic_map = lib.load_json(parent / "repo-topic-map.json", default={})
    generation = lib.load_json(parent / "prompt-generation-manifest.json", default={})
    errors: list[dict] = []
    warnings: list[dict] = []
    if not topic_map:
        item = _problem(
            "prompt_topic_map_missing",
            "缺少 repo-topic-map.json，无法在运行前核对 P5/H3 和 workload_design",
            owner="s11-gsb-prompt-create",
            action="refresh_prompt_handoff",
        )
        (errors if strict else warnings).append(item)
    else:
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
                slices = workload.get("work_slices") if isinstance(workload, dict) else None
                paths = (
                    workload.get("verification_paths")
                    if isinstance(workload, dict)
                    else None
                )
                if (
                    not isinstance(slices, list)
                    or len(slices) < 4
                    or not isinstance(paths, list)
                    or len(set(paths)) < 2
                ):
                    errors.append(_problem(
                        "p6_workload_design_weak",
                        "workload_design 未达到 4 个工作切片和 2 条验证路径；工具调用次数不作为要求",
                        owner="s11-gsb-prompt-create",
                        action="expand_workload_before_run",
                        evidence={
                            "work_slices": len(slices) if isinstance(slices, list) else 0,
                            "verification_paths": len(set(paths)) if isinstance(paths, list) else 0,
                        },
                    ))

    if not generation:
        item = _problem(
            "prompt_generation_manifest_missing",
            "缺少 prompt-generation-manifest.json，无法复核 A-5 和跨项目查重结论",
            owner="s11-gsb-prompt-create",
            action="refresh_generation_manifest",
        )
        (errors if strict else warnings).append(item)
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


def evaluate(task_dir: Path, *, strict: bool = True) -> dict[str, Any]:
    task_dir = Path(task_dir)
    prompt_errors, prompt_warnings = prompt_contract_problems(task_dir, strict=strict)
    return {
        "version": 1,
        "state": "blocked" if prompt_errors else "ready",
        "errors": prompt_errors,
        "warnings": prompt_warnings,
        "checked_at": lib.now_iso(),
    }
