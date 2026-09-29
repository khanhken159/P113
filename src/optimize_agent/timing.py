"""Đo thời gian (1 warm-up + N lần, lấy median) và thu chỉ số từ EXPLAIN ANALYZE (JSON profiling).

Query được materialize bằng `CREATE TEMP TABLE ... AS` — giống dbt materialization `table`,
đảm bảo DuckDB thực thi toàn bộ kết quả (không bị cắt cột/dòng) mà không tốn chi phí chuyển sang Python.
"""

from __future__ import annotations

import json
import statistics
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.optimize_agent.settings import TimingConfig

_BENCH_TABLE = "__optimizer_bench"
MEMORY_POLL_S = 0.005


@dataclass
class TimingResult:
    runs_s: list[float]
    median_s: float

    def to_dict(self) -> dict[str, Any]:
        return {"runs_s": self.runs_s, "median_s": self.median_s}


@dataclass
class PlanMetrics:
    rows_scanned: int
    peak_memory_bytes: int
    latency_s: float
    operators: list[dict[str, Any]] = field(default_factory=list)
    plan_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _run_once(con: duckdb.DuckDBPyConnection, sql: str) -> float:
    start = time.perf_counter()
    con.execute(f"CREATE OR REPLACE TEMP TABLE {_BENCH_TABLE} AS {sql}")
    elapsed = time.perf_counter() - start
    con.execute(f"DROP TABLE IF EXISTS {_BENCH_TABLE}")
    return elapsed


def measure(con: duckdb.DuckDBPyConnection, sql: str, cfg: TimingConfig) -> TimingResult:
    """Warm-up rồi đo `measured_runs` lần; trả về các lần đo và median."""
    for _ in range(cfg.warmup_runs):
        _run_once(con, sql)
    runs = [_run_once(con, sql) for _ in range(cfg.measured_runs)]
    return TimingResult(runs_s=runs, median_s=statistics.median(runs))


def fetch_result(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """Lấy toàn bộ kết quả query về DataFrame (dùng cho Correctness Gate)."""
    return con.execute(sql).df()


def _walk(node: dict[str, Any], depth: int, out: list[dict[str, Any]]) -> None:
    op_type = node.get("operator_type") or node.get("operator_name")
    if op_type:
        out.append(
            {
                "depth": depth,
                "operator": op_type,
                "name": node.get("operator_name", ""),
                "cardinality": node.get("operator_cardinality", 0),
                "rows_scanned": node.get("operator_rows_scanned", 0),
                "timing_s": node.get("operator_timing", 0.0),
                "extra_info": node.get("extra_info", {}),
            }
        )
    for child in node.get("children", []):
        _walk(child, depth + 1, out)


def _plan_text(operators: list[dict[str, Any]]) -> str:
    lines = []
    for op in operators:
        table = op["extra_info"].get("Table", "") if isinstance(op["extra_info"], dict) else ""
        suffix = f" table={table}" if table else ""
        lines.append(
            f"{'  ' * op['depth']}{op['operator']} rows={op['cardinality']} "
            f"scanned={op['rows_scanned']} t={op['timing_s']:.4f}s{suffix}"
        )
    return "\n".join(lines)


def _memory_used(cursor: duckdb.DuckDBPyConnection) -> int:
    return int(cursor.execute("SELECT sum(memory_usage_bytes) FROM duckdb_memory()").fetchone()[0] or 0)


def run_tracking_memory(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    """Chạy query, đồng thời một thread đọc duckdb_memory() -> trả về bộ nhớ làm việc đỉnh (đỉnh - trước khi chạy).

    Không dùng `system_peak_buffer_memory` của profiling vì đó là đỉnh tích lũy từ lúc mở DB, không riêng query.
    """
    cursor = con.cursor()
    before = _memory_used(cursor)
    peak, stop = [before], threading.Event()

    def poll() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], _memory_used(cursor))
            stop.wait(MEMORY_POLL_S)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        con.execute(sql).fetchall()
    finally:
        stop.set()
        thread.join()
        cursor.close()
    return max(0, peak[0] - before)


def profile(con: duckdb.DuckDBPyConnection, sql: str) -> PlanMetrics:
    """Chạy query với JSON profiling (tương đương EXPLAIN ANALYZE), trả về rows scanned, peak memory, cây operator."""
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "profile.json"
        con.execute("SET enable_profiling='json'")
        con.execute("SET profiling_mode='detailed'")
        con.execute(f"SET profiling_output='{out_file.as_posix()}'")
        try:
            peak_memory = run_tracking_memory(con, sql)
        finally:
            con.execute("PRAGMA disable_profiling")
        data = json.loads(out_file.read_text(encoding="utf-8"))
    operators: list[dict[str, Any]] = []
    _walk(data, 0, operators)
    return PlanMetrics(
        rows_scanned=int(data.get("cumulative_rows_scanned", 0)),
        peak_memory_bytes=peak_memory,
        latency_s=float(data.get("latency", 0.0)),
        operators=operators,
        plan_text=_plan_text(operators),
    )
