"""Đề xuất bố trí dữ liệu vật lý (CLAUDE.md 7.6): DuckDB ít dùng index cho truy vấn phân tích,
nên thay vào đó đề xuất SẮP XẾP bảng lớn theo cột lọc (zone-map của row group giúp bỏ qua dữ liệu) — có đo.

Bản sao sắp xếp được tạo trong schema `optimizer_physical` của sandbox; bảng gốc không bị động tới.
Đề xuất vật lý đi qua đúng Correctness Gate như ứng viên SQL.
"""

from __future__ import annotations

from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from src.optimize_agent.analyze import parse_qualified
from src.optimize_agent.rewrite import Candidate

PHYSICAL_SCHEMA = "optimizer_physical"
_COMPARISONS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between)


def _is_constant(node: exp.Expression) -> bool:
    return not list(node.find_all(exp.Column))


def filter_columns(sql: str, schema: dict[str, Any]) -> list[tuple[str, str]]:
    """(bảng, cột) xuất hiện trong điều kiện lọc so với hằng số, vd `l_shipdate >= DATE '1994-01-01'`."""
    tree = parse_qualified(sql, schema)
    aliases = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    found: list[tuple[str, str]] = []
    for where in tree.find_all(exp.Where):
        for cmp in where.find_all(*_COMPARISONS):
            column = cmp.this if isinstance(cmp.this, exp.Column) else None
            others = [v for k, v in cmp.args.items() if k != "this" and isinstance(v, exp.Expression)]
            if column is None or not column.table or not all(_is_constant(o) for o in others):
                continue
            key = (aliases.get(column.table, column.table), column.name)
            if key not in found:
                found.append(key)
    return found


def _row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def sorted_copy(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str:
    """Tạo (một lần) bản sao `optimizer_physical.<table>__by_<column>` sắp theo cột lọc."""
    name = f"{table}__by_{column}"
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {PHYSICAL_SCHEMA}")
    con.execute(f'CREATE TABLE IF NOT EXISTS {PHYSICAL_SCHEMA}."{name}" AS SELECT * FROM "{table}" ORDER BY "{column}"')
    return f"{PHYSICAL_SCHEMA}.{name}"


def retarget_table(sql: str, table: str, new_table: str) -> str:
    """Trỏ mọi tham chiếu tới `table` sang `new_table`, giữ alias để các cột `table.col` vẫn đúng."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    schema_name, name = new_table.split(".", 1)
    for node in list(tree.find_all(exp.Table)):
        if node.name.lower() == table.lower() and not node.db:
            alias = node.alias or node.name
            node.replace(exp.table_(name, db=schema_name, alias=alias))
    return tree.sql(dialect="duckdb", pretty=True)


def physical_candidate(con: duckdb.DuckDBPyConnection, sql: str, schema: dict[str, Any], min_rows: int) -> Candidate | None:
    """Chọn bảng lớn nhất (≥ min_rows) có cột lọc, tạo bản sao sắp xếp và trả về SQL trỏ vào bản sao."""
    options = [(t, c) for t, c in filter_columns(sql, schema) if t in schema]
    sized = sorted(((_row_count(con, t), t, c) for t, c in options), reverse=True)
    if not sized or sized[0][0] < min_rows:
        return None
    rows, table, column = sized[0]
    new_table = sorted_copy(con, table, column)
    return Candidate(
        source="physical",
        sql=retarget_table(sql, table, new_table),
        explanation=f"Đề xuất lưu bảng {table} ({rows:,} dòng) SẮP XẾP theo cột lọc {column} (vd `CREATE TABLE ... AS "
        f"SELECT * FROM {table} ORDER BY {column}` hoặc ghi Parquet sắp xếp/partition theo {column}). Zone-map min/max "
        "của từng row group cho phép DuckDB bỏ qua các khối không thỏa điều kiện lọc. Đã đo trên bản sao "
        f"{new_table} trong sandbox; bảng gốc không bị thay đổi.",
        applied=[f"sort:{table}.{column}"],
    )
