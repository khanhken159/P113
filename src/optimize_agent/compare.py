"""Correctness Gate: so kết quả query tối ưu với kết quả tham chiếu (CLAUDE.md 5.1).

- Thiếu cột của tham chiếu -> FAIL; cột thừa được phép.
- Số dòng phải bằng nhau (bắt lỗi JOIN nhân/mất dòng).
- Không phân biệt thứ tự dòng trừ khi `ordered=True`.
- Tên cột so không phân biệt hoa/thường (ground truth Snowflake viết HOA).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.optimize_agent.normalize import coerce_column, normalize_frame
from src.optimize_agent.settings import CompareConfig

MAX_EXAMPLES = 3


@dataclass
class CompareResult:
    passed: bool
    expected_rows: int
    actual_rows: int
    missing_columns: list[str] = field(default_factory=list)
    mismatched_columns: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def values_equal(a: Any, b: Any, tol: CompareConfig) -> bool:
    """So hai giá trị đã chuẩn hóa. NULL ≡ NULL, số thực so theo sai số tương đối."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=tol.rel_tol, abs_tol=tol.abs_tol)
    return type(a) is type(b) and a == b


def _sort_token(value: Any) -> tuple[int, Any]:
    """Khóa sắp xếp ổn định cho mọi kiểu; số thực làm tròn 6 chữ số để hai bên sai số nhỏ vẫn cùng vị trí."""
    if value is None:
        return (0, "")
    if isinstance(value, float):
        return (1, float(f"{value:.6g}"))
    if isinstance(value, dt.datetime):
        return (2, value.isoformat())
    return (3, str(value))


Columns = dict[str, list[Any]]


def _align(cols: Columns, sort_columns: list[str]) -> Columns:
    """Sắp xếp dòng theo các cột cho trước để hai bên thẳng hàng."""
    n = len(next(iter(cols.values()), []))
    token_cols = [[_sort_token(v) for v in cols[c]] for c in sort_columns]
    order = sorted(range(n), key=lambda i: tuple(tc[i] for tc in token_cols))
    return {c: [values[i] for i in order] for c, values in cols.items()}


def _keys_unique(cols: Columns, keys: list[str]) -> bool:
    rows = list(zip(*(cols[k] for k in keys), strict=True))
    return len(set(rows)) == len(rows)


def _choose_sort_columns(expected: Columns, actual: Columns, keys: list[str] | None) -> list[str]:
    """Dùng unique key nếu hợp lệ và không trùng ở cả hai bên; ngược lại sắp theo mọi cột."""
    if keys and all(k in expected for k in keys):
        if _keys_unique(expected, keys) and _keys_unique(actual, keys):
            return keys + [c for c in expected if c not in keys]
    return list(expected)


def _coerce_pair(expected: Columns, actual: Columns) -> None:
    """Ép kiểu chuỗi (CSV) theo kiểu của phía còn lại, từng cột, tại chỗ."""
    for col in expected:
        exp_values, act_values = expected[col], actual[col]
        expected[col] = coerce_column(exp_values, act_values)
        actual[col] = coerce_column(act_values, exp_values)


def _diff_columns(expected: Columns, actual: Columns, tol: CompareConfig, result: CompareResult) -> None:
    for col, exp_values in expected.items():
        act_values = actual[col]
        bad = [i for i, (a, b) in enumerate(zip(exp_values, act_values, strict=True)) if not values_equal(a, b, tol)]
        if bad:
            result.mismatched_columns[col] = len(bad)
            for i in bad[: max(0, MAX_EXAMPLES - len(result.examples))]:
                result.examples.append(f"{col}[{i}]: expected={exp_values[i]!r} actual={act_values[i]!r}")


def _to_columns(frame: pd.DataFrame) -> Columns:
    normalized = normalize_frame(frame)
    return {c: normalized[c].tolist() for c in normalized.columns}


def compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    key_columns: list[str] | None = None,
    ordered: bool = False,
    tol: CompareConfig | None = None,
) -> CompareResult:
    """So `actual` với `expected` theo Correctness Gate. Trả về PASS/FAIL kèm lý do."""
    tol = tol or CompareConfig()
    exp, act = _to_columns(expected), _to_columns(actual)
    result = CompareResult(passed=False, expected_rows=len(expected), actual_rows=len(actual))
    result.missing_columns = [c for c in exp if c not in act]
    if result.missing_columns:
        result.reasons.append(f"thiếu cột: {result.missing_columns}")
    if len(expected) != len(actual):
        result.reasons.append(f"số dòng khác nhau: expected={len(expected)} actual={len(actual)}")
    if result.reasons:
        return result

    act = {c: act[c] for c in exp}  # bỏ cột thừa (được phép)
    _coerce_pair(exp, act)
    if not ordered:
        keys = [k.lower() for k in key_columns] if key_columns else None
        sort_columns = _choose_sort_columns(exp, act, keys)
        exp, act = _align(exp, sort_columns), _align(act, sort_columns)

    _diff_columns(exp, act, tol, result)
    if result.mismatched_columns:
        result.reasons.append(f"cột sai giá trị: {result.mismatched_columns}")
    result.passed = not result.reasons
    return result
