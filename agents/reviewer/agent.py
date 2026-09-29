import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECISION = ROOT / "generated" / "review_decision.json"


def run(state: dict) -> dict:
    approved = bool(state.get("approved")) and state.get("test_status") == "PASS"
    decision = {
        "agent": "reviewer",
        "test_status": state.get("test_status"),
        "decision": "APPROVED" if approved else "REJECTED",
        "reason": "Reviewer approved after tests passed" if approved else state.get("review_reason") or "Approval missing or tests failed",
    }
    DECISION.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = dict(state.get("artifacts", {}))
    artifacts["review"] = str(DECISION)
    return {"artifacts": artifacts, "review_status": decision["decision"], "status": "ready" if approved else "review_required"}
