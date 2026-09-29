import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("FLOWFORGE_DATA_DIR", str(ROOT / "eval" / "results" / "flowforge" / "data"))

from src.flow import build_graph

SESSION_DIR = ROOT / "generated" / "sessions"


def get_session_path(session_name: str) -> Path:
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", session_name)
    return SESSION_DIR / f"{safe_name}.json"


def load_history(session_path: Path) -> list[dict]:
    if not session_path.exists():
        return []

    try:
        return json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_history(session_path: Path, history: list[dict]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    session_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run FlowForge Multi-Agent MVP"
    )

    parser.add_argument(
        "--request",
        required=True,
        help="Natural-language data requirement",
    )

    parser.add_argument(
        "--approve",
        action="store_true",
        help="Reviewer approves after tests pass",
    )

    parser.add_argument(
        "--provider",
        choices=["mock", "gemini", "openai"],
        default="mock",
        help="AI provider",
    )

    parser.add_argument(
        "--session",
        default="default",
        help="Tên phiên hội thoại cần ghi nhớ",
    )

    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Xóa lịch sử của session trước khi chạy",
    )

    parser.add_argument(
        "--defer-human-review",
        action="store_true",
        help="Return a pending review to an external UI instead of prompting in the terminal",
    )
    parser.add_argument("--clarification", default="{}", help="Structured answers to Clarifier questions")

    args = parser.parse_args()

    session_path = get_session_path(args.session)

    if args.reset_session:
        history = []
    else:
        history = load_history(session_path)

    graph = build_graph()

    result = graph.invoke({
        "request": args.request,
        "provider": args.provider,
        "approved": args.approve,
        "interactive_review": not args.defer_human_review,
        "conversation_history": history,
        "clarification": json.loads(args.clarification),
    })

    history.append({
        "role": "user",
        "content": args.request,
    })

    if result.get("clarification_questions"):
        history.append({
            "role": "assistant",
            "content": "Clarifier hỏi: " + " | ".join(
                result["clarification_questions"]
            ),
        })

    save_history(session_path, history)

    print(json.dumps({
        "status": result["status"],
        "clarification_questions": result.get(
            "clarification_questions",
            [],
        ),
        "clarification_fields": result.get("clarification_fields", []),
        "ambiguity_resolution": result.get("ambiguity_resolution", {}),
        "resolved_business_rules": result.get("resolved_business_rules", {}),
        "data_profile_summary": {
            "sources": [{key: source.get(key) for key in ("source_name", "row_count", "data_grain", "data_grain_confidence", "data_grain_evidence", "status_counts", "exact_duplicate_row_count")}
                        for source in result.get("data_profile", {}).get("sources", [])]
        },
        "artifacts": result.get("artifacts", {}),
        "test_status": result.get("test_status"),
        "baseline_test_status": result.get("baseline_test_status"),
        "baseline_metrics": result.get("baseline_metrics"),
        "candidate_test_status": result.get("candidate_test_status"),
        "candidate_metrics": result.get("candidate_metrics"),
        "fallback_test_status": result.get("fallback_test_status"),
        "fallback_metrics": result.get("fallback_metrics"),
        "performance_metrics": result.get("performance_metrics"),
        "optimization_phase": result.get("optimization_phase"),
        "optimizer_status": result.get("optimizer_status"),
        "optimization_thresholds": result.get("optimization_thresholds"),
        "optimizer_rollback": result.get("optimizer_rollback", False),
        "review_status": result.get("review_status"),
        "acceptance_status": result.get("acceptance_status"),
        "human_review_status": result.get("human_review_status"),
        "error": result.get("error"),
        "plan": result.get("plan"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
