"""Ứng viên 2: rule rewrite xác định trên AST sqlglot (không cần LLM).

Mỗi rule hoặc tương đương tuyệt đối, hoặc ghi rõ Assumption kèm SQL kiểm chứng (trả về số vi phạm).
Rule được áp lặp lại tới khi không đổi (fixpoint).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from src.optimize_agent.analyze import is_top_level
from src.optimize_agent.rewrite import Assumption, Candidate

MAX_PASSES = 20
ORDER_SENSITIVE = (exp.GroupConcat, exp.ArrayAgg, exp.First, exp.Last, exp.AnyValue)
INTEGER_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"}


@dataclass
class RuleContext:
    schema: dict[str, Any]
    applied: list[str] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)

    def note(self, rule: str, explanation: str, assumptions: list[Assumption] | None = None) -> None:
        if explanation not in self.explanations:
            self.explanations.append(explanation)
        self.applied.append(rule)
        for assumption in assumptions or []:
            if assumption.text not in {a.text for a in self.assumptions}:
                self.assumptions.append(assumption)


def _single_table(select: exp.Select) -> exp.Table | None:
    """Bảng vật lý duy nhất trong FROM (không JOIN), hoặc None."""
    source = select.args.get("from_")
    if source is None or select.args.get("joins") or not isinstance(source.this, exp.Table):
        return None
    return source.this


def _star_base_table(select: exp.Select) -> exp.Table | None:
    """Bảng gốc nếu FROM là bảng, hoặc chuỗi subquery `SELECT * FROM ... [WHERE]` đơn giản dẫn về một bảng.

    WHERE được phép vì tập con của một bảng không trùng dòng thì cũng không trùng dòng.
    """
    source = select.args.get("from_")
    if source is None or select.args.get("joins"):
        return None
    if isinstance(source.this, exp.Table):
        return source.this
    inner = source.this.this if isinstance(source.this, exp.Subquery) else None
    if not isinstance(inner, exp.Select) or not _is_star_select(inner):
        return None
    if any(inner.args.get(k) for k in ("distinct", "group", "limit", "offset", "order", "having")):
        return None
    return _star_base_table(inner)


def _is_star_select(select: exp.Select) -> bool:
    return len(select.expressions) == 1 and isinstance(select.expressions[0], exp.Star)


def _table_sql(table: exp.Table) -> str:
    return exp.table_(table.name, db=table.db or None).sql("duckdb")


def _no_duplicate_rows(table: exp.Table) -> Assumption:
    name = _table_sql(table)
    return Assumption(
        text=f"Bảng {name} không có dòng trùng lặp hoàn toàn (mỗi dòng là duy nhất)",
        check_sql=f"SELECT (SELECT count(*) FROM {name}) - (SELECT count(*) FROM (SELECT DISTINCT * FROM {name}))",
    )


def drop_subquery_order_by(tree: exp.Expression, ctx: RuleContext) -> bool:
    """ORDER BY trong subquery không có LIMIT/OFFSET không ảnh hưởng kết quả (chuẩn SQL)."""
    if tree.find(*ORDER_SENSITIVE):
        return False
    changed = False
    for select in list(tree.find_all(exp.Select)):
        if is_top_level(select, tree) or not select.args.get("order"):
            continue
        if select.args.get("limit") or select.args.get("offset"):
            continue
        select.set("order", None)
        changed = True
    if changed:
        ctx.note("drop_subquery_order_by", "Bỏ ORDER BY trong subquery: thứ tự dòng của subquery không được SQL đảm bảo "
                 "và không ảnh hưởng kết quả cuối, nhưng tốn chi phí sắp xếp.")
    return changed


def self_union_to_distinct(tree: exp.Expression, ctx: RuleContext) -> bool:
    """`X UNION X` ≡ `SELECT DISTINCT ... X` (tương đương tuyệt đối)."""
    for union in list(tree.find_all(exp.Union)):
        left, right = union.left, union.right
        if not union.args.get("distinct") or left.sql() != right.sql() or not isinstance(left, exp.Select):
            continue
        if left.args.get("limit") or left.args.get("order") or left.args.get("distinct"):
            continue
        replacement = left.copy()
        replacement.set("distinct", exp.Distinct())
        union.replace(replacement)
        ctx.note("self_union_to_distinct", "Hai nhánh UNION giống hệt nhau: X UNION X chỉ là X đã khử trùng.")
        return True
    return False


def drop_distinct_star_table(tree: exp.Expression, ctx: RuleContext) -> bool:
    """`SELECT DISTINCT * FROM t [WHERE ...]` (t có thể là chuỗi subquery `SELECT *`) -> bỏ DISTINCT nếu t không trùng dòng."""
    for select in list(tree.find_all(exp.Select)):
        table = _star_base_table(select)
        if is_top_level(select, tree) or table is None or not select.args.get("distinct"):
            continue
        if not _is_star_select(select) or select.args.get("group"):
            continue
        select.set("distinct", None)
        ctx.note("drop_distinct_star_table", f"Bỏ DISTINCT trên bảng {_table_sql(table)}: bảng không có dòng trùng nên "
                 "DISTINCT không loại gì mà vẫn phải hash toàn bộ dữ liệu.", [_no_duplicate_rows(table)])
        return True
    return False


def union_to_union_all(tree: exp.Expression, ctx: RuleContext) -> bool:
    """UNION giữa hai nhánh `SELECT * FROM t WHERE p` cùng bảng -> UNION ALL nếu p1, p2 không giao nhau và t không trùng dòng."""
    for union in list(tree.find_all(exp.Union)):
        left, right = union.left, union.right
        if not union.args.get("distinct") or not isinstance(left, exp.Select) or not isinstance(right, exp.Select):
            continue
        lt, rt = _single_table(left), _single_table(right)
        if lt is None or rt is None or _table_sql(lt) != _table_sql(rt) or not (_is_star_select(left) and _is_star_select(right)):
            continue
        lw, rw = left.args.get("where"), right.args.get("where")
        if lw is None or rw is None:
            continue
        name = _table_sql(lt)
        overlap = Assumption(
            text=f"Hai điều kiện lọc của UNION không giao nhau trên {name}",
            check_sql=f"SELECT count(*) FROM {name} WHERE ({lw.this.sql('duckdb')}) AND ({rw.this.sql('duckdb')})",
        )
        union.set("distinct", False)
        ctx.note("union_to_union_all", "Đổi UNION thành UNION ALL: hai nhánh lấy các dòng khác nhau của cùng một bảng "
                 "không trùng dòng, nên bước khử trùng (hash toàn bộ dòng) là thừa.", [_no_duplicate_rows(lt), overlap])
        return True
    return False


def rownumber_to_constant(tree: exp.Expression, ctx: RuleContext) -> bool:
    """row_number() OVER (PARTITION BY k) trên bảng mà k duy nhất luôn = 1 -> thay bằng hằng 1."""
    for window in list(tree.find_all(exp.Window)):
        partition = window.args.get("partition_by") or []
        select = window.find_ancestor(exp.Select)
        table = _single_table(select) if select else None
        if not isinstance(window.this, exp.RowNumber) or not partition or table is None:
            continue
        if not all(isinstance(p, exp.Column) for p in partition):
            continue
        keys = ", ".join(p.sql("duckdb") for p in partition)
        name = _table_sql(table)
        assumption = Assumption(
            text=f"({keys}) là khóa duy nhất của {name}",
            check_sql=f"SELECT count(*) FROM (SELECT {keys} FROM {name} GROUP BY {keys} HAVING count(*) > 1)",
        )
        window.replace(exp.Literal.number(1))
        ctx.note("rownumber_to_constant", f"row_number() theo khóa ({keys}) luôn bằng 1 vì khóa duy nhất: thay bằng hằng 1, "
                 "bỏ bước phân vùng/sắp xếp toàn bảng.", [assumption])
        return True
    return False


def _comparison_key(node: exp.Expression) -> tuple[str, str] | None:
    """Chuẩn hóa `a >= b` và `b <= a` về cùng dạng (a, b) nghĩa là a >= b."""
    if isinstance(node, exp.GTE):
        return node.this.sql(), node.expression.sql()
    if isinstance(node, exp.LTE):
        return node.expression.sql(), node.this.sql()
    return None


def _merge_range_pairs(condition: exp.Expression) -> exp.Expression | None:
    conjuncts = list(condition.flatten()) if isinstance(condition, exp.And) else [condition]
    for i, first in enumerate(conjuncts):
        key = _comparison_key(first)
        if key is None:
            continue
        for j, second in enumerate(conjuncts):
            other = _comparison_key(second)
            if j != i and other == (key[1], key[0]):
                ge = first if isinstance(first, exp.GTE) else None
                a, b = (ge.this, ge.expression) if ge else (first.expression, first.this)
                rest = [c for k, c in enumerate(conjuncts) if k not in (i, j)]
                return exp.and_(exp.EQ(this=a.copy(), expression=b.copy()), *rest)
    return None


def range_to_equality(tree: exp.Expression, ctx: RuleContext) -> bool:
    """`a >= b AND a <= b` ≡ `a = b` (tương đương cả với NULL) -> cho phép hash join."""
    for holder in list(tree.find_all(exp.Where, exp.Join)):
        key = "this" if isinstance(holder, exp.Where) else "on"
        condition = holder.args.get(key)
        merged = _merge_range_pairs(condition) if condition is not None else None
        if merged is not None:
            holder.set(key, merged)
            ctx.note("range_to_equality", "Cặp điều kiện `a >= b AND a <= b` chính là `a = b`; viết dạng bằng để DuckDB "
                     "dùng hash join thay vì range join.")
            return True
    return False


def _column_types(schema: dict[str, Any]) -> dict[str, set[str]]:
    types: dict[str, set[str]] = {}

    def visit(node: dict[str, Any]) -> None:
        for key, value in node.items():
            if isinstance(value, dict):
                visit(value)
            else:
                types.setdefault(key.lower(), set()).add(str(value).upper())

    visit(schema)
    return types


def cast_equality(tree: exp.Expression, ctx: RuleContext) -> bool:
    """CAST(x AS VARCHAR) = CAST(y AS VARCHAR) với x, y số nguyên ≡ x = y."""
    types = _column_types(ctx.schema)
    for eq in list(tree.find_all(exp.EQ)):
        left, right = eq.this, eq.expression
        if not (isinstance(left, exp.Cast) and isinstance(right, exp.Cast)):
            continue
        if not (isinstance(left.this, exp.Column) and isinstance(right.this, exp.Column)):
            continue
        lt, rt = types.get(left.this.name.lower(), set()), types.get(right.this.name.lower(), set())
        if not lt or not rt or not (lt | rt) <= INTEGER_TYPES or left.to.sql() != right.to.sql():
            continue
        eq.replace(exp.EQ(this=left.this.copy(), expression=right.this.copy()))
        ctx.note("cast_equality", "Hai vế đều là số nguyên bị CAST sang cùng kiểu chuỗi: so sánh trực tiếp số nguyên cho "
                 "cùng kết quả và nhanh hơn.")
        return True
    return False


RULES: list[Callable[[exp.Expression, RuleContext], bool]] = [
    range_to_equality,
    cast_equality,
    drop_subquery_order_by,
    self_union_to_distinct,
    union_to_union_all,
    drop_distinct_star_table,
    rownumber_to_constant,
]


def rules_candidate(sql: str, schema: dict[str, Any]) -> Candidate | None:
    """Áp mọi rule tới fixpoint; trả về None nếu không rule nào áp dụng được."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    ctx = RuleContext(schema=schema)
    for _ in range(MAX_PASSES):
        if not any(rule(tree, ctx) for rule in RULES):
            break
    if not ctx.applied:
        return None
    return Candidate(
        source="rules",
        sql=tree.sql(dialect="duckdb", pretty=True),
        explanation=" ".join(ctx.explanations),
        applied=ctx.applied,
        assumptions=ctx.assumptions,
    )
