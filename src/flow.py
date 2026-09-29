from typing import TypedDict

from langgraph.graph import (
    END,
    START,
    StateGraph,
)

from agents.clarifier.agent import (
    run as clarifier_run,
)
from agents.schema_inspection import run as schema_inspection_run
from agents.ambiguity_resolver import route_after_resolution, run as ambiguity_resolver_run

from agents.planner.agent import (
    run as planner_run,
)

from agents.coder.agent import (
    run as coder_run,
)
from agents.requirement_contract import run as requirement_extraction_run
from agents.plan_validator import run as plan_validator_run

from agents.tester.agent import (
    run as tester_run,
)

from agents.human_review import run as human_review_run
from agents.acceptance import run as acceptance_run
from agents.sql_optimizer import (
    MAX_MEMORY_REGRESSION,
    MAX_RUNTIME_REGRESSION,
    rollback_to_baseline,
    run_after_baseline_test,
)

from agents.reviewer.agent import (
    run as reviewer_run,
)


class FlowState(
    TypedDict,
    total=False,
):
    request: str
    provider: str
    approved: bool

    conversation_history: list[dict]

    needs_clarification: bool
    clarification_questions: list[str]
    clarification_fields: list[dict]
    clarification: dict
    data_profile: dict
    default_rules: dict
    ambiguity_resolution: dict
    resolved_business_rules: dict
    assumptions: list[dict]
    warnings: list[str]

    plan: dict
    requirement_contract: dict
    plan_valid: bool
    plan_validation_errors: list[str]
    artifacts: dict

    test_status: str
    review_status: str

    human_review_attempts: int
    human_review_status: str
    initial_metrics: dict
    baseline_metrics: dict
    candidate_metrics: dict
    fallback_metrics: dict
    performance_metrics: dict
    baseline_test_status: str
    candidate_test_status: str
    fallback_test_status: str
    optimization_phase: str
    optimizer_status: str
    optimization_thresholds: dict
    optimizer_rollback: bool
    acceptance_status: str
    interactive_review: bool

    status: str
    error: str
    schema_inspection_status: str


def route_after_clarifier(
    state: FlowState,
) -> str:
    return "end"


def route_after_ambiguity_resolver(state: FlowState) -> str:
    return route_after_resolution(state)


def route_after_requirements(state: FlowState) -> str:
    return "end" if state.get("status") == "failed" else "ambiguity_resolver"


def route_after_planner(state: FlowState) -> str:
    return "end" if state.get("status") == "failed" else "plan_validator"


def route_after_plan_validator(state: FlowState) -> str:
    return "coder" if state.get("plan_valid") else "end"


def route_after_coder(state: FlowState) -> str:
    return "end" if state.get("status") == "failed" else "tester"


def route_after_tester(
    state: FlowState,
) -> str:
    phase = state.get("optimization_phase", "baseline")
    passed = state.get("test_status") == "PASS"

    if phase == "verify_candidate":
        is_optimized_candidate = state.get("optimizer_status") == "OPTIMIZED"
        if not passed or (is_optimized_candidate and _candidate_regressed(state)):
            return "rollback"
        return "acceptance"

    if phase == "verify_fallback":
        return "acceptance" if passed else "human_review"

    # A correctness failure goes to repair. Optimizing incorrect output cannot
    # repair its meaning. Only a passing baseline proceeds to SQL optimization.
    return "optimizer" if passed else "human_review"


def _candidate_regressed(state: FlowState) -> bool:
    baseline = state.get("baseline_metrics", {})
    candidate = state.get("candidate_metrics", state.get("performance_metrics", {}))
    # Allow 5% measurement noise; a larger regression in either measured
    # pipeline metric restores the pre-optimizer SQL.
    tolerances = {
        "runtime_seconds": MAX_RUNTIME_REGRESSION,
        "peak_memory_mb": MAX_MEMORY_REGRESSION,
    }
    for metric, tolerance in tolerances.items():
        before, after = baseline.get(metric), candidate.get(metric)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            if after > before * (1 + tolerance):
                return True
    return False


def route_after_human_review(state: FlowState) -> str:
    return "end" if state.get("human_review_status") in {"FALLBACK", "PENDING"} else "tester"


def route_after_acceptance(state: FlowState) -> str:
    return "reviewer" if state.get("acceptance_status") == "ACCEPTED" else "end"


def build_graph():
    graph = StateGraph(FlowState)

    graph.add_node("schema_inspection", schema_inspection_run)
    graph.add_node("ambiguity_resolver", ambiguity_resolver_run)
    graph.add_node("clarifier", clarifier_run)

    graph.add_node(
        "planner",
        planner_run,
    )
    graph.add_node("requirements", requirement_extraction_run)
    graph.add_node("plan_validator", plan_validator_run)

    graph.add_node(
        "coder",
        coder_run,
    )

    graph.add_node(
        "tester",
        tester_run,
    )

    graph.add_node("human_review", human_review_run)
    graph.add_node("acceptance", acceptance_run)
    graph.add_node("optimizer", run_after_baseline_test)
    graph.add_node("rollback", rollback_to_baseline)

    graph.add_node(
        "reviewer",
        reviewer_run,
    )

    graph.add_edge(START, "schema_inspection")
    graph.add_edge("schema_inspection", "requirements")

    graph.add_conditional_edges(
        "requirements",
        route_after_requirements,
        {"ambiguity_resolver": "ambiguity_resolver", "end": END},
    )

    graph.add_conditional_edges(
        "ambiguity_resolver",
        route_after_ambiguity_resolver,
        {"planner": "planner", "clarifier": "clarifier", "end": END},
    )

    graph.add_conditional_edges(
        "clarifier",
        route_after_clarifier,
        {"end": END},
    )

    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"plan_validator": "plan_validator", "end": END},
    )

    graph.add_conditional_edges(
        "plan_validator",
        route_after_plan_validator,
        {"coder": "coder", "end": END},
    )

    graph.add_conditional_edges(
        "coder",
        route_after_coder,
        {"tester": "tester", "end": END},
    )

    graph.add_conditional_edges(
        "tester",
        route_after_tester,
        {
            "optimizer": "optimizer",
            "rollback": "rollback",
            "acceptance": "acceptance",
            "human_review": "human_review",
        },
    )

    graph.add_edge("optimizer", "tester")
    graph.add_edge("rollback", "tester")

    graph.add_conditional_edges(
        "human_review",
        route_after_human_review,
        {"tester": "tester", "end": END},
    )

    graph.add_conditional_edges(
        "acceptance",
        route_after_acceptance,
        {"reviewer": "reviewer", "end": END},
    )

    graph.add_edge(
        "reviewer",
        END,
    )

    return graph.compile()
