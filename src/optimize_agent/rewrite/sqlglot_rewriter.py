"""Ứng viên 1: sqlglot.optimizer (rẻ, xác định)."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot.optimizer import optimize

from src.optimize_agent.rewrite import Candidate


def _canonical(sql: str) -> str:
    return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb", normalize=True)


def sqlglot_candidate(sql: str, schema: dict[str, Any]) -> Candidate | None:
    """Chạy sqlglot.optimizer (qualify, pushdown, unnest subquery, merge subquery, simplify...)."""
    try:
        optimized = optimize(sqlglot.parse_one(sql, read="duckdb"), schema=schema or None, dialect="duckdb")
        new_sql = optimized.sql(dialect="duckdb", pretty=True)
    except (sqlglot.errors.SqlglotError, KeyError, ValueError) as exc:
        return Candidate(source="sqlglot", sql="", explanation=f"sqlglot.optimizer lỗi: {exc}")
    if _canonical(new_sql) == _canonical(sql):
        return None
    return Candidate(
        source="sqlglot",
        sql=new_sql,
        explanation="sqlglot.optimizer chuẩn hóa query: gắn tên bảng cho cột, đẩy điều kiện lọc xuống sớm, "
        "gộp/khử subquery, rút gọn biểu thức. Không đổi logic.",
        applied=["sqlglot.optimizer.optimize"],
    )
