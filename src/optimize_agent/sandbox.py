"""Kết nối sandbox DuckDB local và các rào chắn an toàn.

Chỉ được mở `data/sandbox.duckdb` (CLAUDE.md mục 2). SQL do agent/LLM sinh ra phải là
một câu SELECT duy nhất — không ATTACH, COPY, INSTALL, CREATE, INSERT...
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from src.optimize_agent.settings import PROJECT_ROOT, AgentConfig

_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class SandboxError(RuntimeError):
    """Vi phạm ràng buộc sandbox."""


def sandbox_path(config: AgentConfig) -> Path:
    """Đường dẫn sandbox đã kiểm tra: phải là file .duckdb nằm trong thư mục data/ của project."""
    path = config.resolve(config.sandbox.path).resolve()
    data_dir = (PROJECT_ROOT / "data").resolve()
    if path.suffix != ".duckdb" or data_dir not in path.parents:
        raise SandboxError(f"Sandbox phải là file .duckdb trong {data_dir}, nhận được: {path}")
    return path


def connect(config: AgentConfig, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Mở sandbox DuckDB và đặt số thread cố định để đo thời gian công bằng."""
    path = sandbox_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    configure(con, config)
    return con


def configure(con: duckdb.DuckDBPyConnection, config: AgentConfig) -> None:
    """Cấu hình chung cho mọi kết nối (sandbox thật hoặc in-memory trong test)."""
    con.execute(f"SET threads={int(config.sandbox.threads)}")
    # Không cho DuckDB tự tải extension từ mạng trong lúc chạy agent.
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET enable_progress_bar=false")


def validate_select_sql(sql: str) -> str:
    """Chấp nhận đúng một câu truy vấn đọc (SELECT/WITH/UNION...). Trả lại SQL đã strip."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise SandboxError(f"SQL không parse được: {exc}") from exc
    if len(statements) != 1:
        raise SandboxError(f"Chỉ cho phép 1 câu lệnh, nhận {len(statements)}")
    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise SandboxError(f"Chỉ cho phép truy vấn đọc, nhận {type(root).__name__}")
    return sql.strip().rstrip(";")


def environment_info(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Phiên bản DuckDB + cấu hình máy để ghi vào report (CLAUDE.md 5.3)."""
    threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    memory_limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    return {
        "duckdb_version": duckdb.__version__,
        "sqlglot_version": sqlglot.__version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "threads": threads,
        "memory_limit": memory_limit,
    }
