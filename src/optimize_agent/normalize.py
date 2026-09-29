"""Chuẩn hóa giá trị trước khi so kết quả (CLAUDE.md 5.1, theo ELT-Bench-Verified).

Quy tắc:
- boolean: true/false ≡ 1/0 (kể cả chuỗi 'true'/'false')
- NULL: NaN, None, NaT, chuỗi rỗng -> None
- số: int/float/Decimal/numpy -> float (so với sai số tương đối ở compare.py)
- ngày giờ: date/datetime/pandas.Timestamp -> datetime (không làm tròn)
- chuỗi: giữ nguyên, KHÔNG trim, KHÔNG đổi hoa/thường
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

_BOOL_STRINGS = {"true": 1.0, "false": 0.0}


def is_null(value: Any) -> bool:
    """True nếu giá trị được coi là NULL (None, NaN, NaT, pd.NA, chuỗi rỗng)."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, np.floating) and np.isnan(value):
        return True
    return isinstance(value, str) and value == ""


def normalize_value(value: Any) -> Any:
    """Đưa một giá trị về dạng chuẩn để so sánh."""
    if is_null(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, Decimal, np.integer, np.floating)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.lower() in _BOOL_STRINGS:
        return _BOOL_STRINGS[value.lower()]
    return value


def coerce_like(value: Any, other: Any) -> Any:
    """Ép chuỗi (thường từ CSV ground truth) về kiểu của giá trị bên kia nếu parse được."""
    if not isinstance(value, str) or isinstance(other, str) or other is None:
        return value
    if isinstance(other, float):
        try:
            return float(value)
        except ValueError:
            return value
    if isinstance(other, dt.datetime):
        try:
            return pd.Timestamp(value).to_pydatetime()
        except (ValueError, TypeError):
            return value
    return value


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Chuẩn hóa toàn bộ DataFrame: tên cột về chữ thường, giá trị qua normalize_value."""
    columns = {}
    for col in frame.columns:
        # Dựng Series dtype=object để pandas không tự đổi None thành NaN.
        values = [normalize_value(v) for v in frame[col].astype(object).tolist()]
        columns[str(col).lower()] = pd.Series(values, dtype=object)
    return pd.DataFrame(columns, index=range(len(frame)))


def coerce_column(values: list[Any], reference: list[Any]) -> list[Any]:
    """Ép cả cột chuỗi theo kiểu của cột tham chiếu (lấy mẫu giá trị khác NULL đầu tiên)."""
    sample = next((v for v in reference if v is not None), None)
    if sample is None or isinstance(sample, str):
        return values
    return [coerce_like(v, sample) for v in values]
