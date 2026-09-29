"""Ghi log từng bước vào reports/<run_id>/steps.jsonl để không chạy lặp tác vụ đã xong (CLAUDE.md mục 8)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class StepLog:
    """Log append-only; `get_done` cho phép bỏ qua bước đã hoàn thành khi chạy lại cùng run_id."""

    def __init__(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "steps.jsonl"
        self._done: dict[tuple[str, str], dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("status") == "done":
                self._done[(entry["model"], entry["step"])] = entry.get("data", {})

    def record(self, model: str, step: str, status: str, data: dict[str, Any] | None = None) -> None:
        """Ghi một dòng log. status: started | done | skipped | failed."""
        entry = {"ts": time.time(), "model": model, "step": step, "status": status, "data": data or {}}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        if status == "done":
            self._done[(model, step)] = entry["data"]

    def get_done(self, model: str, step: str) -> dict[str, Any] | None:
        """Dữ liệu của bước đã hoàn thành trước đó, hoặc None."""
        return self._done.get((model, step))

    def count_steps(self, model: str) -> int:
        """Số bước đã hoàn thành của một model (dùng cho agent_cost)."""
        return sum(1 for (m, _step) in self._done if m == model)
