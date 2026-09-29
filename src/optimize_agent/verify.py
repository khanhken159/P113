"""Verify: Correctness Gate TRƯỚC, rồi mới đo tốc độ (CLAUDE.md 7.7). Fail -> loại."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import duckdb
import pandas as pd

from src.optimize_agent.compare import compare_frames
from src.optimize_agent.datasets.base import ModelSpec
from src.optimize_agent.rewrite import Assumption, Candidate
from src.optimize_agent.sandbox import SandboxError, validate_select_sql
from src.optimize_agent.settings import AgentConfig
from src.optimize_agent.timing import PlanMetrics, TimingResult, fetch_result, measure, profile

T = TypeVar("T")


@dataclass
class Baseline:
    timing: TimingResult
    plan: PlanMetrics


@dataclass
class CandidateResult:
    candidate: Candidate
    status: str  # accepted | rejected_gate | rejected_assumption | rejected_slow | error
    error: str | None = None
    gate: dict[str, Any] | None = None
    assumption_checks: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] | None = None
    rows_scanned: int | None = None
    peak_memory_bytes: int | None = None
    speedup: float | None = None
    cost_reduction_rows: float | None = None
    cost_reduction_memory: float | None = None
    plan_text: str = ""

    @property
    def passed_gate(self) -> bool:
        return bool(self.gate and self.gate.get("passed"))

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "candidate"}
        data["candidate"] = self.candidate.to_dict()
        data["passed_gate"] = self.passed_gate
        return data


def run_with_timeout(con: duckdb.DuckDBPyConnection, fn: Callable[[], T], timeout_s: float) -> T:
    """Chạy fn; quá timeout thì con.interrupt() -> duckdb.InterruptException."""
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        return fn()
    finally:
        timer.cancel()


def timeout_for(baseline: Baseline, cfg: AgentConfig) -> float:
    return max(cfg.optimize.candidate_timeout_min_s, baseline.timing.median_s * cfg.optimize.candidate_timeout_factor)


def check_assumptions(con: duckdb.DuckDBPyConnection, assumptions: list[Assumption], timeout_s: float) -> list[dict[str, Any]]:
    """Chạy SQL kiểm chứng từng giả định; holds=True khi số vi phạm = 0."""
    checks = []
    for assumption in assumptions:
        entry: dict[str, Any] = {"text": assumption.text, "check_sql": assumption.check_sql, "holds": None, "violations": None}
        if assumption.check_sql:
            try:
                sql = validate_select_sql(assumption.check_sql)
                value = run_with_timeout(con, lambda s=sql: con.execute(s).fetchone()[0], timeout_s)
                entry["violations"] = int(value)
                entry["holds"] = int(value) == 0
            except (SandboxError, duckdb.Error, TypeError, ValueError) as exc:
                entry["error"] = str(exc)
        checks.append(entry)
    return checks


def _reduction(opt: int, base: int) -> float | None:
    return None if base <= 0 else round(1 - opt / base, 4)


def verify_candidate(
    con: duckdb.DuckDBPyConnection,
    candidate: Candidate,
    expected: pd.DataFrame,
    context: tuple[ModelSpec, Baseline, AgentConfig],
) -> CandidateResult:
    """Gate -> kiểm chứng giả định -> đo median + EXPLAIN ANALYZE -> speedup."""
    model, baseline, cfg = context
    result = CandidateResult(candidate=candidate, status="error")
    timeout_s = timeout_for(baseline, cfg)
    try:
        sql = validate_select_sql(candidate.sql)
        actual = run_with_timeout(con, lambda: fetch_result(con, sql), timeout_s)
    except (SandboxError, duckdb.Error) as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:500]
        return result
    gate = compare_frames(expected, actual, key_columns=model.unique_key, ordered=model.ordered, tol=cfg.compare)
    result.gate = gate.to_dict()
    if not gate.passed:
        result.status = "rejected_gate"
        return result
    result.assumption_checks = check_assumptions(con, candidate.assumptions, timeout_s)
    if any(c["holds"] is False for c in result.assumption_checks):
        result.status = "rejected_assumption"
        return result
    _measure_candidate(con, sql, baseline, cfg, result)
    return result


def _measure_candidate(con: duckdb.DuckDBPyConnection, sql: str, baseline: Baseline, cfg: AgentConfig, result: CandidateResult) -> None:
    timing = measure(con, sql, cfg.timing)
    plan = profile(con, sql)
    result.timing = timing.to_dict()
    result.rows_scanned, result.peak_memory_bytes = plan.rows_scanned, plan.peak_memory_bytes
    result.plan_text = plan.plan_text
    result.speedup = round(baseline.timing.median_s / timing.median_s, 3) if timing.median_s > 0 else None
    result.cost_reduction_rows = _reduction(plan.rows_scanned, baseline.plan.rows_scanned)
    result.cost_reduction_memory = _reduction(plan.peak_memory_bytes, baseline.plan.peak_memory_bytes)
    fast_enough = result.speedup is not None and result.speedup >= cfg.optimize.min_speedup
    result.status = "accepted" if fast_enough else "rejected_slow"
