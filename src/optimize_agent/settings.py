"""Đọc config.yaml thành các Pydantic model có kiểu."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"


class SandboxConfig(BaseModel):
    path: str = "data/sandbox.duckdb"
    threads: int = 4


class TimingConfig(BaseModel):
    warmup_runs: int = Field(default=1, ge=0)
    measured_runs: int = Field(default=5, ge=1)


class DetectConfig(BaseModel):
    slow_threshold_s: float = 1.0
    large_scan_rows: int = 5_000_000


class CompareConfig(BaseModel):
    rel_tol: float = 1e-6
    abs_tol: float = 1e-12


class OptimizeConfig(BaseModel):
    max_candidates: int = Field(default=3, ge=1)
    min_speedup: float = 1.1
    use_llm: bool = True
    candidate_timeout_factor: float = 3.0
    candidate_timeout_min_s: float = 30.0
    physical: Literal["auto", "always", "off"] = "auto"


class LLMConfig(BaseModel):
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    price_input_per_mtok: float = 0.15
    price_output_per_mtok: float = 0.60


class TpchConfig(BaseModel):
    scale_factor: float = 1
    slow_dir: str = "datasets/tpch/slow"


class EltBenchConfig(BaseModel):
    repo_url: str = "https://github.com/czanoli/ELT-Bench.git"
    repo_branch: str = "elt-bench-pr"
    archives: dict[str, str] = Field(default_factory=dict)  # tên zip -> Google Drive id
    repo_dir: str = "data/external/ELT-Bench-Verified"
    raw_dir: str = "data/external/eltbench_raw"
    models_dir: str = "datasets/eltbench/models"
    databases: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    detect: DetectConfig = Field(default_factory=DetectConfig)
    compare: CompareConfig = Field(default_factory=CompareConfig)
    optimize: OptimizeConfig = Field(default_factory=OptimizeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tpch: TpchConfig = Field(default_factory=TpchConfig)
    eltbench: EltBenchConfig = Field(default_factory=EltBenchConfig)
    reports_dir: str = "reports"

    def resolve(self, relative: str) -> Path:
        """Chuyển đường dẫn tương đối trong config thành đường dẫn tuyệt đối theo gốc project."""
        path = Path(relative)
        return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path | None = None) -> AgentConfig:
    """Đọc config.yaml (mặc định ở gốc project)."""
    config_path = path or DEFAULT_CONFIG
    if not config_path.exists():
        return AgentConfig()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AgentConfig.model_validate(data)
