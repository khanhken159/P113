"""Resolve SQL kiểu dbt thành SQL DuckDB chạy được (không chạy dbt thật).

Hỗ trợ: {{ ref('model') }}, {{ source('schema', 'table') }}, {{ config(...) }} (bị bỏ đi).
Jinja khác -> lỗi rõ ràng thay vì đoán.
"""

from __future__ import annotations

import re

_REF = re.compile(r"\{\{\s*ref\(\s*['\"]([\w.]+)['\"]\s*\)\s*\}\}")
_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)\s*\}\}")
_CONFIG = re.compile(r"\{\{\s*config\(.*?\)\s*\}\}", re.S)
_JINJA = re.compile(r"\{\{|\{%")


class DbtRenderError(ValueError):
    """SQL có cú pháp Jinja chưa hỗ trợ."""


def render_model_sql(sql: str, ref_schema: str | None = None) -> str:
    """Thay ref/source bằng tên bảng DuckDB. `ref_schema` là schema chứa các model đã build."""

    def _ref(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"{ref_schema}.{name}" if ref_schema and "." not in name else name

    rendered = _CONFIG.sub("", sql)
    rendered = _REF.sub(_ref, rendered)
    rendered = _SOURCE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", rendered)
    if _JINJA.search(rendered):
        raise DbtRenderError("SQL còn Jinja chưa hỗ trợ (chỉ hỗ trợ ref/source/config)")
    return rendered.strip().rstrip(";").strip()
