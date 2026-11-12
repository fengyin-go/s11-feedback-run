"""Evidence-based retry accounting. No worker, budget or platform side effects.

``classify_attempt(record)`` classifies terminal evidence, never retry history.
``ledger(attempts)`` counts physical dispatches and separate retry classes.
``admission(attempts, cfg)`` applies run limits before another dispatch.
``ensure_attempt_waiting_base`` persists the first segment's waiting baseline;
``attempt_waiting`` subtracts it from the current cumulative waiting counter.

Unknown historical failures consume a model attempt conservatively. Push records
are delivery operations, so do not consume generation/physical attempt quotas.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Mapping

import fbrlib as lib


def _decision(category, action, reason, *, unknown=False):
    return {"category": category, "action": action, "retryable": action == "retry",
            "unknown": unknown, "reason": reason}


def _terminal_payload(record):
    """Read designated terminal fields only; never scan arbitrary tool output.

    api_retry/retry_events/history are deliberately excluded: a recovered retry
    does not establish the cause of a later timeout or a model failure.
    """
    texts = []
    statuses = []
    def add(value):
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, Mapping):
            for key in ("status", "status_code", "error_status", "http_status"):
                status = value.get(key)
                if isinstance(status, (int, str)) and str(status).isdigit():
                    statuses.append(int(status))
            for key in ("error", "message", "text", "result", "detail", "body", "payload"):
                if key in value:
                    add(value[key])
    for key in ("error", "api_error", "terminal_error", "error_payload", "result",
                "detail", "stderr_tail"):
        if key in record:
            add(record[key])
    # Top-level status is accepted only when the record identifies an API error.
    if any(record.get(k) for k in ("error", "api_error", "terminal_error", "error_payload")):
        for key in ("error_status", "http_status", "status_code"):
            value = record.get(key)
            if isinstance(value, (int, str)) and str(value).isdigit():
                statuses.append(int(value))
    text = "\n".join(texts)
    statuses += [int(x) for x in re.findall(
        r"\b(?:API\s+Error|HTTP(?:\s+Error)?|status(?:_code)?)[\s:=]+(\d{3})\b", text, re.I)]
    return text, statuses


def classify_attempt(record: Mapping) -> dict:
    """Return category(model/throttle/infra/push/unknown/budget) and action.

    ``termination_reason`` is the authoritative monitor cause. A generic legacy
    timeout stays unknown even if an API retry was observed earlier. Only a
    terminal API payload (not a keyword in arbitrary tool output) establishes
    an API error. ``action`` is accept/retry/defer/stop.
    """
    # Explicit, evidence-backed reconciliation of historical records wins over
    # legacy heuristic classification. A stop protocol error still stays stop.
    explicit = record.get("failure_category")
    if explicit in ("model", "throttle", "infra", "push", "unknown", "budget"):
        text, statuses = _terminal_payload(record)
        protocol = (400 in statuses and "context_management" in text.lower()
                    and ("not supported" in text.lower() or "unsupported" in text.lower()))
        action = record.get("failure_action")
        if action not in ("accept", "retry", "stop", "defer"):
            action = "defer" if explicit == "budget" else record.get("verdict")
            action = action if action in ("accept", "stop") else "retry"
            if explicit == "budget":
                action = "defer"
        return _decision(explicit, "stop" if protocol else action,
                         "api_protocol_context_management" if protocol else
                         record.get("termination_reason") or "explicit_failure_category",
                         unknown=explicit == "unknown")
    if record.get("stage") in ("push", "publish") or record.get("operation") in ("push", "publish"):
        good = record.get("verdict") == "accept" or record.get("state") in ("ok", "published")
        return _decision("push", "accept" if good else "retry", "snapshot_publish")
    termination = record.get("termination_reason", "")
    if not termination and isinstance(record.get("termination"), Mapping):
        termination = record["termination"].get("reason", "")
    # Explicit protocol failure always blocks an unchanged configuration.
    text, statuses = _terminal_payload(record)
    low = text.lower()
    if 400 in statuses and "context_management" in low and (
            "not supported" in low or "unsupported" in low):
        return _decision("infra", "stop", "api_protocol_context_management")
    # Known local termination takes precedence over past/recovered API errors.
    if termination in ("batch_budget", "worker_guard"):
        return _decision("budget", "defer", termination)
    if termination == "task_budget":
        return _decision("model", "defer", termination)
    if termination in ("timeout", "stalled", "model_timeout", "zero_code_changes", "interrupted"):
        return _decision("model", "retry", termination)
    if termination in ("gateway_wait_exhausted", "gateway_wait_limit", "gateway_wait_total"):
        return _decision("throttle" if any(s in (429, 500, 502, 503, 504, 529) for s in statuses)
                         else "infra", "retry", termination)
    if termination in ("worker_crash", "container_crash", "oom_killed",
                       "gateway_error", "network_error"):
        return _decision("infra", "retry", termination)
    # Accept is checked before residual text/status fields so successful recovery
    # is a model run, never an infrastructure failure.
    if record.get("verdict") == "accept":
        return _decision("model", "accept", "completed")
    if 429 in statuses or re.search(r"\b(?:rate_limit_error|overloaded_error)\b", text):
        return _decision("throttle", "retry", "api_throttle")
    if any(s in (401, 403) for s in statuses):
        return _decision("infra", "stop", "api_authentication")
    if any(400 <= s < 500 and s not in (408, 429) for s in statuses):
        return _decision("infra", "stop", "api_invalid_request")
    if any(s in (500, 502, 503, 504, 529) for s in statuses):
        return _decision("throttle", "retry", "api_gateway_pressure")
    if 408 in statuses:
        return _decision("infra", "retry", "api_transient")
    reason = str(record.get("reason", ""))
    if record.get("timed_out") or "进程超时" in reason:
        return _decision("unknown", "retry", "legacy_timeout_without_termination", unknown=True)
    if record.get("verdict") == "stop":
        return _decision("unknown", "stop", "unclassified_stop", unknown=True)
    if reason in ("零代码改动", "无代码改动", "无故中断", "模型中断", "没有代码改动"):
        return _decision("model", "retry", "model_incomplete")
    return _decision("unknown", "retry", "unclassified_failure", unknown=True)


def ledger(attempts) -> dict:
    """Count every dispatched generation record once; unknown also counts model.

    A caller may include preparation-only records using ``dispatched: false``;
    those do not consume any quota. Never pass individual continuation segments
    as separate attempts. No records or historical counters are rewritten.
    """
    counts = {"physical": 0, "model": 0, "infra": 0, "throttle": 0,
              "push": 0, "unknown": 0, "budget": 0}
    decisions = []
    for index, record in enumerate(attempts):
        d = classify_attempt(record)
        d = {"index": index, "attempt": record.get("n", record.get("attempt")), **d}
        decisions.append(d)
        if record.get("dispatched") is False:
            d["counted"] = False
            continue
        d["counted"] = True
        category = d["category"]
        if category == "push":
            counts["push"] += 1
            continue
        counts["physical"] += 1
        counts["model" if category == "unknown" else category] += 1
        if d["unknown"]:
            counts["unknown"] += 1
    return {**counts, "decisions": decisions}


def _limit(cfg, name, default):
    value = cfg.get(name, default)
    if value is None and default is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer" + (" or null" if default is None else ""))
    return value


def admission(attempts, cfg: Mapping) -> dict:
    """Apply independent quotas; accepts a run config or full settings dict.

    Model max defaults to 3, infra to 3, throttle to 8. The optional physical
    cap is a hard authorization boundary. Infra/throttle counters are bounded
    cumulative totals (not consecutive counters); exhausting a class halts
    further retries of that class. Model/physical quotas bound every dispatch.
    Latest stop decisions block unchanged configuration. Deferred budget reasons
    are checked against current absolute budgets by the caller, allowing an
    authorized extension without rewriting the historical termination record.
    """
    cfg = cfg.get("run", cfg)
    totals = ledger(attempts)
    limits = {"model": _limit(cfg, "max_attempts", 3),
              "infra": _limit(cfg, "max_infra_attempts", 3),
              "throttle": _limit(cfg, "max_throttle_attempts", 8),
              "physical": _limit(cfg, "max_physical_attempts", None)}
    result = {"allowed": True, "reason": "allowed", "ledger": totals, "limits": limits}
    latest = next((x for x in reversed(totals["decisions"])
                   if x["counted"] and x["category"] != "push"), None)
    def deny(reason):
        return {**result, "allowed": False, "reason": reason}
    if latest and latest["action"] == "stop":
        return deny(latest["reason"])
    if limits["physical"] is not None and totals["physical"] >= limits["physical"]:
        return deny("max_physical_attempts")
    if totals["model"] >= limits["model"]:
        return deny("max_attempts")
    if latest and latest["category"] in ("infra", "throttle"):
        category = latest["category"]
        if totals[category] >= limits[category]:
            return deny("max_" + category + "_attempts")
    return result


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite nonnegative seconds")
    return float(value)


def attempt_waiting(current: Mapping, cumulative_waiting) -> float:
    """Waiting in this physical attempt, including all continuation segments.

    Missing legacy baseline raises rather than silently granting a new allowance.
    The caller must establish a historical start budget or conservatively use 0.
    """
    if "attempt_waiting_base" not in current:
        raise ValueError("missing attempt_waiting_base; recover the original attempt baseline")
    base = _seconds(current["attempt_waiting_base"], "attempt_waiting_base")
    if isinstance(cumulative_waiting, Mapping):
        cumulative_waiting = cumulative_waiting.get("waiting_seconds", 0)
    total = _seconds(cumulative_waiting, "waiting_seconds")
    if total < base:
        raise ValueError("cumulative waiting counter regressed below attempt_waiting_base")
    return total - base


def ensure_attempt_waiting_base(task_dir: Path, current: dict, budget: Mapping) -> dict:
    """Persist a first-dispatch baseline without resetting it at continuation.

    Call under the existing per-task operation lock, immediately before the
    first dispatch. Legacy dispatched attempts require an explicit recovered
    baseline; deriving it from the *current* counter would erase elapsed wait.
    This helper writes only run/current.json, never budget.json.
    """
    path = Path(task_dir) / "run/current.json"
    saved = lib.load_json(path, default={})
    if saved and saved.get("attempt") != current.get("attempt"):
        raise ValueError("attempt changed while persisting waiting baseline")
    if "attempt_waiting_base" in saved:
        current["attempt_waiting_base"] = _seconds(saved["attempt_waiting_base"], "attempt_waiting_base")
        return current
    if "attempt_waiting_base" not in current:
        if current.get("request_id") or current.get("pid") or current.get("segment", 0) > 0:
            raise ValueError("dispatched legacy attempt needs recovered attempt_waiting_base")
        current["attempt_waiting_base"] = _seconds(budget.get("waiting_seconds", 0), "waiting_seconds")
    else:
        _seconds(current["attempt_waiting_base"], "attempt_waiting_base")
    lib.save_json(path, current)
    return current
