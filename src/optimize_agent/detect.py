"""Detect: đánh dấu query chậm theo ngưỡng thời gian hoặc plan có full scan/join lớn (CLAUDE.md 7.4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.optimize_agent.analyze import SLOW_JOIN_OPERATORS
from src.optimize_agent.settings import DetectConfig
from src.optimize_agent.timing import PlanMetrics, TimingResult


@dataclass
class DetectResult:
    is_slow: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_slow(timing: TimingResult, plan: PlanMetrics, cfg: DetectConfig) -> DetectResult:
    """Chậm nếu median > ngưỡng, hoặc plan có join không-hash trên dữ liệu lớn."""
    reasons = []
    if timing.median_s > cfg.slow_threshold_s:
        reasons.append(f"median {timing.median_s:.3f}s > ngưỡng {cfg.slow_threshold_s}s")
    for op in plan.operators:
        if op["operator"] in SLOW_JOIN_OPERATORS and op["cardinality"] > cfg.large_scan_rows:
            reasons.append(f"plan có {op['operator']} trên {op['cardinality']:,} dòng")
    return DetectResult(is_slow=bool(reasons), reasons=reasons)
