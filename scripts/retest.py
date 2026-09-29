import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.acceptance import run as acceptance_run
from agents.human_review import run as human_review_run
from agents.reviewer.agent import run as reviewer_run
from agents.tester.agent import run as tester_run


def main() -> None:
    state = json.loads((ROOT / "generated" / "ui_state.json").read_text(encoding="utf-8"))
    tested = tester_run(state)
    state.update(tested)
    if state.get("test_status") == "PASS":
        state.update(acceptance_run(state))
        state.update(reviewer_run({**state, "approved": False}))
    else:
        state.update(human_review_run({**state, "interactive_review": False}))
    (ROOT / "generated" / "ui_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: state.get(key) for key in (
        "status", "test_status", "review_status", "acceptance_status",
        "human_review_status", "initial_metrics", "human_review_attempts", "artifacts",
    )}, ensure_ascii=False))


if __name__ == "__main__":
    main()
