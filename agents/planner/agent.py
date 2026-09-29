import json
import os
import re
import unicodedata
from copy import deepcopy
from pathlib import Path

from agents.llm import generate_text
from agents.prompt_loader import load_system_prompt
from agents.requirement_contract import (
    build_revenue_semantics,
    canonical_metric,
    is_revenue_request,
    revenue_status_filter_is_covered,
)

ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = ROOT / "generated" / "pipeline_plan.json"
SOURCE_STATUSES = {"shopee": "completed", "tiki": "delivered", "website": "done"}
CUSTOM_METRICS = {
    "record_count", "completed_count", "cancelled_count", "total_amount",
    "distinct_order_count", "average_amount", "min_amount", "max_amount", "median_amount",
    "completion_rate", "cancellation_rate", "average_distance", "average_duration", "sum_value",
    "customer_lifetime_value", "running_revenue", "customer_rank",
}


def _currency_column(schema: dict) -> str | None:
    return next((column for column in schema.get("columns", [])
                 if re.sub(r"[^a-z]", "", column.casefold()) in
                 {"currency", "currencycode", "currencyunit"}), None)


def _guess_column(columns: list[str], terms: tuple[str, ...]) -> str | None:
    for column in columns:
        normalized = "".join(char.lower() for char in column if char.isalnum())
        if any(
            (normalized == term if term == "id" else term in normalized)
            for term in terms
        ):
            return column
    return None


def _as_mapping(value) -> dict:
    """Treat malformed optional LLM mapping fields as empty and repair by schema."""
    return value if isinstance(value, dict) else {}


def _normalize_model_collection(value, field: str, item_types: tuple[type, ...]) -> list:
    """Normalize a collection container using its declared item types only."""
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, item_types):
        items = [value]
    else:
        raise ValueError(
            f"Planner {field} must be a JSON list; received {type(value).__name__}"
        )
    if any(not isinstance(item, item_types) for item in items):
        expected = " or ".join(item_type.__name__ for item_type in item_types)
        raise ValueError(f"Planner {field} entries must be {expected} values")
    return items


def _semantic_label_key(value: str) -> str:
    """Compare output labels without treating spaces and underscores as distinct."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _normalize_output_measure_bindings(columns: list[str], metrics: list[str], mapping: dict,
                                       window_specs: list[dict]) -> tuple[dict, list[dict]]:
    """Keep output aliases, grouped measures, and window inputs on one metric binding.

    Output labels can be semantic aliases from the request. If such a label maps
    to a different supported measure, the label's canonical contract binding is
    authoritative. Repair references that reused that same stale binding only
    when it was unique, so an independent requested measure is never rewritten.
    """
    normalized_mapping = dict(mapping)
    stale_measure_bindings = {}
    for label in columns:
        canonical = canonical_metric(label)
        if canonical not in metrics or label == canonical:
            continue
        previous = normalized_mapping.get(label)
        if previous and previous != canonical:
            if canonical_metric(previous) != canonical:
                other_targets = {target for target, runtime in normalized_mapping.items()
                                 if target != label and runtime == previous}
                if not other_targets:
                    stale_measure_bindings[previous] = canonical
            normalized_mapping[label] = canonical

    normalized_windows = []
    for source in window_specs:
        window = dict(source)
        value_field = window.get("value_field")
        if value_field in stale_measure_bindings:
            window["value_field"] = stale_measure_bindings[value_field]
        normalized_windows.append(window)
    return normalized_mapping, normalized_windows


def _normalize_output_metric_bindings(columns: list[str], metrics: list[str], mapping: dict,
                                      window_specs: list[dict], requested_metrics: set[str]
                                      ) -> tuple[list[str], dict, list[dict]]:
    """Resolve declared metric labels against the requested semantic metric slots.

    A model may bind an output label to a different supported slot. When the
    requested contract already identifies that label's semantic metric, use
    the contract binding and discard an otherwise-unrequested stale slot.
    """
    normalized_metrics = list(dict.fromkeys(metrics))
    for label in columns:
        semantic_metric = canonical_metric(label)
        if semantic_metric in requested_metrics and semantic_metric in CUSTOM_METRICS:
            normalized_metrics.append(semantic_metric)
    normalized_metrics = list(dict.fromkeys(normalized_metrics))
    original_mapping = dict(mapping)
    normalized_mapping, normalized_windows = _normalize_output_measure_bindings(
        columns, normalized_metrics, original_mapping, window_specs,
    )
    remaining_targets = {canonical_metric(runtime) for runtime in normalized_mapping.values()}
    stale_slots = {
        canonical_metric(original_mapping[label])
        for label in columns
        if label in original_mapping
        and normalized_mapping.get(label) != original_mapping[label]
        and canonical_metric(original_mapping[label]) in normalized_metrics
        and canonical_metric(original_mapping[label]) not in requested_metrics
        and canonical_metric(original_mapping[label]) not in remaining_targets
    }
    normalized_metrics = [metric for metric in normalized_metrics if metric not in stale_slots]
    return normalized_metrics, normalized_mapping, normalized_windows


def _normalize_identity_metric_aliases(columns: list[str], metrics: list[str], mapping: dict,
                                       derived_metrics: list[dict], requested_metric_labels: set[str]
                                       ) -> tuple[list[str], dict, list[dict]]:
    """Bind requested output aliases that explicitly copy a supported metric."""
    normalized_metrics = list(metrics)
    normalized_mapping = dict(mapping)
    remaining_derived = []
    for derived in derived_metrics:
        name = str(derived.get("name", ""))
        inputs = derived.get("inputs", [])
        operation = str(derived.get("operation", "")).casefold()
        metric = canonical_metric(inputs[0]) if isinstance(inputs, list) and len(inputs) == 1 else ""
        if (name in columns and _semantic_label_key(name) in requested_metric_labels
                and operation in {"copy", "identity"} and metric in CUSTOM_METRICS):
            normalized_metrics.append(metric)
            normalized_mapping[name] = metric
        else:
            remaining_derived.append(derived)
    return list(dict.fromkeys(normalized_metrics)), normalized_mapping, remaining_derived


def _compile_aggregate_formula(formula: dict, spec: dict, output_dimensions: list[str]) -> str | None:
    """Compile a contract-level aggregate formula into the existing output slots."""
    name = str(formula.get("name", "")).strip()
    operation = str(formula.get("operation", "")).casefold()
    if not name or operation not in {"add", "subtract", "multiply", "divide"}:
        return "formula needs a named output and supported binary operation"

    mapping = spec.get("column_mapping", {})
    columns = spec.setdefault("columns", [])
    metrics = spec.get("metrics", [])
    operands = [formula.get("numerator"), formula.get("denominator")]
    canonical_operands = []
    for operand in operands:
        if not isinstance(operand, dict):
            return "formula operands must declare semantic metrics and grouping dimensions"
        metric_label = str(operand.get("metric", ""))
        mapped_metric = next((runtime for label, runtime in mapping.items()
                              if _semantic_label_key(label) == _semantic_label_key(metric_label)), None)
        metric = canonical_metric(mapped_metric if mapped_metric is not None else metric_label)
        if metric not in metrics:
            return f"base metric {metric!r} is not present as an executable aggregate"
        dimensions = operand.get("dimensions", [])
        if not isinstance(dimensions, list):
            return "formula operand dimensions must be a list"
        resolved_dimensions = []
        for dimension in dimensions:
            match = next((runtime for label, runtime in mapping.items()
                          if _semantic_label_key(label) == _semantic_label_key(dimension)), None)
            resolved = match if match is not None else dimension
            if not any(_semantic_label_key(resolved) == _semantic_label_key(item)
                       for item in output_dimensions):
                return f"formula grouping dimension {dimension!r} is not present in this output grain"
            resolved_dimensions.append(resolved)
        canonical_operands.append((metric, list(dict.fromkeys(resolved_dimensions))))

    numerator_metric, numerator_dimensions = canonical_operands[0]
    denominator_metric, denominator_dimensions = canonical_operands[1]
    normalized_dimensions = {_semantic_label_key(value) for value in output_dimensions}
    if {_semantic_label_key(value) for value in numerator_dimensions} != normalized_dimensions:
        return "numerator grouping scope does not match the output grain"
    denominator_scope = {_semantic_label_key(value) for value in denominator_dimensions}
    if not denominator_scope.issubset(normalized_dimensions):
        return "denominator grouping scope must be the output grain or one of its parent scopes"

    def measure_alias(metric: str) -> str:
        return next((label for label, runtime in mapping.items()
                     if label in columns and canonical_metric(runtime) == metric), metric)

    inputs = [measure_alias(numerator_metric)]
    if denominator_scope != normalized_dimensions:
        windows = spec.setdefault("window_specs", [])
        matching_window = next((window for window in windows
                                if str(window.get("function", "")).upper() == "SUM"
                                and {_semantic_label_key(value) for value in window.get("partition_by", [])}
                                == denominator_scope
                                and canonical_metric(window.get("value_field", "")) == denominator_metric), None)
        if matching_window:
            window_name = matching_window.get("name")
        else:
            window_name = f"__formula_{len(windows) + 1}_denominator"
            windows.append({
                "name": window_name,
                "function": "SUM",
                "partition_by": denominator_dimensions,
                "value_field": measure_alias(denominator_metric),
            })
        inputs.append(window_name)
    else:
        inputs.append(measure_alias(denominator_metric))

    if name not in columns:
        columns.append(name)
    derived = spec.setdefault("derived_metrics", [])
    replacement = {"name": name, "operation": operation, "inputs": inputs}
    previous = next((index for index, item in enumerate(derived) if item.get("name") == name), None)
    if previous is None:
        derived.append(replacement)
    else:
        derived[previous] = replacement
    return None


def _resolve_schema_name(value, schemas: list[dict], context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Planner {context} must name an uploaded CSV")
    name = value.strip()
    exact = [schema for schema in schemas if name in {
        str(schema.get("name", "")), str(schema.get("original_name", ""))
    }]
    if not exact:
        folded = name.casefold()
        exact = [schema for schema in schemas if folded in {
            str(schema.get("name", "")).casefold(), str(schema.get("original_name", "")).casefold()
        }]
    if len(exact) != 1:
        raise ValueError(f"Planner {context} {name!r} does not identify one uploaded CSV")
    return str(exact[0]["name"])


def _normalize_model_sources(raw: dict, schemas: list[dict], request: str = "",
                             requirements: dict | None = None,
                             clarification: dict | None = None) -> None:
    """Resolve model file references locally and validate JSON object shapes."""
    requirements = requirements or {}
    clarification = clarification or {}
    source_lists = ("files", "joins", "derived_fields", "filters", "output_specs")
    for key in source_lists:
        raw[key] = _normalize_model_collection(raw.get(key, []), key, (dict,))
    for index, spec in enumerate(raw["output_specs"]):
        for key in ("columns", "group_dimensions", "metrics"):
            if key in spec:
                spec[key] = _normalize_model_collection(spec[key], f"output_specs[{index}].{key}", (str,))
        for key in ("window_specs", "post_window_filters"):
            if key in spec:
                spec[key] = _normalize_model_collection(spec[key], f"output_specs[{index}].{key}", (dict,))
        if "derived_metrics" in spec:
            spec["derived_metrics"] = _normalize_model_collection(
                spec["derived_metrics"], f"output_specs[{index}].derived_metrics", (dict,)
            )
        mapping = spec.get("column_mapping")
        if mapping is not None and (not isinstance(mapping, dict) or any(
            not isinstance(target, str) or not isinstance(role, str) for target, role in mapping.items()
        )):
            raise ValueError("Planner output column_mapping must map string targets to string runtime roles")

    schema_by_name = {schema["name"]: schema for schema in schemas}
    files_by_name = {}
    normalized_files = []
    for item in raw["files"]:
        schema_name = _resolve_schema_name(item.get("name"), schemas, "files entry")
        schema = schema_by_name[schema_name]
        normalized = {**item, "name": schema_name}
        for key in ("fields", "dimensions", "constants", "constant_provenance", "semantic_roles"):
            normalized[key] = _as_mapping(normalized.get(key))
        normalized_files.append(normalized)
        files_by_name[schema_name] = normalized
    raw["files"] = normalized_files

    base = raw.get("base_file")
    if base:
        raw["base_file"] = _resolve_schema_name(base, schemas, "base_file")

    unions = _normalize_model_collection(raw.get("union_files", []), "union_files", (str, dict))
    normalized_unions = []

    def normalized_label(value) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")

    request_text = unicodedata.normalize("NFKD", str(request).casefold().replace("đ", "d"))
    request_text = "".join(char for char in request_text if not unicodedata.combining(char))
    requested_operations = requirements.get("requested_operations", [])
    explicit_union = (
        str(clarification.get("operation", "")).casefold() == "append"
        or (isinstance(requested_operations, list) and "union" in {
            str(operation).casefold() for operation in requested_operations
        })
        or bool(re.search(r"\b(union|append|stack|combine|gop|tong hop|hop nhat|noi cac dong)\b", request_text))
    )
    output_labels = set()
    for output_name in requirements.get("requested_output_files", []):
        output_labels.add(normalized_label(Path(str(output_name)).stem))
    for output_spec in raw["output_specs"]:
        if isinstance(output_spec, dict):
            output_labels.add(normalized_label(output_spec.get("name")))
            output_labels.add(normalized_label(Path(str(output_spec.get("file_name", ""))).stem))

    def recover_output_label(value, item_index: int) -> list[dict] | None:
        """Recover a mistaken output label only from confirmed union sources."""
        confirmed_sources = requirements.get("requested_sources", [])
        # A single logical Union reference can be expanded only when the
        # contract independently establishes every physical source and each
        # source already has a runtime binding. Labels never establish members.
        logical_group = (len(unions) == 1 and len(confirmed_sources) > 1 and not raw["joins"]
                         and all(any(source in {schema.get("name"), schema.get("original_name")}
                                     and schema["name"] in files_by_name for schema in schemas)
                                 for source in confirmed_sources))
        if not explicit_union or (not logical_group and normalized_label(Path(str(value)).stem) not in output_labels):
            return None

        requested_sources = requirements.get("requested_sources", [])
        if requested_sources:
            if not isinstance(requested_sources, list):
                raise ValueError("Requirement contract requested_sources must be a JSON list")
            source_names = [
                _resolve_schema_name(source, schemas, f"requested_sources[{index}]")
                for index, source in enumerate(requested_sources)
            ]
        else:
            # Planner's per-file mappings are acceptable evidence only when they
            # cover multiple real uploads and the request explicitly asks to union.
            source_names = list(files_by_name)

        source_names = list(dict.fromkeys(source_names))
        if len(source_names) < 2:
            raise ValueError(
                f"Planner union_files[{item_index}] names output {value!r}, but fewer than two "
                "confirmed uploaded CSV sources are available for the requested union"
            )
        return [dict(files_by_name.get(name, {"name": name, "fields": {}, "dimensions": {}}))
                for name in source_names]

    for index, item in enumerate(unions):
        if isinstance(item, str):
            try:
                schema_name = _resolve_schema_name(item, schemas, f"union_files[{index}]")
            except ValueError:
                recovered = recover_output_label(item, index)
                if recovered is None:
                    raise
                normalized_unions.extend(recovered)
                continue
            item = dict(files_by_name.get(schema_name, {"name": schema_name, "fields": {}, "dimensions": {}}))
        elif isinstance(item, dict):
            try:
                schema_name = _resolve_schema_name(item.get("name"), schemas, f"union_files[{index}]")
            except ValueError:
                recovered = recover_output_label(item.get("name"), index)
                if recovered is None:
                    raise
                normalized_unions.extend(recovered)
                continue
            item = {**item, "name": schema_name}
        else:
            raise ValueError(f"Planner union_files[{index}] must be a filename or JSON object")
        for key in ("fields", "dimensions", "constants", "status_mapping"):
            item[key] = _as_mapping(item.get(key))
        normalized_unions.append(item)
    raw["union_files"] = normalized_unions

    for index, join in enumerate(raw["joins"]):
        for key in ("left_file", "right_file"):
            if join.get(key):
                join[key] = _resolve_schema_name(join[key], schemas, f"joins[{index}].{key}")


def _profile_for_schema(schema: dict, data_profile: dict) -> dict:
    wanted = {str(schema.get("name", "")), str(schema.get("original_name", ""))}
    return next((item for item in data_profile.get("sources", [])
                 if item.get("name") in wanted or item.get("source_name") in wanted), {})


def _resolved_profile_format(schema: dict, ambiguity_resolution: dict, issue: str) -> str:
    """Return a unique, profile-backed format decision for one source."""
    names = {str(schema.get("name", "")), str(schema.get("original_name", ""))}
    decisions = {
        str(item.get("decision", "")).casefold()
        for item in ambiguity_resolution.get("resolved_automatically", [])
        if item.get("issue") == issue
        and any(str(item.get("source", "")).startswith(f"{name}.") for name in names if name)
        and item.get("decision")
    }
    return next(iter(decisions)) if len(decisions) == 1 else ""


def _prepared_projection_mapping(mapping: dict, sources: list[dict]) -> dict:
    """Bind equivalent output aliases to the existing prepared execution slot.

    Equivalence requires the same physical binding in every Union source, not
    similar headers or labels. A separate source field remains separate.
    """
    prepared_roles = {"date", "amount", "status", "distance", "duration", "value"}
    result = dict(mapping)
    for target, runtime in mapping.items():
        if runtime in prepared_roles or not sources:
            continue
        candidates = set(prepared_roles)
        for source in sources:
            bindings = {**source.get("fields", {}), **source.get("dimensions", {})}
            physical = bindings.get(runtime)
            candidates &= {role for role in prepared_roles
                           if physical is not None and bindings.get(role) == physical}
        if len(candidates) == 1:
            result[target] = next(iter(candidates))
    return result


def _profile_semantic_fields(schema: dict, data_profile: dict, business_rules: dict | None = None) -> dict:
    business_rules = business_rules or {}
    profile = _profile_for_schema(schema, data_profile)
    basis_by_file = business_rules.get("revenue_basis_by_file", {})
    basis = str(basis_by_file.get(schema.get("original_name", ""),
                                  basis_by_file.get(schema.get("name", ""),
                                                     business_rules.get("revenue_basis", ""))))
    result = {}
    for column in profile.get("column_profiles", []):
        candidates = sorted(column.get("semantic_candidates", []),
                            key=lambda item: item.get("confidence", 0), reverse=True)
        if not candidates or float(candidates[0].get("confidence", 0)) < 0.90:
            continue
        semantic = candidates[0].get("semantic_field")
        source_column = column.get("name")
        if semantic and source_column in schema.get("columns", []):
            result.setdefault(semantic, source_column)
        if basis and semantic == basis and source_column in schema.get("columns", []):
            result["amount"] = source_column
    return result


def _planner_profile_context(data_profile: dict, resolved_business_rules: dict | None = None) -> dict:
    """Give the LLM profile evidence without sending raw row or sample values."""
    source_platforms = (resolved_business_rules or {}).get("source_platforms", {})
    sources = []
    for source in data_profile.get("sources", []):
        columns = []
        for column in source.get("column_profiles", []):
            columns.append({key: column.get(key) for key in (
                "name", "normalized_name", "datatype", "null_count", "null_ratio",
                "unique_count", "unique_ratio", "candidate_key", "semantic_candidates",
                "date_format_candidates", "date_format_candidate", "number_format_candidate",
                "number_format_evidence", "unit_candidates", "invalid_numeric_count",
                "business_role_candidates", "numeric_parse_ratio", "date_parse_ratio", "min", "max", "value_distribution",
            ) if key in column})
        sources.append({key: source.get(key) for key in (
            "name", "source_name", "columns", "row_count", "status_counts",
            "duplicate_order_id_count", "exact_duplicate_row_count", "data_grain",
            "data_grain_confidence", "data_grain_evidence",
        ) if key in source} | {"column_profiles": columns})
        source_name = str(source.get("source_name", source.get("name", "")))
        if source_name in source_platforms:
            sources[-1]["confirmed_platform"] = source_platforms[source_name]
    return {"profile_version": data_profile.get("profile_version", 1), "sources": sources}


def _infer_join_key_pairs(left_columns: list[str], right_columns: list[str]) -> list[tuple[str, str]]:
    """Find unambiguous shared identifier headers without guessing on plain `id`."""
    def normalize(column: str) -> str:
        return "".join(char.lower() for char in column if char.isalnum())

    left_by_name: dict[str, list[str]] = {}
    right_by_name: dict[str, list[str]] = {}
    for column in left_columns:
        left_by_name.setdefault(normalize(column), []).append(column)
    for column in right_columns:
        right_by_name.setdefault(normalize(column), []).append(column)

    pairs = []
    for normalized in left_by_name.keys() & right_by_name.keys():
        # A shared descriptive key such as customer_id is useful. A bare `id`,
        # UUID, or row number is too ambiguous to join automatically.
        if (len(normalized) < 5 or not normalized.endswith("id")
                or normalized in {"uuid", "guid", "rowid", "recordid"}):
            continue
        left_matches = left_by_name[normalized]
        right_matches = right_by_name[normalized]
        if len(left_matches) == 1 and len(right_matches) == 1:
            pairs.append((left_matches[0], right_matches[0]))
    return pairs


def custom_csv_plan(request: str, provider: str, schemas: list[dict], clarification: dict | None = None,
                    requirements: dict | None = None, data_profile: dict | None = None,
                    ambiguity_resolution: dict | None = None, *, _retry_feedback: dict | None = None) -> dict:
    """Use the existing one-correction budget for the complete plan boundary."""
    feedback = _retry_feedback
    attempts = 1 if provider == "mock" or feedback is not None else 2
    for index in range(attempts):
        attempt_context = {}
        try:
            return _compile_custom_csv_plan(
                request, provider, schemas, clarification, requirements, data_profile,
                ambiguity_resolution, _retry_feedback=feedback, _attempt_context=attempt_context,
            )
        except (ValueError, TypeError) as error:
            if index + 1 == attempts:
                raise
            feedback = {**attempt_context,
                        "validation_errors": attempt_context.get("validation_errors") or [str(error)]}
    raise RuntimeError("Planner correction budget exhausted")


def _compile_custom_csv_plan(request: str, provider: str, schemas: list[dict], clarification: dict | None = None,
                            requirements: dict | None = None, data_profile: dict | None = None,
                            ambiguity_resolution: dict | None = None, *, _retry_feedback: dict | None = None,
                            _attempt_context: dict | None = None) -> dict:
    attempt_context = _attempt_context if _attempt_context is not None else {}
    clarification = clarification or {}
    requirements = requirements or {}
    data_profile = data_profile or {"sources": []}
    ambiguity_resolution = ambiguity_resolution or {}
    resolved_business_rules = ambiguity_resolution.get("resolved_business_rules", {})
    revenue_mode = is_revenue_request(request, requirements)
    revenue_role_proposals = {}

    def revenue_fields_for(schema: dict, proposals: dict | None = None) -> dict:
        semantics = build_revenue_semantics(request, requirements, [schema], data_profile,
                                            clarification, resolved_business_rules,
                                            {schema["name"]: proposals or {}})
        roles = semantics["sources"][0]["roles"]
        return {field: roles[role]["column"] for field, role in (
            ("amount", "transaction_monetary_value"), ("id", "transaction_entity_key"),
            ("date", "business_event_timestamp"), ("status", "transaction_status"),
        ) if role in roles}
    context_request = request + " " + str(clarification.get("additional_context", ""))
    lower = unicodedata.normalize("NFKD", context_request.casefold().replace("đ", "d")).encode("ascii", "ignore").decode()
    requested_append = clarification.get("operation") == "append" or any(term in lower for term in
        ("append", "union", "stack", "combine", "vertically", "theo chieu doc", "noi cac dong", "gop don hang"))
    source_labels = [Path(schema.get("original_name", schema["name"])).stem.split("_")[-1].casefold()
                     for schema in schemas]
    named_sources_in_request = len(source_labels) > 1 and len(set(source_labels)) == len(source_labels) and all(
        re.search(rf"\b{re.escape(label)}\b", lower) for label in source_labels
    )
    # When the user asks for a cross-platform report and names each uploaded source,
    # treat the files as separate fact exports to append, not dimension tables to join.
    asks_to_append = requested_append or named_sources_in_request
    asks_to_join = any(term in lower for term in ("join", "merge", "ghep"))
    if "requested_operations" in requirements:
        operations = set(requirements["requested_operations"])
        asks_to_append = "union" in operations or clarification.get("operation") == "append"
        asks_to_join = "join" in operations or clarification.get("operation") == "join"
    prohibited_operations = set(requirements.get("prohibited_operations", []))
    asks_to_append = asks_to_append and "union" not in prohibited_operations
    asks_to_join = asks_to_join and "join" not in prohibited_operations
    asks_per_file = any(term in lower for term in ("tung bang", "moi bang", "theo bang", "per file", "each file", "per source", "tung nguon"))
    asks_average = any(term in lower for term in ("average", "mean", "trung binh"))
    asks_revenue = any(term in lower for term in ("revenue", "doanh thu", "fare", "amount"))
    asks_completed = any(term in lower for term in ("completed", "complete", "hoan thanh", "hoan tat", "done"))
    asks_platform_group = any(term in lower for term in ("by platform", "per platform", "theo nen tang", "theo platform"))
    requested_group_by = (
        "week" if any(term in lower for term in ("weekly", "by week", "per week", "theo tuan", "hang tuan")) else
        "month" if any(term in lower for term in ("monthly", "by month", "per month", "theo thang", "hang thang")) else
        "day" if any(term in lower for term in ("daily", "by day", "per day", "theo ngay", "hang ngay", "moi ngay")) else
        None
    )
    field_aliases = {
        "id": ("orderid", "madonhang", "receiptno", "rideid", "tripid", "id"),
        "date": ("date", "time", "createdat", "timestamp", "ngaydat", "saledate"),
        "status": ("status", "state"),
        "amount": ("amount", "amountraw", "giatridon", "paid", "revenue", "fare", "price", "cost", "total"),
        "value": ("qty", "quantity", "stock", "units", "delta"),
        "distance": ("distance", "mileage", "km"),
        "duration": ("duration", "minutes", "runtime"),
    }
    group_clause = re.split(r"\b(?:by|per|theo)\b", lower)[-1].strip()
    group_match = re.match(r"([a-z0-9_]+)", group_clause)
    requested_group_header = ("".join(char.lower() for char in group_match.group(1) if char.isalnum())
                              if group_match else "")

    if provider == "mock":
        files = []
        for schema in schemas:
            columns = schema.get("columns", [])
            fields = {name: match for name, aliases in field_aliases.items()
                      if (match := _guess_column(columns, aliases))}
            if revenue_mode:
                fields = {key: value for key, value in fields.items() if key not in {"id", "date", "status", "amount"}}
                fields.update(revenue_fields_for(schema))
            else:
                fields.update(_profile_semantic_fields(schema, data_profile, resolved_business_rules))
            dimensions = {}
            dimension_aliases = {"country": ("country",), "city": ("city",), "segment": ("segment",)}
            if requested_group_header == "category":
                dimension_aliases["category"] = ("category",)
            elif requested_group_header == "product":
                dimension_aliases["product"] = ("product_name", "product")
            for name, aliases in dimension_aliases.items():
                if any(alias in requested_group_header for alias in aliases):
                    match = _guess_column(columns, aliases)
                    if match:
                        dimensions[name] = match
            files.append({"name": schema["name"], "fields": fields, "dimensions": dimensions})

        scores = {item["name"]: sum(field in item["fields"] for field in ("date", "status", "amount", "id", "value"))
                  for item in files}
        base_file = max(scores, key=scores.get) if scores else None
        joins = []
        if asks_to_join and base_file:
            base_columns = next((set(item.get("columns", [])) for item in schemas if item["name"] == base_file), set())
            for schema in schemas:
                if schema["name"] == base_file:
                    continue
                right_columns = set(schema.get("columns", []))
                # Honor explicit qualified pairs such as orders.id = customers.customer_id.
                explicit_pair = None
                for match in re.finditer(r"([\w .-]+)\.([\w -]+)\s*(?:=|\bwith\b|\bto\b)\s*([\w .-]+)\.([\w -]+)", request, re.I):
                    left_file, left_key, right_file, right_key = (part.strip() for part in match.groups())
                    if (left_file.casefold() == base_file.casefold() and left_key in base_columns
                            and right_file.casefold() == schema["name"].casefold() and right_key in right_columns):
                        explicit_pair = (left_key, right_key)
                        break
                    if (right_file.casefold() == base_file.casefold() and right_key in base_columns
                            and left_file.casefold() == schema["name"].casefold() and left_key in right_columns):
                        explicit_pair = (right_key, left_key)
                        break
                shared = base_columns.intersection(right_columns)
                key = next((column for column in shared if column.casefold() in lower), None)
                if key is None:
                    key = next((column for column in shared if "customer" in column.casefold() and "id" in column.casefold()), None)
                if explicit_pair or key:
                    left_key, right_key = explicit_pair or (key, key)
                    joins.append({"left_file": base_file, "left_key": left_key, "right_file": schema["name"],
                                  "right_key": right_key, "how": "left"})

        metrics = ["record_count"]
        if any(term in lower for term in ("completed", "delivered", "shipped", "hoan thanh", "hoan tat")):
            metrics.append("completed_count")
        if any(term in lower for term in ("cancelled", "canceled", "returned", "huy")):
            metrics.append("cancelled_count")
        if any(term in lower for term in ("revenue", "doanh thu", "fare", "amount", "total", "tong")):
            metrics.extend(["total_amount", "average_amount"])
        elif any(term in lower for term in ("sum", "total", "tong", "tinh tong", "sum of")):
            if any("value" in item["fields"] for item in files):
                metrics.append("sum_value")
        if any(term in lower for term in ("distance", "quang duong")):
            metrics.append("average_distance")
        # "Thoi gian" commonly means a reporting time period (for example,
        # revenue by time), not a duration measure. Only add average_duration
        # when the request explicitly refers to elapsed time.
        if any(term in lower for term in (
            "duration", "thoi luong", "thoi gian xu ly", "thoi gian giao hang",
            "so phut", "minutes", "average time",
        )):
            metrics.append("average_duration")
        group_by = ("week" if any(term in lower for term in ("week", "tuan")) else
                    "month" if any(term in lower for term in ("month", "thang")) else
                    "day" if any(term in lower for term in ("day", "daily", "date", "ngay")) else "all")
        group_dimensions = [requested_group_header] if requested_group_header in {
            "country", "city", "segment", "product", "category"
        } else []
        raw = {
            "base_file": base_file, "files": files, "joins": joins, "group_by": group_by,
            "group_dimensions": group_dimensions, "metrics": metrics,
            "completed_status_values": ["delivered", "shipped", "completed", "done"],
            "cancelled_status_values": ["cancelled", "canceled"],
            "include_status_values": (
                ["delivered", "shipped", "completed", "done"] if asks_completed else
                [value for value in ("delivered", "shipped", "completed", "done") if value in lower]
            ) if any(term in lower for term in ("only", "keep", "retain", "chi giu")) else [],
            "exclude_status_values": ["cancelled", "canceled"]
                if any(term in lower for term in ("exclude", "excluding", "loai")) else [],
        }
    else:
        prompt = f"""Uploaded CSV files and headers:
{json.dumps(schemas, ensure_ascii=False)}
User request:
{request}
Requirement contract:
{json.dumps(requirements, ensure_ascii=False)}
Data profile:
{json.dumps(_planner_profile_context(data_profile, resolved_business_rules), ensure_ascii=False)}
Ambiguity resolution and confirmed business rules:
{json.dumps(ambiguity_resolution, ensure_ascii=False)}
Confirmed clarification answers:
{json.dumps(clarification, ensure_ascii=False)}"""
        if _retry_feedback:
            prompt += "\nPrevious plan and generic validation errors:\n" + json.dumps(_retry_feedback, ensure_ascii=False)
            prompt += ("\nReturn a complete corrected plan preserving every requirement. Resolve bindings with current "
                       "semantic/schema evidence, not aliases or guessed values. Unsupported derivations cannot "
                       "substitute for existing parsers/constants. Keep unresolved requirements explicit. Return JSON only.")
            if any(isinstance(item, dict) and item.get("type") == "aggregate_formula"
                   for item in requirements.get("requested_transformations", [])):
                prompt += (" A structured aggregate_formula in the requirement contract is an explicit executable "
                           "calculation contract: preserve its named output, operand measures, arithmetic, and grouping "
                           "scopes in output_specs using derived_metrics and window_specs. Do not leave that output in "
                           "unsupported_requirements when those existing SQL slots represent it.")
        response = generate_text(provider, prompt, system_prompt=load_system_prompt("prompt_plan"))
        attempt_context["previous_model_response"] = response
        raw = read_json(response)
        if not isinstance(raw, dict):
            raise ValueError("Planner must return a JSON object")
        attempt_context["previous_model_plan"] = deepcopy(raw)
        _normalize_model_sources(raw, schemas, request, requirements, clarification)
        valid_names = {schema["name"] for schema in schemas}
        has_mapped_fact_date = any(
            item.get("name") in valid_names
            and isinstance(item.get("fields"), dict)
            and item.get("fields", {}).get("date") in next(
                (schema.get("columns", []) for schema in schemas if schema["name"] == item.get("name")), []
            )
            for item in (raw.get("files") if isinstance(raw.get("files"), list) else [])
            if isinstance(item, dict)
        )
        if raw.get("base_file") not in valid_names and not has_mapped_fact_date:
            # Preserve real Planner/Coder use while repairing incomplete model JSON
            # from the already validated local schema and request.
            fallback = custom_csv_plan(request, "mock", schemas, clarification, requirements,
                                       data_profile, ambiguity_resolution)
            raw = {
                "base_file": fallback["base_file"], "files": fallback["files"],
                "joins": fallback["joins"], "union_files": fallback["union_files"],
                "group_by": fallback["group_by"], "group_dimensions": fallback["group_dimensions"],
                "metrics": fallback["metrics"],
                "completed_status_values": fallback["completed_status_values"],
                "cancelled_status_values": fallback["cancelled_status_values"],
                "include_status_values": fallback["include_status_values"],
                "exclude_status_values": fallback["exclude_status_values"],
                "output_specs": fallback.get("output_specs", []),
                "unsupported_requirements": fallback.get("unsupported_requirements", []),
            }

    raw_files = raw.get("files") if isinstance(raw.get("files"), list) else []
    raw["files"] = raw_files
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        schema = next((candidate for candidate in schemas if candidate.get("name") == item.get("name")), None)
        if not schema:
            continue
        fields = _as_mapping(item.get("fields"))
        if revenue_mode:
            proposals = _as_mapping(item.get("semantic_roles"))
            revenue_role_proposals[schema["name"]] = proposals
            fields = {key: value for key, value in fields.items() if key not in {"id", "date", "status", "amount"}}
            fields.update(revenue_fields_for(schema, proposals))
        else:
            for semantic, column in _profile_semantic_fields(schema, data_profile, resolved_business_rules).items():
                fields.setdefault(semantic, column)
        item["fields"] = fields
        constants = dict(_as_mapping(item.get("constants")))
        platform_column = next((column for column in schema.get("columns", [])
                                if "".join(char.casefold() for char in column if char.isalnum()) == "platform"), None)
        if platform_column:
            fields["platform"] = platform_column
            constants.pop("platform", None)
        else:
            fields.pop("platform", None)
            source_name = str(schema.get("original_name", schema["name"]))
            platform_label = resolved_business_rules.get("source_platforms", {}).get(
                source_name, resolved_business_rules.get("source_platforms", {}).get(schema["name"])
            )
            if platform_label:
                constants["platform"] = str(platform_label)
            else:
                constants.pop("platform", None)
        item["constants"] = constants
        distance_header = "".join(char.casefold() for char in str(fields.get("distance", "")) if char.isalnum())
        if distance_header in {"distancemeter", "distancemeters"}:
            item["distance_factor"] = 0.001

    # Never let an LLM or legacy default silently reinterpret `returned` as cancelled.
    returned_policy = str(resolved_business_rules.get("returned_as_cancelled", "")).casefold().strip()
    raw_cancelled_values = raw.get("cancelled_status_values", [])
    if not isinstance(raw_cancelled_values, list):
        raw_cancelled_values = []
    cancelled_values = [str(value).casefold().strip() for value in raw_cancelled_values]
    cancelled_values = [value for value in cancelled_values if value != "returned"]
    if returned_policy in {"yes", "true", "1"}:
        cancelled_values.append("returned")
    raw["cancelled_status_values"] = list(dict.fromkeys(cancelled_values))
    raw_excluded_values = raw.get("exclude_status_values", [])
    if not isinstance(raw_excluded_values, list):
        raw_excluded_values = []
    excluded = [str(value).casefold().strip() for value in raw_excluded_values]
    excluded = [value for value in excluded if value != "returned"]
    if returned_policy in {"yes", "true", "1"}:
        excluded.append("returned")
    raw["exclude_status_values"] = list(dict.fromkeys(excluded))

    for item in raw.get("union_files", []):
        schema = next((candidate for candidate in schemas if candidate.get("name") == item.get("name")), None)
        if schema:
            fields = _as_mapping(item.get("fields"))
            if revenue_mode:
                fields = {key: value for key, value in fields.items() if key not in {"id", "date", "status", "amount"}}
                fields.update(revenue_fields_for(schema, revenue_role_proposals.get(schema["name"])))
            else:
                for semantic, column in _profile_semantic_fields(schema, data_profile, resolved_business_rules).items():
                    fields.setdefault(semantic, column)
            item["fields"] = fields
            constants = dict(_as_mapping(item.get("constants")))
            platform_column = next((column for column in schema.get("columns", [])
                                    if "".join(char.casefold() for char in column if char.isalnum()) == "platform"), None)
            if platform_column:
                fields["platform"] = platform_column
                constants.pop("platform", None)
            else:
                fields.pop("platform", None)
                source_name = str(schema.get("original_name", schema["name"]))
                platform_label = resolved_business_rules.get("source_platforms", {}).get(
                    source_name, resolved_business_rules.get("source_platforms", {}).get(schema["name"])
                )
                if platform_label:
                    constants["platform"] = str(platform_label)
                else:
                    constants.pop("platform", None)
            item["constants"] = constants
            distance_header = "".join(char.casefold() for char in str(fields.get("distance", "")) if char.isalnum())
            item["distance_factor"] = 0.001 if distance_header in {"distancemeter", "distancemeters"} else 1

    for clarification_key, planner_key in (
        ("completed_status_values_by_file", "completed_status_values"),
        ("cancelled_status_values_by_file", "cancelled_status_values"),
    ):
        by_file = clarification.get(clarification_key) or {}
        if isinstance(by_file, dict) and by_file:
            values = []
            for raw_values in by_file.values():
                if str(raw_values).casefold().strip() == "none":
                    continue
                values.extend(part.strip().casefold() for part in re.split(r"[,;|]", str(raw_values)) if part.strip())
            raw[planner_key] = list(dict.fromkeys(values))
    if requirements.get("requested_filters"):
        raw["filters"] = requirements["requested_filters"]

    # Preserve explicit sums of non-monetary quantities. For a physical
    # quantity header named in the request, map it into the generic `value`
    # slot and keep the sum visible in the generated SQL contract.
    requests_quantity_sum = (
        any(term in lower for term in ("sum", "total", "tong", "tinh tong"))
        and not any(term in lower for term in ("revenue", "doanh thu", "fare", "amount", "price"))
    )
    if requests_quantity_sum:
        request_compact = "".join(char.lower() for char in lower if char.isalnum())

        def normalized_header(value: str) -> str:
            return "".join(char.lower() for char in value if char.isalnum())

        quantity_headers = [
            (schema, column)
            for schema in schemas
            for column in schema.get("columns", [])
            if normalized_header(column) in request_compact
            and any(token in normalized_header(column)
                    for token in ("qty", "quantity", "stock", "units", "delta"))
        ]
        named_schema = next((schema for schema in schemas if normalized_header(
            Path(schema.get("original_name", schema["name"])).stem
        ) in request_compact), None)
        candidates = [pair for pair in quantity_headers if not named_schema or pair[0] == named_schema]
        if len(candidates) == 1:
            measure_schema, measure_column = candidates[0]
            raw["base_file"] = measure_schema["name"]
            raw["metrics"] = ["sum_value"]
            raw_files = raw.get("files", [])
            if not isinstance(raw_files, list):
                raw_files = []
            file_plan = next((item for item in raw_files
                              if isinstance(item, dict) and item.get("name") == measure_schema["name"]), None)
            if file_plan is None:
                file_plan = {"name": measure_schema["name"], "fields": {}, "dimensions": {}}
                raw_files.append(file_plan)
            file_plan.setdefault("fields", {})["value"] = measure_column
            mentioned_dimensions = [
                column for column in measure_schema.get("columns", [])
                if column != measure_column
                and normalized_header(column) == requested_group_header
                and not any(token in normalized_header(column)
                            for token in ("id", "date", "time", "status", "qty", "quantity", "stock", "units", "delta"))
            ]
            if mentioned_dimensions:
                raw["group_dimensions"] = list(dict.fromkeys(
                    [*raw.get("group_dimensions", []), *mentioned_dimensions]
                ))
            raw["files"] = raw_files

    if asks_to_join and len(schemas) > 1 and not asks_to_append:
        base_name_for_join = raw.get("base_file")
        base_schema_for_join = next((schema for schema in schemas if schema["name"] == base_name_for_join), None)
        if base_schema_for_join:
            request_compact = "".join(char.lower() for char in request if char.isalnum())
            for schema in schemas:
                if schema["name"] == base_name_for_join:
                    continue
                right_columns = set(schema.get("columns", []))
                shared_explicit = [column for column in base_schema_for_join.get("columns", [])
                                   if column in right_columns and
                                   "".join(char.lower() for char in column if char.isalnum()) in request_compact]
                if len(shared_explicit) < 2:
                    continue
                raw["joins"] = [join for join in raw.get("joins", [])
                                if join.get("right_file") != schema["name"]]
                raw.setdefault("joins", []).append({
                    "left_file": base_name_for_join, "left_key": shared_explicit[0],
                    "left_keys": shared_explicit, "right_file": schema["name"],
                    "right_key": shared_explicit[0], "right_keys": shared_explicit, "how": "left",
                })
        supplied_join_keys = clarification.get("join_keys") or {}
        for schema in schemas:
            if schema["name"] == base_name_for_join:
                continue
            original_name = schema.get("original_name", schema["name"])
            supplied = (supplied_join_keys.get(original_name) or supplied_join_keys.get(schema["name"])) \
                if isinstance(supplied_join_keys, dict) else supplied_join_keys
            if not supplied:
                continue
            left_columns = base_schema_for_join.get("columns", [])
            right_columns = schema.get("columns", [])
            left_by_key = {"".join(char.lower() for char in column if char.isalnum()): column
                           for column in left_columns}
            right_by_key = {"".join(char.lower() for char in column if char.isalnum()): column
                            for column in right_columns}
            requested_keys = [part.strip() for part in re.split(r",|;|\+|\band\b|\bvà\b", str(supplied), flags=re.I)
                              if part.strip()]
            normalized_keys = ["".join(char.lower() for char in key if char.isalnum()) for key in requested_keys]
            if normalized_keys and all(key in left_by_key and key in right_by_key for key in normalized_keys):
                left_keys = [left_by_key[key] for key in normalized_keys]
                right_keys = [right_by_key[key] for key in normalized_keys]
                raw["joins"] = [join for join in raw.get("joins", [])
                                if join.get("right_file") != schema["name"]]
                raw.setdefault("joins", []).append({
                    "left_file": base_name_for_join, "left_key": left_keys[0], "left_keys": left_keys,
                    "right_file": schema["name"], "right_key": right_keys[0],
                    "right_keys": right_keys, "how": "left",
                })

    if asks_to_append and len(schemas) > 1:
        def normalized_name(value: str) -> str:
            return "".join(char.lower() for char in value if char.isalnum())

        semantic_aliases = {
            "id": {"id", "tripcode", "bookingid", "orderid", "madonhang", "receiptno", "rideid", "tripid"},
            "date": {"orderedat", "pickuptime", "date", "datetime", "createdat", "timestamp", "orderdate", "ngaydat", "saledate"},
            "status": {"tripstatus", "status", "orderstatus"},
            "amount": {"price", "farevnd", "fare", "amount", "amountraw", "giatridon", "paid", "totalamount"},
            "customer_name": {"customername", "buyername", "clientname", "purchasername"},
            "distance": {"distancemeter", "distancekm", "distance", "mileage"},
            "duration": {"duration", "durationminutes", "minutes"},
        }
        mapped_files = []
        union_files = []
        existing_files = {item["name"]: item for item in raw.get("files", [])}
        existing_unions = {item["name"]: item for item in raw.get("union_files", [])}
        selected_names = {_resolve_schema_name(name, schemas, "requested source")
                          for name in requirements.get("requested_sources", [])}
        union_schemas = [schema for schema in schemas if not selected_names or schema["name"] in selected_names]
        for schema in union_schemas:
            columns = schema.get("columns", [])
            by_normalized = {normalized_name(column): column for column in columns}
            existing = existing_files.get(schema["name"], {})
            existing_union = existing_unions.get(schema["name"], {})
            fields = {**existing.get("fields", {}), **existing.get("dimensions", {}),
                      **existing_union.get("fields", {}), **existing_union.get("dimensions", {})}
            if provider == "mock":
                for semantic, aliases in semantic_aliases.items():
                    match = next((by_normalized[alias] for alias in aliases if alias in by_normalized), None)
                    if match:
                        fields.setdefault(semantic, match)
            if revenue_mode:
                fields = {key: value for key, value in fields.items() if key not in {"id", "date", "status", "amount"}}
                fields.update(revenue_fields_for(schema, revenue_role_proposals.get(schema["name"])))
            else:
                fields.update(_profile_semantic_fields(schema, data_profile, resolved_business_rules))
            source_name = schema.get("original_name", schema["name"])
            source_label = str(resolved_business_rules.get("source_platforms", {}).get(
                source_name, resolved_business_rules.get("source_platforms", {}).get(schema["name"], "")
            )).strip()
            constants = {**existing.get("constants", {}), **existing_union.get("constants", {})}
            if source_label and ("platform" in lower or "nen tang" in lower or named_sources_in_request or asks_per_file):
                constants["platform"] = source_label
            # Union field/dimension bindings must point to uploaded headers.
            # User-confirmed labels belong in constants, never in physical maps.
            fields = {key: value for key, value in fields.items() if isinstance(value, str) and value in columns}
            dimensions = {key: value for key, value in _as_mapping(existing_union.get("dimensions")).items()
                          if isinstance(value, str) and value in columns}
            mapped_files.append({**existing, "name": schema["name"], "fields": fields, "dimensions": {}, "constants": constants})
            distance_column = fields.get("distance", "")
            factor = 0.001 if normalized_name(distance_column) in {"distancemeter", "distancemeters"} else 1
            union_files.append({**existing_union, "name": schema["name"], "fields": fields,
                                "dimensions": dimensions, "constants": constants,
                                "distance_factor": factor})
        raw["base_file"] = union_schemas[0]["name"]
        raw["files"] = mapped_files
        raw["joins"] = []
        raw["union_files"] = union_files
        if "platform" in lower or "nen tang" in lower or named_sources_in_request or asks_per_file:
            existing_dimensions = [dimension for dimension in raw.get("group_dimensions", [])
                                   if dimension not in {"source", "file", "channel", "platform"}]
            raw["group_dimensions"] = list(dict.fromkeys([*existing_dimensions, "platform"]))
        if asks_average and asks_revenue:
            raw["metrics"] = ["average_amount"]
        if asks_completed:
            raw["include_status_values"] = ["done", "completed", "delivered", "shipped"]
        if requested_group_by:
            raw["group_by"] = requested_group_by
        elif asks_platform_group or named_sources_in_request or asks_per_file:
            raw["group_by"] = "all"

    # Model plans sometimes name a physical header as a group but omit its
    # dimension mapping. Resolve that exact header locally when it exists.
    request_compact = "".join(char.lower() for char in lower if char.isalnum())
    if any(marker in lower for marker in (" by ", " theo ", " per ", "group by", "nhom theo")):
        already_named = set()
        for item in raw.get("files", []):
            if not isinstance(item, dict):
                continue
            for dimension, column in _as_mapping(item.get("dimensions")).items():
                already_named.add("".join(char.lower() for char in str(dimension) if char.isalnum()))
                already_named.add("".join(char.lower() for char in str(column) if char.isalnum()))
        mentioned_dimensions = []
        for schema in schemas:
            for column in schema.get("columns", []):
                normalized_column = "".join(char.lower() for char in column if char.isalnum())
                if (normalized_column == requested_group_header and normalized_column not in already_named
                        and not any(token in normalized_column for token in
                                    ("id", "date", "time", "status", "amount", "price", "qty", "quantity", "stock", "units", "delta"))):
                    mentioned_dimensions.append(column)
        if mentioned_dimensions:
            raw["group_dimensions"] = list(dict.fromkeys(
                [*raw.get("group_dimensions", []), *mentioned_dimensions]
            ))
    raw_group_dimensions = raw.get("group_dimensions", [])
    if isinstance(raw_group_dimensions, list):
        raw_files = raw.get("files", [])
        if not isinstance(raw_files, list):
            raw_files = []
            raw["files"] = raw_files
        for requested_dimension in raw_group_dimensions:
            normalized_dimension = "".join(char.lower() for char in str(requested_dimension) if char.isalnum())
            already_mapped = any(
                normalized_dimension == "".join(char.lower() for char in str(name) if char.isalnum())
                or normalized_dimension == "".join(char.lower() for char in str(column) if char.isalnum())
                for item in raw_files if isinstance(item, dict)
                for name, column in _as_mapping(item.get("dimensions")).items()
                if column in next((schema.get("columns", []) for schema in schemas
                                   if schema["name"] == item.get("name")), [])
                and (normalized_dimension == "".join(char.lower() for char in str(name) if char.isalnum())
                     or normalized_dimension == "".join(char.lower() for char in str(column) if char.isalnum()))
            )
            if already_mapped:
                continue
            owner = next((schema for schema in schemas if any(
                normalized_dimension == "".join(char.lower() for char in str(column) if char.isalnum())
                for column in schema.get("columns", []))), None)
            if owner is None:
                continue
            item = next((entry for entry in raw_files if isinstance(entry, dict)
                         and entry.get("name") == owner["name"]), None)
            if item is None:
                item = {"name": owner["name"], "fields": {}, "dimensions": {}}
                raw_files.append(item)
            item.setdefault("dimensions", {})[str(requested_dimension)] = next(
                column for column in owner.get("columns", [])
                if normalized_dimension == "".join(char.lower() for char in str(column) if char.isalnum())
            )

    if asks_revenue and any(term in lower for term in ("tong", "total", "sum")) and not asks_average:
        raw["metrics"] = ["total_amount"]

    if asks_completed and clarification.get("status_scope") != "all":
        raw["include_status_values"] = ["completed", "delivered", "done", "shipped"]

    if clarification.get("status_scope") == "completed":
        raw["include_status_values"] = ["completed", "delivered", "done", "shipped"]
    elif clarification.get("status_scope") == "all":
        raw["include_status_values"] = []
        raw["exclude_status_values"] = []

    # An explicit time grain in the user's request takes precedence over model defaults.
    # If none was requested, preserve the existing planner behavior for older requests.
    if requested_group_by:
        raw["group_by"] = requested_group_by

    if requirements.get("requested_groupings"):
        requested_grouping = requirements["requested_groupings"][0]
        requested_grain = requested_grouping.get("time_grain")
        if requested_grain in {"day", "week", "month", "all"}:
            raw["group_by"] = requested_grain
        requested_dimensions = requested_grouping.get("dimensions", [])
        if requested_dimensions:
            raw["group_dimensions"] = list(dict.fromkeys(requested_dimensions))
    requested_metric_names = requirements.get("requested_metrics", [])
    if requested_metric_names:
        raw["metrics"] = list(dict.fromkeys(canonical_metric(metric) for metric in requested_metric_names))

    request_interpretation = {
        "operation": "append" if asks_to_append and len(schemas) > 1 else ("join" if raw.get("joins") else "single_source"),
        "metric": "average_amount" if asks_average and asks_revenue else None,
        "filter": {"field": "status", "values": ["done", "completed", "delivered", "shipped"]}
                  if asks_completed or clarification.get("status_scope") == "completed" else None,
        "group_by": requested_group_by or raw.get("group_by", "all"),
        "explicit_group_by": requested_group_by,
        "group_dimensions": ["platform"] if asks_platform_group or named_sources_in_request or asks_per_file else [],
    }

    valid_schemas = {schema["name"]: set(schema.get("columns", [])) for schema in schemas}
    raw_files = raw.get("files", [])
    if not isinstance(raw_files, list):
        raw_files = []
    raw["files"] = raw_files
    raw_output_specs_for_metrics = raw.get("output_specs", [])
    metric_alias_targets: dict[str, set[str]] = {}
    for spec in raw_output_specs_for_metrics if isinstance(raw_output_specs_for_metrics, list) else []:
        if not isinstance(spec, dict):
            continue
        for label, runtime in _as_mapping(spec.get("column_mapping")).items():
            metric = canonical_metric(runtime)
            if metric in CUSTOM_METRICS:
                metric_alias_targets.setdefault(_semantic_label_key(label), set()).add(metric)
    output_metric_aliases = {label: next(iter(targets)) for label, targets in metric_alias_targets.items()
                             if label and len(targets) == 1}

    def resolve_output_metric(value: str) -> str | None:
        canonical = canonical_metric(value)
        return canonical if canonical in CUSTOM_METRICS else output_metric_aliases.get(_semantic_label_key(value))

    derived_metric_names = {
        str(item.get("name", ""))
        for spec in raw_output_specs_for_metrics if isinstance(spec, dict)
        for item in spec.get("derived_metrics", []) if isinstance(item, dict) and item.get("name")
    }
    planner_unsupported = [item for item in raw.get("unsupported_requirements", [])
                           if not any(re.search(rf"\b{re.escape(name)}\b", str(item), re.I)
                                      for name in derived_metric_names)
                           and _semantic_label_key(item) not in output_metric_aliases]
    base_file = raw.get("base_file")
    if base_file not in valid_schemas:
        base_file = next((item.get("name") for item in raw_files if isinstance(item, dict)
                          and item.get("name") in valid_schemas
                          and _as_mapping(item.get("fields")).get("date") in valid_schemas[item.get("name")]), None)
    if base_file not in valid_schemas:
        raise ValueError("Planner could not identify a fact CSV with the requested rows")

    group_by_value = requested_group_by or raw.get("group_by", "all")
    base_schema = next(schema for schema in schemas if schema["name"] == base_file)
    fact_plan = next((item for item in raw_files
                      if isinstance(item, dict) and item.get("name") == base_file), None)
    if fact_plan is None:
        fact_plan = {"name": base_file, "fields": {}, "dimensions": {}}
        raw_files.append(fact_plan)
    fact_fields = dict(_as_mapping(fact_plan.get("fields")))
    if not revenue_mode and group_by_value in {"day", "week", "month"} and fact_fields.get("date") not in valid_schemas[base_file]:
        date_column = _guess_column(base_schema.get("columns", []), field_aliases["date"])
        if date_column:
            fact_fields["date"] = date_column
    order_id_columns = [column for column in base_schema.get("columns", [])
                        if "".join(char.lower() for char in column if char.isalnum())
                        in {"id", "orderid", "madonhang", "receiptno", "rideid", "tripid"}]
    if not revenue_mode and len(order_id_columns) == 1:
        fact_fields["id"] = order_id_columns[0]
    fact_plan["fields"] = fact_fields

    planned_files = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in valid_schemas:
            continue
        columns = valid_schemas[name]
        fields = {key: value for key, value in _as_mapping(item.get("fields")).items() if value in columns}
        dimensions = {key: value for key, value in _as_mapping(item.get("dimensions")).items() if value in columns}
        planned_files.append({"name": name,
                              "original_name": next((schema.get("original_name", name) for schema in schemas
                                                     if schema["name"] == name), name),
                              "fields": fields, "dimensions": dimensions,
                              "constants": item.get("constants", {}),
                              "constant_provenance": item.get("constant_provenance", {})})
    planned_by_name = {item["name"]: item for item in planned_files}
    if base_file not in planned_by_name:
        raise ValueError("Planner did not map the selected fact CSV")

    joins = []
    for item in raw.get("joins", []):
        left_file, right_file = item.get("left_file"), item.get("right_file")
        left_keys = item.get("left_keys") or [item.get("left_key")]
        right_keys = item.get("right_keys") or [item.get("right_key")]
        how = item.get("how", "left")
        if (left_file not in valid_schemas or right_file not in valid_schemas or left_file == right_file
                or not left_keys or len(left_keys) != len(right_keys)
                or any(key not in valid_schemas[left_file] for key in left_keys)
                or any(key not in valid_schemas[right_file] for key in right_keys)
                or how not in {"left", "inner"}):
            raise ValueError(f"Planner returned an invalid join: {item}")
        if left_file != base_file:
            raise ValueError("Each dimension CSV must join directly to the fact CSV")
        joins.append({"left_file": left_file, "left_key": left_keys[0], "left_keys": left_keys,
                      "right_file": right_file, "right_key": right_keys[0], "right_keys": right_keys,
                      "how": how})
    if len(schemas) > 1 and not joins and not raw.get("union_files"):
        base_schema = next(schema for schema in schemas if schema["name"] == base_file)
        inferred_joins = []
        for schema in schemas:
            if schema["name"] == base_file:
                continue
            key_pairs = _infer_join_key_pairs(base_schema.get("columns", []), schema.get("columns", []))
            if len(key_pairs) == 1:
                left_key, right_key = key_pairs[0]
                inferred_joins.append({
                    "left_file": base_file,
                    "left_key": left_key,
                    "right_file": schema["name"],
                    "right_key": right_key,
                    "how": "left",
                })
            else:
                inferred_joins = []
                break
        joins = inferred_joins
    asks_to_join = any(term in lower for term in ("join", "merge", "ghep"))
    if len(schemas) > 1 and not joins and not raw.get("union_files") and (asks_to_join or raw.get("group_dimensions")):
        planner_unsupported.append(
            "Requested multi-source operation has no unambiguous schema-backed join key; "
            "specify the exact key columns or confirm a UNION."
        )

    declared_outputs = requirements.get("requested_outputs", [])
    rows_only = bool(declared_outputs) and all(output.get("kind") == "rows" for output in declared_outputs)
    raw_metrics = ([] if rows_only else raw.get("metrics", [])) if isinstance(raw.get("metrics", []), list) else []
    unsupported_metrics = [metric for metric in raw_metrics
                           if resolve_output_metric(metric) is None
                           and str(metric) not in derived_metric_names]
    planner_unsupported.extend(unsupported_metrics)
    metrics = list(dict.fromkeys(resolved for metric in raw_metrics
                                 if (resolved := resolve_output_metric(metric)) is not None))
    requested_runtime_metrics = {
        resolved for metric in requirements.get("requested_metrics", [])
        if (resolved := resolve_output_metric(metric)) is not None
    }
    if not metrics and not planner_unsupported and not rows_only:
        metrics = ["record_count"]
    if any(metric in metrics for metric in ("total_amount", "average_amount")):
        currency_by_file = clarification.get("currency_by_file") or {}
        target_currency = str(clarification.get("target_currency", "")).upper()
        if not re.fullmatch(r"[A-Z]{3}", target_currency):
            raise ValueError("Output currency must be clarified before calculating revenue")
        for schema in schemas:
            original_name = schema.get("original_name", schema["name"])
            if not any(term in "".join(char.lower() for char in column if char.isalnum())
                       for column in schema.get("columns", [])
                       for term in ("amount", "price", "revenue", "fare", "giatridon", "paid")):
                continue
            if _currency_column(schema):
                continue
            source_currency = str(currency_by_file.get(original_name, "")).upper()
            if not re.fullmatch(r"[A-Z]{3}", source_currency):
                raise ValueError(f"Currency must be clarified for {original_name}")
            if source_currency != target_currency:
                try:
                    rate_text = str((clarification.get("conversion_rates") or {}).get(original_name, 0)).strip()
                    rate = float(rate_text) if not re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", rate_text) else 0
                except (TypeError, ValueError):
                    rate = 0
                if not 0 < rate < float("inf"):
                    raise ValueError(f"A positive conversion rate is required for {original_name}")
    group_by = raw.get("group_by", "all")
    if rows_only:
        group_by = "all"
    if group_by not in {"day", "week", "month", "all"}:
        group_by = "all"
    base_fields = planned_by_name[base_file]["fields"]
    base_distance_header = "".join(char.casefold() for char in str(base_fields.get("distance", "")) if char.isalnum())
    base_distance_factor = 0.001 if base_distance_header in {"distancemeter", "distancemeters"} else 1
    base_schema = next(schema for schema in schemas if schema["name"] == base_file)
    base_profile = _profile_for_schema(base_schema, data_profile)
    row_identity_headers = {"".join(char.lower() for char in column if char.isalnum())
                           for column in base_schema.get("columns", [])
                           if any(token in "".join(char.lower() for char in column if char.isalnum())
                                  for token in ("itemid", "lineid", "detailid", "productid", "productsku", "variantid"))}
    id_header = "".join(char.lower() for char in str(base_fields.get("id", "")) if char.isalnum())
    asks_to_deduplicate = any(token in lower for token in
                              ("duplicate", "duplicates", "deduplicate", "dedup", "trung lap", "loai trung"))
    if "requested_operations" in requirements:
        asks_to_deduplicate = "deduplicate" in requirements["requested_operations"]
    asks_to_deduplicate = asks_to_deduplicate and "deduplicate" not in prohibited_operations
    grains = [str(_profile_for_schema(schema, data_profile).get("data_grain", "unknown")) for schema in schemas]
    dedup_policy = str(resolved_business_rules.get("dedup_policy", ""))
    grain_allows_order_dedup = bool(grains) and all(grain == "order_level" for grain in grains)
    grain_allows_order_dedup = grain_allows_order_dedup or dedup_policy in {"keep_first", "keep_latest"}
    if "item_level" in grains or "event_level" in grains or row_identity_headers:
        grain_allows_order_dedup = False
    deduplicate_ids = (asks_to_deduplicate and grain_allows_order_dedup
                       and id_header in {"id", "orderid", "madonhang", "receiptno", "rideid", "tripid"})
    planned_derived_fields = [item for item in raw.get("derived_fields", []) if isinstance(item, dict)]
    constant_fields = set.intersection(*(
        set(item.get("constants", {})) for item in raw.get("union_files", []) if isinstance(item, dict)
    )) if raw.get("union_files") else set()
    # Per-source constants already materialize a shared union dimension; a
    # model-added identity derivation has no row-level input and is redundant.
    planned_derived_fields = [item for item in planned_derived_fields
                              if item.get("name") not in constant_fields]
    requests_duration = "average_duration" in metrics or any(
        term in lower for term in ("duration", "thoi luong", "thoi gian chuyen", "average time")
    )
    can_derive_duration = (
        all({"start_time", "end_time"}.issubset(item.get("fields", {})) for item in raw.get("union_files", []))
        if raw.get("union_files") else {"start_time", "end_time"}.issubset(base_fields)
    )
    if requests_duration and can_derive_duration and not any(item.get("name") == "duration" for item in planned_derived_fields):
        planned_derived_fields.append({
            "name": "duration", "operation": "datetime_difference",
            "start_field": "start_time", "end_field": "end_time", "unit": "second",
            "deterministic": True,
            "inputs": ["start_time", "end_time"],
            "provenance": "profiled start_time and end_time columns; default duration unit is seconds",
        })
    required_fields = {
        "completed_count": "status", "cancelled_count": "status", "total_amount": "amount",
        "distinct_order_count": "id", "average_amount": "amount", "min_amount": "amount",
        "max_amount": "amount", "median_amount": "amount", "completion_rate": "status",
        "cancellation_rate": "status", "average_distance": "distance", "average_duration": "duration",
        "sum_value": "value", "customer_lifetime_value": "amount",
        "running_revenue": "amount", "customer_rank": "amount",
    }
    available_metric_fields = set(base_fields) | {item.get("name") for item in planned_derived_fields}
    unavailable = [metric for metric in metrics
                   if required_fields.get(metric) and required_fields[metric] not in available_metric_fields]
    if unavailable:
        planner_unsupported.extend(
            f"Metric {metric} requires schema field {required_fields[metric]!r}, which is missing from {base_file}."
            for metric in unavailable
        )
    if group_by != "all" and "date" not in base_fields:
        planner_unsupported.append(f"Requested time grouping {group_by!r} needs a mapped date field in {base_file}.")

    output_label_bindings = {}
    for spec in raw.get("output_specs", []):
        if isinstance(spec, dict):
            for label, runtime_field in _as_mapping(spec.get("column_mapping")).items():
                output_label_bindings[_semantic_label_key(label)] = runtime_field
    dimensions = [] if rows_only else list(dict.fromkeys(
        output_label_bindings.get(_semantic_label_key(value), value)
        for value in raw.get("group_dimensions", [])
    ))
    dimension_owner = {}
    for item in planned_files:
        for dimension in (*item["fields"], *item["dimensions"], *item.get("constants", {})):
            dimension_owner.setdefault(dimension, item["name"])
    missing_dimensions = [name for name in dimensions if name not in dimension_owner]
    if missing_dimensions:
        planner_unsupported.append(f"Requested grouping field(s) are not mapped in uploaded CSVs: {missing_dimensions}")
    join_targets = {item["right_file"] for item in joins}
    unjoined_dimensions = [name for name in dimensions
                           if name in dimension_owner and dimension_owner[name] != base_file and dimension_owner[name] not in join_targets]
    if unjoined_dimensions:
        planner_unsupported.append(f"Requested grouping field(s) require an unplanned join: {unjoined_dimensions}")

    output_columns = (["report_period"] if group_by != "all" else []) + dimensions + metrics

    raw_output_specs = raw.get("output_specs")
    # Reconcile the explicit interface with runtime bindings. Output names are
    # labels, never evidence of output kind, columns, grouping or business rules.
    if declared_outputs:
        model_specs = raw_output_specs if isinstance(raw_output_specs, list) else []
        reconciled = []
        for declared in declared_outputs:
            name = declared["name"]
            candidates = [spec for spec in model_specs
                          if Path(str(spec.get("file_name") or spec.get("name", ""))).stem.casefold()
                          == Path(name).stem.casefold()]
            spec = dict(candidates[0]) if len(candidates) == 1 else {}
            spec.setdefault("name", Path(name).stem)
            spec.setdefault("file_name", name if Path(name).suffix else f"{name}.csv")
            spec.setdefault("kind", declared["kind"])
            required_fields = declared.get("required_fields", [])
            columns = _normalize_model_collection(spec.get("columns"), "output columns", (str,))
            spec["columns"] = list(dict.fromkeys([*required_fields, *columns]))
            mapping = dict(_as_mapping(spec.get("column_mapping")))
            if declared["kind"] == "rows":
                sources = raw.get("union_files") or [planned_by_name[base_file]]
                for field in spec["columns"]:
                    if field in mapping:
                        continue
                    choices = []
                    for source in sources:
                        bindings = {**source.get("fields", {}), **source.get("dimensions", {})}
                        available = set(bindings) | set(source.get("constants", {}))
                        # Exact runtime names or inverse physical bindings only;
                        # unknown synonyms require semantic reasoning, not aliases.
                        choices.append({field} if field in available else
                                       {role for role, physical in bindings.items() if physical == field})
                    common = set.intersection(*choices) if choices else set()
                    if len(common) == 1:
                        mapping[field] = next(iter(common))
            spec["column_mapping"] = mapping
            reconciled.append(spec)
        # Keep unexpected specs visible so validation rejects extra outputs.
        declared_stems = {Path(output["name"]).stem.casefold() for output in declared_outputs}
        reconciled.extend(spec for spec in model_specs
                          if Path(str(spec.get("file_name") or spec.get("name", ""))).stem.casefold()
                          not in declared_stems)
        raw_output_specs = reconciled
    if not isinstance(raw_output_specs, list) or not raw_output_specs:
        raw_output_specs = []
        requested_outputs = requirements.get("requested_output_files", [])
        if requested_outputs:
            for requested_output in requested_outputs:
                output_name = str(requested_output).strip()
                file_name = output_name if Path(output_name).suffix else f"{output_name}.csv"
                stem = Path(file_name).stem
                requested_groups = requirements.get("requested_groupings", [])
                selected = [group for group in requested_groups if group.get("output_file") == output_name]
                selected_group = selected[0] if len(selected) == 1 else requested_groups[0] if len(requested_groups) == 1 else {}
                if len(requested_groups) > 1 and not selected:
                    planner_unsupported.append(f"Output {file_name} needs an explicit grouping binding")
                output_grain = str(selected_group.get("time_grain") or group_by)
                output_dimensions = list(selected_group.get("dimensions", dimensions))
                raw_output_specs.append({
                    "name": stem, "file_name": file_name, "kind": "aggregate",
                    "format": Path(file_name).suffix.lstrip(".") or "csv",
                    "group_by": output_grain, "group_dimensions": output_dimensions, "metrics": metrics,
                })
        else:
            raw_output_specs = [{
                "name": "fct_daily_revenue", "file_name": "fct_daily_revenue.csv", "format": "csv",
                "group_by": group_by, "group_dimensions": dimensions, "metrics": metrics,
            }]
    output_specs = []
    for index, spec in enumerate(raw_output_specs):
        if not isinstance(spec, dict):
            planner_unsupported.append(f"Malformed output specification at index {index}")
            continue
        output_name = str(spec.get("name") or Path(str(spec.get("file_name", ""))).stem).strip()
        file_name = str(spec.get("file_name") or (f"{output_name}.csv" if output_name else "")).strip()
        output_kind = str(spec.get("kind", "aggregate")).casefold()
        if output_kind not in {"aggregate", "rows", "table", "row_level"}:
            planner_unsupported.append(f"Output {file_name} has unsupported output kind {output_kind!r}")
        output_metrics = [] if output_kind in {"rows", "table", "row_level"} else list(dict.fromkeys(
            resolved for item in spec.get("metrics", metrics)
            if (resolved := resolve_output_metric(item)) is not None
        ))
        if output_kind not in {"rows", "table", "row_level"}:
            output_metrics = list(dict.fromkeys([
                *output_metrics,
                *(output_metric_aliases[_semantic_label_key(column)]
                  for column in spec.get("columns", [])
                  if _semantic_label_key(column) in output_metric_aliases
                  and output_metric_aliases[_semantic_label_key(column)] in requested_runtime_metrics),
            ]))
        derived_metrics = list(spec.get("derived_metrics", []))
        derived_names = {item.get("name") for item in derived_metrics if isinstance(item, dict)}
        output_metrics = [metric for metric in output_metrics if metric not in derived_names]
        if output_kind not in {"rows", "table", "row_level"} and len(raw_output_specs) == 1 and requirements.get("requested_metrics"):
            # A single declared aggregate owns the request's complete metric
            # contract. Preserve supported base metrics even if the model
            # returned a stale representative metric in this output spec.
            output_metrics = list(dict.fromkeys(
                canonical_metric(metric) for metric in requirements["requested_metrics"]
                if canonical_metric(metric) in CUSTOM_METRICS
                and canonical_metric(metric) not in derived_names
            ))
        output_grain = spec.get("group_by", group_by)
        output_dimensions = [] if output_kind in {"rows", "table", "row_level"} else list(dict.fromkeys(
            output_label_bindings.get(_semantic_label_key(value), value)
            for value in spec.get("group_dimensions", dimensions)
        ))
        column_mapping = _as_mapping(spec.get("column_mapping"))
        if output_kind in {"rows", "table", "row_level"}:
            column_mapping = _prepared_projection_mapping(
                column_mapping, raw.get("union_files") or [planned_by_name[base_file]]
            )
        window_specs = list(spec.get("window_specs", []))
        requested_metric_labels = {
            _semantic_label_key(value) for value in requirements.get("requested_metrics", [])
            if isinstance(value, str)
        }
        output_metrics, column_mapping, derived_metrics = _normalize_identity_metric_aliases(
            list(spec.get("columns", [])), output_metrics, column_mapping,
            derived_metrics, requested_metric_labels,
        )
        output_metrics, column_mapping, window_specs = _normalize_output_metric_bindings(
            list(spec.get("columns", [])), output_metrics, column_mapping, window_specs,
            requested_runtime_metrics,
        )
        base_metric_aliases = {runtime: target for target, runtime in column_mapping.items()
                               if runtime in output_metrics}
        derived_metrics = [item for item in derived_metrics
                           if not (item.get("name") in column_mapping
                                   and canonical_metric(column_mapping[item.get("name")]) in output_metrics)]
        for item in derived_metrics:
            item["inputs"] = [base_metric_aliases.get(value, value) for value in item.get("inputs", [])]
        output_specs.append({
            "name": output_name or f"output_{index + 1}",
            "file_name": file_name,
            "format": str(spec.get("format", Path(file_name).suffix.lstrip(".") or "csv")).lower(),
            "kind": output_kind,
            "group_by": output_grain,
            "group_dimensions": output_dimensions,
            "metrics": output_metrics,
            "columns": list(spec.get("columns", [])),
            "column_mapping": column_mapping,
            "window_specs": window_specs,
            "post_window_filters": list(spec.get("post_window_filters", [])),
            "derived_metrics": derived_metrics,
        })

    aggregate_formulas = [item for item in requirements.get("requested_transformations", [])
                          if isinstance(item, dict) and item.get("type") == "aggregate_formula"]
    compiled_formula_names = set()
    for formula in aggregate_formulas:
        formula_name = str(formula.get("name", "")).strip()
        candidates = [spec for spec in output_specs
                      if formula_name in spec.get("columns", [])]
        if not candidates and len(output_specs) == 1 and output_specs[0].get("kind") == "aggregate":
            candidates = output_specs
        if len(candidates) != 1:
            planner_unsupported.append(
                f"Aggregate formula {formula_name!r} must bind to exactly one declared aggregate output"
            )
            continue
        mapped_result = candidates[0].get("column_mapping", {}).get(formula_name)
        if mapped_result and canonical_metric(mapped_result) in candidates[0].get("metrics", []):
            # The output alias already binds this result field to a supported
            # aggregate, so a duplicate derived formula is unnecessary.
            compiled_formula_names.add(formula_name)
            continue
        error = _compile_aggregate_formula(formula, candidates[0], candidates[0].get("group_dimensions", []))
        if error:
            planner_unsupported.append(f"Aggregate formula {formula_name!r} could not be represented: {error}")
        else:
            compiled_formula_names.add(formula_name)
    if compiled_formula_names:
        planner_unsupported = [item for item in planner_unsupported
                               if not any(re.search(rf"\b{re.escape(name)}\b", str(item), re.I)
                                          for name in compiled_formula_names)]

    expected_group_values = {}
    if "platform" in dimensions:
        expected_platforms = sorted({
            item.get("constants", {}).get("platform")
            for item in raw.get("union_files", [])
            if item.get("constants", {}).get("platform")
        })
        if expected_platforms:
            expected_group_values["platform"] = expected_platforms
    profile_summary = []
    for source in data_profile.get("sources", []):
        profile_summary.append({key: source.get(key) for key in (
            "source_name", "row_count", "data_grain", "data_grain_confidence", "data_grain_evidence",
            "status_counts", "exact_duplicate_row_count", "duplicate_order_id_count",
        )})
    data_grain_by_file = {
        source.get("source_name", source.get("name", "")): source.get("data_grain", "unknown")
        for source in data_profile.get("sources", [])
    }
    field_defaults = {}
    for item in ambiguity_resolution.get("defaults_used", []):
        if item.get("issue") == "missing_customer_name" and item.get("decision") not in {"keep_null", None}:
            # Preserve compatibility with previously confirmed custom defaults;
            # the generic profile default is keep_null and must not become a literal.
            field_defaults["customer_name"] = item["decision"]
    duplicate_scope = "per_file" if resolved_business_rules.get("cross_source_identity") == "independent" else clarification.get("duplicate_scope", "global")
    duplicate_resolution = resolved_business_rules.get("dedup_policy") or clarification.get("duplicate_resolution", "ask")
    def status_values_by_file(rule_key: str, clarification_key: str) -> dict:
        value = resolved_business_rules.get(rule_key) or clarification.get(clarification_key) or {}
        if not isinstance(value, dict):
            return {}
        return value

    completed_by_file = status_values_by_file("completed_status_values_by_file", "completed_status_values_by_file")
    cancelled_by_file = status_values_by_file("cancelled_status_values_by_file", "cancelled_status_values_by_file")
    per_file_status_mappings = {}
    for schema in schemas:
        names = {str(schema.get("name", "")).casefold(),
                 str(schema.get("original_name", schema.get("name", ""))).casefold()}
        completed_values = next((values for name, values in completed_by_file.items() if str(name).casefold() in names), [])
        cancelled_values = next((values for name, values in cancelled_by_file.items() if str(name).casefold() in names), [])
        if isinstance(completed_values, str):
            completed_values = re.split(r"[,;|]", completed_values)
        if isinstance(cancelled_values, str):
            cancelled_values = re.split(r"[,;|]", cancelled_values)
        source_mapping = {str(value).strip().casefold(): "completed" for value in completed_values if str(value).strip()}
        source_mapping.update({str(value).strip().casefold(): "cancelled" for value in cancelled_values if str(value).strip()})
        if returned_policy in {"yes", "true", "1"}:
            source_mapping["returned"] = "cancelled"
        per_file_status_mappings[schema["name"]] = source_mapping
    for item in raw.get("union_files", []):
        item["status_mapping"] = per_file_status_mappings.get(item.get("name"), {})
    base_schema_name = base_schema.get("name")
    status_mapping = per_file_status_mappings.get(base_schema_name, {}) if not raw.get("union_files") else {}
    normalized_statuses = {"completed", "cancelled", "canceled"}
    unknown_status_values = sorted({
        str(value).strip() for source in data_profile.get("sources", [])
        for value in (source.get("status_counts") or {})
        if str(value).strip().casefold() not in normalized_statuses
        and not (str(value).strip().casefold() == "returned" and returned_policy in {"yes", "true", "1"})
        and str(value).strip().casefold() not in {
            item for values in (*completed_by_file.values(), *cancelled_by_file.values())
            for item in (values if isinstance(values, (list, tuple, set)) else re.split(r"[,;|]", str(values)))
        }
    })
    raw["completed_status_values"] = ["completed"]
    raw["cancelled_status_values"] = ["cancelled"]
    revenue_semantics = None
    if revenue_mode:
        fact_names = {item["name"] for item in raw.get("union_files", [])} or {base_file}
        revenue_semantics = build_revenue_semantics(request, requirements,
            [schema for schema in schemas if schema["name"] in fact_names], data_profile,
            clarification, resolved_business_rules, revenue_role_proposals)
        # Preserve the confirmed role/policy answers as run-local provenance.
        resolved_business_rules = dict(resolved_business_rules)
        for key in ("revenue_roles_by_file", "revenue_measure_basis", "revenue_status_policy",
                    "recognized_status_values_by_file", "refund_treatment"):
            if key in clarification:
                resolved_business_rules[key] = clarification[key]
        policy = revenue_semantics["decisions"]["status_policy"]
        raw["filters"] = [predicate for predicate in requirements.get("requested_filters", [])
                          if not revenue_status_filter_is_covered(predicate, revenue_semantics)]
        status_mapping = {}
        if policy == "recognized":
            if raw.get("union_files"):
                eligible = {source["name"]: source["recognized_status_values"] for source in revenue_semantics["sources"]}
                for source in raw["union_files"]:
                    source["status_mapping"] = {value: "revenue_recognized" for value in eligible.get(source["name"], [])}
                raw["include_status_values"] = ["revenue_recognized"]
            else:
                raw["include_status_values"] = revenue_semantics["sources"][0]["recognized_status_values"]
            raw["exclude_status_values"] = []
        else:
            raw["include_status_values"] = []
            raw["exclude_status_values"] = []
            for source in raw.get("union_files", []):
                source["status_mapping"] = {}
        for schema in schemas:
            semantic_source = next((source for source in revenue_semantics.get("sources", [])
                                    if source.get("name") == schema.get("name")), {})
            if semantic_source.get("transaction_grain"):
                data_grain_by_file[schema.get("original_name", schema.get("name", ""))] = semantic_source[
                    "transaction_grain"
                ]
    date_formats = {}
    for schema in schemas:
        name = schema["name"]
        supplied = (clarification.get("date_format_by_file") or {}).get(schema.get("original_name", name))
        if supplied:
            date_formats[name] = str(supplied)
        elif revenue_semantics:
            source = next((source for source in revenue_semantics["sources"] if source["name"] == name), {})
            event_column = source.get("roles", {}).get("business_event_timestamp", {}).get("column")
            column_profile = next((column for column in _profile_for_schema(schema, data_profile).get("column_profiles", [])
                                   if column.get("name") == event_column), {})
            date_formats[name] = (column_profile.get("date_format_candidate")
                                  or _resolved_profile_format(schema, ambiguity_resolution, "date_format")
                                  or "unknown")
        else:
            date_formats[name] = (_resolved_profile_format(schema, ambiguity_resolution, "date_format")
                                  or "day_first")
    plan = {
        "agent": "planner", "request": request, "dataset_mode": "generic_csv", "sources": ["custom_csv"],
        "base_file": base_file, "joins": joins, "files": planned_files, "group_by": group_by,
        "union_files": raw.get("union_files", []),
        "source_files_used": sorted(next((schema.get("original_name", schema["name"]) for schema in schemas
                                           if schema["name"] == name), name)
                                     for name in (set(item.get("name") for item in raw.get("union_files", []) if item.get("name"))
                                                  or {base_file, *(item["right_file"] for item in joins)})),
        "derived_fields": planned_derived_fields,
        "distance_factor": base_distance_factor,
        "filters": raw.get("filters", []),
        "deduplicate_keys": raw.get("deduplicate_keys", []),
        "status_mapping": status_mapping,
        "data_profile_summary": profile_summary,
        "data_grain_by_file": data_grain_by_file,
        "resolved_automatically": ambiguity_resolution.get("resolved_automatically", []),
        "defaults_used": ambiguity_resolution.get("defaults_used", []),
        "resolved_business_rules": resolved_business_rules,
        "ambiguity_resolution": ambiguity_resolution,
        "assumptions": [item.get("decision") for item in ambiguity_resolution.get("resolved_automatically", [])],
        "warnings": [*ambiguity_resolution.get("warnings", []),
                     *([f"Unmapped status values preserved and reported: {unknown_status_values}"] if unknown_status_values else [])],
        "unknown_status_values": unknown_status_values,
        "field_defaults": field_defaults,
        "output_specs": output_specs,
        "unsupported_requirements": list(dict.fromkeys(planner_unsupported)),
        "requirement_contract": requirements,
        "revenue_semantics": revenue_semantics,
        "currency_by_file": {schema["name"]: str((clarification.get("currency_by_file") or {}).get(
            schema.get("original_name", schema["name"]), "")).upper() for schema in schemas},
        "currency_columns": {schema["name"]: (next((source.get("currency_column") for source in revenue_semantics["sources"]
            if source["name"] == schema["name"]), None) if revenue_semantics else _currency_column(schema)) for schema in schemas},
        "currency_rates": {str(code).upper(): float(rate) for code, rate in
                           (clarification.get("currency_rates") or {}).items()},
        "missing_currency_by_file": {schema["name"]: str((clarification.get("missing_currency_by_file") or {}).get(
            schema.get("original_name", schema["name"]), "")).upper() for schema in schemas},
        "number_format_by_file": {schema["name"]: str((clarification.get("number_format_by_file") or {}).get(
            schema.get("original_name", schema["name"]),
            _resolved_profile_format(schema, ambiguity_resolution, "numeric_format"))) for schema in schemas},
        "number_format_by_currency": {str(code).upper(): str(value)
                                       for code, value in (clarification.get("number_format_by_currency") or {}).items()},
        "date_format_by_file": date_formats,
        "report_timezone": str(clarification.get("report_timezone", "UTC")),
        "missing_amount_policy": {schema["name"]: str((clarification.get("missing_amount_policy") or {}).get(
            schema.get("original_name", schema["name"]), "exclude")) for schema in schemas},
        "duplicate_resolution": duplicate_resolution,
        "duplicate_scope": duplicate_scope,
        "deduplicate_ids": deduplicate_ids,
        "deduplicate_policy": dedup_policy,
        "join_duplicate_policy": clarification.get("join_duplicate_policy", "reject"),
        "orphan_policy": clarification.get("orphan_policy", "reject"),
        "target_currency": str(clarification.get("target_currency", "")).upper(),
        "conversion_rates": {schema["name"]: (1.0 if str((clarification.get("currency_by_file") or {}).get(
            schema.get("original_name", schema["name"]), "")).upper() == str(clarification.get("target_currency", "")).upper()
            else float((clarification.get("conversion_rates") or {}).get(schema.get("original_name", schema["name"]), 1)))
            for schema in schemas},
        "request_interpretation": request_interpretation,
        "expected_group_values": expected_group_values,
        "group_dimensions": dimensions, "metrics": metrics, "output_columns": output_columns,
        "completed_status_values": ["completed"],
        "cancelled_status_values": ["cancelled"],
        "include_status_values": [str(value).lower() for value in raw.get("include_status_values", [])],
        "exclude_status_values": [str(value).lower() for value in raw.get("exclude_status_values", [])],
    }
    if provider != "mock" and requirements.get("requested_outputs") and _retry_feedback is None:
        from agents.plan_validator import validate_plan
        errors = validate_plan(plan, requirements, schemas,
                               Path(os.getenv("FLOWFORGE_DATA_DIR", ROOT / "data")), data_profile)
        if errors:
            # One bounded semantic correction in Planner. The original flow's
            # Validator checkpoint still checks the resulting plan before Coder.
            attempt_context.update(previous_plan=plan, validation_errors=errors)
            raise ValueError("Planner contract validation failed: " + "; ".join(errors))
    return plan

def read_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())


def run(state: dict) -> dict:
    request = state["request"].strip()
    provider = state.get("provider", "mock")
    requirements = state.get("requirement_contract", {})
    history = state.get("conversation_history", [])
    custom_schemas = json.loads(os.getenv("FLOWFORGE_CUSTOM_CSV_SCHEMAS", "[]"))
    if custom_schemas:
        try:
            plan = custom_csv_plan(
                request, provider, custom_schemas, state.get("clarification", {}), requirements,
                state.get("data_profile", {}), state.get("ambiguity_resolution", {}),
            )
        except Exception as error:
            return {"status": "failed", "error": f"Planner could not map CSV data: {error}"}
        PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = dict(state.get("artifacts", {}))
        artifacts["plan"] = str(PLAN_PATH)
        return {"plan": plan, "requirement_contract": requirements, "artifacts": artifacts, "status": "planned"}
    sources = [name for name in os.getenv("FLOWFORGE_AVAILABLE_SOURCES", "shopee,tiki,website").split(",") if name in SOURCE_STATUSES]
    if not request:
        return {"status": "failed", "error": "Request is empty"}
    if not sources:
        return {"status": "failed", "error": "No supported uploaded data source was provided"}

    if provider == "mock":
        dual = any(term in request.lower() for term in ("ngày và tháng", "tháng và ngày", "daily and monthly", "day and month"))
        selected_sources = [source for source in requirements.get("requested_sources", []) if source in sources]
        if not selected_sources:
            selected_sources = sources
        plan = {
            "agent": "planner", "request": request, "sources": selected_sources,
            "valid_statuses": [SOURCE_STATUSES[source] for source in selected_sources],
            "group_by": "day_and_month" if dual else "day",
            "include_channel_revenue": False,
            # The built-in mode has no profiled grain evidence, so deduplication
            # must stay off unless a later explicit rule supports it.
            "remove_duplicate_order_ids": False,
            "drop_missing_revenue": True,
            "metrics": [canonical_metric(metric) for metric in requirements.get("requested_metrics", [])],
            "output_specs": [{"name": "fct_daily_revenue", "file_name": "fct_daily_revenue.csv",
                              "format": "csv", "group_by": "day_and_month" if dual else "day",
                              "group_dimensions": [], "metrics": [canonical_metric(metric) for metric in requirements.get("requested_metrics", [])]}],
        }
    else:
        prompt = f"""Available input files: {sources}
Requirement contract: {json.dumps(requirements, ensure_ascii=False)}
Conversation history: {json.dumps(history, ensure_ascii=False)}
Latest user requirement: {request}"""
        try:
            plan = read_json(generate_text(
                provider, prompt, system_prompt=load_system_prompt("prompt_plan")
            ))
            plan["agent"] = "planner"
            plan["request"] = request
            plan["sources"] = [source for source in plan.get("sources", []) if source in sources]
            plan["valid_statuses"] = [SOURCE_STATUSES[source] for source in plan["sources"]]
            if not plan["sources"]:
                return {"status": "failed", "error": "Planner selected no uploaded sources"}
            request_lower = request.lower()
            asks_for_both_grains = (
                ("ngày" in request_lower and "tháng" in request_lower)
                or ("daily" in request_lower and "monthly" in request_lower)
            )
            if asks_for_both_grains:
                plan["group_by"] = "day_and_month"
            asks_for_channels = any(
                term in request_lower
                for term in ("từng kênh", "theo kênh", "từng nguồn", "per channel", "by channel", "each source")
            )
            plan["include_channel_revenue"] = asks_for_channels
            plan["requirement_contract"] = requirements
            if plan.get("group_by") not in {"day", "week", "month", "day_and_month"}:
                return {"status": "failed", "error": "Planner returned an unsupported group_by value"}
        except Exception as error:
            return {"status": "failed", "error": f"Planner could not create a plan: {error}"}

    plan["requirement_contract"] = requirements
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = dict(state.get("artifacts", {}))
    artifacts["plan"] = str(PLAN_PATH)
    return {"plan": plan, "requirement_contract": requirements, "artifacts": artifacts, "status": "planned"}
