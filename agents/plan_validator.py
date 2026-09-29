"""Code-level gate between Planner and SQL Coder."""

from __future__ import annotations

import csv
import json
import os
import re
from copy import deepcopy
from pathlib import Path

from agents.requirement_contract import (
    REVENUE_PATTERN,
    canonical_metric,
    is_revenue_request,
    normalize_text,
    revenue_semantic_issues,
    revenue_status_filter_is_covered,
)
from agents.schema_inspection import profile_source

ROOT = Path(__file__).resolve().parents[1]


def _normal(value) -> str:
    return str(value or "").strip().casefold()


def _output_key(value: str) -> tuple[str, str | None]:
    name = Path(str(value or "")).name.casefold()
    suffix = Path(name).suffix
    return (Path(name).stem, suffix or None)


def _plan_sources(plan: dict) -> set[str]:
    if plan.get("dataset_mode") != "generic_csv":
        return set(plan.get("sources", []))
    if plan.get("source_files_used"):
        return set(plan["source_files_used"])
    if plan.get("union_files"):
        return {item.get("name") for item in plan["union_files"] if item.get("name")}
    return {plan.get("base_file"), *(item.get("right_file") for item in plan.get("joins", []))} - {None}


def _planned_metrics(plan: dict) -> set[str]:
    if plan.get("output_specs"):
        metrics = {metric for output in plan["output_specs"] for metric in output.get("metrics", [])}
        metrics.update(item.get("name") for output in plan["output_specs"]
                       for item in output.get("derived_metrics", []) if item.get("name"))
    else:
        metrics = set(plan.get("metrics", []))
    return {canonical_metric(metric) for metric in metrics}


def _has_unique_key(path: Path, keys: list[str]) -> tuple[bool, str]:
    seen = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = [key for key in keys if key not in (reader.fieldnames or [])]
            if missing:
                return False, f"Join key column(s) missing from {path.name}: {missing}"
            for row_number, row in enumerate(reader, start=2):
                key = tuple((row.get(name) or "").strip() for name in keys)
                if any(value == "" for value in key):
                    continue
                if key in seen:
                    return False, f"Join key {keys} is duplicated in {path.name} (near row {row_number})"
                seen.add(key)
    except OSError as error:
        return False, f"Could not verify join cardinality for {path.name}: {error}"
    return True, ""


def _validate_revenue(plan: dict, requirements: dict, schemas: list[dict], data_dir: Path,
                      data_profile: dict | None) -> list[str]:
    if not is_revenue_request(plan.get("request", requirements.get("request", "")), requirements):
        return []
    if plan.get("dataset_mode") != "generic_csv":
        # The existing built-in contract is a separate, fixed demo mode. It is
        # not evidence that an unknown schema has passed this business pattern.
        return []
    semantics = deepcopy(plan.get("revenue_semantics"))
    if not isinstance(semantics, dict) or semantics.get("pattern") != REVENUE_PATTERN:
        return ["Revenue / Sales is missing the reusable semantic business pattern"]
    errors = []
    request = normalize_text(requirements.get("request", plan.get("request", "")))
    explicit_basis = "gross" if re.search(r"\bgross\b", request) else "net" if re.search(r"\bnet\b", request) else ""
    if explicit_basis and semantics.get("decisions", {}).get("measure_basis") != explicit_basis:
        errors.append("Revenue basis does not preserve the user's explicit gross/net request")
    confirmed_rules = plan.get("resolved_business_rules", {})
    for rule, decision in (("revenue_status_policy", "status_policy"), ("revenue_measure_basis", "measure_basis"),
                           ("refund_treatment", "refund_treatment")):
        if confirmed_rules.get(rule) and confirmed_rules[rule] != semantics.get("decisions", {}).get(decision):
            errors.append(f"Revenue {decision} differs from the confirmed Human business rule")
    fact_names = {source.get("name") for source in plan.get("union_files", [])} or {plan.get("base_file")}
    if {source.get("name") for source in semantics.get("sources", [])} != fact_names:
        errors.append("Revenue role bindings do not cover exactly the selected transaction facts")
    profiles = (data_profile or {}).get("sources", [])
    file_specs = {item.get("name"): item for item in plan.get("files", [])}
    union_specs = {item.get("name"): item for item in plan.get("union_files", [])}
    semantics["time_reporting"] = any(spec.get("group_by", plan.get("group_by")) in {"day", "week", "month"}
                                      for spec in plan.get("output_specs", []))
    for source in semantics.get("sources", []):
        name = source.get("name")
        schema = next((schema for schema in schemas if schema.get("name") == name), {})
        profile = next((profile for profile in profiles if profile.get("name") == name), None)
        if profile is None:
            profile = profile_source(schema, data_dir)
        columns = {column["name"]: column for column in profile.get("column_profiles", [])}
        confirmed_statuses = confirmed_rules.get("recognized_status_values_by_file", {})
        if isinstance(confirmed_statuses, dict):
            statuses = confirmed_statuses.get(schema.get("original_name", name), confirmed_statuses.get(name))
            if isinstance(statuses, str):
                statuses = [value.strip() for value in re.split(r"[,|]", statuses) if value.strip()]
            if statuses is not None and set(map(_normal, statuses)) != set(source.get("recognized_status_values", [])):
                errors.append(f"Revenue eligibility in {name} differs from Human confirmation")
        source["status_present"] = any(candidate.get("role") == "transaction_status"
            for column in columns.values() for candidate in column.get("business_role_candidates", []))
        mappings = (union_specs.get(name) or file_specs.get(name, {})).get("fields", {})
        for role, field in (("transaction_monetary_value", "amount"), ("transaction_entity_key", "id"),
                            ("business_event_timestamp", "date"), ("transaction_status", "status")):
            binding = source.get("roles", {}).get(role, {})
            if not binding:
                continue
            column = columns.get(binding.get("column"), {})
            if not column or mappings.get(field) != binding.get("column"):
                errors.append(f"Revenue role {role} in {name} differs from the physical execution mapping")
            candidates = column.get("business_role_candidates", [])
            if binding.get("authority") not in {"human", "profile", "model"}:
                errors.append(f"Revenue role {role} in {name} has no mapping provenance")
            elif binding.get("authority") != "human" and not any(
                    candidate.get("role") == role and candidate.get("confidence", 0) >= 0.90
                    for candidate in candidates):
                errors.append(f"Revenue role {role} in {name} lacks independent profile evidence; ask Human")
            if role in {"transaction_monetary_value", "transaction_entity_key"} and any(
                    candidate.get("role") == "quantity_field" for candidate in candidates):
                errors.append(f"Revenue {role} in {name} points to quantity instead of transaction value/identity")
            if role == "transaction_monetary_value":
                binding["numeric_parse_ratio"] = column.get("numeric_parse_ratio", 0)
                if any(candidate.get("meaning") in {"payment", "cost", "balance", "refund"} for candidate in candidates):
                    errors.append(f"Revenue monetary role in {name} points to a different business measure")
                if any(candidate.get("scale") == "unit_price" for candidate in candidates):
                    errors.append(f"Revenue monetary role in {name} is a unit price; a confirmed transaction value derivation is required")
            if role == "business_event_timestamp":
                binding["date_parse_ratio"] = column.get("date_parse_ratio", 0)
                confirmed_format = column.get("date_format_candidate")
                if confirmed_format and plan.get("date_format_by_file", {}).get(name) != confirmed_format:
                    errors.append(f"Revenue event date format in {name} contradicts unambiguous local data evidence")
                if any(candidate.get("meaning") in {"payment", "shipment", "registration"} for candidate in candidates) and not (
                        binding.get("authority") == "human" and binding.get("meaning") == "recognition" and binding.get("recognition_rule")):
                    errors.append(f"Revenue reporting time in {name} refers to a different event; Human must confirm a recognition rule")
            if role == "transaction_entity_key" and column.get("null_count", 0):
                errors.append(f"Revenue entity key in {name} contains missing identities")
        status_binding = source.get("roles", {}).get("transaction_status", {})
        status_values = columns.get(status_binding.get("column"), {}).get("value_distribution", {})
        source["observed_statuses"] = list(status_values)
        refund_states = {str(value).strip().casefold() for value in status_values
                         if "refund" in str(value).casefold() or "return" in str(value).casefold()}
        source["refund_observed"] = bool(refund_states) or any(candidate.get("meaning") == "refund"
            for column in columns.values() for candidate in column.get("business_role_candidates", []))
        decisions = semantics.get("decisions", {})
        policy = decisions.get("status_policy")
        if policy == "recognized":
            eligible = set(source.get("recognized_status_values", []))
            if union_specs:
                expected_mapping = {value: "revenue_recognized" for value in eligible}
                if union_specs.get(name, {}).get("status_mapping") != expected_mapping or plan.get("include_status_values") != ["revenue_recognized"]:
                    errors.append(f"Revenue eligibility differs from the confirmed source states in {name}")
            elif set(plan.get("include_status_values", [])) != eligible or plan.get("status_mapping"):
                errors.append(f"Revenue eligibility differs from the confirmed source states in {name}")
            if plan.get("exclude_status_values"):
                errors.append("Revenue eligibility includes an unconfirmed exclusion")
        elif policy in {"all", "requested_filters"} and (
                plan.get("include_status_values") or plan.get("exclude_status_values") or plan.get("status_mapping")
                or union_specs.get(name, {}).get("status_mapping")):
            errors.append("Revenue status scope contains unconfirmed status aliases or filters")
        if policy == "requested_filters" and not any(predicate.get("field") == "status" for predicate in plan.get("filters", [])):
            errors.append("Confirmed revenue status predicate is missing from execution")
        if decisions.get("refund_treatment") == "exclude" and refund_states:
            eligible = set(source.get("recognized_status_values", []))
            predicates = [predicate for predicate in plan.get("filters", []) if predicate.get("field") == "status"]
            if policy == "all" or (policy == "recognized" and eligible & refund_states) or (policy == "requested_filters" and not predicates):
                errors.append("Refund exclusion is not represented by the executed eligibility rule")
        if decisions.get("refund_treatment") == "already_net" and decisions.get("measure_basis") != "net":
            errors.append("Refunds already netted from the monetary value require a net revenue definition")
        if source.get("transaction_grain") == "item_level" and plan.get("deduplicate_ids"):
            errors.append("Revenue cannot remove line items by transaction entity key")
        if source.get("currency_column") and source["currency_column"] != plan.get("currency_columns", {}).get(name):
            errors.append(f"Revenue currency column differs from execution for {name}")
        if source.get("currency_column"):
            for code in source.get("currency_codes", []):
                if code != decisions.get("target_currency"):
                    try:
                        same_rate = float(plan.get("currency_rates", {}).get(code, 0)) == float(decisions.get("currency_rates", {}).get(code, -1))
                    except (TypeError, ValueError):
                        same_rate = False
                    if not same_rate:
                        errors.append(f"Revenue exchange rate for {code} differs from the confirmed policy")
        if not source.get("currency_column") and source.get("currency") != plan.get("currency_by_file", {}).get(name):
            errors.append(f"Revenue currency binding differs from execution for {name}")
        if not source.get("currency_column") and source.get("currency") != decisions.get("target_currency"):
            rate = plan.get("conversion_rates", {}).get(name)
            if not isinstance(rate, (int, float)) or not 0 < rate < float("inf"):
                errors.append(f"Revenue needs a confirmed positive exchange rate for {name}")
            else:
                try:
                    confirmed_rate = float(source.get("conversion_rate"))
                except (TypeError, ValueError):
                    confirmed_rate = None
                if confirmed_rate != rate:
                    errors.append(f"Revenue source-to-reporting exchange rate in {name} differs from Human confirmation")
    if semantics.get("decisions", {}).get("target_currency") != plan.get("target_currency"):
        errors.append("Revenue reporting currency differs from execution")
    errors.extend(f"Revenue / Sales ({name or 'policy'}): {issue}" for _, name, issue in revenue_semantic_issues(semantics))
    return errors


def validate_plan(plan: dict, requirements: dict, schemas: list[dict], data_dir: Path,
                  data_profile: dict | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["Planner did not return a plan object"]
    if not isinstance(requirements, dict):
        return ["Requirement Extraction did not return a requirement contract"]

    unsupported = [*requirements.get("unsupported_requirements", []), *plan.get("unsupported_requirements", [])]
    if unsupported:
        errors.append(f"Unsupported requirements must not proceed to Coder: {unsupported}")
    ambiguities = requirements.get("ambiguities", [])
    if ambiguities:
        errors.append(f"Unresolved ambiguities require clarification: {ambiguities}")

    revenue_semantics = plan.get("revenue_semantics") or {}
    requested_filters = [predicate for predicate in requirements.get("requested_filters", [])
                         if not revenue_status_filter_is_covered(predicate, revenue_semantics)]
    planned_filters = plan.get("filters", [])
    realized_operations = {
        "union": bool(plan.get("union_files")),
        "join": bool(plan.get("joins")),
        "filter": bool(planned_filters or plan.get("include_status_values") or plan.get("exclude_status_values")
                       or any(output.get("post_window_filters") for output in plan.get("output_specs", []))),
        "deduplicate": bool(plan.get("deduplicate_ids") or plan.get("deduplicate_keys")),
        "aggregate": any(output.get("kind", "aggregate") == "aggregate" for output in plan.get("output_specs", [])),
    }
    for operation in requirements.get("prohibited_operations", []):
        if realized_operations.get(operation):
            errors.append(f"Plan violates prohibited operation: {operation}")
    if requested_filters != planned_filters:
        errors.append(f"Filter mismatch: requested={requested_filters}, planned={planned_filters}")

    requested_sources = {_normal(item) for item in requirements.get("requested_sources", [])}
    planned_sources = {_normal(item) for item in _plan_sources(plan)}
    if requested_sources and requested_sources != planned_sources:
        errors.append(
            "Source mismatch: "
            f"requested={sorted(requested_sources)}, planned={sorted(planned_sources)}"
        )

    planned_metrics = _planned_metrics(plan)
    metric_alias_targets: dict[str, set[str]] = {}
    for output in plan.get("output_specs", []):
        for label, runtime in output.get("column_mapping", {}).items():
            metric = canonical_metric(runtime)
            if metric in planned_metrics:
                key = re.sub(r"[^a-z0-9]+", "", normalize_text(label))
                if key:
                    metric_alias_targets.setdefault(key, set()).add(metric)
    metric_aliases = {key: next(iter(targets)) for key, targets in metric_alias_targets.items()
                      if len(targets) == 1}
    requested_metrics = set()
    for item in requirements.get("requested_metrics", []):
        canonical = canonical_metric(item)
        label_key = re.sub(r"[^a-z0-9]+", "", normalize_text(item))
        requested_metrics.add(canonical if canonical in planned_metrics else
                              metric_aliases.get(label_key, canonical))
    if requested_metrics != planned_metrics:
        errors.append(
            "Metric mismatch: "
            f"requested={sorted(requested_metrics)}, planned={sorted(planned_metrics)}"
        )

    requested_groups = requirements.get("requested_groupings", [])
    if requested_groups:
        output_mappings = [output.get("column_mapping", {}) for output in plan.get("output_specs", [])]

        def dimension_key(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", normalize_text(value))

        def mapped_dimension(value: str) -> str:
            for mapping in output_mappings:
                match = next((runtime for label, runtime in mapping.items()
                              if dimension_key(label) == dimension_key(value)), None)
                if match is not None:
                    return dimension_key(match)
            return dimension_key(value)

        actual_groups = {
            (_normal(output.get("group_by", plan.get("group_by", "all"))),
             frozenset(dimension_key(value) for value in output.get("group_dimensions", plan.get("group_dimensions", []))))
            for output in plan.get("output_specs", [])
            if output.get("kind") not in {"rows", "table", "row_level"}
        } or {(_normal(plan.get("group_by", "all")),
              frozenset(_normal(value) for value in plan.get("group_dimensions", [])))}
        expected_groups = {
             (_normal(group.get("time_grain") or "all"),
             frozenset(mapped_dimension(value) for value in group.get("dimensions", [])))
            for group in requested_groups
        }
        if actual_groups != expected_groups:
            errors.append(
                "Grouping mismatch: "
                f"requested={[(grain, sorted(dimensions)) for grain, dimensions in expected_groups]}, "
                f"planned={[(grain, sorted(dimensions)) for grain, dimensions in actual_groups]}"
            )
    requested_outputs = {_normal(item) for item in requirements.get("requested_output_files", [])}
    output_specs = plan.get("output_specs") or [{"file_name": "fct_daily_revenue.csv"}]
    planned_outputs = [str(spec.get("file_name") or spec.get("name") or "") for spec in output_specs]
    missing_outputs = [requested for requested in requested_outputs
                       if not any(_output_key(value)[0] == _output_key(requested)[0]
                                  and (_output_key(requested)[1] is None
                                       or _output_key(requested)[1] == _output_key(value)[1])
                                  for value in planned_outputs)]
    extra_outputs = [planned for planned in planned_outputs
                     if requested_outputs and not any(_output_key(planned)[0] == _output_key(requested)[0]
                                                      and (_output_key(requested)[1] is None
                                                           or _output_key(requested)[1] == _output_key(planned)[1])
                                                      for requested in requested_outputs)]
    if missing_outputs or extra_outputs:
        errors.append(
            "Output mismatch: "
            f"requested={sorted(requested_outputs)}, planned={sorted(planned_outputs)}"
        )
    seen_output_paths = set()
    for spec in output_specs:
        file_name = str(spec.get("file_name", ""))
        if not file_name or Path(file_name).name != file_name or Path(file_name).suffix.casefold() not in {".csv", ".json"}:
            errors.append(f"Unsafe or unsupported output path/format: {file_name!r}")
        if file_name.casefold() in seen_output_paths:
            errors.append(f"Duplicate output filename: {file_name}")
        seen_output_paths.add(file_name.casefold())

    schema_columns = {item.get("name"): set(item.get("columns", [])) for item in schemas}
    file_plans = {item.get("name"): item for item in plan.get("files", [])}
    for source in plan.get("union_files", []):
        if source.get("name") not in schema_columns:
            errors.append(f"Union source is not an uploaded file: {source.get('name')}")
        for semantic, physical in source.get("fields", {}).items():
            if physical not in schema_columns.get(source.get("name"), set()):
                errors.append(f"Unproven union field mapping {source.get('name')}.{semantic} -> {physical}")
    for name, item in file_plans.items():
        for semantic_group in ("fields", "dimensions"):
            for semantic, physical in item.get(semantic_group, {}).items():
                if physical not in schema_columns.get(name, set()):
                    errors.append(f"Unproven field mapping {name}.{semantic} -> {physical}")
        original_name = next((schema.get("original_name", name) for schema in schemas if schema.get("name") == name), name)
        stem = Path(original_name).stem.casefold()
        for field, value in item.get("constants", {}).items():
            if field == "platform" and str(value).casefold() in stem:
                continue
            if field not in item.get("constant_provenance", {}):
                errors.append(f"Unproven fabricated constant {field}={value!r} for {name}")

    normalized_fields = set()
    for item in file_plans.values():
        normalized_fields.update(item.get("fields", {}).keys())
        normalized_fields.update(item.get("dimensions", {}).keys())
        normalized_fields.update(item.get("constants", {}).keys())
    for source in plan.get("union_files", []):
        normalized_fields.update(source.get("fields", {}).keys())
        normalized_fields.update(source.get("constants", {}).keys())
    for derived in plan.get("derived_fields", []):
        if derived.get("name"):
            normalized_fields.add(derived["name"])
    for item in plan.get("joins", []):
        right = file_plans.get(item.get("right_file"), {})
        normalized_fields.update(right.get("dimensions", {}).keys())
    for predicate in planned_filters:
        if predicate.get("field") not in normalized_fields:
            errors.append(f"Filter field {predicate.get('field')!r} has no mapped schema/constant provenance")

    for output in output_specs:
        declared = [spec for spec in requirements.get("requested_outputs", [])
                    if _output_key(spec["name"])[0] == _output_key(output.get("file_name", ""))[0]]
        if len(declared) == 1:
            expected = declared[0]
            if output.get("kind") != expected["kind"]:
                errors.append(f"Output {output.get('file_name')} kind differs from requirement contract")
            missing = set(expected.get("required_fields", [])) - set(output.get("columns", []))
            if missing:
                errors.append(f"Output {output.get('file_name')} dropped required fields: {sorted(missing)}")
        window_specs = output.get("window_specs", [])
        derived_metrics = output.get("derived_metrics", [])
        allowed_window_functions = {"ROW_NUMBER", "RANK", "LAG", "LEAD", "SUM", "AVG", "COUNT", "MIN", "MAX"}
        output_mapping = output.get("column_mapping", {})
        output_metric_fields = set(output.get("metrics", []))
        output_fields = (normalized_fields | set(output.get("group_dimensions", []))
                         | output_metric_fields | {"report_period"})
        for label, runtime_field in output_mapping.items():
            if runtime_field in normalized_fields or canonical_metric(runtime_field) in output_metric_fields:
                output_fields.update({label, runtime_field})
        output_fields.update(window.get("name") for window in window_specs if window.get("name"))
        output_fields.update(item.get("name") for item in derived_metrics if item.get("name"))
        for window in window_specs:
            function = str(window.get("function", "")).upper()
            name = window.get("name")
            if function not in allowed_window_functions or not name:
                errors.append(f"Unsupported or unnamed window function in output {output.get('file_name')}: {window}")
            order_fields = [item if isinstance(item, str) else item.get("field") for item in window.get("order_by", [])]
            referenced = set(window.get("partition_by", [])) | {field for field in order_fields if field}
            if window.get("value_field"):
                referenced.add(window["value_field"])
            missing = referenced - output_fields
            if missing:
                errors.append(f"Window {name!r} references unmapped field(s): {sorted(missing)}")
            if not order_fields:
                if function in {"ROW_NUMBER", "RANK", "LAG", "LEAD"} or window.get("frame"):
                    errors.append(f"Window {name!r} has no deterministic order_by")
            if function in {"LAG", "LEAD", "SUM", "AVG", "MIN", "MAX"} and not window.get("value_field"):
                errors.append(f"Window {name!r} requires a schema-backed value_field")
            if window.get("aggregate_input") and window.get("aggregate_function") not in {
                None, "COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX",
            }:
                errors.append(f"Window {name!r} uses an unsupported aggregate_input function")
            frame = str(window.get("frame", "")).upper().strip()
            if frame and not re.fullmatch(r"(?:ROWS|RANGE) BETWEEN (?:UNBOUNDED PRECEDING|\d+ PRECEDING) AND (?:CURRENT ROW|\d+ FOLLOWING|UNBOUNDED FOLLOWING)", frame):
                errors.append(f"Window {name!r} has an unsupported frame {frame!r}")
        window_names = {window.get("name") for window in window_specs}
        for predicate in output.get("post_window_filters", []):
            if predicate.get("field") not in window_names:
                errors.append(f"Post-window filter references unknown window field {predicate.get('field')!r}")
            if predicate.get("operator") not in {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"}:
                errors.append(f"Post-window filter uses unsupported operator {predicate.get('operator')!r}")
        if output.get("post_window_filters") and not window_specs:
            errors.append(f"Output {output.get('file_name')} has post-window filters but no window fields")
        allowed_derived_operations = {"add", "subtract", "multiply", "divide", "copy", "identity"}
        defined_metrics = (set(output.get("metrics", [])) | window_names
                           | set(output.get("column_mapping", {}).keys()))
        for derived in derived_metrics:
            name = derived.get("name")
            inputs = derived.get("inputs", [])
            operation = _normal(derived.get("operation"))
            expected_inputs = 2 if operation in {"add", "subtract", "multiply", "divide"} else 1
            if (not name or not isinstance(inputs, list) or len(inputs) != expected_inputs
                    or not set(inputs).issubset(defined_metrics)):
                errors.append(f"Derived metric {name!r} has missing or unbound aggregate inputs")
            if operation not in allowed_derived_operations:
                errors.append(f"Derived metric {name!r} uses an unsupported operation")
        if output.get("kind") in {"rows", "table", "row_level"}:
            mapping = output.get("column_mapping", {})
            columns = output.get("columns") or list(mapping)
            window_names = {window.get("name") for window in window_specs}
            if not columns:
                errors.append(f"Row-level output {output.get('file_name')} must declare its output columns")
            missing_fields = [mapping.get(column, column) for column in columns
                              if column not in window_names and mapping.get(column, column) not in normalized_fields]
            if missing_fields:
                errors.append(f"Row-level output {output.get('file_name')} references unmapped fields: {missing_fields}")
            if not plan.get("union_files"):
                fact = file_plans.get(plan.get("base_file"), {})
                executed_fields = set(fact.get("fields", {})) | set(fact.get("dimensions", {})) | set(fact.get("constants", {}))
                for join in plan.get("joins", []):
                    executed_fields.update(file_plans.get(join.get("right_file"), {}).get("dimensions", {}))
                executed_fields.update(item.get("name") for item in plan.get("derived_fields", []))
                unavailable = [mapping.get(column, column) for column in columns
                               if column not in window_names and mapping.get(column, column) not in executed_fields]
                if unavailable:
                    errors.append(f"Row output {output.get('file_name')} requires bindings unavailable to fact execution: {unavailable}")
            # Union projections require a binding in every participating source.
            # A binding in one file cannot justify fabricated nulls in another.
            for source in plan.get("union_files", []):
                source_fields = set(source.get("fields", {})) | set(source.get("constants", {}))
                missing = [mapping.get(column, column) for column in columns
                           if column not in window_names and mapping.get(column, column) not in source_fields
                           and mapping.get(column, column) not in {item.get("name") for item in plan.get("derived_fields", [])}]
                if missing:
                    errors.append(f"Row output {output.get('file_name')} source {source.get('name')} lacks bindings: {missing}")
            if output.get("metrics"):
                errors.append(f"Row-level output {output.get('file_name')} cannot be given aggregate metrics")
    if (("window" in requirements.get("requested_operations", []))
            or ("window_function" in requirements.get("requested_transformations", []))) and not any(
                output.get("window_specs") for output in output_specs
            ):
        errors.append("Requested window operation was dropped; at least one output needs explicit window_specs")

    for formula in requirements.get("requested_transformations", []):
        if not isinstance(formula, dict) or formula.get("type") != "aggregate_formula":
            continue
        formula_name = formula.get("name")
        formula_operation = _normal(formula.get("operation"))
        represented = any(
            formula_name in output.get("columns", [])
            and (
                any(item.get("name") == formula_name
                    and _normal(item.get("operation")) == formula_operation
                    for item in output.get("derived_metrics", []))
                or canonical_metric(output.get("column_mapping", {}).get(formula_name, ""))
                in {canonical_metric(metric) for metric in output.get("metrics", [])}
            )
            for output in output_specs
        )
        if not represented:
            errors.append(f"Requested aggregate formula {formula_name!r} was not represented in output_specs")

    available_inputs = set().union(*schema_columns.values()) if schema_columns else set()
    for item in file_plans.values():
        available_inputs.update(item.get("fields", {}).keys())
        available_inputs.update(item.get("dimensions", {}).keys())
        available_inputs.update(item.get("constants", {}).keys())
    derived_fields = plan.get("derived_fields", [])
    for derived in derived_fields:
        field_name = derived.get("name")
        inputs = derived.get("inputs") or [value for value in (
            derived.get("start_field"), derived.get("end_field"),
            derived.get("left_field"), derived.get("right_field"),
        ) if value]
        operation = _normal(derived.get("operation"))
        supported_ops = {"date_diff", "datetime_difference", "copy", "identity", "add", "subtract", "multiply", "divide"}
        if not field_name or not inputs or not set(inputs).issubset(available_inputs):
            errors.append(f"Derived field {field_name!r} has no schema-backed input provenance")
        if operation not in supported_ops or not derived.get("deterministic", False):
            errors.append(f"Derived field {field_name!r} is not backed by a supported deterministic operation")

    custom_root = data_dir / "custom_csv"
    for join in plan.get("joins", []):
        right_file = join.get("right_file")
        keys = join.get("right_keys") or [join.get("right_key")]
        path = custom_root / str(right_file)
        unique, reason = _has_unique_key(path, [key for key in keys if key])
        if not unique and plan.get("join_duplicate_policy") != "keep_first":
            errors.append(f"Unsafe many-to-many join rejected: {reason}")

    for metric in requested_metrics:
        required_field = {
            "total_amount": "amount", "average_amount": "amount", "min_amount": "amount",
            "max_amount": "amount", "median_amount": "amount", "completed_count": "status",
            "distinct_order_count": "id", "cancelled_count": "status",
            "completion_rate": "status", "cancellation_rate": "status",
            "average_distance": "distance", "average_duration": "duration", "sum_value": "value",
        }.get(metric)
        if not required_field:
            continue
        if plan.get("union_files"):
            missing = [source.get("name") for source in plan["union_files"]
                       if required_field not in source.get("fields", {}) and not (
                           required_field == "duration"
                           and any(item.get("name") == "duration" and item.get("operation") in {
                               "date_diff", "datetime_difference"
                           } and {"start_time", "end_time"}.issubset(source.get("fields", {}))
                               for item in plan.get("derived_fields", [])
                           )
                       )]
            if missing:
                errors.append(f"Requested metric {metric} needs {required_field}; source(s) cannot provide it: {missing}")

    errors.extend(_validate_revenue(plan, requirements, schemas, data_dir, data_profile))
    return list(dict.fromkeys(errors))


def run(state: dict) -> dict:
    plan = state.get("plan", {})
    requirements = state.get("requirement_contract", {})
    ambiguity_resolution = state.get("ambiguity_resolution", {})
    schemas = json.loads(os.getenv("FLOWFORGE_CUSTOM_CSV_SCHEMAS", "[]"))
    data_dir = Path(os.getenv("FLOWFORGE_DATA_DIR", ROOT / "test-data"))
    errors = validate_plan(plan, requirements, schemas, data_dir, state.get("data_profile"))
    unresolved_questions = ambiguity_resolution.get("questions_for_user", [])
    if unresolved_questions or ambiguity_resolution.get("can_continue") is False:
        errors.append("Unresolved business ambiguity must be clarified before SQL generation")
    errors = list(dict.fromkeys(errors))
    plan = dict(plan)
    plan["plan_valid"] = not errors
    plan["plan_validation_errors"] = errors
    plan["requirement_contract"] = requirements
    plan_path = ROOT / "generated" / "pipeline_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = plan_path.parent / "plan_validation_report.json"
    report = {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "question_count": len(unresolved_questions),
        "ambiguity_resolution": {
            key: ambiguity_resolution.get(key, default)
            for key, default in (
                ("resolved_automatically", []), ("defaults_used", []),
                ("business_ambiguous", []), ("resolved_business_rules", {}),
                ("data_grain_by_source", {}), ("warnings", []),
            )
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = dict(state.get("artifacts", {}))
    artifacts["plan"] = str(plan_path)
    artifacts["plan_validation_report"] = str(report_path)
    return {
        "plan": plan,
        "artifacts": artifacts,
        "plan_valid": not errors,
        "plan_validation_errors": errors,
        "ambiguity_resolution": ambiguity_resolution,
        "status": "plan_validated" if not errors else "failed",
        "error": None if not errors else "; ".join(errors),
    }
