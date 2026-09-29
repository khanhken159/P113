"""Đọc schema & profile dữ liệu các bảng mà model tham chiếu (CLAUDE.md 7.1-7.2).

Profile: số dòng, kiểu dữ liệu, tỷ lệ NULL, số giá trị có khoảng trắng đầu/cuối, vài dòng mẫu.
"""

from __future__ import annotations

from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

SAMPLE_ROWS = 3


def referenced_tables(sql: str) -> list[tuple[str | None, str]]:
    """Các bảng vật lý (schema, table) được tham chiếu, bỏ qua tên CTE."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = []
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in cte_names:
            continue
        key = (table.db or None, name)
        if key not in tables:
            tables.append(key)
    return tables


def _table_columns(con: duckdb.DuckDBPyConnection, schema: str | None, table: str) -> list[tuple[str, str]]:
    query = "SELECT column_name, data_type FROM information_schema.columns WHERE lower(table_name) = lower(?)"
    params: list[Any] = [table]
    if schema:
        query += " AND lower(table_schema) = lower(?)"
        params.append(schema)
    else:
        query += " AND table_schema = current_schema()"
    return con.execute(query + " ORDER BY ordinal_position", params).fetchall()


def schema_for(con: duckdb.DuckDBPyConnection, sql: str) -> dict[str, Any]:
    """Schema dạng sqlglot ({table: {col: type}} hoặc {schema: {table: {...}}}) cho các bảng được dùng."""
    result: dict[str, Any] = {}
    for schema, table in referenced_tables(sql):
        columns = {name: dtype for name, dtype in _table_columns(con, schema, table)}
        if not columns:
            continue
        if schema:
            result.setdefault(schema, {})[table] = columns
        else:
            result[table] = columns
    return result


def _qualified(schema: str | None, table: str) -> str:
    return f'"{schema}"."{table}"' if schema else f'"{table}"'


def profile_table(con: duckdb.DuckDBPyConnection, schema: str | None, table: str) -> dict[str, Any]:
    """Profile một bảng bằng một lượt quét duy nhất."""
    columns = _table_columns(con, schema, table)
    parts = ["count(*)"]
    for name, dtype in columns:
        parts.append(f'count("{name}")')
        is_text = dtype.upper().startswith("VARCHAR")
        parts.append(f'count(*) FILTER (WHERE "{name}" <> trim("{name}"))' if is_text else "0")
    row = con.execute(f"SELECT {', '.join(parts)} FROM {_qualified(schema, table)}").fetchone()
    total = row[0]
    profile_cols = []
    for i, (name, dtype) in enumerate(columns):
        non_null, padded = row[1 + 2 * i], row[2 + 2 * i]
        null_ratio = 0.0 if total == 0 else 1 - non_null / total
        profile_cols.append({"name": name, "type": dtype, "null_ratio": round(null_ratio, 4), "whitespace_padded": padded})
    sample = con.execute(f"SELECT * FROM {_qualified(schema, table)} LIMIT {SAMPLE_ROWS}").df()
    return {
        "table": f"{schema}.{table}" if schema else table,
        "row_count": total,
        "columns": profile_cols,
        "sample": sample.astype(str).to_dict(orient="records"),
    }


def profile_tables(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    """Profile mọi bảng vật lý mà query tham chiếu."""
    return [profile_table(con, schema, table) for schema, table in referenced_tables(sql) if _table_columns(con, schema, table)]
