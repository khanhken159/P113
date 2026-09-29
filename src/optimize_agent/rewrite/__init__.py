"""Sinh ứng viên rewrite: sqlglot.optimizer -> rule xác định -> LLM (tối đa 3 ứng viên/query)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Assumption:
    """Giả định về dữ liệu mà rewrite dựa vào; `check_sql` trả về số vi phạm (0 = giả định đúng)."""

    text: str
    check_sql: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    source: str  # "sqlglot" | "rules" | "llm"
    sql: str
    explanation: str  # lý do bằng lời dễ hiểu (tiếng Việt)
    applied: list[str] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    llm_usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["assumptions"] = [a.to_dict() for a in self.assumptions]
        return data
