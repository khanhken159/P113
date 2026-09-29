"""Apply a human review decision to the pending pipeline artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.reviewer.agent import run as reviewer_run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved", action="store_true")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    report_path = ROOT / "generated" / "test_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result = reviewer_run({
        "approved": args.approved,
        "review_reason": args.reason,
        "test_status": report.get("status"),
        "artifacts": {},
    })
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
