"""TPC-H: sinh dữ liệu bằng extension tpch, nạp các biến thể chậm cố ý trong datasets/tpch/slow/.

Query gốc (đáp án đúng) lấy từ `tpch_queries()` — chính là SQL mà `PRAGMA tpch(n)` chạy.
"""

from __future__ import annotations

import duckdb

from src.optimize_agent.datasets.base import ModelSpec, has_top_level_order_by, parse_header, strip_header
from src.optimize_agent.dbt_sql import render_model_sql
from src.optimize_agent.settings import AgentConfig

META_TABLE = "_optimizer_meta"


def _meta_get(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    con.execute(f"CREATE TABLE IF NOT EXISTS {META_TABLE} (key VARCHAR PRIMARY KEY, value VARCHAR)")
    row = con.execute(f"SELECT value FROM {META_TABLE} WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def _meta_set(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    con.execute(f"INSERT OR REPLACE INTO {META_TABLE} VALUES (?, ?)", [key, value])


def setup_tpch(con: duckdb.DuckDBPyConnection, config: AgentConfig) -> str:
    """Cài extension tpch và sinh dữ liệu (bỏ qua nếu đã sinh cùng scale factor)."""
    con.execute("INSTALL tpch")
    con.execute("LOAD tpch")
    sf = str(config.tpch.scale_factor)
    if _meta_get(con, "tpch_sf") == sf:
        return f"TPC-H sf={sf} đã có sẵn, bỏ qua"
    for table in ("lineitem", "orders", "customer", "part", "partsupp", "supplier", "nation", "region"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"CALL dbgen(sf={sf})")
    _meta_set(con, "tpch_sf", sf)
    return f"Đã sinh TPC-H sf={sf}"


def reference_query(con: duckdb.DuckDBPyConnection, number: int) -> str:
    """SQL chuẩn của TPC-H query `number` (giống PRAGMA tpch(n))."""
    con.execute("LOAD tpch")
    row = con.execute("SELECT query FROM tpch_queries() WHERE query_nr = ?", [number]).fetchone()
    if row is None:
        raise ValueError(f"Không có TPC-H query {number}")
    return row[0].strip().rstrip(";")


def load_tpch_models(con: duckdb.DuckDBPyConnection, config: AgentConfig) -> list[ModelSpec]:
    """Đọc mọi file .sql trong thư mục slow; mỗi file khai báo `-- reference: tpch:<n>`."""
    models = []
    for path in sorted(config.resolve(config.tpch.slow_dir).glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        meta = parse_header(text)
        number = int(meta["reference"].split(":")[1])
        reference = reference_query(con, number)
        models.append(
            ModelSpec(
                name=f"tpch/{path.stem}",
                dataset="tpch",
                sql=render_model_sql(strip_header(text)),
                source_path=str(path),
                pattern=meta.get("pattern", ""),
                reference_sql=reference,
                ordered=has_top_level_order_by(reference),
                spec=f"TPC-H Q{number}. Kết quả phải giống hệt query chuẩn TPC-H Q{number}.",
                extra={"tpch_query": number},
            )
        )
    return models
