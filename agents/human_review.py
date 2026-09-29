import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated"
PIPELINE = GENERATED / "generated_pipeline.py"
FALLBACK = GENERATED / "fallback_pipeline.py"
FAILURES = GENERATED / "failures"
KNOWLEDGE_BASE = GENERATED / "knowledge_base.json"


def run(state: dict) -> dict:
    """Persist the failed attempt, let a person edit the pipeline, then retest."""
    report_path = GENERATED / "test_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    FAILURES.mkdir(parents=True, exist_ok=True)
    attempt = int(state.get("human_review_attempts", 0)) + 1
    failed_code = PIPELINE.read_text(encoding="utf-8") if PIPELINE.exists() else ""
    failed_checks = [name for name, passed in report.get("checks", {}).items() if not passed]
    error_text = report.get("stderr", "") or ", ".join(failed_checks)
    if not FALLBACK.exists() and failed_code:
        FALLBACK.write_text(failed_code, encoding="utf-8")
    failure_path = FAILURES / f"attempt-{attempt}.json"
    failure_path.write_text(json.dumps({
        "request": state.get("request", ""),
        "attempt": attempt,
        "code": failed_code,
        "metrics": report.get("metrics", {}),
        "checks": report.get("checks", {}),
        "stderr": report.get("stderr", ""),
        "error": error_text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    entries = []
    if KNOWLEDGE_BASE.exists():
        try:
            entries = json.loads(KNOWLEDGE_BASE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    matches = [entry for entry in entries if entry.get("error") and entry["error"] in error_text]

    if not state.get("interactive_review", True):
        return {
            "human_review_attempts": attempt,
            "human_review_status": "PENDING",
            "initial_metrics": state.get("initial_metrics") or report.get("metrics", {}),
            "status": "human_review_pending",
        }

    print(f"\nTester FAIL. Report: {failure_path}")
    if matches:
        print("Knowledge Base gợi ý:")
        for match in matches[-3:]:
            print(f"- {match.get('fix', '')}")
    print(f"Sửa {PIPELINE} rồi nhấn Enter để retest; nhập 'skip' để fallback.")
    decision = input("Human review> ").strip().lower()

    if decision == "skip":
        best = GENERATED / "best_pipeline.py"
        fallback = best if best.exists() else FALLBACK
        if fallback.exists():
            PIPELINE.write_text(fallback.read_text(encoding="utf-8"), encoding="utf-8")
        return {"human_review_attempts": attempt, "human_review_status": "FALLBACK", "status": "fallback"}

    return {
        "human_review_attempts": attempt,
        "human_review_status": "RETEST",
        "initial_metrics": state.get("initial_metrics") or report.get("metrics", {}),
        "status": "human_reviewed",
    }
