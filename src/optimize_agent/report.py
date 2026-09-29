"""Report: report.json + report.md cho người duyệt (HITL) và review.json để Engineer điền quyết định.

Chỉ ứng viên qua Correctness Gate + đủ nhanh mới xuất hiện trong mục "Đề xuất". SQL gốc không bị sửa.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import sqlglot

METRIC_ROWS = [
    ("n_models", "Số model"),
    ("n_slow", "Số query chậm (Detect)"),
    ("n_models_optimized", "Số model được tối ưu (có ứng viên)"),
    ("n_proposals", "Số đề xuất"),
    ("correctness_rate", "correctness_rate (đề xuất qua gate / đề xuất)"),
    ("raw_candidate_pass_rate", "Tỉ lệ ứng viên thô qua gate"),
    ("model_gate_rate", "#model đạt gate / #model được tối ưu"),
    ("srdt", "SRDT (model đúng hoàn toàn / tổng)"),
    ("baseline_correct_rate", "SQL gốc khớp tham chiếu"),
    ("speedup_median", "speedup (median các đề xuất)"),
    ("speedup_geomean", "speedup (trung bình nhân)"),
    ("cost_reduction_rows_median", "cost_reduction — rows scanned (median)"),
    ("cost_reduction_memory_median", "cost_reduction — peak memory (median)"),
    ("improvement_rate", "improvement_rate (chậm → speedup ≥ 1.1 & qua gate)"),
    ("agent_cost_usd_avg", "agent_cost — $ LLM trung bình / task"),
    ("agent_steps_avg", "agent_cost — số bước trung bình / task"),
    ("llm_calls_total", "Tổng số lần gọi LLM"),
    ("pass_at_k", "pass@k"),
    ("pass_hat_k", "pass^k"),
    ("rejection_rate", "rejection_rate (điền sau review)"),
]


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _bytes(n: int | None) -> str:
    return "—" if n is None else f"{n / 1024 / 1024:.1f} MB"


def pretty_sql(sql: str) -> str:
    """Format bằng sqlglot để diff chỉ thể hiện thay đổi logic, không phải khác biệt định dạng."""
    try:
        return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb", pretty=True)
    except sqlglot.errors.ParseError:
        return sql


def sql_diff(before: str, after: str) -> str:
    before, after = pretty_sql(before), pretty_sql(after)
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), "original.sql", "proposal.sql", lineterm="")
    return "\n".join(lines)


def metrics_table(metrics_by_dataset: dict[str, dict[str, Any]]) -> str:
    names = list(metrics_by_dataset)
    header = "| Chỉ số | " + " | ".join(names) + " |\n|---|" + "---|" * len(names)
    rows = [f"| {label} | " + " | ".join(_fmt(metrics_by_dataset[d].get(key)) for d in names) + " |" for key, label in METRIC_ROWS]
    return header + "\n" + "\n".join(rows)


def _proposal_section(model: dict[str, Any]) -> str:
    p, base = model["proposal"], model["baseline"]
    checks = "\n".join(
        f"  - {c['text']} — {'đúng' if c['holds'] else ('SAI' if c['holds'] is False else 'chưa kiểm được')} (vi phạm: {_fmt(c.get('violations'))})"
        for c in p["assumption_checks"]
    ) or "  - (không có — rewrite tương đương tuyệt đối)"
    findings = "\n".join(f"  - `{f['code']}`: {f['message']}" for f in model["findings"]) or "  - —"
    return f"""### {model['name']}
- **Anti-pattern cố ý:** {model['pattern'] or '—'}
- **Nguyên nhân phát hiện:**
{findings}
- **Nguồn đề xuất:** `{p['candidate']['source']}` ({', '.join(p['candidate']['applied'])})
- **Trước → Sau (median):** {base['timing']['median_s']:.3f}s → {p['timing']['median_s']:.3f}s — **speedup {p['speedup']}x**
- **Rows scanned:** {base['rows_scanned']:,} → {p['rows_scanned']:,} (giảm {_fmt(p['cost_reduction_rows'])})
- **Peak memory:** {_bytes(base['peak_memory_bytes'])} → {_bytes(p['peak_memory_bytes'])} (giảm {_fmt(p['cost_reduction_memory'])})
- **Correctness Gate:** PASS ({p['gate']['actual_rows']} dòng, khớp tham chiếu)
- **Lý do (dễ hiểu):** {p['candidate']['explanation']}
- **Giả định dữ liệu (Reviewer cần xác nhận):**
{checks}

```diff
{sql_diff(model['original_sql'], p['candidate']['sql'])}
```
"""


def _rejected_section(model: dict[str, Any]) -> str:
    lines = [f"### {model['name']} — median {model['baseline']['timing']['median_s']:.3f}s"]
    for c in model["candidates"]:
        reason = c.get("error") or "; ".join((c.get("gate") or {}).get("reasons", [])) or (f"speedup {c.get('speedup')}" if c.get("speedup") else "")
        lines.append(f"- `{c['candidate']['source']}` → **{c['status']}** {reason}")
    if not model["candidates"]:
        lines.append("- Không sinh được ứng viên nào (rule không áp dụng, LLM tắt/không có key).")
    return "\n".join(lines)


def _physical_section(model: dict[str, Any]) -> str:
    p = model["physical"]
    if p["status"] != "accepted":
        reason = p.get("error") or "; ".join((p.get("gate") or {}).get("reasons", [])) or f"speedup {p.get('speedup')}"
        return f"- {model['name']}: đã thử `{', '.join(p['candidate']['applied'])}` → **{p['status']}** {reason}"
    return (
        f"- **{model['name']}**: `{', '.join(p['candidate']['applied'])}` — {model['baseline']['timing']['median_s']:.3f}s → "
        f"{p['timing']['median_s']:.3f}s (**speedup {p['speedup']}x** so với SQL gốc, qua gate). {p['candidate']['explanation']}"
    )


def render_markdown(run_id: str, env: dict[str, Any], metrics: dict[str, dict[str, Any]], models: list[dict[str, Any]]) -> str:
    proposals = [m for m in models if m.get("proposal")]
    rejected = [m for m in models if m["detect"]["is_slow"] and not m.get("proposal")]
    not_slow = [m for m in models if not m["detect"]["is_slow"]]
    env_lines = "\n".join(f"- {k}: `{v}`" for k, v in env.items())
    fast = "\n".join(f"- {m['name']}: {m['baseline']['timing']['median_s']:.3f}s (SQL gốc {'đúng' if m['baseline']['correct'] else 'SAI'})" for m in not_slow) or "- —"
    return f"""# Optimizer Agent — Báo cáo run `{run_id}`

> Đây là **đề xuất**, chưa được áp dụng. SQL gốc không bị sửa. Engineer duyệt bằng cách điền `review.json`.

## Môi trường đo
{env_lines}

## Bảng chỉ số
{metrics_table(metrics)}

## Đề xuất chờ duyệt ({len(proposals)})
{chr(10).join(_proposal_section(m) for m in proposals) or '_Không có đề xuất._'}

## Query chậm nhưng không có đề xuất ({len(rejected)})
{chr(10).join(_rejected_section(m) for m in rejected) or '_Không có._'}

## Đề xuất bố trí dữ liệu vật lý (sắp xếp / partition)
{chr(10).join(_physical_section(m) for m in models if m.get("physical")) or '_Không có query nào vẫn chậm sau rewrite SQL — không cần đề xuất vật lý._'}

## Model không chậm — không cần tối ưu ({len(not_slow)})
{fast}
"""


def review_template(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "instructions": "Điền decision = approve | reject cho từng đề xuất, kèm comment. rejection_rate tính từ file này.",
        "proposals": {m["name"]: {"decision": "", "comment": ""} for m in models if m.get("proposal")},
    }


def write_reports(run_dir: Path, payload: dict[str, Any]) -> None:
    """Ghi report.json, report.md, và review.json (không ghi đè nếu Engineer đã điền)."""
    (run_dir / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown = render_markdown(payload["run_id"], payload["environment"], payload["metrics"], payload["models"])
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    review_path = run_dir / "review.json"
    if not review_path.exists():
        review_path.write_text(json.dumps(review_template(payload["models"]), ensure_ascii=False, indent=2), encoding="utf-8")
