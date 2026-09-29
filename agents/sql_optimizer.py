"""Run the bundled SQL optimizer against a generated FlowForge query."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import duckdb

from src.optimize_agent.agent import AgentOptions, BaselineCache, OptimizerAgent
from src.optimize_agent.datasets.base import ModelSpec
from src.optimize_agent.settings import (
    AgentConfig,
    DetectConfig,
    OptimizeConfig,
    TimingConfig,
)

ROOT = Path(__file__).resolve().parents[0]
REPORTS = ROOT / "generated" / "sql_optimizer"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_SQL_SPEEDUP = 1.10
MAX_RUNTIME_REGRESSION = 0.05
MAX_MEMORY_REGRESSION = 0.05


def optimization_thresholds() -> dict:
    return {
        "minimum_speedup": MIN_SQL_SPEEDUP,
        "maximum_runtime_regression": MAX_RUNTIME_REGRESSION,
        "maximum_peak_memory_regression": MAX_MEMORY_REGRESSION,
    }


def run_after_baseline_test(state: dict) -> dict:
    """Run the generated pipeline in optimizer mode after a passing baseline test."""
    generated = PROJECT_ROOT / "generated"
    pipeline = generated / "generated_pipeline.py"
    baseline_path = generated / "sql_baseline.sql"
    selected_path = generated / "generated_query.sql"
    baseline_queries_path = generated / "sql_baselines.json"
    selected_queries_path = generated / "generated_queries.json"
    report_path = generated / "sql_optimization_report.json"
    if not pipeline.is_file() or not baseline_path.is_file():
        return {
            "status": "failed",
            "optimization_phase": "verify_candidate",
            "optimizer_status": "MISSING_BASELINE_ARTIFACT",
            "optimization_thresholds": optimization_thresholds(),
        }

    env = os.environ.copy()
    env["FLOWFORGE_SQL_RUN_MODE"] = "optimize"
    try:
        run = subprocess.run(
            [sys.executable, str(pipeline)], cwd=PROJECT_ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if run.returncode:
            raise RuntimeError((run.stderr or run.stdout or "Optimizer subprocess failed")[-4000:])
        reports = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else []
        statuses = [item.get("optimizer_status") for item in reports if isinstance(item, dict)]
        optimizer_status = (
            "OPTIMIZED" if "OPTIMIZED" in statuses else
            next((status for status in statuses if status and status.startswith("OPTIMIZER_ERROR")), None) or
            "NO_IMPROVEMENT"
        )
        return {
            "status": "optimized",
            "optimization_phase": "verify_candidate",
            "optimizer_status": optimizer_status,
            "optimization_thresholds": optimization_thresholds(),
            "optimizer_stdout": run.stdout[-4000:],
            "optimizer_stderr": run.stderr[-4000:],
        }
    except Exception as error:
        baseline_sql = baseline_path.read_text(encoding="utf-8").strip().rstrip(";").strip()
        baseline_queries = json.loads(baseline_queries_path.read_text(encoding="utf-8")) if baseline_queries_path.is_file() else {}
        selected_queries_path.write_text(json.dumps(baseline_queries, ensure_ascii=False, indent=2), encoding="utf-8")
        selected_path.write_text(baseline_sql, encoding="utf-8")
        report_path.write_text(json.dumps([{
            "sql": baseline_sql,
            "optimized": False,
            "optimizer_status": "OPTIMIZER_ERROR_FALLBACK",
            "error": str(error),
        }], ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "status": "optimized",
            "optimization_phase": "verify_candidate",
            "optimizer_status": "OPTIMIZER_ERROR_FALLBACK",
            "optimizer_error": str(error),
            "optimization_thresholds": optimization_thresholds(),
        }


def rollback_to_baseline(state: dict) -> dict:
    """Restore the exact pre-optimizer SQL before the final fallback test."""
    generated = PROJECT_ROOT / "generated"
    baseline_path = generated / "sql_baseline.sql"
    selected_path = generated / "generated_query.sql"
    baseline_queries_path = generated / "sql_baselines.json"
    selected_queries_path = generated / "generated_queries.json"
    if not baseline_path.is_file():
        return {"status": "failed", "error": "Baseline SQL is missing; cannot roll back."}
    baseline_sql = baseline_path.read_text(encoding="utf-8").strip().rstrip(";").strip()
    selected_path.write_text(baseline_sql, encoding="utf-8")
    if baseline_queries_path.is_file():
        selected_queries_path.write_text(baseline_queries_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "status": "baseline_restored",
        "optimization_phase": "verify_fallback",
        "optimizer_rollback": True,
    }


def optimize_query(con: duckdb.DuckDBPyConnection, sql: str) -> dict:
    """Optimize SQL and return the best correctness-verified query, if any."""
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
    run_dir = REPORTS / f"run-{uuid.uuid4().hex}"
    config = AgentConfig(
        timing=TimingConfig(warmup_runs=0, measured_runs=3),
        detect=DetectConfig(slow_threshold_s=0.0),
        optimize=OptimizeConfig(
            use_llm=False,
            physical="off",
            min_speedup=MIN_SQL_SPEEDUP,
            max_candidates=2,
        ),
        reports_dir=str(REPORTS),
    )
    model = ModelSpec(
        name=f"flowforge/{digest}",
        dataset="flowforge",
        sql=sql,
        source_path="generated/generated_query.sql",
        reference_sql=sql,
        ordered=True,
        spec="FlowForge-generated aggregation; preserve exact result rows and order.",
    )
    cache = BaselineCache(run_dir / "baseline", "flowforge")
    agent = OptimizerAgent(con, config, run_dir, cache)
    result = agent.run_model(model, AgentOptions(use_llm=False))
    proposal = result.get("proposal")
    candidate_sql = proposal["candidate"]["sql"] if proposal else sql
    if (proposal and re.search(r"\bSELECT\s+DISTINCT\b", candidate_sql, re.IGNORECASE)
            and not re.search(r"\bSELECT\s+DISTINCT\b", sql, re.IGNORECASE)):
        return {
            "sql": sql,
            "optimized": False,
            "report": str(run_dir / "models" / f"flowforge__{digest}.json"),
            "optimizer_status": "REJECTED_DISTINCT_INTRODUCED",
            "baseline_seconds": result.get("baseline", {}).get("timing", {}).get("median_s"),
            "speedup": None,
        }
    return {
        "sql": candidate_sql,
        "optimized": bool(proposal),
        "report": str(run_dir / "models" / f"flowforge__{digest}.json"),
        "optimizer_status": "OPTIMIZED" if proposal else "NO_IMPROVEMENT",
        "baseline_seconds": result.get("baseline", {}).get("timing", {}).get("median_s"),
        "speedup": proposal.get("speedup") if proposal else None,
    }
