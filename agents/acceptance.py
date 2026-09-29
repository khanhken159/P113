import difflib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated"
PIPELINE = GENERATED / "generated_pipeline.py"
BEST = GENERATED / "best_pipeline.py"
FALLBACK = GENERATED / "fallback_pipeline.py"
REPORT = GENERATED / "test_report.json"
COMPARISON = GENERATED / "before_after.json"
FAILURES = GENERATED / "failures"
KNOWLEDGE = GENERATED / "knowledge_base.json"


def run(state: dict) -> dict:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    after = report.get("metrics", {})
    before = state.get("initial_metrics")
    comparisons = {}
    if before:
        for key in ("runtime_seconds", "cost_usd", "peak_memory_mb", "cpu_seconds", "correctness"):
            old, new = before.get(key), after.get(key)
            comparisons[key] = {"before": old, "after": new,
                                "delta": round(new - old, 6) if isinstance(old, (int, float)) and isinstance(new, (int, float)) else None}
    # Correctness is the configured gate: all Tester checks must pass.
    accepted = report.get("status") == "PASS" and after.get("correctness") == 1.0
    outcome = "BETTER" if accepted else "WORSE"
    if accepted:
        shutil.copyfile(PIPELINE, BEST)
        if before:
            failures = sorted(FAILURES.glob("attempt-*.json"))
            if failures:
                failure = json.loads(failures[-1].read_text(encoding="utf-8"))
                old_code = failure.get("code", "").splitlines()
                new_code = PIPELINE.read_text(encoding="utf-8").splitlines()
                fix = "\n".join(difflib.unified_diff(old_code, new_code, fromfile="before", tofile="after", lineterm=""))
                entries = json.loads(KNOWLEDGE.read_text(encoding="utf-8")) if KNOWLEDGE.exists() else []
                entries.append({"error": failure.get("error", failure.get("stderr", "")), "fix": fix, "metrics_before": failure.get("metrics", {}), "metrics_after": after})
                KNOWLEDGE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        fallback = BEST if BEST.exists() else FALLBACK
        if fallback.exists():
            shutil.copyfile(fallback, PIPELINE)
    COMPARISON.write_text(json.dumps({"outcome": outcome, "accepted": accepted,
        "thresholds": {"correctness": 1.0}, "metrics": comparisons,
        "best_version": str(BEST)}, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = dict(state.get("artifacts", {}))
    artifacts.update({"accepted_pipeline": str(BEST) if accepted else None, "comparison": str(COMPARISON)})
    return {"artifacts": artifacts, "acceptance_status": "ACCEPTED" if accepted else "FALLBACK",
            "status": "accepted" if accepted else "fallback"}
