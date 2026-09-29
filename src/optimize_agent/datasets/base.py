"""Mô tả một model (dbt-style SQL) cần tối ưu và cách lấy kết quả tham chiếu."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

_HEADER = re.compile(r"^--\s*(\w+)\s*:\s*(.*)$")


@dataclass
class ModelSpec:
    name: str  # vd "tpch/q01_distinct_star"
    dataset: str  # "tpch" | "eltbench"
    sql: str  # SQL model (đã resolve ref/source) — đối tượng cần tối ưu
    source_path: str
    pattern: str = ""  # mô tả anti-pattern cố ý (nếu có)
    reference_sql: str | None = None  # query đúng (TPC-H gốc)
    ground_truth_csv: str | None = None  # ground truth (ELT-Bench)
    unique_key: list[str] | None = None
    ordered: bool = False  # True nếu spec yêu cầu ORDER BY
    spec: str = ""  # tài liệu cột / mô tả nghiệp vụ, đọc trước khi tối ưu
    extra: dict[str, Any] = field(default_factory=dict)


def parse_header(sql_text: str) -> dict[str, str]:
    """Đọc các dòng `-- key: value` ở đầu file SQL."""
    meta: dict[str, str] = {}
    for line in sql_text.splitlines():
        match = _HEADER.match(line.strip())
        if not match:
            break
        meta[match.group(1).lower()] = match.group(2).strip()
    return meta


def strip_header(sql_text: str) -> str:
    """Bỏ các dòng metadata `-- key: value` ở đầu file."""
    lines = sql_text.splitlines()
    while lines and _HEADER.match(lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def has_top_level_order_by(sql: str) -> bool:
    """True nếu câu truy vấn ngoài cùng có ORDER BY (kết quả phải so theo thứ tự)."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    return isinstance(tree, exp.Query) and tree.args.get("order") is not None
