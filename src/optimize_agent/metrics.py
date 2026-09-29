"""Chỉ số báo cáo (CLAUDE.md 5.2), tính từ kết quả từng model của một hoặc nhiều lần chạy.

Quy ước đã chốt với người dùng:
- correctness_rate (đề xuất) = #đề xuất qua gate / #đề xuất — phải = 100% vì đề xuất sai bị loại.
- raw_candidate_pass_rate = #ứng viên qua gate / #ứng viên sinh ra — cho thấy gate đã chặn bao nhiêu.
- model_gate_rate = #model có ≥1 ứng viên qua gate / #model được tối ưu (model chậm có ≥1 ứng viên).
- Một lần chạy "đúng" (pass@k / pass^k) = model chậm có đề xuất qua gate VÀ speedup ≥ min_speedup.
"""

from __future__ import annotations

import math
import statistics
from typing import Any


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _geomean(values: list[float]) -> float | None:
    positive = [v for v in values if v and v > 0]
    return round(math.exp(sum(math.log(v) for v in positive) / len(positive)), 4) if positive else None


def run_success(model: dict[str, Any], min_speedup: float) -> bool:
    """Lần chạy đúng: có đề xuất qua gate và speedup ≥ min_speedup."""
    proposal = model.get("proposal")
    return bool(proposal and proposal.get("passed_gate") and (proposal.get("speedup") or 0) >= min_speedup)


def _candidate_stats(models: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [c for m in models for c in m.get("candidates", []) if c["candidate"]["sql"]]
    optimized = [m for m in models if any(c["candidate"]["sql"] for c in m.get("candidates", []))]
    with_pass = [m for m in optimized if any(c["passed_gate"] for c in m["candidates"])]
    proposals = [m["proposal"] for m in models if m.get("proposal")]
    return {
        "n_candidates": len(candidates),
        "raw_candidate_pass_rate": _ratio(sum(c["passed_gate"] for c in candidates), len(candidates)),
        "n_models_optimized": len(optimized),
        "model_gate_rate": _ratio(len(with_pass), len(optimized)),
        "n_proposals": len(proposals),
        "correctness_rate": _ratio(sum(bool(p["passed_gate"]) for p in proposals), len(proposals)),
    }


def _speed_stats(models: list[dict[str, Any]], min_speedup: float) -> dict[str, Any]:
    proposals = [m["proposal"] for m in models if m.get("proposal")]
    speedups = [p["speedup"] for p in proposals if p.get("speedup")]
    slow = [m for m in models if m["detect"]["is_slow"]]
    improved = [m for m in slow if run_success(m, min_speedup)]
    return {
        "n_slow": len(slow),
        "speedup_median": _median(speedups),
        "speedup_geomean": _geomean(speedups),
        "cost_reduction_rows_median": _median([p["cost_reduction_rows"] for p in proposals if p.get("cost_reduction_rows") is not None]),
        "cost_reduction_memory_median": _median([p["cost_reduction_memory"] for p in proposals if p.get("cost_reduction_memory") is not None]),
        "improvement_rate": _ratio(len(improved), len(slow)),
    }


def _cost_stats(models: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(models)
    return {
        "llm_calls_total": sum(m["llm"]["calls"] for m in models),
        "agent_cost_usd_avg": round(sum(m["llm"]["cost_usd"] for m in models) / n, 6) if n else None,
        "agent_steps_avg": round(sum(m.get("steps", 0) for m in models) / n, 2) if n else None,
    }


def compute_metrics(models: list[dict[str, Any]], min_speedup: float) -> dict[str, Any]:
    """Chỉ số cho một lần chạy trên một dataset."""
    metrics: dict[str, Any] = {"n_models": len(models)}
    metrics["srdt"] = _ratio(sum(bool(m["final_correct"]) for m in models), len(models))
    metrics["baseline_correct_rate"] = _ratio(sum(bool(m["baseline"]["correct"]) for m in models), len(models))
    metrics.update(_candidate_stats(models))
    metrics.update(_speed_stats(models, min_speedup))
    metrics.update(_cost_stats(models))
    return metrics


def pass_at_k(runs: list[list[dict[str, Any]]], min_speedup: float) -> dict[str, Any]:
    """pass@k: ≥1 lần đúng; pass^k: cả k lần đều đúng. Chỉ xét model chậm."""
    by_model: dict[str, list[bool]] = {}
    for models in runs:
        for m in models:
            if m["detect"]["is_slow"]:
                by_model.setdefault(m["name"], []).append(run_success(m, min_speedup))
    n = len(by_model)
    return {
        "k": len(runs),
        "n_slow_models": n,
        "pass_at_k": _ratio(sum(any(v) for v in by_model.values()), n),
        "pass_hat_k": _ratio(sum(all(v) and len(v) == len(runs) for v in by_model.values()), n),
    }


def rejection_rate(review: dict[str, Any]) -> float | None:
    """#đề xuất bị Reviewer từ chối / #đề xuất đã review (review.json do Engineer điền)."""
    decisions = [d.get("decision") for d in review.get("proposals", {}).values() if d.get("decision") in ("approve", "reject")]
    return _ratio(sum(d == "reject" for d in decisions), len(decisions))
