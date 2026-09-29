"""Optimizer Agent: quy trình 8 bước cho mỗi model (CLAUDE.md mục 7).

read -> profile -> baseline -> detect -> analyze -> rewrite (sqlglot -> rules -> LLM) -> verify -> report.
Mỗi bước ghi vào reports/<run_id>/steps.jsonl; chạy lại cùng run_id sẽ bỏ qua model đã xong.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.optimize_agent.analyze import Finding, analyze_plan, analyze_sql
from src.optimize_agent.compare import compare_frames
from src.optimize_agent.datasets.base import ModelSpec
from src.optimize_agent.detect import detect_slow
from src.optimize_agent.physical import physical_candidate
from src.optimize_agent.profiler import profile_tables, schema_for
from src.optimize_agent.rewrite import Candidate
from src.optimize_agent.rewrite.llm_rewriter import LLMContext, api_key, llm_candidate
from src.optimize_agent.rewrite.rules import rules_candidate
from src.optimize_agent.rewrite.sqlglot_rewriter import sqlglot_candidate
from src.optimize_agent.settings import AgentConfig
from src.optimize_agent.steps_log import StepLog
from src.optimize_agent.timing import PlanMetrics, TimingResult, fetch_result, measure, profile
from src.optimize_agent.verify import Baseline, CandidateResult, verify_candidate


def safe_name(model_name: str) -> str:
    return model_name.replace("/", "__")


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def load_expected(con: duckdb.DuckDBPyConnection, model: ModelSpec) -> pd.DataFrame:
    """Kết quả tham chiếu: query gốc (TPC-H) hoặc ground truth CSV (ELT-Bench)."""
    if model.reference_sql:
        return fetch_result(con, model.reference_sql)
    if model.ground_truth_csv:
        return pd.read_csv(model.ground_truth_csv, dtype=str, keep_default_na=False)
    raise ValueError(f"Model {model.name} không có kết quả tham chiếu")


class BaselineCache:
    """reports/baseline/<dataset>.json — kết quả `make baseline`, dùng lại cho `make optimize`."""

    def __init__(self, cache_dir: Path, dataset: str) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = cache_dir / f"{dataset}.json"
        self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, model: ModelSpec) -> dict[str, Any] | None:
        entry = self.data.get(model.name)
        return entry if entry and entry.get("sql_hash") == sql_hash(model.sql) else None

    def put(self, model: ModelSpec, entry: dict[str, Any]) -> None:
        self.data[model.name] = {**entry, "sql_hash": sql_hash(model.sql)}
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def baseline_from_entry(entry: dict[str, Any]) -> Baseline:
    timing = TimingResult(runs_s=entry["timing"]["runs_s"], median_s=entry["timing"]["median_s"])
    plan = PlanMetrics(**entry["plan"])
    return Baseline(timing=timing, plan=plan)


@dataclass
class ModelContext:
    """Mọi thứ agent đã đọc/đo về một model, dùng chung cho các bước rewrite/verify."""

    model: ModelSpec
    schema: dict[str, Any]
    profiles: list[dict[str, Any]]
    findings: list[Finding]
    expected: pd.DataFrame
    base: Baseline


@dataclass
class AgentOptions:
    use_llm: bool = True
    baseline_only: bool = False


class OptimizerAgent:
    def __init__(self, con: duckdb.DuckDBPyConnection, cfg: AgentConfig, run_dir: Path, cache: BaselineCache) -> None:
        self.con, self.cfg, self.run_dir, self.cache = con, cfg, run_dir, cache
        self.log = StepLog(run_dir)
        (run_dir / "models").mkdir(parents=True, exist_ok=True)

    # ---- bước 1-3 -------------------------------------------------------------------------
    def read_context(self, model: ModelSpec) -> dict[str, Any]:
        schema = schema_for(self.con, model.sql)
        self.log.record(model.name, "read", "done", {"tables": list(schema), "spec_chars": len(model.spec)})
        return schema

    def profile_data(self, model: ModelSpec) -> list[dict[str, Any]]:
        profiles = profile_tables(self.con, model.sql)
        self.log.record(model.name, "profile", "done", {"tables": [p["table"] for p in profiles]})
        return profiles

    def baseline(self, model: ModelSpec, expected: pd.DataFrame) -> dict[str, Any]:
        """Đo SQL gốc (median + EXPLAIN ANALYZE) và kiểm tra SQL gốc có khớp tham chiếu không."""
        entry = self.cache.get(model)
        if entry is None:
            timing = measure(self.con, model.sql, self.cfg.timing)
            plan = profile(self.con, model.sql)
            gate = compare_frames(expected, fetch_result(self.con, model.sql), model.unique_key, model.ordered, self.cfg.compare)
            entry = {"timing": timing.to_dict(), "plan": plan.to_dict(), "gate": gate.to_dict()}
            self.cache.put(model, entry)
        self.log.record(model.name, "baseline", "done", {"median_s": entry["timing"]["median_s"], "correct": entry["gate"]["passed"]})
        return entry

    # ---- bước 4-7 -------------------------------------------------------------------------
    def verify(self, model: ModelSpec, candidate: Candidate, expected: pd.DataFrame, base: Baseline) -> CandidateResult:
        result = verify_candidate(self.con, candidate, expected, (model, base, self.cfg))
        self.log.record(model.name, f"verify:{candidate.source}", "done", {"status": result.status, "speedup": result.speedup})
        return result

    def deterministic_candidates(self, model: ModelSpec, schema: dict[str, Any]) -> list[Candidate]:
        candidates = [c for c in (sqlglot_candidate(model.sql, schema), rules_candidate(model.sql, schema)) if c and c.sql]
        self.log.record(model.name, "rewrite:deterministic", "done", {"count": len(candidates)})
        return candidates[: self.cfg.optimize.max_candidates]

    def llm_candidates(self, mctx: ModelContext, ctx: LLMContext, budget: int) -> list[CandidateResult]:
        """Gọi LLM tối đa `budget` lần; mỗi lần thất bại được phản hồi lại cho lần sau."""
        model, results = mctx.model, []
        for _ in range(budget):
            candidate = llm_candidate(ctx, self.cfg.llm)
            self.log.record(model.name, "rewrite:llm", "done", {"usage": candidate.llm_usage, "has_sql": bool(candidate.sql)})
            if not candidate.sql:
                results.append(CandidateResult(candidate=candidate, status="error", error=candidate.explanation))
                if candidate.llm_usage is None:
                    break  # LLM tắt/lỗi kết nối: không thử lại
                continue
            result = self.verify(model, candidate, mctx.expected, mctx.base)
            results.append(result)
            if result.status == "accepted":
                break
            ctx.previous_failures.append(describe_failure(result))
        return results

    # ---- toàn bộ quy trình ----------------------------------------------------------------
    def run_model(self, model: ModelSpec, options: AgentOptions) -> dict[str, Any]:
        out_path = self.run_dir / "models" / f"{safe_name(model.name)}.json"
        if self.log.get_done(model.name, "report") and out_path.exists():
            return json.loads(out_path.read_text(encoding="utf-8"))
        result = self._run(model, options)
        result["steps"] = self.log.count_steps(model.name) + 1
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.log.record(model.name, "report", "done", {"path": str(out_path)})
        return result

    def _run(self, model: ModelSpec, options: AgentOptions) -> dict[str, Any]:
        schema = self.read_context(model)
        profiles = self.profile_data(model)
        expected = load_expected(self.con, model)
        entry = self.baseline(model, expected)
        base = baseline_from_entry(entry)
        detect = detect_slow(base.timing, base.plan, self.cfg.detect)
        self.log.record(model.name, "detect", "done", detect.to_dict())
        findings = analyze_sql(model.sql, schema) + analyze_plan(base.plan, self.cfg.detect.large_scan_rows)
        self.log.record(model.name, "analyze", "done", {"codes": sorted({f.code for f in findings})})
        result = model_summary(model, schema, profiles, entry, detect.to_dict(), [f.to_dict() for f in findings])
        if options.baseline_only or not detect.is_slow:
            return result
        ctx = ModelContext(model, schema, profiles, findings, expected, base)
        result = finalize(result, self._optimize(ctx, options))
        result["physical"] = self._physical(ctx, result)
        return result

    def _physical(self, ctx: ModelContext, result: dict[str, Any]) -> dict[str, Any] | None:
        """Đề xuất sắp xếp dữ liệu vật lý khi query vẫn chậm sau rewrite SQL (hoặc physical=always)."""
        mode = self.cfg.optimize.physical
        proposal = result.get("proposal")
        best_s = proposal["timing"]["median_s"] if proposal else ctx.base.timing.median_s
        if mode == "off" or (mode == "auto" and best_s <= self.cfg.detect.slow_threshold_s):
            return None
        sql = proposal["candidate"]["sql"] if proposal else ctx.model.sql
        candidate = physical_candidate(self.con, sql, ctx.schema, self.cfg.detect.large_scan_rows)
        if candidate is None:
            self.log.record(ctx.model.name, "physical", "skipped", {"reason": "không có cột lọc trên bảng lớn"})
            return None
        return self.verify(ctx.model, candidate, ctx.expected, ctx.base).to_dict()

    def _optimize(self, ctx: ModelContext, options: AgentOptions) -> list[CandidateResult]:
        """sqlglot + rule trước; chỉ gọi LLM khi chưa có ứng viên đạt và còn ngân sách ứng viên."""
        model = ctx.model
        results = [self.verify(model, c, ctx.expected, ctx.base) for c in self.deterministic_candidates(model, ctx.schema)]
        budget = self.cfg.optimize.max_candidates - len(results)
        llm_on = options.use_llm and self.cfg.optimize.use_llm and api_key() is not None
        if budget > 0 and llm_on and not any(r.status == "accepted" for r in results):
            llm_ctx = LLMContext(
                sql=model.sql,
                spec=model.spec,
                schema=ctx.schema,
                profile=ctx.profiles,
                findings=[f.to_dict() for f in ctx.findings],
                plan_text=ctx.base.plan.plan_text,
                previous_failures=[describe_failure(r) for r in results],
            )
            results += self.llm_candidates(ctx, llm_ctx, budget)
        return results


def describe_failure(result: CandidateResult) -> str:
    """Tóm tắt vì sao ứng viên bị loại, để phản hồi cho LLM và ghi report."""
    reason = result.error or ""
    if result.gate and not result.gate["passed"]:
        reason = "; ".join(result.gate["reasons"] + result.gate.get("examples", []))
    elif result.status == "rejected_assumption":
        reason = "assumption violated: " + ", ".join(c["text"] for c in result.assumption_checks if c["holds"] is False)
    elif result.status == "rejected_slow":
        reason = f"speedup only {result.speedup}"
    return f"[{result.candidate.source}] {result.status}: {reason}\nSQL:\n{result.candidate.sql[:1500]}"


def model_summary(model: ModelSpec, schema: dict[str, Any], profiles: list[dict[str, Any]], entry: dict[str, Any], detect: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: PLR0913
    plan = entry["plan"]
    return {
        "name": model.name,
        "dataset": model.dataset,
        "source_path": model.source_path,
        "pattern": model.pattern,
        "original_sql": model.sql,
        "schema": schema,
        "profile": profiles,
        "baseline": {
            "timing": entry["timing"],
            "rows_scanned": plan["rows_scanned"],
            "peak_memory_bytes": plan["peak_memory_bytes"],
            "plan_text": plan["plan_text"],
            "gate": entry["gate"],
            "correct": entry["gate"]["passed"],
        },
        "detect": detect,
        "findings": findings,
        "candidates": [],
        "proposal": None,
        "physical": None,
        "final_correct": entry["gate"]["passed"],
        "llm": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
    }


def finalize(result: dict[str, Any], candidates: list[CandidateResult]) -> dict[str, Any]:
    """Chọn đề xuất = ứng viên accepted có speedup cao nhất. Ứng viên sai gate không bao giờ được chọn."""
    result["candidates"] = [c.to_dict() for c in candidates]
    accepted = [c for c in candidates if c.status == "accepted" and c.passed_gate]
    if accepted:
        best = max(accepted, key=lambda c: c.speedup or 0)
        result["proposal"] = best.to_dict()
        result["final_correct"] = True
    usages = [c.candidate.llm_usage for c in candidates if c.candidate.llm_usage]
    result["llm"] = {
        "calls": len(usages),
        "input_tokens": sum(u["input_tokens"] for u in usages),
        "output_tokens": sum(u["output_tokens"] for u in usages),
        "cost_usd": round(sum(u["cost_usd"] for u in usages), 6),
    }
    return result
