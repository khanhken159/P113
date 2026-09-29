"""Ứng viên 3: LLM (OpenAI gpt-4o-mini qua langchain-openai). Chỉ gọi sau sqlglot + rule.

Key đọc từ .env (OPENAI_API_KEY); key trống/placeholder -> LLM bị tắt, agent vẫn chạy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from src.optimize_agent.rewrite import Assumption, Candidate
from src.optimize_agent.settings import LLMConfig

SYSTEM_PROMPT = """You are a senior DuckDB performance engineer. Rewrite the given SQL so it runs faster on DuckDB
while returning EXACTLY the same result set (same columns, same column names and order, same rows, same values).

Hard rules:
- Output ONE read-only DuckDB SELECT statement (WITH/UNION allowed). No DDL/DML, no ATTACH/COPY/INSTALL/PRAGMA.
- Keep the same output column names and order. Keep the top-level ORDER BY / LIMIT semantics, including tie handling.
- Never change NULL semantics: do not add COALESCE, do not turn NULL into 0, keep LEFT/INNER join types.
- Never trim strings or change letter case. Do not change numeric precision/rounding.
- Watch JOIN fan-out: the row count must not change.
- If the rewrite relies on a data property (e.g. a key being unique, no duplicate rows), list it in "assumptions"
  with a check_sql: a SELECT returning a single integer = number of violations (0 means the assumption holds).

Answer with a JSON object: {"sql": "...", "explanation_vi": "<short explanation in Vietnamese for a reviewer>",
"assumptions": [{"text": "<Vietnamese>", "check_sql": "SELECT ..."}]}"""

_PLACEHOLDER_PREFIXES = ("sk-your", "your-", "test-key")


@dataclass
class LLMContext:
    """Thông tin agent đã đọc/đo được, gửi cho LLM."""

    sql: str
    spec: str
    schema: dict[str, Any]
    profile: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    plan_text: str
    previous_failures: list[str]


def api_key() -> str | None:
    """OPENAI_API_KEY hợp lệ từ môi trường/.env, hoặc None."""
    load_dotenv()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or key.startswith(_PLACEHOLDER_PREFIXES):
        return None
    return key


def build_user_prompt(ctx: LLMContext) -> str:
    """Prompt: spec -> schema/profile -> SQL -> phân tích -> plan -> các lần thử thất bại trước."""
    profile = [
        {"table": p["table"], "row_count": p["row_count"], "columns": [{k: c[k] for k in ("name", "type", "null_ratio")} for c in p["columns"]]}
        for p in ctx.profile
    ]
    parts = [
        f"## Model spec\n{ctx.spec or '(none)'}",
        f"## Schema\n{json.dumps(ctx.schema, ensure_ascii=False)}",
        f"## Data profile\n{json.dumps(profile, ensure_ascii=False)}",
        f"## SQL to optimize\n```sql\n{ctx.sql}\n```",
        f"## Detected problems\n{json.dumps(ctx.findings, ensure_ascii=False)}",
        f"## EXPLAIN ANALYZE (operator tree)\n{ctx.plan_text[:4000]}",
    ]
    if ctx.previous_failures:
        parts.append("## Previous attempts that FAILED (do not repeat)\n" + "\n".join(f"- {f}" for f in ctx.previous_failures))
    return "\n\n".join(parts)


def _parse_response(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def llm_candidate(ctx: LLMContext, cfg: LLMConfig) -> Candidate:
    """Gọi LLM một lần, trả về Candidate (sql rỗng nếu lỗi). Ghi token + chi phí ước tính vào llm_usage."""
    from langchain_openai import ChatOpenAI  # import muộn để test không cần langchain-openai

    key = api_key()
    if key is None:
        return Candidate(source="llm", sql="", explanation="LLM bị tắt: thiếu OPENAI_API_KEY hợp lệ")
    llm = ChatOpenAI(model=cfg.model, api_key=key, temperature=cfg.temperature).bind(response_format={"type": "json_object"})
    try:
        response = llm.invoke([("system", SYSTEM_PROMPT), ("user", build_user_prompt(ctx))])
    except Exception as exc:  # noqa: BLE001 — lỗi mạng/API không được làm hỏng cả lượt chạy
        return Candidate(source="llm", sql="", explanation=f"Gọi LLM lỗi: {exc}")
    usage = response.usage_metadata or {}
    tokens_in, tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    cost = tokens_in / 1e6 * cfg.price_input_per_mtok + tokens_out / 1e6 * cfg.price_output_per_mtok
    usage_info = {"model": cfg.model, "input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": round(cost, 6)}
    try:
        data = _parse_response(str(response.content))
    except json.JSONDecodeError as exc:
        return Candidate(source="llm", sql="", explanation=f"LLM trả về JSON lỗi: {exc}", llm_usage=usage_info)
    assumptions = [Assumption(a.get("text", ""), a.get("check_sql", "")) for a in data.get("assumptions", []) if isinstance(a, dict)]
    return Candidate(
        source="llm",
        sql=str(data.get("sql", "")).strip().rstrip(";"),
        explanation=str(data.get("explanation_vi", "")),
        applied=[f"llm:{cfg.model}"],
        assumptions=assumptions,
        llm_usage=usage_info,
    )
