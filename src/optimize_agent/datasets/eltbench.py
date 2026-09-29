"""ELT-Bench-Verified subset: nạp bảng nguồn vào DuckDB, lấy ground truth CSV, nạp SQL model.

Cấu trúc đã đọc từ repo (không đoán):
- tasks/<db>/config.yaml: nguồn của từng bảng (postgres, mongodb, aws_s3, custom_api, flat_files)
- tasks/<db>/data_model.yaml: spec cột của từng model (đọc trước khi tối ưu)
- evaluation/<db>/<model>.sql: `SELECT * FROM <db>.airbyte_schema.<model> ORDER BY <key>` -> unique key
- data_db.zip: db/<db>/<table>.csv (postgres, mongodb) và db/<db>/<file> (file S3)
- data_api.zip: api/<db>/<table>.csv; ground_truth.zip: gt_verified/<db>/<model>.csv
- flat_files: link Google Drive riêng cho từng bảng
- example/retails/*.sql: SQL tham chiếu viết cho Snowflake -> sqlglot.transpile sang DuckDB
"""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
import yaml
from sqlglot import exp

from src.optimize_agent.datasets.base import ModelSpec, parse_header, strip_header
from src.optimize_agent.dbt_sql import render_model_sql
from src.optimize_agent.settings import AgentConfig

_DRIVE_ID = re.compile(r"[?&]id=([\w-]+)")
_AIRBYTE_REF = re.compile(r"\b(\w+)\.airbyte_schema\.(\w+)\b", re.IGNORECASE)


@dataclass
class SourceTable:
    name: str
    kind: str  # postgres | mongodb | aws_s3 | custom_api | flat_files
    location: str  # đường dẫn trong zip hoặc URL
    fmt: str  # csv | jsonl | parquet


def _paths(cfg: AgentConfig) -> tuple[Path, Path]:
    return cfg.resolve(cfg.eltbench.repo_dir), cfg.resolve(cfg.eltbench.raw_dir)


def read_task_config(repo: Path, db: str) -> dict[str, Any]:
    return yaml.safe_load((repo / "tasks" / db / "config.yaml").read_text(encoding="utf-8"))


def source_tables(task_config: dict[str, Any], db: str) -> list[SourceTable]:
    """Liệt kê bảng nguồn và vị trí dữ liệu theo đúng quy ước của loaders trong repo."""
    tables: list[SourceTable] = []
    for kind in ("postgres", "mongodb"):
        for name in (task_config.get(kind) or {}).get("config", {}).get("tables", []):
            tables.append(SourceTable(name, kind, f"db/{db}/{name}.csv", "csv"))
    for item in (task_config.get("aws_s3") or {}).get("data", []):
        filename = item["path"].split("/")[-1]
        tables.append(SourceTable(item["table"], "aws_s3", f"db/{db}/{filename}", filename.rsplit(".", 1)[-1]))
    for name in (task_config.get("custom_api") or {}).get("config", {}).get("tables", []):
        tables.append(SourceTable(name, "custom_api", f"api/{db}/{name}.csv", "csv"))
    for item in task_config.get("flat_files") or []:
        tables.append(SourceTable(item["table"], "flat_files", item["path"], item["format"]))
    return tables


def _extract(zip_path: Path, member: str, target: Path) -> Path:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf, zf.open(member) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return target


def _download_drive(url: str, target: Path) -> Path:
    if not target.exists():
        match = _DRIVE_ID.search(url)
        if not match:
            raise ValueError(f"Không đọc được id Google Drive: {url}")
        direct = f"https://drive.usercontent.google.com/download?id={match.group(1)}&export=download&confirm=t"
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(direct, timeout=300) as resp, target.open("wb") as dst:  # noqa: S310 — URL cố định từ repo benchmark
            shutil.copyfileobj(resp, dst)
    return target


def materialize_file(raw: Path, db: str, table: SourceTable) -> Path:
    """Đưa file dữ liệu của một bảng ra đĩa (giải nén từ zip hoặc tải flat file), có cache."""
    local = raw / "extracted"
    if table.kind == "flat_files":
        # data_db.zip đã có bản sao db/<db>/<table>.<fmt>; chỉ tải Drive khi thiếu.
        member = f"db/{db}/{table.name}.{table.fmt}"
        with zipfile.ZipFile(raw / "data_db.zip") as zf:
            if member in zf.namelist():
                return _extract(raw / "data_db.zip", member, local / member)
        return _download_drive(table.location, local / "flat" / db / f"{table.name}.{table.fmt}")
    zip_name = "data_api.zip" if table.kind == "custom_api" else "data_db.zip"
    return _extract(raw / zip_name, table.location, local / table.location)


def _reader(path: Path, fmt: str, all_varchar: bool = False) -> str:
    p = path.as_posix()
    if fmt == "parquet":
        return f"read_parquet('{p}')"
    if fmt in ("jsonl", "json"):
        return f"read_json_auto('{p}', format='newline_delimited')"
    return f"read_csv('{p}', header=true, auto_detect=true, sample_size=-1, all_varchar={str(all_varchar).lower()})"


def load_database(con: duckdb.DuckDBPyConnection, cfg: AgentConfig, db: str) -> list[str]:
    """Nạp mọi bảng nguồn của một DB vào schema `<db>` trong sandbox."""
    repo, raw = _paths(cfg)
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{db}"')
    loaded = []
    for table in source_tables(read_task_config(repo, db), db):
        path = materialize_file(raw, db, table)
        # Airbyte nạp CSV từ nguồn S3 thành chuỗi -> ground truth được tính trên cột VARCHAR (đã kiểm chứng ở
        # shipping.city.population: "ít dân nhất" theo thứ tự chuỗi mới khớp ground truth). Giữ nguyên hành vi đó.
        reader = _reader(path, table.fmt, all_varchar=table.kind == "aws_s3")
        con.execute(f'CREATE OR REPLACE TABLE "{db}"."{table.name}" AS SELECT * FROM {reader}')
        rows = con.execute(f'SELECT count(*) FROM "{db}"."{table.name}"').fetchone()[0]
        loaded.append(f"{db}.{table.name} ({table.kind}, {rows:,} dòng)")
    return loaded


def extract_ground_truth(cfg: AgentConfig, db: str) -> list[Path]:
    _, raw = _paths(cfg)
    with zipfile.ZipFile(raw / "ground_truth.zip") as zf:
        members = [m for m in zf.namelist() if m.startswith(f"gt_verified/{db}/") and m.endswith(".csv")]
    return [_extract(raw / "ground_truth.zip", m, raw / "extracted" / m) for m in members]


def transpile_snowflake(sql: str, db: str) -> str:
    """SQL Snowflake của benchmark -> SQL DuckDB kiểu dbt: `<db>.airbyte_schema.<t>` -> {{ source('<db>', '<t>') }}."""
    duck = sqlglot.transpile(sql, read="snowflake", write="duckdb", pretty=True)[0]
    return _AIRBYTE_REF.sub(lambda m: f"{{{{ source('{db}', '{m.group(2).lower()}') }}}}", duck)


def import_reference_models(cfg: AgentConfig, db: str) -> list[Path]:
    """Nếu repo có SQL tham chiếu (example/<db>/*.sql) thì transpile sang models_dir (không ghi đè file đã có)."""
    repo, _ = _paths(cfg)
    out_dir = cfg.resolve(cfg.eltbench.models_dir) / db
    written = []
    for src in sorted((repo / "example" / db).glob("*.sql")):
        target = out_dir / src.name
        if target.exists():
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        header = f"-- origin: ELT-Bench-Verified example/{db}/{src.name} (Snowflake, transpile bằng sqlglot)\n"
        target.write_text(header + transpile_snowflake(src.read_text(encoding="utf-8"), db) + "\n", encoding="utf-8")
        written.append(target)
    return written


DRIVE_URL = "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t"
MAX_RESUMES = 20


def ensure_repo(cfg: AgentConfig) -> str:
    """Sparse clone repo benchmark (chỉ tasks/, evaluation/, example/) nếu chưa có."""
    repo, _ = _paths(cfg)
    if (repo / "tasks").exists():
        return f"repo đã có: {repo}"
    repo.parent.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(args, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", "-b", cfg.eltbench.repo_branch, cfg.eltbench.repo_url, str(repo))
    run("git", "-C", str(repo), "sparse-checkout", "set", "tasks", "evaluation", "example")
    return f"đã clone {cfg.eltbench.repo_url}@{cfg.eltbench.repo_branch}"


def _download_resumable(url: str, target: Path) -> None:
    """Tải file lớn từ Drive; kết nối hay bị ngắt giữa chừng nên tải tiếp bằng HTTP Range tới khi zip hợp lệ."""
    for _ in range(MAX_RESUMES):
        done = target.stat().st_size if target.exists() else 0
        request = urllib.request.Request(url, headers={"Range": f"bytes={done}-"} if done else {})
        try:
            with urllib.request.urlopen(request, timeout=300) as resp, target.open("ab" if done else "wb") as dst:  # noqa: S310
                shutil.copyfileobj(resp, dst, length=1 << 20)
        except OSError:
            pass
        if zipfile.is_zipfile(target):
            return
    raise RuntimeError(f"Tải không đủ file {target.name} sau {MAX_RESUMES} lần")


def ensure_archives(cfg: AgentConfig) -> list[str]:
    _, raw = _paths(cfg)
    raw.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, drive_id in cfg.eltbench.archives.items():
        target = raw / name
        if not (target.exists() and zipfile.is_zipfile(target)):
            _download_resumable(DRIVE_URL.format(id=drive_id), target)
            lines.append(f"đã tải {name}")
    return lines


def setup_eltbench(con: duckdb.DuckDBPyConnection, cfg: AgentConfig) -> str:
    lines = [ensure_repo(cfg), *ensure_archives(cfg)]
    for db in cfg.eltbench.databases:
        lines += load_database(con, cfg, db)
        lines += [f"ground truth: {p.name}" for p in extract_ground_truth(cfg, db)]
        lines += [f"transpiled: {p}" for p in import_reference_models(cfg, db)]
    return "\n".join(lines)


def unique_key(repo: Path, db: str, model: str) -> list[str] | None:
    """Khóa để căn dòng = cột ORDER BY trong evaluation/<db>/<model>.sql của benchmark."""
    path = repo / "evaluation" / db / f"{model}.sql"
    if not path.exists():
        return None
    tree = sqlglot.parse_one(path.read_text(encoding="utf-8"), read="snowflake")
    order = tree.args.get("order")
    return [o.this.name.lower() for o in order.expressions if isinstance(o.this, exp.Column)] if order else None


def model_spec_text(repo: Path, db: str, model: str) -> str:
    data = yaml.safe_load((repo / "tasks" / db / "data_model.yaml").read_text(encoding="utf-8"))
    for item in data.get("models", []):
        if item["name"] == model:
            cols = "\n".join(f"- {c['name']}: {c.get('description', '').strip()}" for c in item.get("columns", []))
            return f"Model {db}.{model}: {item.get('description', '')}\nColumns:\n{cols}"
    return ""


def load_eltbench_models(con: duckdb.DuckDBPyConnection, cfg: AgentConfig) -> list[ModelSpec]:
    """Model SQL trong datasets/eltbench/models/<db>/<model>.sql, so với ground truth gt_verified/<db>/<model>.csv."""
    repo, raw = _paths(cfg)
    models = []
    for db in cfg.eltbench.databases:
        for path in sorted((cfg.resolve(cfg.eltbench.models_dir) / db).glob("*.sql")):
            text = path.read_text(encoding="utf-8")
            meta = parse_header(text)
            gt_name = meta.get("ground_truth", path.stem)  # biến thể chậm dùng chung ground truth với model gốc
            models.append(
                ModelSpec(
                    name=f"eltbench/{db}.{path.stem}",
                    dataset="eltbench",
                    sql=render_model_sql(strip_header(text)),
                    source_path=str(path),
                    pattern=meta.get("pattern", ""),
                    ground_truth_csv=str(raw / "extracted" / "gt_verified" / db / f"{gt_name}.csv"),
                    unique_key=unique_key(repo, db, gt_name),
                    ordered=False,
                    spec=model_spec_text(repo, db, gt_name),
                    extra={"origin": meta.get("origin", "")},
                )
            )
    return models
