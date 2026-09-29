"""Classify profile findings into deterministic, defaultable, or business ambiguity."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from agents.schema_inspection import DEFAULT_RULES, normalize_name
from agents.requirement_contract import (
    build_revenue_semantics, canonical_metric, is_revenue_request,
    revenue_ambiguity_is_resolved, revenue_semantic_issues,
)


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _profile_sources(data_profile: dict) -> list[dict]:
    return [item for item in data_profile.get("sources", []) if isinstance(item, dict)]


def _columns(source: dict) -> list[dict]:
    return [item for item in source.get("column_profiles", []) if isinstance(item, dict)]


def _has_term(request: str, *terms: str) -> bool:
    text = _plain(request)
    return any(_plain(term) in text for term in terms)


def _contract_values(contract: dict, *keys: str) -> list[str]:
    values = []
    for key in keys:
        value = contract.get(key, [])
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    values.extend(str(part) for part in item.values() if isinstance(part, (str, int, float)))
                    values.extend(str(part) for part in item.get("required_fields", []) if isinstance(part, str))
                elif isinstance(item, str):
                    values.append(item)
    return values


def _mentions_semantic_role(values: list[str], *roles: str) -> bool:
    text = " ".join(_plain(value) for value in values)
    compact = re.sub(r"[^a-z0-9]+", "_", text)
    return any(re.search(rf"(?:^|_){re.escape(_plain(role).replace(' ', '_'))}(?:_|$)", compact)
               for role in roles)


def _requested_data_value_scope(contract: dict, request: str = "") -> tuple[bool, bool]:
    """Return whether input numeric parsing and currency resolution are required.

    This scopes data checks from extracted metrics, output fields and explicit
    transformations. It does not infer a business metric from a source header
    or from a negative mention in the prose request.
    """
    metric_values = _contract_values(contract, "requested_metrics", "requested_validation_checks")
    output_values = _contract_values(contract, "requested_outputs")
    transform_values = _contract_values(contract, "requested_transformations", "requested_operations")
    all_values = metric_values + output_values + transform_values
    numeric_roles = (
        "amount", "monetary", "money", "revenue", "sales", "payment", "cost", "profit", "margin",
        "price", "balance", "refund", "quantity", "value", "distance", "duration",
    )
    financial_roles = (
        "amount", "monetary", "money", "revenue", "sales", "payment", "cost", "profit", "margin",
        "price", "balance", "refund", "currency",
    )
    needs_numeric_parse = _mentions_semantic_role(all_values, *numeric_roles) or _mentions_semantic_role(
        transform_values, "numeric", "number", "parse"
    )
    needs_currency = _mentions_semantic_role(metric_values, *financial_roles) or _mentions_semantic_role(
        transform_values, "currency", "exchange rate", "currency conversion"
    )
    # A generic unit conversion can mean distance, time, or currency. Use the
    # explicit target phrase only when the contract also contains a conversion.
    conversion_requested = _mentions_semantic_role(transform_values, "conversion", "convert", "unit_conversion")
    if conversion_requested and _has_term(request, "currency", "exchange rate", "reporting currency"):
        needs_currency = True
    needs_numeric_parse = needs_numeric_parse or needs_currency
    return needs_numeric_parse, needs_currency


def _business_answer_map(state: dict) -> dict:
    resolved = dict(state.get("resolved_business_rules") or {})
    clarification = state.get("clarification") or {}
    nested = clarification.get("resolved_business_rules") or {}
    if isinstance(nested, dict):
        resolved.update(nested)
    for key in (
        "returned_as_cancelled", "refunded_in_revenue", "revenue_basis", "revenue_basis_by_file",
        "dedup_policy", "cross_source_identity", "completed_status_values_by_file",
        "cancelled_status_values_by_file", "source_platforms",
        "revenue_roles_by_file", "revenue_measure_basis", "revenue_status_policy",
        "recognized_status_values_by_file", "refund_treatment",
    ):
        if clarification.get(key) not in (None, ""):
            resolved[key] = clarification[key]
    return resolved


def _parse_source_platform_answers(raw, sources: list[dict]) -> dict[str, str]:
    if isinstance(raw, dict):
        values = raw
    else:
        values = {}
        for part in re.split(r"[;\n]+", str(raw or "")):
            if "=" not in part:
                continue
            name, label = part.split("=", 1)
            values[name.strip()] = label.strip()
    result = {}
    for source in sources:
        name = str(source.get("source_name", source.get("name", "")))
        short_name = str(source.get("name", ""))
        label = values.get(name) or values.get(short_name)
        if isinstance(label, str) and label.strip():
            result[name] = label.strip()
    return result


def _parse_revenue_basis_answers(raw, sources: list[dict]) -> dict[str, str]:
    if isinstance(raw, dict):
        values = raw
    else:
        values = {}
        for part in re.split(r"[;\n]+", str(raw or "")):
            if "=" not in part:
                continue
            name, basis = part.split("=", 1)
            values[name.strip()] = basis.strip()
    result = {}
    for source in sources:
        name = str(source.get("source_name", source.get("name", "")))
        short_name = str(source.get("name", ""))
        basis = str(values.get(name) or values.get(short_name) or "").strip().casefold()
        if basis in {"gross_amount", "net_amount"}:
            result[name] = basis
    return result


def _contract_rule_key(text: str) -> str:
    normalized = _plain(text)
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")[:36] or "unlabeled"
    return f"requirement_ambiguity_{slug}"


def _ambiguity_domains(value: str) -> set[str]:
    """Group paraphrased contract issues with the generic checks that resolve them."""
    text = re.sub(r"[_-]+", " ", _plain(value))
    domains = set()
    patterns = {
        "status_policy": r"\b(status|state|eligib(?:le|ility)|valid(?:ity)?|invalid|recognized|completed|cancelled|canceled)\b",
        "source_identity": r"\b(platform|source label|source identity|derive.*source|source.*mapping)\b",
        "currency": r"\b(currency|currencies|exchange rate|amount scale|money unit)\b",
        "measure_basis": r"\b(gross|net|basis)\b",
        "measure_semantics": r"\b(revenue|sales|payment|cost|price|amount|balance|refund|monetary|profit|margin)\b",
        "numeric_notation": r"\b(decimal|thousands|separator|number format|numeric format)\b",
        "refund_policy": r"\b(refund|refunded|return|returned)\b",
        "data_grain": r"\b(grain|row level|line item|item level|order level|transaction level)\b",
        "entity_identity": r"\b(entity key|transaction key|order key|identifier|identity)\b",
        "event_time": r"\b(timestamp|event time|recognition time|reporting date)\b",
        "duplicate_policy": r"\b(duplicate|dedup|deduplicate|repeated key)\b",
        "data_operation": r"\b(append|union|join|merge|operation)\b",
        "schema_mapping": r"\b(column mapping|schema mapping|mapping of columns|map columns|unified naming|field mapping|normalization transformation)\b",
    }
    for domain, pattern in patterns.items():
        if re.search(pattern, text):
            domains.add(domain)
    return domains


def _yes(value) -> bool | None:
    normalized = _plain(str(value or "")).strip()
    if normalized in {"yes", "true", "1", "cancelled", "include", "gross", "net"}:
        return True
    if normalized in {"no", "false", "0", "keep_separate", "exclude", "preserve"}:
        return False
    return None


def _column(source: dict, names: set[str]) -> dict | None:
    return next((item for item in _columns(source) if item.get("normalized_name") in names), None)


def _parse_status_answers(raw, sources: list[dict]) -> dict[str, list[str]]:
    if isinstance(raw, dict):
        result = {}
        for name, values in raw.items():
            if isinstance(values, (list, tuple, set)):
                result[str(name)] = [str(value).strip().casefold() for value in values if str(value).strip()]
            else:
                result[str(name)] = ([] if str(values).strip().casefold() == "none" else
                                     [part.strip().casefold() for part in re.split(r"[,;|]", str(values)) if part.strip()])
        return result
    text = str(raw or "").strip()
    if not text:
        return {}
    names = [str(source.get("source_name", source.get("name", ""))) for source in sources]
    result: dict[str, list[str]] = {}
    for part in re.split(r"[;\n]+", text):
        if "=" in part:
            name, values = part.split("=", 1)
        elif ":" in part:
            name, values = part.split(":", 1)
        elif len(names) == 1:
            name, values = names[0], part
        else:
            continue
        source_name = next((candidate for candidate in names if candidate.casefold() == name.strip().casefold()), None)
        if source_name:
            result[source_name] = ([] if values.strip().casefold() == "none" else
                                   [value.strip().casefold() for value in re.split(r"[,|]", values) if value.strip()])
    return result


def _entry(issue: str, decision: str, source: str, confidence: float, evidence: list[str]) -> dict:
    return {"issue": issue, "decision": decision, "source": source, "confidence": confidence, "evidence": evidence}


def resolve_ambiguities(state: dict) -> dict:
    request = str(state.get("request", ""))
    profile = state.get("data_profile") or {"sources": []}
    sources = _profile_sources(profile)
    clarification = state.get("clarification") or {}
    rules = _business_answer_map(state)
    auto: list[dict] = []
    defaults: list[dict] = []
    ambiguous: list[dict] = []
    questions: list[str] = []
    fields: list[dict] = []
    warnings: list[str] = []
    number_format_questions: set[str] = set()
    contract = state.get("requirement_contract") or {}
    numeric_scope_requested, currency_scope_requested = _requested_data_value_scope(contract, request)
    try:
        schemas = json.loads(os.getenv("FLOWFORGE_CUSTOM_CSV_SCHEMAS", "[]"))
    except json.JSONDecodeError:
        schemas = []
    revenue_mode = bool(schemas) and is_revenue_request(request, contract)
    numeric_scope_requested = numeric_scope_requested or revenue_mode
    currency_scope_requested = currency_scope_requested or revenue_mode
    revenue_semantics = (build_revenue_semantics(request, contract, schemas, profile, clarification, rules)
                         if revenue_mode else None)
    requested_groups = contract.get("requested_groupings", [])
    asks_platform = _has_term(request, "platform", "per platform", "by platform", "nen tang") or any(
        isinstance(group, dict) and any("platform" in _plain(str(dimension))
                                        for dimension in group.get("dimensions", []))
        for group in requested_groups
    )
    request_evidence = _plain(request + " " + " ".join(map(str, contract.get("requested_sources", []))))
    platform_answers = _parse_source_platform_answers(rules.get("source_platforms"), sources)
    source_platforms: dict[str, str] = {}

    # A filename suffix can suggest an identity, but only an explicit platform
    # field, named source label, or user confirmation establishes its meaning.
    for source in sources:
        if source.get("profile_truncated"):
            warnings.append(
                f"{source.get('source_name', source.get('name', 'source'))}: grain analysis used the first 100,000 rows"
            )
        candidate = source.get("source_identity_candidate") or {}
        source_name = str(source.get("source_name", source.get("name", "")))
        candidate_label = str(candidate.get("value") or "").strip()
        explicitly_named = bool(candidate_label and re.search(
            rf"\b{re.escape(_plain(candidate_label))}\b", request_evidence
        ))
        confirmed_label = platform_answers.get(source_name)
        platform_column = _column(source, {"platform"})
        if confirmed_label:
            # Preserve the user's chosen display value and let an explicit
            # per-file mapping override a filename-derived suggestion.
            source_platforms[source_name] = confirmed_label
        elif candidate_label and explicitly_named:
            source_platforms[source_name] = candidate_label
            auto.append(_entry(
                "source_identity", candidate_label, source_name,
                float(candidate.get("confidence", 0.93)), candidate.get("evidence", []) + [
                    "the request explicitly names this source label"
                ],
            ))
        elif platform_column:
            auto.append(_entry("platform_field", platform_column.get("name"), source_name, 0.99,
                               ["an explicit platform column exists in the source schema"]))
        for column in _columns(source):
            normalized = column.get("normalized_name", "")
            if column.get("trim_whitespace_count", 0) > 0:
                auto.append(_entry(
                    "trim_text", "trim leading/trailing whitespace",
                    f"{source.get('source_name')}.{column.get('name')}", 0.99,
                    [f"{column.get('trim_whitespace_count')} values contain surrounding whitespace"],
                ))
            if column.get("date_format_candidate") in {"iso", "day_first", "month_first"}:
                auto.append(_entry(
                    "date_format", column["date_format_candidate"],
                    f"{source.get('source_name')}.{column.get('name')}", 0.96,
                    column.get("date_format_evidence", []),
                ))
            if column.get("number_format_candidate") in {"thousands", "decimal"}:
                auto.append(_entry(
                    "numeric_format", column["number_format_candidate"],
                    f"{source.get('source_name')}.{column.get('name')}", 0.95,
                    column.get("number_format_evidence", []),
                ))
            if column.get("number_format_candidate") == "ambiguous_separator" and numeric_scope_requested:
                source_name = str(source.get("source_name", source.get("name", "")))
                answer = (clarification.get("number_format_by_file") or {}).get(source_name)
                if answer not in {"thousands", "decimal"}:
                    questions.append(
                        f"Các giá trị số trong {source_name} có một dấu phân cách nên có thể là hàng nghìn hoặc thập phân. Chọn cách đọc để tính metric: thousands hay decimal?"
                    )
                    fields.append({"key": "number_format_by_file", "file": source_name,
                                   "type": "select", "options": ["thousands", "decimal"]})
                    number_format_questions.add(source_name)
            if normalized in {"status", "orderstatus", "orderstate", "state"} or normalized.endswith("status"):
                auto.append(_entry(
                    "status_text_normalization", "trim whitespace and compare case-insensitively",
                    f"{source.get('source_name')}.{column.get('name')}", 0.99,
                    ["status is a categorical text field", "normalization does not map one business state to another"],
                ))
            if column.get("null_ratio", 0) > 0:
                defaults.append(_entry(
                    "empty_string_to_null", DEFAULT_RULES["empty_string_to_null"],
                    f"{source.get('source_name')}.{column.get('name')}", 0.99,
                    [f"{column.get('null_count')} empty values in {column.get('row_count')} rows"],
                ))
            semantics = {item.get("semantic_field"): item for item in column.get("semantic_candidates", [])}
            if "customer_name" in semantics and column.get("null_count", 0) > 0:
                defaults.append(_entry(
                    "missing_descriptive_field", DEFAULT_RULES["missing_descriptive_field"],
                    f"{source.get('source_name')}.{column.get('name')}",
                    float(semantics["customer_name"].get("confidence", 0.90)),
                    ["preserve source missingness unless the user requests imputation",
                     f"{column.get('null_count')} missing values"],
                ))
            numeric_semantics = {"amount", "gross_amount", "net_amount", "quantity", "distance_km", "start_time", "end_time"}
            if semantics.keys() & numeric_semantics and column.get("invalid_numeric_count", 0) > 0:
                defaults.append(_entry(
                    "invalid_numeric", DEFAULT_RULES["invalid_numeric"],
                    f"{source.get('source_name')}.{column.get('name')}", 0.95,
                    [f"{column.get('invalid_numeric_count')} values do not parse as numeric"],
                ))
            if column.get("invalid_date_count", 0) > 0 and (
                "order_date" in semantics or any(token in normalized for token in ("date", "time", "timestamp"))
            ):
                defaults.append(_entry(
                    "invalid_date", DEFAULT_RULES["invalid_date"],
                    f"{source.get('source_name')}.{column.get('name')}", 0.95,
                    [f"{column.get('invalid_date_count')} values do not parse as dates/timestamps"],
                ))
            if semantics.get("distance_km") and normalize_name(column.get("name", "")) in {"distancemeter", "distancemeters"}:
                auto.append(_entry(
                    "distance_unit_conversion", "distance_km = distance_meter / 1000",
                    f"{source.get('source_name')}.{column.get('name')}", 0.99,
                    ["unit is explicit in the source column name"],
                ))
        names = {item.get("normalized_name") for item in _columns(source)}
        wants_duration = _has_term(request, "duration", "average duration", "thoi gian chuyen", "thoi luong")
        if {"starttime", "endtime"}.issubset(names) and wants_duration:
            auto.append(_entry(
                "duration_derivation", "duration = end_time - start_time, measured in seconds",
                source.get("source_name", ""), 0.93,
                ["both start and end timestamps are present"],
            ))
            defaults.append(_entry(
                "derived_duration_unit", DEFAULT_RULES["derived_duration_unit"], source.get("source_name", ""),
                0.95, ["elapsed timestamp differences use the configured default unit"],
            ))
        if source.get("exact_duplicate_row_count", 0):
            defaults.append(_entry(
                "exact_duplicate_rows", DEFAULT_RULES["exact_duplicate_rows"], source.get("source_name", ""),
                0.95, [f"{source['exact_duplicate_row_count']} byte-equivalent full-row copies detected"],
            ))
        for column in _columns(source):
            if (column.get("normalized_name") in {"status", "orderstatus", "orderstate", "state"}
                    or str(column.get("normalized_name", "")).endswith("status")):
                known = {"completed", "complete", "done", "delivered", "cancelled", "canceled"}
                unknown = [value for value in (source.get("status_counts") or {}) if _plain(value).strip() not in known]
                if unknown:
                    defaults.append(_entry(
                        "unknown_status", DEFAULT_RULES["unknown_status"], source.get("source_name", ""),
                        0.95, [f"unmapped status values preserved: {unknown}"],
                    ))

    if asks_platform:
        needs_mapping = [source for source in sources
                         if _column(source, {"platform"}) is None
                         and source.get("source_name", source.get("name", "")) not in source_platforms]
        if needs_mapping:
            names = [str(source.get("source_name", source.get("name", "source"))) for source in needs_mapping]
            suggestions = [str((source.get("source_identity_candidate") or {}).get("value") or "")
                           for source in needs_mapping]
            shown = "; ".join(f"{name} → {suggestion}" if suggestion else name
                               for name, suggestion in zip(names, suggestions))
            ambiguous.append({"rule_key": "source_platforms",
                              "issue": "File names alone do not establish platform labels",
                              "sources": names, "alternatives": suggestions})
            questions.append(
                f"Bạn yêu cầu nhóm theo platform. Tên file gợi ý {shown}. Hãy xác nhận hoặc sửa mapping theo dạng `file.csv=platform; file_khac.csv=platform`."
            )
            fields.append({"key": "source_platforms", "type": "text"})
    if source_platforms:
        rules["source_platforms"] = source_platforms

    status_column_sources = [source for source in sources if _column(
        source, {"status", "orderstatus", "orderstate", "state", "tripstatus", "ridestatus"}
    )]
    requested_metric_roles = {
        canonical_metric(item) for item in contract.get("requested_metrics", [])
        if isinstance(item, str)
    }
    # In a revenue request, observed/confirmed states describe revenue
    # eligibility. They do not also request a separate completed-order metric.
    # Keep the legacy clarification when completion itself is requested.
    asks_completed = (
        bool(requested_metric_roles & {"completed_count", "completion_rate"})
        if revenue_mode else
        _has_term(request, "completed", "completed_count", "completed orders", "completed trips", "completion_rate",
                  "hoan thanh", "hoan tat", "so don hoan thanh", "so chuyen hoan thanh")
    )
    completed_answers = _parse_status_answers(rules.get("completed_status_values_by_file"), sources)
    completed_candidates = {}
    for source in status_column_sources:
        name = str(source.get("source_name", source.get("name", "")))
        observed = source.get("status_counts") or {}
        candidate_values = [value for value in observed
                            if _plain(value).strip() in {"complete", "done", "delivered", "fulfilled", "success"}]
        confirmed = set(completed_answers.get(name, []))
        unresolved = ([] if name in completed_answers else
                      [value for value in candidate_values if _plain(value).strip() not in confirmed])
        if unresolved and asks_completed:
            completed_candidates[name] = [(value, observed[value]) for value in unresolved]
    if completed_candidates:
        shown = "; ".join(f"{name}: " + ", ".join(f"{value} ({count})" for value, count in values)
                           for name, values in completed_candidates.items())
        ambiguous.append({"rule_key": "completed_status_values_by_file",
                          "issue": "Observed status labels may or may not mean completed",
                          "sources": list(completed_candidates),
                          "alternatives": {name: [value for value, _ in values]
                                           for name, values in completed_candidates.items()}})
        questions.append(
            f"Profile thấy các trạng thái gần nghĩa hoàn thành: {shown}. Hãy ghi các giá trị được tính là completed theo mẫu `file.csv=giá_trị1|giá_trị2; file_khac.csv=giá_trị`. Nhập `none` sau dấu `=` nếu không có giá trị nào."
        )
        fields.append({"key": "completed_status_values_by_file", "type": "text"})
    elif completed_answers:
        rules["completed_status_values_by_file"] = completed_answers
    if asks_completed:
        for source in status_column_sources:
            observed = source.get("status_counts") or {}
            canonical = [value for value in observed if _plain(value).strip() == "completed"]
            if canonical:
                auto.append(_entry("completed_status", canonical, source.get("source_name", ""), 0.99,
                                   ["exact canonical status value observed in the source"]))

    # Profile proves common line/event grains; repeated entity IDs remain valid there.
    duplicate_requested = _has_term(request, "deduplicate", "dedup", "remove duplicates", "loai trung", "xoa trung")
    duplicate_sources = [source for source in sources if source.get("data_grain") in {"item_level", "event_level"}]
    if duplicate_requested:
        for source in duplicate_sources:
            auto.append(_entry(
                "deduplication_policy", "preserve rows; repeated entity IDs are valid at this data grain",
                source.get("source_name", ""), float(source.get("data_grain_confidence", 0)),
                source.get("data_grain_evidence", []),
            ))
        for source in sources:
            if source.get("data_grain") == "order_level":
                auto.append(_entry(
                    "deduplication_policy", "deduplicate repeated order IDs after conflict checks",
                    source.get("source_name", ""), float(source.get("data_grain_confidence", 0)),
                    source.get("data_grain_evidence", []),
                ))

    unknown_grain_with_duplicates = [source for source in sources
                                     if source.get("data_grain", "unknown") == "unknown"
                                     and (source.get("duplicate_order_id_count", 0) > 0
                                          or source.get("exact_duplicate_row_count", 0) > 0)]
    if duplicate_requested and unknown_grain_with_duplicates:
        names = [source.get("source_name", source.get("name", "source")) for source in unknown_grain_with_duplicates]
        has_order_key = all(_column(source, {"orderid", "ordernumber", "transactionid", "receiptno"})
                            for source in unknown_grain_with_duplicates)
        has_date = all(any(candidate.get("semantic_field") == "order_date"
                           for column in _columns(source)
                           for candidate in column.get("semantic_candidates", []))
                       for source in unknown_grain_with_duplicates)
        dedup_options = ["keep_all_rows", "exact_duplicates_only"]
        if has_order_key:
            dedup_options.append("keep_first")
        if has_order_key and has_date:
            dedup_options.append("keep_latest")
        answer = rules.get("dedup_policy")
        if answer in dedup_options:
            rules["dedup_policy"] = answer
        else:
            ambiguous.append({
                "rule_key": "dedup_policy", "issue": "Data grain is unknown and deduplication changes the result",
                "sources": names, "alternatives": dedup_options,
            })
            questions.append(
                f"{', '.join(names)} có nhiều dòng cùng mã đơn nhưng chưa xác định được dữ liệu ở cấp đơn hay cấp dòng. Bạn muốn giữ mọi dòng, gộp theo đơn, hay chỉ bỏ các dòng trùng hoàn toàn?"
            )
            questions[-1] = (f"{', '.join(names)} has repeated rows, but the row grain is unknown. "
                             f"Choose one supported policy: {', '.join(dedup_options)}.")
            fields.append({"key": "resolved_business_rules", "file": "dedup_policy", "type": "select",
                           "options": dedup_options})

    distinct_entity_metric = _has_term(request, "distinct_order_count", "distinct orders", "unique orders", "unique trips")
    if (duplicate_requested or distinct_entity_metric) and len(sources) > 1:
        ids_by_source = {
            source.get("source_name", source.get("name", "")): set(source.get("order_id_values_sample", []))
            for source in sources
        }
        collided_ids = set()
        source_items = list(ids_by_source.items())
        for index, (_, left) in enumerate(source_items):
            for _, right in source_items[index + 1:]:
                collided_ids.update(left & right)
        answer = _plain(str(rules.get("cross_source_identity", "")))
        if collided_ids and answer not in {"same_entity", "independent"}:
            examples = sorted(collided_ids)[:5]
            ambiguous.append({"rule_key": "cross_source_identity", "issue": "Order IDs overlap across sources",
                              "sources": list(ids_by_source), "alternatives": ["same_entity", "independent"]})
            questions.append(
                f"Có mã đơn trùng giữa các nguồn ({', '.join(examples)}). Khi xử lý trùng, các mã này là cùng đơn hay mỗi nguồn có mã riêng?"
            )
            fields.append({"key": "resolved_business_rules", "file": "cross_source_identity", "type": "select",
                           "options": ["same_entity", "independent"]})
        elif collided_ids:
            rules["cross_source_identity"] = answer

    # A returned state is not silently equated with cancellation.
    returned_sources = [source for source in sources if any(
        _plain(value).strip() == "returned" for value in (source.get("status_counts") or {})
    )]
    asks_cancel = (
        bool(requested_metric_roles & {"cancelled_count", "cancellation_rate"})
        if revenue_mode else
        _has_term(request, "cancelled", "canceled", "cancellation rate", "cancelled_count", "so don huy", "don huy")
    )
    if returned_sources and asks_cancel:
        answer = _yes(rules.get("returned_as_cancelled"))
        if answer is None:
            names = [source.get("source_name", source.get("name", "source")) for source in returned_sources]
            ambiguous.append({"rule_key": "returned_as_cancelled", "issue": "Whether returned orders count as cancelled changes cancellation metrics",
                              "sources": names, "alternatives": ["yes", "no"]})
            questions.append(f"{', '.join(names)} có trạng thái `returned`. Có tính `returned` vào số đơn hủy không? Nếu không, trạng thái này sẽ được giữ riêng và báo cáo riêng.")
            fields.append({"key": "resolved_business_rules", "file": "returned_as_cancelled", "type": "select",
                           "options": ["yes", "no"]})
        else:
            rules["returned_as_cancelled"] = "yes" if answer else "no"

    requests_revenue = _has_term(request, "revenue", "doanh thu", "gross amount", "net amount", "sum amount", "tổng tiền", "tong tien")
    requests_revenue = requests_revenue or _has_term(request, "amount", "total amount")
    if requests_revenue and not revenue_mode:
        basis_fields_by_source = {}
        for source in sources:
            names = {item.get("normalized_name") for item in _columns(source)}
            basis_fields = []
            if names & {"grossamount", "grossrevenue"}:
                basis_fields.append("gross_amount")
            if names & {"netamount", "netrevenue"}:
                basis_fields.append("net_amount")
            if basis_fields:
                basis_fields_by_source[str(source.get("source_name", source.get("name", "source")))] = basis_fields
        bases_present = {basis for values in basis_fields_by_source.values() for basis in values}
        option_sets = {tuple(sorted(set(options))) for options in basis_fields_by_source.values()}
        if len(option_sets) > 1:
            per_file = _parse_revenue_basis_answers(rules.get("revenue_basis_by_file"), sources)
            if all(name in per_file and per_file[name] in options
                   for name, options in basis_fields_by_source.items()):
                rules["revenue_basis_by_file"] = per_file
                for name, basis in per_file.items():
                    auto.append(_entry("revenue_basis", basis, name, 1.0,
                                       ["user confirmed the revenue measure for this source"]))
            else:
                shown = "; ".join(f"{name}: {' or '.join(options)}"
                                   for name, options in basis_fields_by_source.items())
                ambiguous.append({"rule_key": "revenue_basis_by_file",
                                  "issue": "Sources expose different gross/net revenue fields",
                                  "sources": list(basis_fields_by_source), "alternatives": basis_fields_by_source})
                questions.append(
                    f"Các nguồn có sẵn trường doanh thu khác nhau ({shown}). Hãy chọn trường cho từng nguồn theo dạng `file.csv=gross_amount; file_khac.csv=net_amount`."
                )
                fields.append({"key": "revenue_basis_by_file", "type": "text"})
        elif basis_fields_by_source and len(bases_present) == 1:
            basis = next(iter(bases_present))
            rules["revenue_basis"] = basis
            for name in basis_fields_by_source:
                auto.append(_entry("revenue_basis", basis, name, 0.97,
                                   ["all profiled gross/net candidates use the same explicitly named measure"]))
        elif basis_fields_by_source and next(iter(option_sets), ()) == ("gross_amount", "net_amount") \
                and not rules.get("revenue_basis"):
            both_basis_sources = list(basis_fields_by_source)
            ambiguous.append({"rule_key": "revenue_basis", "issue": "Gross and net amounts are both available",
                              "sources": both_basis_sources, "alternatives": ["gross_amount", "net_amount"]})
            questions.append(f"{', '.join(both_basis_sources)} có cả gross_amount và net_amount. Revenue cần dùng cột nào?")
            fields.append({"key": "resolved_business_rules", "file": "revenue_basis", "type": "select",
                           "options": ["gross_amount", "net_amount"]})

    existing_business_rule_keys = {item.get("rule_key") for item in ambiguous}
    covered_contract_domains = set().union(*(
        _ambiguity_domains(" ".join(str(item.get(key, "")) for key in ("issue", "rule_key", "decision")))
        for item in ambiguous
    )) if ambiguous else set()
    for item in auto:
        covered_contract_domains.update(_ambiguity_domains(" ".join(
            str(item.get(key, "")) for key in ("issue", "decision", "evidence")
        )))
    # Physical column binding is Planner work when the schema is available.
    if schemas:
        covered_contract_domains.add("schema_mapping")
    if revenue_mode and revenue_semantics:
        for key, _, issue in revenue_semantic_issues(revenue_semantics):
            covered_contract_domains.update(_ambiguity_domains(f"{key} {issue}"))
    contract_ambiguities = (state.get("requirement_contract") or {}).get("ambiguities", [])
    resolved_contract_ambiguities = []
    for contract_ambiguity in contract_ambiguities:
        text = str(contract_ambiguity)
        normalized = _plain(text)
        domains = _ambiguity_domains(text)
        # Several existing checks already ask the same business question using
        # evidence from the current schema. Keep the contract lossless, but ask
        # the Human only once for each unresolved semantic decision.
        if domains and domains.issubset(covered_contract_domains):
            resolved_contract_ambiguities.append(text)
            continue
        if revenue_ambiguity_is_resolved(text, revenue_semantics):
            resolved_contract_ambiguities.append(text)
            continue
        if "gross" in normalized and "net" in normalized and (
            rules.get("revenue_basis") or rules.get("revenue_basis_by_file")
        ):
            continue
        if ("gross" in normalized and "net" in normalized and
                ("revenue_basis" in existing_business_rule_keys or "revenue_basis_by_file" in existing_business_rule_keys)) \
                or ("returned" in normalized and "cancel" in normalized
                    and "returned_as_cancelled" in existing_business_rule_keys) \
                or (("duplicate" in normalized or "dedup" in normalized)
                    and "dedup_policy" in existing_business_rule_keys):
            continue
        if not revenue_mode and "gross" in normalized and "net" in normalized and not rules.get("revenue_basis") \
                and "revenue_basis" not in existing_business_rule_keys:
            ambiguous.append({"rule_key": "revenue_basis", "issue": text,
                              "sources": [], "alternatives": ["gross_amount", "net_amount"]})
            questions.append("Revenue có thể dùng gross_amount hoặc net_amount. Bạn muốn lấy cơ sở nào?")
            fields.append({"key": "resolved_business_rules", "file": "revenue_basis", "type": "select",
                           "options": ["gross_amount", "net_amount"]})
            existing_business_rule_keys.add("revenue_basis")
        elif text and _contract_rule_key(text) not in rules:
            rule_key = _contract_rule_key(text)
            if rule_key not in existing_business_rule_keys:
                ambiguous.append({"rule_key": rule_key, "issue": text,
                                  "sources": [], "alternatives": []})
                questions.append(f"Cần bạn quyết định điểm nghiệp vụ này trước khi tiếp tục: {text}")
                fields.append({"key": "resolved_business_rules", "file": rule_key, "type": "text"})
                existing_business_rule_keys.add(rule_key)
                covered_contract_domains.update(domains)

    # Reuse existing profiled checks for other genuinely result-changing choices
    # (currency, date interpretation, join key/cardinality, or append vs join).
    if schemas:
        from agents.clarifier.agent import custom_csv_questions

        legacy_questions, legacy_fields = custom_csv_questions(request, schemas, clarification)
        suppressed = {
            "missing_amount_policy", "completed_status_values_by_file", "cancelled_status_values_by_file",
            "duplicate_scope", "duplicate_resolution",
        }
        if not numeric_scope_requested:
            suppressed.add("number_format_by_file")
        if not currency_scope_requested:
            suppressed.update({"status_scope", "missing_amount_policy", "currency_by_file", "target_currency",
                               "missing_currency_by_file", "currency_rates", "conversion_rates"})
        elif revenue_mode:
            suppressed.update({"status_scope", "currency_by_file", "target_currency", "missing_currency_by_file", "currency_rates", "conversion_rates"})
        outputs = contract.get("requested_outputs", [])
        projection_only = bool(outputs) and all(output.get("kind") == "rows" for output in outputs)
        conversion_requested = "unit_conversion" in contract.get("requested_transformations", [])
        if projection_only and not contract.get("requested_metrics") and not conversion_requested:
            # Parsing recorded values does not select a reporting currency or a
            # recognition policy. Numeric/date ambiguity checks still apply.
            suppressed.update({"status_scope", "currency_by_file", "target_currency", "missing_currency_by_file", "currency_rates", "conversion_rates"})
        for question, field in zip(legacy_questions, legacy_fields):
            key = field.get("key")
            if key in suppressed or (key == "join_duplicate_policy" and duplicate_sources):
                if key == "missing_amount_policy" and currency_scope_requested:
                    defaults.append(_entry("missing_amount_policy", "exclude invalid/missing numeric values from numeric metrics",
                                           field.get("file", ""), 0.99,
                                           ["missing values remain null and visible in source rows",
                                            "numeric aggregations do not treat unknown values as zero"]))
                continue
            # Date and numeric parsing questions are retained only when profiles
            # cannot distinguish the interpretation; other legacy checks are
            # business choices or join-integrity blockers.
            if key == "number_format_by_file":
                source = next((item for item in sources if item.get("source_name") == field.get("file")), {})
                if field.get("file") in number_format_questions:
                    continue
                if source.get("number_format_candidate") in {"thousands", "decimal"}:
                    auto.append(_entry("numeric_format", source["number_format_candidate"], field.get("file", ""),
                                       0.95, source.get("number_format_evidence", [])))
                    continue
            if key == "date_format_by_file":
                source = next((item for item in sources if item.get("source_name") == field.get("file")), {})
                if source.get("date_format_candidate") in {"day_first", "month_first", "iso"}:
                    auto.append(_entry("date_format", source["date_format_candidate"], field.get("file", ""),
                                       0.96, source.get("date_format_evidence", [])))
                    continue
            ambiguous.append({"rule_key": key or "additional_context", "issue": question,
                              "sources": [field.get("file")] if field.get("file") else [],
                              "alternatives": field.get("options", [])})
            questions.append(question)
            fields.append(field)

    if revenue_mode:
        revenue_schemas = schemas
        revenue_semantics = build_revenue_semantics(request, contract, revenue_schemas, profile, clarification, rules)
        grouped_issues = {}
        for key, file_name, issue in revenue_semantic_issues(revenue_semantics):
            original = next((schema.get("original_name", file_name) for schema in revenue_schemas
                             if schema.get("name") == file_name), file_name)
            grouped_issues.setdefault((key, original), []).append(issue)
        for (key, file_name), issues in grouped_issues.items():
            field = {"key": key, "type": "text"}
            if file_name:
                field["file"] = file_name
            if key == "revenue_measure_basis":
                field.update({"type": "select", "options": ["gross", "net"]})
            elif key == "refund_treatment":
                field.update({"type": "select", "options": ["exclude", "already_net", "include_as_recorded"]})
            elif key in {"currency_by_file", "missing_currency_by_file", "target_currency"}:
                field["type"] = "currency"
            elif key in {"currency_rates", "conversion_rates"}:
                field["type"] = "rate"
            question = f"Revenue / Sales ({file_name or 'business policy'}): {'; '.join(issues)}."
            if key == "revenue_roles_by_file":
                question += (" Confirm a JSON role binding with transaction_monetary_value (column, meaning, basis, scale, value_grain), "
                             "transaction_entity_key, business_event_timestamp and transaction_status. Physical columns apply to this dataset only.")
            elif key == "recognized_status_values_by_file":
                question += " Enter eligible observed states separated by |, or explicitly choose revenue_status_policy=all."
            ambiguous.append({"rule_key": key, "issue": question,
                              "sources": [file_name] if file_name else [], "alternatives": field.get("options", [])})
            questions.append(question)
            fields.append(field)

    # Keep one question per rule and one input per question.
    unique_questions, unique_fields, seen = [], [], set()
    for question, field in zip(questions, fields):
        identity = (str(field.get("key", "")), str(field.get("file", "")), question)
        if identity in seen:
            continue
        seen.add(identity)
        unique_questions.append(question)
        unique_fields.append(field)

    grain_by_source = {source.get("source_name", source.get("name", "")): {
        "data_grain": source.get("data_grain", "unknown"),
        "confidence": source.get("data_grain_confidence", 0),
        "evidence": source.get("data_grain_evidence", []),
    } for source in sources}
    resolution = {
        "resolved_automatically": auto,
        "defaults_used": defaults,
        "business_ambiguous": ambiguous,
        "resolved_contract_ambiguities": resolved_contract_ambiguities,
        "resolved_business_rules": rules,
        "questions_for_user": unique_questions,
        "clarification_fields": unique_fields,
        "data_grain_by_source": grain_by_source,
        "can_continue": not unique_questions,
        "warnings": warnings,
        **({"revenue_semantics": revenue_semantics} if revenue_semantics else {}),
    }
    return resolution


def run(state: dict) -> dict:
    resolution = resolve_ambiguities(state)
    requirement_contract = state.get("requirement_contract")
    if isinstance(requirement_contract, dict):
        requirement_contract = dict(requirement_contract)
        unresolved = []
        for ambiguity in requirement_contract.get("ambiguities", []):
            normalized = _plain(str(ambiguity))
            if str(ambiguity) in resolution.get("resolved_contract_ambiguities", []):
                continue
            if revenue_ambiguity_is_resolved(str(ambiguity), resolution.get("revenue_semantics")):
                continue
            if "gross" in normalized and "net" in normalized and (
                resolution["resolved_business_rules"].get("revenue_basis")
                or resolution["resolved_business_rules"].get("revenue_basis_by_file")
            ):
                continue
            if _contract_rule_key(str(ambiguity)) in resolution["resolved_business_rules"]:
                continue
            unresolved.append(ambiguity)
        requirement_contract["ambiguities"] = unresolved
    return {
        "ambiguity_resolution": resolution,
        "resolved_business_rules": resolution["resolved_business_rules"],
        **({"requirement_contract": requirement_contract} if isinstance(requirement_contract, dict) else {}),
        "status": "ambiguities_resolved" if resolution["can_continue"] else "clarification_required",
    }


def route_after_resolution(state: dict) -> str:
    if state.get("status") == "failed":
        return "end"
    resolution = state.get("ambiguity_resolution", {})
    return "planner" if resolution.get("can_continue", True) else "clarifier"
