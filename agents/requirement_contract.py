"""Lossless request extraction shared by Planner, Plan Validator, and Tester."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from agents.llm import generate_text
from agents.prompt_loader import load_system_prompt

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_KEYS = (
    "requested_sources",
    "requested_operations",
    "requested_cleaning_steps",
    "requested_transformations",
    "requested_groupings",
    "requested_metrics",
    "requested_output_files",
    "requested_outputs",
    "prohibited_operations",
    "requested_filters",
    "requested_validation_checks",
    "requested_performance_metrics",
    "unsupported_requirements",
    "ambiguities",
    "assumptions",
)

METRIC_ALIASES = {
    "record_count": {"record_count", "row_count", "total_orders", "total_trips", "total_count", "count"},
    "distinct_order_count": {"distinct_order_count", "order_count", "unique_orders", "unique_trips"},
    "completed_count": {"completed_count", "completed_orders", "completed_trips"},
    "cancelled_count": {"cancelled_count", "cancelled_orders", "cancelled_trips", "canceled_orders"},
    "total_amount": {"total_amount", "total_revenue", "revenue", "sum_amount", "sum_revenue"},
    "average_amount": {"average_amount", "average_order_value", "average_revenue", "avg_amount", "aov"},
    "min_amount": {"min_amount", "minimum_amount", "min_order_value", "minimum_order_value"},
    "max_amount": {"max_amount", "maximum_amount", "max_order_value", "maximum_order_value"},
    "median_amount": {"median", "median_amount", "median_order_value"},
    "completion_rate": {"completion_rate", "completed_rate"},
    "cancellation_rate": {"cancellation_rate", "cancelled_rate", "canceled_rate"},
    "average_distance": {"average_distance", "avg_distance", "mean_distance"},
    "average_duration": {"average_duration", "avg_duration", "mean_duration"},
    "sum_value": {"sum_value", "total_quantity", "quantity_sum"},
    "customer_lifetime_value": {"customer_lifetime_value", "clv"},
    "running_revenue": {"running_revenue", "cumulative_revenue", "running_sum"},
    "customer_rank": {"customer_rank", "rank_customers"},
}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value).casefold().replace("đ", "d"))
    return "".join(char for char in value if not unicodedata.combining(char))


def _concept_is_negated(text: str, match: re.Match) -> bool:
    """Read local polarity around a concept mention without changing its meaning."""
    prefix = text[max(0, match.start() - 60):match.start()]
    suffix = text[match.end():match.end() + 40]
    negation = r"(?:no|not|never|without|don't|do not|does not|cannot|must not|khong|khong tinh|khong thuc hien)"
    if re.search(rf"\b{negation}\b(?:\s+[\w'-]+){{0,4}}\s*$", prefix):
        return True
    if re.match(r"\s*(?:(?:is|was|will be|should be|must be)\s+)?(?:not|never|excluded|excluded from)\b", suffix):
        return True
    return False


def canonical_metric(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", normalize_text(value)).strip("_")
    tokens = key.split("_")
    if "revenue" in tokens or "sales" in tokens:
        # Gross/net changes the business definition, not the aggregation slot.
        # This normalizes metric language only; it never selects an input column.
        key = "_".join("revenue" if token == "sales" else token
                       for token in tokens if token not in {"gross", "net"})
    for canonical, aliases in METRIC_ALIASES.items():
        if key in aliases:
            return canonical
    return key


# This pattern contains business roles only. Physical headers belong to a run's
# schema bindings; they must never become aliases in this reusable definition.
REVENUE_PATTERN = {
    "business_intent": "revenue_sales_aggregation",
    "required_semantic_roles": ["transaction_monetary_value", "transaction_entity_key"],
    "conditional_semantic_roles": {
        "business_event_timestamp": "required for a reporting period",
        "transaction_status": "required when source transactions have business states",
    },
    "metric_definition": "SUM(transaction_monetary_value) for eligible transactions at the declared value grain in one reporting currency",
    "business_rules": [
        "transaction count and quantity are different measures",
        "sales revenue and cash payments are different business events",
        "gross and net revenue require different measure definitions",
        "eligibility, refund treatment and event time follow confirmed business policy",
    ],
    "reuse_conditions": [
        "transaction value, entity/grain and required event/status roles are supported by current schema evidence or Human confirmation",
        "revenue basis, eligibility and currency are resolved for the current request",
    ],
    "do_not_reuse_when": [
        "the measure is quantity, unit price, payment, cost, balance or refund instead of transaction value",
        "a value is repeated across a finer grain or a join multiplies transaction values",
        "recognition policy, gross/net basis, refund treatment or currency is unresolved",
        "the reporting timestamp represents a different event without a confirmed recognition rule",
    ],
    "validation_rules": ["role binding provenance", "value grain preservation", "recognized status scope",
                         "reporting event time", "currency consistency", "independent business result comparison"],
}


def is_revenue_request(request: str, requirements: dict | None = None) -> bool:
    outputs = (requirements or {}).get("requested_outputs", [])
    if outputs and all(output.get("kind") == "rows" for output in outputs) and not (requirements or {}).get("requested_metrics"):
        # The extracted operation/output contract outranks keyword mentions,
        # including a metric mentioned in a prohibition or an explanatory note.
        return False
    text = normalize_text(request)
    intent_pattern = re.compile(r"\b(revenue|sales|doanh thu|gia tri ban hang|tien ban hang)\b")
    intent = any(not _concept_is_negated(text, match) for match in intent_pattern.finditer(text))
    metrics = {canonical_metric(item) for item in (requirements or {}).get("requested_metrics", [])}
    money = {"total_amount", "average_amount", "min_amount", "max_amount", "median_amount",
             "running_revenue", "customer_lifetime_value", "customer_rank"}
    return intent and (not metrics or bool(metrics & money) or any("revenue" in metric for metric in metrics))


def build_revenue_semantics(request: str, requirements: dict, schemas: list[dict], data_profile: dict,
                            clarification: dict, business_rules: dict, proposed_roles: dict | None = None) -> dict:
    from agents.schema_inspection import revenue_role_bindings

    answers = {**business_rules, **clarification}
    nested = clarification.get("resolved_business_rules") or {}
    if isinstance(nested, dict):
        answers.update(nested)
    text = normalize_text(request)
    basis = str(answers.get("revenue_measure_basis") or answers.get("revenue_basis") or "").casefold()
    basis = "gross" if basis.startswith("gross") else "net" if basis.startswith("net") else ""
    requested_basis = "gross" if re.search(r"\bgross\b", text) else "net" if re.search(r"\bnet\b", text) else ""
    basis = basis or requested_basis
    status_policy = str(answers.get("revenue_status_policy") or "")
    recognized = answers.get("recognized_status_values_by_file") or {}
    if answers.get("status_scope") == "all" or re.search(r"\b(all statuses|all states|moi trang thai|tat ca trang thai)\b", text):
        status_policy = status_policy or "all"
    elif recognized:
        status_policy = status_policy or "recognized"
    elif _has_explicit_status_filter(request, requirements.get("requested_filters", [])):
        status_policy = status_policy or "requested_filters"
    if not isinstance(recognized, dict):
        recognized = {}
    manual = answers.get("revenue_roles_by_file") or {}
    if not isinstance(manual, dict):
        manual = {}
    sources = []
    for schema in schemas:
        name = schema.get("name", "")
        original = schema.get("original_name", name)
        profile = next((source for source in data_profile.get("sources", [])
                        if source.get("name") == name or source.get("source_name") == original), {})
        bindings = revenue_role_bindings(profile, manual.get(original, manual.get(name)),
                                         (proposed_roles or {}).get(name))
        transaction_grain = profile.get("data_grain", "unknown")
        transaction_grain_evidence = list(profile.get("data_grain_evidence", []))
        entity_key = bindings.get("transaction_entity_key", {})
        entity_profile = next((column for column in profile.get("column_profiles", [])
                               if column.get("name") == entity_key.get("column")), {})
        if (transaction_grain == "unknown" and entity_key.get("authority") == "human"
                and entity_profile.get("candidate_key") and not entity_profile.get("null_count")):
            # A run-local Human binding gives a generic profile key its transaction
            # meaning. When that key is complete and unique, the row/value grain is
            # one transaction per row; no physical header alias is needed globally.
            transaction_grain = "order_level"
            transaction_grain_evidence.append(
                "the run-local transaction entity binding is complete and unique across profiled rows"
            )
        monetary = bindings.get("transaction_monetary_value", {})
        if monetary.get("authority") == "human" and monetary.get("basis") == "unspecified" and basis:
            monetary["basis"] = basis
        status = bindings.get("transaction_status", {})
        status_profile = next((column for column in profile.get("column_profiles", [])
                               if column.get("name") == status.get("column")), {})
        values = status_profile.get("value_distribution", {})
        refund_observed = any("refund" in normalize_text(value) or "return" in normalize_text(value) for value in values)
        refund_observed = refund_observed or any(candidate.get("meaning") == "refund"
            for column in profile.get("column_profiles", []) for candidate in column.get("business_role_candidates", []))
        statuses = recognized.get(original, recognized.get(name, []))
        if isinstance(statuses, str):
            statuses = [value.strip() for value in re.split(r"[,|]", statuses) if value.strip()]
        currency_profiles = [column for column in profile.get("column_profiles", []) if any(
            candidate.get("role") == "currency_field" for candidate in column.get("business_role_candidates", []))]
        currency_profile = currency_profiles[0] if len(currency_profiles) == 1 else {}
        currency_codes = [str(value).upper() for value in currency_profile.get("value_distribution", {})]
        currency = str((answers.get("currency_by_file") or {}).get(original,
                       (answers.get("currency_by_file") or {}).get(name, ""))).upper()
        sources.append({
            "name": name, "roles": bindings, "transaction_grain": transaction_grain,
            "transaction_grain_evidence": transaction_grain_evidence,
            "status_present": bool(status) or any(candidate.get("role") == "transaction_status"
                for column in profile.get("column_profiles", []) for candidate in column.get("business_role_candidates", [])),
            "observed_statuses": list(values), "recognized_status_values": [str(value).strip().casefold() for value in statuses],
            "refund_observed": refund_observed,
            "currency": currency or (currency_codes[0] if len(currency_codes) == 1 else ""),
            "currency_column": currency_profile.get("name"), "currency_codes": currency_codes,
            "currency_missing": currency_profile.get("null_count", 0),
            "conversion_rate": (answers.get("conversion_rates") or {}).get(original,
                                (answers.get("conversion_rates") or {}).get(name)),
            "missing_currency": str((answers.get("missing_currency_by_file") or {}).get(original,
                                    (answers.get("missing_currency_by_file") or {}).get(name, ""))).upper(),
        })
    return {"pattern": REVENUE_PATTERN, "sources": sources, "decisions": {
        "measure_basis": basis, "requested_basis": requested_basis,
        "status_policy": status_policy, "refund_treatment": answers.get("refund_treatment", ""),
        "target_currency": str(answers.get("target_currency", "")).upper(),
        "currency_rates": answers.get("currency_rates") or {},
    }, "time_reporting": any(group.get("time_grain") in {"day", "week", "month"}
                                for group in requirements.get("requested_groupings", []) if isinstance(group, dict))}


def _has_explicit_status_filter(request: str, filters: list) -> bool:
    """A model-proposed status predicate is not Human policy unless its value was stated.

    This prevents a broad request for "eligible" transactions from being silently
    converted into a concrete status rule by extraction. Exact predicates remain
    usable when their observed values are present in the user's request.
    """
    request_text = normalize_text(request)
    for predicate in filters:
        if not isinstance(predicate, dict):
            continue
        field = normalize_text(str(predicate.get("field", "")))
        operator = str(predicate.get("operator", "")).casefold().strip()
        field_tokens = {token for token in re.split(r"[^a-z0-9]+", field) if token}
        if not field_tokens.intersection({"status", "state"}) or operator not in {"eq", "ne", "in", "not_in"}:
            continue
        value = predicate.get("value")
        values = value if isinstance(value, (list, tuple, set)) else [value]
        explicit_values = [normalize_text(str(item)).strip() for item in values
                           if isinstance(item, str) and item.strip()]
        if explicit_values and all(re.search(rf"\b{re.escape(item)}\b", request_text)
                                   for item in explicit_values):
            return True
    return False


def revenue_semantic_issues(semantics: dict) -> list[tuple[str, str, str]]:
    """Shared business constraints; callers still validate physical bindings and SQL."""
    issues = []
    decisions = semantics.get("decisions", {})
    if decisions.get("measure_basis") not in {"gross", "net"}:
        issues.append(("revenue_measure_basis", "", "Revenue needs a confirmed gross or net definition"))
    if decisions.get("requested_basis") and decisions.get("measure_basis") != decisions["requested_basis"]:
        issues.append(("revenue_measure_basis", "", "Revenue basis differs from the requested gross/net concept"))
    if not re.fullmatch(r"[A-Z]{3}", decisions.get("target_currency", "")):
        issues.append(("target_currency", "", "Reporting currency is unresolved"))
    for source in semantics.get("sources", []):
        name = source.get("name", "")
        roles = source.get("roles", {})
        amount = roles.get("transaction_monetary_value", {})
        if not amount or amount.get("meaning") != "transaction_value":
            issues.append(("revenue_roles_by_file", name, "Identify transaction monetary value; payment, cost, balance, refund and quantity cannot substitute for sales revenue"))
        elif amount.get("scale") != "transaction_total":
            issues.append(("revenue_roles_by_file", name, "Unit price is not transaction revenue; a confirmed quantity/price transformation is required"))
        elif amount.get("basis") != decisions.get("measure_basis"):
            issues.append(("revenue_roles_by_file", name, "The mapped monetary value does not establish the requested gross/net basis"))
        if amount and amount.get("numeric_parse_ratio", 0) < 0.8:
            issues.append(("revenue_roles_by_file", name, "Monetary role lacks sufficient numeric data evidence"))
        grain = source.get("transaction_grain")
        if grain not in {"order_level", "item_level"} or amount.get("value_grain") != grain:
            issues.append(("revenue_roles_by_file", name, "Transaction/value grain is unknown or incompatible; repeated order totals must not be summed at item/event grain"))
        if not roles.get("transaction_entity_key"):
            issues.append(("revenue_roles_by_file", name, "Transaction entity identity is unresolved; quantity is not a transaction key"))
        event = roles.get("business_event_timestamp", {})
        if semantics.get("time_reporting") and (not event or event.get("meaning") not in {"transaction", "recognition"}
                                                or event.get("date_parse_ratio", 0) < 0.8):
            issues.append(("revenue_roles_by_file", name, "Reporting needs the transaction/recognition event timestamp; payment or shipment time must not be substituted"))
        if source.get("status_present"):
            if not roles.get("transaction_status"):
                issues.append(("revenue_roles_by_file", name, "Map the transaction status role before applying revenue eligibility"))
            if decisions.get("status_policy") not in {"all", "recognized", "requested_filters"}:
                issues.append(("recognized_status_values_by_file", name, "Human must define which transaction states are eligible for revenue"))
            elif decisions.get("status_policy") == "recognized" and not source.get("recognized_status_values"):
                issues.append(("recognized_status_values_by_file", name, "Recognized revenue states are unresolved for this source"))
            if source.get("recognized_status_values") and not set(source["recognized_status_values"]).issubset(
                    {str(value).strip().casefold() for value in source.get("observed_statuses", [])}):
                issues.append(("recognized_status_values_by_file", name, "An eligible status was not observed in the profiled source"))
        if source.get("currency_column"):
            if source.get("currency_missing") and not re.fullmatch(r"[A-Z]{3}", source.get("missing_currency", "")):
                issues.append(("missing_currency_by_file", name, "Some transaction rows have no currency; Human must resolve them"))
            for code in {*source.get("currency_codes", []), *([source["missing_currency"]] if source.get("missing_currency") else [])}:
                if not re.fullmatch(r"[A-Z]{3}", code):
                    issues.append(("currency_by_file", name, "A transaction currency code is invalid or unproven"))
                if code != decisions.get("target_currency") and re.fullmatch(r"[A-Z]{3}", decisions.get("target_currency", "")):
                    rate = decisions.get("currency_rates", {}).get(code)
                    try:
                        valid_rate = 0 < float(rate) < float("inf")
                    except (TypeError, ValueError):
                        valid_rate = False
                    if not valid_rate:
                        issues.append(("currency_rates", code, "A confirmed exchange rate is needed for this transaction currency"))
        elif not re.fullmatch(r"[A-Z]{3}", source.get("currency", "")):
            issues.append(("currency_by_file", name, "Transaction currency is unresolved"))
        elif source["currency"] != decisions.get("target_currency") and re.fullmatch(r"[A-Z]{3}", decisions.get("target_currency", "")):
            try:
                valid_rate = 0 < float(source.get("conversion_rate")) < float("inf")
            except (TypeError, ValueError):
                valid_rate = False
            if not valid_rate:
                issues.append(("conversion_rates", name, "Human must confirm the source-to-reporting currency rate; do not assume parity"))
        if source.get("refund_observed") and decisions.get("refund_treatment") not in {"exclude", "already_net", "include_as_recorded"}:
            issues.append(("refund_treatment", "", "Human must define refund/return treatment; it cannot be inferred from a status label"))
    return list(dict.fromkeys(issues))


def revenue_ambiguity_is_resolved(ambiguity: str, semantics: dict | None) -> bool:
    """Reconcile supported business questions with this run's confirmed roles/rules."""
    if not semantics:
        return False
    text = normalize_text(ambiguity)
    # These choices need their own Human answer; a basis label or date binding
    # does not establish tax, fee, discount, timezone or duplicate policy.
    if re.search(r"\b(tax|vat|fee|fees|discount|discounts|timezone|dedup|duplicate|duplicates|thue|phi|chiet khau)\b", text):
        return False
    checks = (
        (r"\b(gross|net|revenue basis|measure basis|co so doanh thu)\b", {"revenue_measure_basis", "revenue_roles_by_file"}),
        (r"\b(status|statuses|state|states|eligible|eligibility|recognized|recognition|trang thai)\b", {"recognized_status_values_by_file", "revenue_roles_by_file"}),
        (r"\b(currency|currencies|exchange rate|exchange rates|tien te|ty gia)\b", {"currency_by_file", "missing_currency_by_file", "target_currency", "currency_rates", "conversion_rates"}),
        (r"\b(refund|refunds|return|returns|returned|hoan tien|tra hang)\b", {"refund_treatment", "recognized_status_values_by_file"}),
        (r"\b(timestamp|event time|event date|reporting date|transaction date|ngay giao dich)\b", {"revenue_roles_by_file"}),
        (r"\b(grain|transaction identity|transaction key|monetary|payment|payments|unit price|value field|amount field|revenue field|revenue column)\b", {"revenue_roles_by_file"}),
    )
    relevant = set()
    for pattern, keys in checks:
        if re.search(pattern, text):
            relevant.update(keys)
    unresolved = {key for key, _, _ in revenue_semantic_issues(semantics)}
    return bool(relevant) and not (relevant & unresolved)


def revenue_status_filter_is_covered(predicate: dict, semantics: dict | None) -> bool:
    """A confirmed eligible-state predicate is already realized by the revenue policy.

    This matches a filter to each run's semantic status binding and recognized
    values. It does not infer aliases from physical column names or discard a
    predicate whose values narrow the confirmed eligible set.
    """
    if not isinstance(predicate, dict) or not isinstance(semantics, dict):
        return False
    if semantics.get("decisions", {}).get("status_policy") != "recognized":
        return False
    operator = str(predicate.get("operator", "")).casefold().strip()
    if operator not in {"eq", "in"}:
        return False
    field_key = re.sub(r"[^a-z0-9]+", "", normalize_text(str(predicate.get("field", ""))))
    raw_values = predicate.get("value")
    values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
    normalized_values = {normalize_text(str(value)).strip() for value in values
                         if isinstance(value, str) and value.strip()}
    if not normalized_values:
        return False
    for source in semantics.get("sources", []):
        status = source.get("roles", {}).get("transaction_status", {})
        status_column = re.sub(r"[^a-z0-9]+", "", normalize_text(str(status.get("column", ""))))
        recognized = {normalize_text(str(value)).strip()
                      for value in source.get("recognized_status_values", [])}
        if field_key and field_key == status_column and normalized_values.issubset(recognized):
            return True
    return False


def _empty_contract(request: str) -> dict:
    contract = {key: [] for key in CONTRACT_KEYS}
    contract.update({"request": request, "version": 1, "extraction_method": "heuristic"})
    return contract


def _heuristic_contract(request: str, schemas: list[dict], available_sources: list[str]) -> dict:
    text = normalize_text(request)
    contract = _empty_contract(request)
    declaration = re.search(
        r"(?:output[^\n]*[:\n]\s*)?([\w.-]+)\s+(?:gom(?:\s+toi\s+thieu)?|includes?|columns?[: ]|fields?[: ])\s*:?\s*\n"
        r"((?:\s*[-*]\s*[\w.-]+\s*\n?)+)", text
    )
    if declaration:
        # Target field names describe a projection, not separate operations,
        # metrics or grouping intent. Interpret the surrounding request only.
        text = text[:declaration.start()] + text[declaration.end():]

    # Mock mode understands bounded clauses; semantic extraction in live mode
    # remains the LLM's responsibility. Negation has scope within its clause.
    clauses = re.split(r"[\n;.!?]|,\s*(?=(?:khong|do not|don't|never|without)\b)", text)
    negative = re.compile(r"\b(khong|do not|don't|never|without|must not|not)\b")
    positive_text = " ".join(clause for clause in clauses if not negative.search(clause))
    operation_patterns = {
        "union": r"\b(union|append|stack|combine|gop|hop nhat)\b",
        "join": r"\b(join|ghep)\b",
        "filter": r"\b(filter|exclude|remove|drop|loai bo|loai)\b",
        "deduplicate": r"\b(deduplicate|dedup|duplicates|trung lap)\b",
    }
    contract["prohibited_operations"] = [
        operation for operation, pattern in operation_patterns.items()
        if any(negative.search(clause) and re.search(pattern, clause)
               for clause in clauses)
    ]

    operations = []
    if re.search(r"\b(union|append|stack|combine|merge|gop|tong hop|hop nhat)\b", text):
        operations.append("union")
    if re.search(r"\b(join|left join|inner join|ghep)\b", positive_text):
        operations.append("join")
    if re.search(r"\b(filter|where|exclude|only|chi|loai)\b", positive_text):
        operations.append("filter")
    if re.search(r"\b(aggregate|aggregation|summary|summarize|tong hop|tinh)\b", text):
        operations.append("aggregate")
    if re.search(r"\b(deduplicate|dedup|duplicate|duplicates|trung lap)\b", positive_text):
        operations.append("deduplicate")
    if re.search(r"\b(normalize|standardize|chuan hoa)\b", text):
        operations.append("normalize")
    if re.search(r"\b(derive|derived|duration|convert|conversion|chuyen doi)\b", text):
        operations.append("derive")
    if re.search(r"\b(row_number|rank\(|lag\(|lead\(|window|rolling|luy ke|xep hang)\b", text):
        operations.append("window")
    contract["requested_operations"] = list(dict.fromkeys(
        operation for operation in operations if operation not in contract["prohibited_operations"]
    ))

    schema_names = [schema.get("original_name", schema.get("name", "")) for schema in schemas]
    mentioned = []
    for schema, name in zip(schemas, schema_names):
        stem = normalize_text(Path(name).stem)
        source_label = stem.split("_")[-1]
        if stem and (stem in text or (len(source_label) > 2 and re.search(rf"\b{re.escape(source_label)}\b", text))):
            mentioned.append(name)
    if not schemas:
        mentioned = [source for source in available_sources
                     if re.search(rf"\b{re.escape(normalize_text(source))}\b", text)]
    if mentioned:
        contract["requested_sources"] = mentioned
    elif len(schema_names) == 1:
        contract["requested_sources"] = schema_names
    elif schema_names and any(token in text for token in ("all sources", "both", "each file", "every source", "2 file", "hai bang", "hai file", "2 bang")):
        contract["requested_sources"] = schema_names
    elif schema_names and "union" in operations:
        contract["requested_sources"] = schema_names
    elif not schemas and any(token in text for token in ("all sources", "both", "each source", "gop", "tong hop", "union")):
        contract["requested_sources"] = list(available_sources)

    known_fields = {
        "status": "status", "order_status": "status", "trip_status": "status",
        "amount": "amount", "total_amount": "amount", "revenue": "amount",
        "date": "date", "order_date": "date", "created_at": "date",
        "id": "id", "order_id": "id", "trip_id": "id",
        "platform": "platform", "customer_id": "customer_id",
    }
    for schema in schemas:
        for column in schema.get("columns", []):
            key = re.sub(r"[^a-z0-9]+", "_", normalize_text(column)).strip("_")
            if key:
                known_fields.setdefault(key, key)
    parsed_filters = []
    for field_name, semantic in sorted(known_fields.items(), key=lambda item: len(item[0]), reverse=True):
        escaped = re.escape(field_name)
        pattern = re.compile(
            rf"\b{escaped}\s*(is not|is|equals|equal to|>=|<=|!=|<>|==|=|>|<)\s*(?:'([^']*)'|\"([^\"]*)\"|([\w./:-]+))",
            re.I,
        )
        for match in pattern.finditer(text):
            operator = match.group(1).casefold()
            value = next((group for group in match.groups()[1:] if group is not None), "")
            operator = {"is": "eq", "equals": "eq", "equal to": "eq", "is not": "ne", "==": "eq", "=": "eq", "!=": "ne", "<>": "ne"}.get(operator, operator)
            parsed_filters.append({"field": semantic, "operator": operator, "value": value})
    if "platform" in known_fields:
        for match in re.finditer(r"\b(?:for|where)\s+platform\s+([a-z0-9_-]+)\b", text):
            parsed_filters.append({"field": "platform", "operator": "eq", "value": match.group(1)})
    for match in re.finditer(r"\b(?:only|status\s+(?:is\s+)?)\s*(completed|complete|delivered|done|cancelled|canceled|returned)\b", text):
        parsed_filters.append({"field": "status", "operator": "eq", "value": match.group(1)})
    contract["requested_filters"] = list({
        (item["field"], item["operator"], str(item["value"])): item for item in parsed_filters
    }.values())
    if "filter" in operations and not contract["requested_filters"]:
        contract["unsupported_requirements"].append("Filter predicate could not be extracted without guessing fields or values")

    metric_terms = {
        "record_count": (r"\b(total orders|total trips|order count|trip count|count of orders|number of orders|so don|so chuyen)\b",),
        "distinct_order_count": (r"\b(count distinct|distinct orders|unique orders|unique trips|don hang duy nhat)\b",),
        "completed_count": (r"\b(completed orders|completed trips|completed_count|so don hoan thanh|so chuyen hoan thanh)\b",),
        "cancelled_count": (r"\b(cancelled orders|canceled orders|cancelled trips|cancelled_count|so don huy|so chuyen huy)\b",),
        "total_amount": (r"\b(total revenue|total_amount|sum of amount|sum of revenue|tong doanh thu|tong amount|total fare)\b",),
        "average_amount": (r"\b(average order value|average revenue|average_amount|avg amount|aov|doanh thu trung binh|gia tri don hang trung binh)\b",),
        "min_amount": (r"\b(min order value|minimum order value|min_amount|minimum amount|gia tri don nho nhat)\b",),
        "max_amount": (r"\b(max order value|maximum order value|max_amount|maximum amount|gia tri don lon nhat)\b",),
        "median_amount": (r"\b(median|trung vi)\b",),
        "completion_rate": (r"\b(completion rate|completion_rate|ty le hoan thanh)\b",),
        "cancellation_rate": (r"\b(cancellation rate|cancellation_rate|ty le huy)\b",),
        "average_distance": (r"\b(average distance|mean distance|avg_distance|quang duong trung binh)\b",),
        "average_duration": (r"\b(average duration|mean duration|avg_duration|thoi luong trung binh|thoi gian chuyen trung binh)\b",),
        "sum_value": (r"\b(total quantity|sum_value|sum of quantity|tong so luong)\b",),
        "customer_lifetime_value": (r"\b(customer lifetime value|clv|gia tri vong doi khach hang)\b",),
        "running_revenue": (r"\b(running revenue|cumulative revenue|doanh thu luy ke)\b",),
        "customer_rank": (r"\b(rank customers|customer ranking|xep hang khach hang)\b",),
    }
    for metric, patterns in metric_terms.items():
        if any(re.search(pattern, text) for pattern in patterns):
            contract["requested_metrics"].append(metric)
    plain_words = re.sub(r"[^a-z0-9]+", " ", text)
    for metric, aliases in METRIC_ALIASES.items():
        for alias in aliases:
            if alias in {"count", "revenue"}:
                continue
            phrase = re.sub(r"[^a-z0-9]+", " ", alias).strip()
            if phrase and re.search(rf"\b{re.escape(phrase)}\b", plain_words):
                contract["requested_metrics"].append(metric)
                break
    contract["requested_metrics"] = list(dict.fromkeys(contract["requested_metrics"]))
    if is_revenue_request(request, contract) and not contract["requested_metrics"] and re.search(r"\b(total|sum|tong)\b", text):
        contract["requested_metrics"].append("total_amount")
    if not contract["requested_metrics"]:
        if re.search(r"\b(median|trung vi)\b", text):
            contract["requested_metrics"].append("median_amount")
        elif re.search(r"\b(count|number of|dem so|so luong)\b", text):
            contract["requested_metrics"].append("record_count")

    time_grain = next((grain for grain, patterns in {
        "day": (r"\b(daily|per day|by day|theo ngay|hang ngay|moi ngay)\b",),
        "week": (r"\b(weekly|per week|by week|theo tuan|hang tuan)\b",),
        "month": (r"\b(monthly|per month|by month|theo thang|hang thang)\b",),
    }.items() if any(re.search(pattern, text) for pattern in patterns)), None)
    dimensions = []
    if re.search(r"\b(platform|per platform|by platform|theo nen tang)\b", text):
        dimensions.append("platform")
    if re.search(r"\b(customer|customer_id|by customer|per customer|theo khach hang)\b", text):
        dimensions.append("customer_id")
    if re.search(r"\b(product|by product|per product|theo san pham)\b", text):
        dimensions.append("product")
    if schemas:
        grouping_text = " ".join(re.findall(r"(?:\bgroup\s+by|\bby|\bper|\btheo)\s+([^.;\n]+)", text))
        for schema in schemas:
            for column in schema.get("columns", []):
                normalized = re.sub(r"[^a-z0-9]+", "", normalize_text(column))
                if normalized and re.search(rf"\b{re.escape(normalized)}\b", re.sub(r"[^a-z0-9]+", " ", grouping_text)):
                    if normalized not in {"id", "amount", "date", "status"} and column not in dimensions:
                        dimensions.append(column)
    if time_grain or dimensions:
        contract["requested_groupings"] = [{"time_grain": time_grain or "all", "dimensions": dimensions}]

    filenames = re.findall(r"(?i)\b([a-z0-9_.-]+\.(?:csv|json|parquet))\b", request)
    filenames = [name for name in filenames if name not in schema_names]
    contract["requested_output_files"] = [name.strip() for name in filenames]
    if not contract["requested_output_files"]:
        named_outputs = re.findall(r"\b([a-z][a-z0-9_]*(?:summary|report|orders|output))\b", text)
        if named_outputs:
            contract["requested_output_files"] = list(dict.fromkeys(named_outputs))

    # Structural output declarations, independent of business terms or filenames.
    if declaration:
        output_name = declaration.group(1)
        fields = re.findall(r"[-*]\s*([\w.-]+)", declaration.group(2))
        contract["requested_outputs"] = [{"name": output_name, "kind": "rows", "required_fields": fields}]
        contract["requested_output_files"] = [output_name]

    cleaning_patterns = {
        "missing_values": r"\b(missing|null|blank|empty|gia tri thieu|rong)\b",
        "duplicates": r"\b(duplicate|duplicates|dedup|trung lap)\b",
        "invalid_types": r"\b(invalid|malformed|type|cast|datatype|sai kieu)\b",
        "status_normalization": r"\b(status|normalize status|chuan hoa trang thai)\b",
    }
    contract["requested_cleaning_steps"] = [name for name, pattern in cleaning_patterns.items() if re.search(pattern, text)]
    transform_patterns = {
        "unit_conversion": r"\b(convert|conversion|unit|meter|kilometer|km|don vi|chuyen doi)\b",
        "date_normalization": r"\b(date|datetime|timezone|timestamp|ngay|gio)\b",
        "derived_field": r"\b(derived|duration|difference|derive|tinh thoi gian)\b",
        "status_normalization": r"\b(status|completed|cancelled|canceled|hoan thanh|huy)\b",
        "window_function": r"\b(row_number|rank\(|lag\(|lead\(|window|rolling|running total|luy ke|xep hang)\b",
    }
    contract["requested_transformations"] = [name for name, pattern in transform_patterns.items() if re.search(pattern, text)]
    if re.search(r"\b(runtime|peak memory|memory usage|thoi gian chay|bo nho)\b", text):
        contract["requested_performance_metrics"] = [
            metric for metric, pattern in (("runtime_seconds", r"\b(runtime|thoi gian chay)\b"),
                                           ("peak_memory_mb", r"\b(peak memory|memory usage|bo nho)\b"))
            if re.search(pattern, text)
        ]
    if re.search(r"\bgross\b", text) and re.search(r"\bnet\b", text) and re.search(r"\b(revenue|amount|doanh thu)\b", text):
        contract["ambiguities"].append("Revenue basis (gross or net) is not specified.")
    custom_metrics = re.findall(r"\bmetric[_ -]?custom[_ -]?\w+\b", text)
    contract["unsupported_requirements"].extend(custom_metrics)
    return contract


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("Requirement extractor must return a JSON object")
    return value


def _normalize_contract_collections(contract: dict) -> None:
    """Normalize list-typed contract fields without interpreting their contents."""
    for key in CONTRACT_KEYS:
        value = contract.get(key)
        if value is None:
            contract[key] = []
        elif isinstance(value, list):
            continue
        elif isinstance(value, str):
            contract[key] = [value]
        else:
            raise ValueError(
                f"Requirement contract key {key!r} must be a list or string; "
                f"received {type(value).__name__}"
            )
    # Ambiguities are user-facing decision text. Accept a structured draft
    # item only when it contains a text field for that same decision, then
    # normalize it to the prompt's string-list interface without losing its
    # domain label.
    normalized_ambiguities = []
    for item in contract.get("ambiguities", []):
        if isinstance(item, str):
            normalized_ambiguities.append(item)
        elif isinstance(item, dict):
            detail = next((item.get(key) for key in ("ambiguity", "issue", "question", "description")
                           if isinstance(item.get(key), str) and item[key].strip()), None)
            if not detail:
                raise ValueError("Requirement contract ambiguities must be text or objects with decision text")
            domain = next((item.get(key) for key in ("field", "domain", "rule")
                           if isinstance(item.get(key), str) and item[key].strip()), "")
            normalized_ambiguities.append(f"{domain}: {detail}" if domain else detail)
        else:
            raise ValueError("Requirement contract ambiguities must contain text entries")
    contract["ambiguities"] = normalized_ambiguities


def _contract_consistency_issues(contract: dict) -> list[str]:
    """Detect contradictory output interfaces without rewriting their meaning."""
    outputs = contract.get("requested_outputs") or []
    if not isinstance(outputs, list) or not outputs:
        return []
    rows_only = all(isinstance(output, dict) and output.get("kind") == "rows" for output in outputs)
    transformations = contract.get("requested_transformations") or []
    has_formula = isinstance(transformations, list) and any(
        isinstance(item, dict) and item.get("type") == "aggregate_formula"
        for item in transformations
    )
    if (rows_only and contract.get("requested_groupings")
            and not contract.get("requested_metrics") and not has_formula):
        return [
            "Rows-only outputs declare aggregate grouping without an aggregate measure or output. "
            "Projection attributes belong in requested_outputs.required_fields. Review the exact request: "
            "retain any genuine summary intent as an aggregate output, or remove grouping inferred only "
            "from the projection schema."
        ]
    return []


def extract_requirement_contract(request: str, provider: str, schemas: list[dict], available_sources: list[str],
                                 context: dict | None = None) -> dict:
    if provider == "mock":
        contract = _heuristic_contract(request, schemas, available_sources)
    else:
        # In custom CSV mode, the uploaded schemas identify the usable sources.
        # Do not expose the ingestion-mode label (for example, "custom_csv") as
        # though it were a physical source file the Planner may select.
        schema_sources = list(dict.fromkeys(
            schema.get("original_name") or schema.get("name", "")
            for schema in schemas if schema.get("original_name") or schema.get("name")
        ))
        prompt = json.dumps({
            "request": request,
            "available_sources": schema_sources or available_sources,
            "uploaded_schemas": schemas,
            "input_context": context or {},
        }, ensure_ascii=False)
        contract = _parse_json(generate_text(
            provider, prompt, system_prompt=load_system_prompt("prompt_requirements")
        ))
        # Lossless semantic extraction is separate from the shape normalizer.
        # Review against the user's intent and existing evidence within this
        # same Agent; never repair semantics with metric/column-specific code.
        review_context = json.loads(prompt)
        review_context["draft_requirement_contract"] = contract
        review_context["contract_consistency_issues"] = _contract_consistency_issues(contract)
        review_context["review_instruction"] = (
            "Review the draft against the exact user request and confirmed input context. Return a complete "
            "lossless contract. Remove inferred requirements that were not requested. A temporal reporting grain "
            "uses the event timestamp as an input binding, not an extra grouping dimension unless separately "
            "requested. Descriptions of metrics are not output filenames or target column names. Preserve all "
            "explicit target fields, operation prohibitions, metrics, formula scopes and separate outputs. Classify "
            "an output with grouped metrics/formulas as aggregate, and a projection preserving input records as rows. "
            "If a requested metric has an explicit formula, express it exactly as {type: aggregate_formula, name, "
            "operation, numerator: {metric, dimensions: []}, denominator: {metric, dimensions: []}} using operation "
            "add/subtract/multiply/divide. Convert a draft free-text derive formula into that structured form; do "
            "not leave its meaning only in assumptions. Do not introduce "
            "physical column bindings; those belong to Planner. Resolve the supplied consistency issues "
            "against user intent, not by discarding requested operations. An explicit list of output attributes "
            "is a projection schema, not a GROUP BY clause. A row-preserving output has no aggregate grouping; "
            "if the user also requests a summary, preserve it as a separate aggregate output. "
            "Do not reopen confirmed choices."
        )
        contract = _parse_json(generate_text(provider, json.dumps(review_context, ensure_ascii=False),
                                           system_prompt=load_system_prompt("prompt_requirements")))
        contract.update({"request": request, "version": 1, "extraction_method": provider})
    _normalize_contract_collections(contract)
    for key in CONTRACT_KEYS:
        if not isinstance(contract.get(key), list):
            raise ValueError(f"Requirement contract key {key!r} must be a list")
    formula_operations = {"add", "subtract", "multiply", "divide"}
    for transformation in contract["requested_transformations"]:
        if not isinstance(transformation, dict) or transformation.get("type") != "aggregate_formula":
            continue
        if (not isinstance(transformation.get("name"), str) or not transformation["name"].strip()
                or transformation.get("operation") not in formula_operations):
            raise ValueError(
                "Aggregate formula transformations need a name and supported binary operation; "
                f"received {transformation!r}"
            )
        for operand_name in ("numerator", "denominator"):
            operand = transformation.get(operand_name)
            if not isinstance(operand, dict) or not isinstance(operand.get("metric"), str) or not operand["metric"].strip():
                raise ValueError(f"Aggregate formula {operand_name} needs a semantic metric")
            dimensions = operand.get("dimensions", [])
            if (not isinstance(dimensions, list)
                    or any(not isinstance(dimension, str) or not dimension.strip() for dimension in dimensions)):
                raise ValueError(f"Aggregate formula {operand_name} dimensions must be a list of names")
    if any(not isinstance(group, dict) for group in contract["requested_groupings"]):
        raise ValueError("Requested groupings must be structured objects")
    if any(not isinstance(operation, str) for operation in contract["prohibited_operations"]):
        raise ValueError("Prohibited operations must be strings")
    for output in contract["requested_outputs"]:
        if not isinstance(output, dict) or not isinstance(output.get("name"), str) or not output["name"]:
            raise ValueError("Requested outputs need structured objects with a nonempty name")
        if output.get("kind") not in {"rows", "aggregate"}:
            raise ValueError("Requested outputs need an explicit rows/aggregate kind")
        fields = output.get("required_fields", [])
        if fields is None:
            fields = []
        elif isinstance(fields, str):
            fields = [fields]
        if not isinstance(fields, list) or any(not isinstance(field, str) or not field for field in fields):
            raise ValueError("Output required_fields must contain nonempty strings")
        output["required_fields"] = fields
        if output["kind"] == "rows" and not fields:
            raise ValueError("Row output requirements need explicit required_fields")
    consistency_issues = _contract_consistency_issues(contract)
    if consistency_issues:
        raise ValueError("Requirement contract is inconsistent: " + "; ".join(consistency_issues))
    if contract["requested_outputs"] and not contract["requested_output_files"]:
        contract["requested_output_files"] = [output["name"] for output in contract["requested_outputs"]]
    if any(not isinstance(predicate, dict) or not all(key in predicate for key in ("field", "operator", "value"))
           for predicate in contract["requested_filters"]):
        raise ValueError("Requested filters need explicit field/operator/value; unresolved business eligibility belongs in ambiguities")
    return contract


def run(state: dict) -> dict:
    request = str(state.get("request", "")).strip()
    if not request:
        return {"status": "failed", "error": "Requirement Extraction received an empty request"}
    try:
        schemas = json.loads(os.getenv("FLOWFORGE_CUSTOM_CSV_SCHEMAS", "[]"))
        available = [value for value in os.getenv("FLOWFORGE_AVAILABLE_SOURCES", "shopee,tiki,website").split(",") if value]
        profile = state.get("data_profile") or {}
        # Share bounded evidence already available in this flow, without sending
        # raw CSV samples. Interface extraction must see confirmed decisions.
        source_keys = ("name", "source_name", "row_count", "data_grain", "data_grain_evidence",
                       "date_format_candidate", "number_format_candidate")
        column_keys = ("name", "inferred_type", "null_count", "distinct_count", "unique_ratio",
                       "semantic_candidates", "business_role_candidates", "date_parse_ratio",
                       "numeric_parse_ratio", "date_format_candidate", "number_format_candidate")
        evidence = [{**{key: source.get(key) for key in source_keys}, "column_profiles": [
            {key: column.get(key) for key in column_keys}
            for column in source.get("column_profiles", [])]} for source in profile.get("sources", [])]
        contract = extract_requirement_contract(request, state.get("provider", "mock"), schemas, available,
            {"data_profile": evidence, "confirmed_answers": state.get("clarification") or {},
             "resolved_business_rules": state.get("resolved_business_rules") or {}})
        resolution = state.get("ambiguity_resolution", {})
        resolved_rules = resolution.get("resolved_business_rules", {})
        if resolved_rules:
            resolved = []
            for ambiguity in contract.get("ambiguities", []):
                normalized = str(ambiguity).casefold()
                if "gross" in normalized and "net" in normalized and resolved_rules.get("revenue_basis"):
                    continue
                if "returned" in normalized and "cancel" in normalized and resolved_rules.get("returned_as_cancelled") is not None:
                    continue
                resolved.append(ambiguity)
            contract["ambiguities"] = resolved
        return {"requirement_contract": contract, "status": "requirements_extracted"}
    except Exception as error:
        return {"status": "failed", "error": f"Requirement extraction failed: {type(error).__name__}: {error}"}
