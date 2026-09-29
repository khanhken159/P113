"""Analyze: tìm nguyên nhân chậm cụ thể từ AST (sqlglot) và từ plan (EXPLAIN ANALYZE)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from src.optimize_agent.timing import PlanMetrics

SLOW_JOIN_OPERATORS = {"NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN", "PIECEWISE_MERGE_JOIN", "IE_JOIN", "CROSS_PRODUCT"}
HEAVY_OPERATOR_SHARE = 0.3  # operator chiếm >30% tổng thời gian plan thì nêu tên


@dataclass
class Finding:
    code: str
    message: str  # giải thích tiếng Việt, dễ hiểu cho Reviewer
    source: str  # "ast" | "plan"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_top_level(node: exp.Expression, root: exp.Expression) -> bool:
    """True nếu node là SELECT ngoài cùng (hoặc nhánh của UNION ngoài cùng)."""
    parent = node.parent
    while isinstance(parent, exp.SetOperation):
        parent = parent.parent
    return node is root or parent is None


def _ast_distinct_and_order(tree: exp.Expression) -> list[Finding]:
    findings = []
    for select in tree.find_all(exp.Select):
        if is_top_level(select, tree):
            continue
        if select.args.get("distinct"):
            findings.append(Finding("redundant_distinct", "DISTINCT trong subquery/CTE bắt DuckDB khử trùng toàn bộ dữ liệu trung gian", "ast", select.sql("duckdb")[:160]))
        if select.args.get("order") and not select.args.get("limit") and not select.args.get("offset"):
            findings.append(Finding("order_in_subquery", "ORDER BY trong subquery không có LIMIT là vô ích (SQL không đảm bảo thứ tự) nhưng tốn chi phí sắp xếp", "ast", select.sql("duckdb")[:160]))
        if any(isinstance(e, exp.Star) for e in select.expressions):
            findings.append(Finding("select_star_subquery", "SELECT * trong subquery kéo mọi cột (khó cắt cột khi có DISTINCT/UNION/window)", "ast"))
    return findings


def _ast_union(tree: exp.Expression) -> list[Finding]:
    findings = []
    for union in tree.find_all(exp.Union):
        if union.args.get("distinct"):
            same = union.left.sql() == union.right.sql()
            msg = "UNION (có khử trùng) giữa hai nhánh giống hệt nhau" if same else "UNION (có khử trùng) thay vì UNION ALL"
            findings.append(Finding("union_distinct", msg + " — phải hash toàn bộ dòng để loại trùng", "ast", union.sql("duckdb")[:160]))
    return findings


def _ast_window_dedupe(tree: exp.Expression) -> list[Finding]:
    findings = []
    for window in tree.find_all(exp.Window):
        if isinstance(window.this, exp.RowNumber) and window.args.get("partition_by"):
            findings.append(Finding("window_dedupe", "row_number() theo PARTITION BY để khử trùng — tốn sắp xếp/phân vùng toàn bảng; thừa nếu khóa đã duy nhất", "ast", window.sql("duckdb")))
    return findings


def _ast_joins(tree: exp.Expression) -> list[Finding]:
    findings = []
    ge_pairs = {(n.this.sql(), n.expression.sql()) for n in tree.find_all(exp.GTE)}
    for le in tree.find_all(exp.LTE):
        if (le.this.sql(), le.expression.sql()) in ge_pairs:
            findings.append(Finding("range_equality_join", f"Điều kiện `{le.this.sql()} >= {le.expression.sql()} AND <=` thực chất là phép bằng nhưng buộc DuckDB dùng range join thay vì hash join", "ast"))
    for eq in tree.find_all(exp.EQ):
        if isinstance(eq.this, exp.Cast) and isinstance(eq.expression, exp.Cast):
            findings.append(Finding("cast_in_join", "So sánh qua CAST ở cả hai vế (vd CAST AS VARCHAR) làm JOIN chậm hơn so khóa gốc", "ast", eq.sql("duckdb")))
    return findings


def _ast_subqueries(tree: exp.Expression) -> list[Finding]:
    findings = []
    for scope in traverse_scope(tree):
        if scope.is_correlated_subquery:
            findings.append(Finding("correlated_subquery", "Subquery tương quan (tham chiếu cột bên ngoài)", "ast", scope.expression.sql("duckdb")[:160]))
    counts = Counter(s.this.sql() for s in tree.find_all(exp.Subquery) if isinstance(s.this, exp.Select))
    for sql, n in counts.items():
        if n > 1:
            findings.append(Finding("repeated_subquery", f"Cùng một subquery xuất hiện {n} lần (tính lặp)", "ast", sql[:160]))
    for cte in tree.find_all(exp.CTE):
        if cte.args.get("materialized"):
            findings.append(Finding("materialized_cte", "CTE MATERIALIZED có thể chặn đẩy filter/cắt cột", "ast", cte.alias))
    return findings


def parse_qualified(sql: str, schema: dict[str, Any] | None = None) -> exp.Expression:
    """Parse SQL; nếu có schema thì gắn tên bảng cho cột để phân tích scope chính xác."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    if not schema:
        return tree
    try:
        return qualify(tree, schema=schema, dialect="duckdb", validate_qualify_columns=False)
    except (sqlglot.errors.OptimizeError, sqlglot.errors.SchemaError):
        return tree


def analyze_sql(sql: str, schema: dict[str, Any] | None = None) -> list[Finding]:
    """Tìm anti-pattern trên AST. Cây gốc cho các mẫu cú pháp (SELECT *...); cây đã qualify cho tương quan subquery."""
    raw = sqlglot.parse_one(sql, read="duckdb")
    findings: list[Finding] = []
    for check in (_ast_distinct_and_order, _ast_union, _ast_window_dedupe, _ast_joins):
        findings.extend(check(raw))
    findings.extend(_ast_subqueries(parse_qualified(sql, schema)))
    return findings


def analyze_plan(metrics: PlanMetrics, large_scan_rows: int) -> list[Finding]:
    """Tìm operator đắt trong plan: join không phải hash, scan lớn, operator chiếm nhiều thời gian."""
    findings = []
    total = sum(op["timing_s"] for op in metrics.operators) or 1.0
    for op in metrics.operators:
        name = op["operator"]
        if name in SLOW_JOIN_OPERATORS:
            findings.append(Finding("slow_join_operator", f"Plan dùng {name} (không phải hash join)", "plan", f"rows={op['cardinality']}"))
        if name == "TABLE_SCAN" and op["rows_scanned"] > large_scan_rows:
            findings.append(Finding("large_scan", f"Full scan lớn: {op['rows_scanned']:,} dòng", "plan", str(op["extra_info"].get("Table", "")) if isinstance(op["extra_info"], dict) else ""))
        if op["timing_s"] / total > HEAVY_OPERATOR_SHARE:
            findings.append(Finding("heavy_operator", f"{name} chiếm {op['timing_s'] / total:.0%} thời gian thực thi", "plan", f"rows={op['cardinality']}"))
    return findings
