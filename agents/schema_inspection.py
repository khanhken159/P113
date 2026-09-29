"""Profile uploaded sources before deciding whether clarification is needed."""

from __future__ import annotations

import csv
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


PROFILE_ROW_LIMIT = 100_000
SAMPLE_LIMIT = 8
DEFAULT_RULES = {
    "trim_text": True,
    "normalize_status_case": True,
    "empty_string_to_null": True,
    "invalid_numeric": "coerce_to_null_and_exclude_from_numeric_metrics",
    "unknown_status": "preserve_and_report",
    "missing_descriptive_field": "keep_null",
    "invalid_date": "coerce_to_null_and_report",
    "exact_duplicate_rows": "preserve_and_report",
    "derived_duration_unit": "second",
}


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _number(value: str) -> float | None:
    raw = str(value or "").strip().replace(" ", "")
    if not raw:
        return None
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+\.\d+", raw):
        return float(raw.replace(",", ""))
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+,\d+", raw):
        return float(raw.replace(".", "").replace(",", "."))
    if re.fullmatch(r"-?\d+,\d{1,2}", raw):
        return float(raw.replace(",", "."))
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return float(raw)
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", raw):
        return float(raw.replace(",", ""))
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", raw):
        return float(raw.replace(".", ""))
    return None


def _looks_date(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}(?:[ T].*)?", raw):
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return "iso"
        except ValueError:
            return None
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
        first, second, year = (int(part) for part in raw.split("/"))
        if first > 12:
            try:
                datetime(year, second, first)
                return "day_first"
            except ValueError:
                return None
        if second > 12:
            try:
                datetime(year, first, second)
                return "month_first"
            except ValueError:
                return None
        day_first_valid = month_first_valid = False
        try:
            datetime(year, second, first)
            day_first_valid = True
        except ValueError:
            pass
        try:
            datetime(year, first, second)
            month_first_valid = True
        except ValueError:
            pass
        if day_first_valid and month_first_valid:
            return "ambiguous_day_month"
        if day_first_valid:
            return "day_first"
        if month_first_valid:
            return "month_first"
        return None
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return "iso_datetime"
    except ValueError:
        return None


def _infer_type(values: list[str]) -> str:
    present = [str(value).strip() for value in values if str(value or "").strip()]
    if not present:
        return "unknown"
    if all(re.fullmatch(r"true|false|yes|no", value, re.I) for value in present):
        return "boolean"
    if all(re.fullmatch(r"[+-]?\d+", value) for value in present):
        return "integer"
    parsed = [_number(value) for value in present]
    if all(value is not None and math.isfinite(value) for value in parsed):
        return "numeric"
    date_kinds = [_looks_date(value) for value in present]
    if all(date_kinds):
        return "datetime" if any(kind == "iso_datetime" for kind in date_kinds) else "date"
    return "string"


def _semantic_candidates(column: str, values: list[str], inferred_type: str) -> list[dict]:
    name = normalize_name(column)
    candidates = []

    def add(semantic: str, confidence: float, *evidence: str) -> None:
        candidates.append({"semantic_field": semantic, "confidence": confidence, "evidence": list(evidence)})

    if name in {"orderid", "ordernumber", "receiptno", "transactionid"}:
        add("order_id", 0.97, "column name identifies an order or transaction key")
    elif name in {"productid", "itemid", "sku", "productsku", "orderitemid", "lineid"}:
        add("item_id", 0.97, "column name identifies an item-level key")
    elif name in {"customerid", "buyerid", "clientid"}:
        add("customer_id", 0.97, "column name identifies a customer key")
    elif name in {"buyer", "buyername", "customername", "clientname", "purchasername"}:
        person_like = bool(values) and all(re.search(r"[A-Za-zÀ-ỹ]", value) for value in values if value)
        add("customer_name", 0.93 if person_like else 0.84,
            "column name denotes a buyer/customer", "sample values are text-like" if person_like else "name evidence is incomplete")
    elif name in {"orderdate", "createdat", "orderedat", "transactiondate", "timestamp"}:
        add("order_date", 0.96, "column name denotes an order or event date")
    elif name in {"status", "orderstatus", "orderstate", "state"} or name.endswith("status"):
        add("status", 0.95, "column name denotes a state or status")
    elif name in {"grossamount", "grossrevenue", "netamount", "netrevenue"}:
        add("gross_amount" if name.startswith("gross") else "net_amount", 0.98,
            "column name explicitly identifies gross/net amount")
    elif name in {"amount", "totalamount", "revenue", "fare", "paidamount"}:
        numeric_values = [_number(value) for value in values if str(value or "").strip()]
        ratio = sum(value is not None for value in numeric_values) / len(numeric_values) if numeric_values else 0.0
        confidence = 0.96 if inferred_type in {"integer", "numeric"} else 0.91 if ratio >= 0.80 else 0.76
        add("amount", confidence, "column name denotes a monetary amount",
            f"{ratio:.0%} of profiled non-empty samples parse as numeric")
    elif name in {"quantity", "qty", "units", "itemcount"} and inferred_type in {"integer", "numeric"}:
        add("quantity", 0.95, "numeric datatype", "column name denotes a quantity, not revenue")
    elif name in {"distancekm", "distancekilometer", "distancekilometers"}:
        add("distance_km", 0.98, "column name explicitly states kilometres")
    elif name in {"distancemeter", "distancemeters"}:
        add("distance_km", 0.99, "column name explicitly states metres; divide by 1000")
    elif name in {"starttime", "startedat", "pickupat"}:
        add("start_time", 0.90, "column name denotes a start timestamp")
    elif name in {"endtime", "endedat", "dropoffat"}:
        add("end_time", 0.90, "column name denotes an end timestamp")
    elif name == "platform":
        add("platform", 0.99, "column name explicitly identifies the platform dimension")
    return candidates


def _business_role_candidates(column: str, values: list[str], profile: dict) -> list[dict]:
    """Propose roles from several signals; a header never proves revenue recognition."""
    name = normalize_name(column)
    present = [str(value).strip() for value in values if str(value or "").strip()]
    numeric_ratio = sum(_number(value) is not None for value in present) / len(present) if present else 0.0
    date_ratio = sum(_looks_date(value) is not None for value in present) / len(present) if present else 0.0
    profile["numeric_parse_ratio"] = numeric_ratio
    profile["date_parse_ratio"] = date_ratio
    candidates = []

    def add(role: str, confidence: float, **attributes) -> None:
        candidates.append({"role": role, "confidence": confidence, "evidence": [
            "semantic words in the header are a hypothesis, not a confirmed business definition",
            f"datatype={profile['datatype']}; numeric_parse_ratio={numeric_ratio:.2f}; date_parse_ratio={date_ratio:.2f}",
            f"unique_ratio={profile['unique_ratio']:.2f}; null_ratio={profile['null_ratio']:.2f}",
        ], **attributes})

    # These are concept words, not a dictionary of physical column aliases.
    quantity = any(term in name for term in ("quantity", "qty", "units", "count", "stock"))
    payment = any(term in name for term in ("payment", "paid", "settlement"))
    refund = "refund" in name or "return" in name
    cost = any(term in name for term in ("cost", "expense", "buy"))
    balance = "balance" in name
    if numeric_ratio >= 0.8 and quantity:
        add("quantity_field", 0.95, meaning="quantity")
    if numeric_ratio >= 0.8 and not quantity and any(term in name for term in (
        "monetary", "amount", "value", "price", "revenue", "sales", "total", "payment", "cost", "balance", "refund"
    )):
        meaning = ("payment" if payment else "refund" if refund else "cost" if cost else
                   "balance" if balance else "transaction_value" if "revenue" in name or (
                       "sales" in name and any(term in name for term in ("monetary", "amount", "value", "price")))
                   else "unknown_monetary_value")
        scale = "unit_price" if "price" in name and any(term in name for term in ("unit", "each", "piece")) else "unknown"
        basis = "gross" if "gross" in name else "net" if "net" in name else "unspecified"
        add("transaction_monetary_value", 0.92 if meaning == "transaction_value" else 0.75,
            meaning=meaning, scale=scale, basis=basis)
    if any(term in name for term in ("status", "state")) and profile["datatype"] in {"string", "integer"}:
        add("transaction_status", 0.92, meaning="business_state")
        profile["value_distribution"] = dict(Counter(present).most_common(20))
    if date_ratio >= 0.8:
        event = ("payment" if payment else "shipment" if "ship" in name else
                 "registration" if any(term in name for term in ("signup", "birth")) else
                 "transaction" if any(term in name for term in ("order", "sale", "transaction", "recognition")) else "unknown")
        add("business_event_timestamp", 0.94 if event == "transaction" else 0.75, meaning=event)
    if "currency" in name:
        valid_codes = bool(present) and all(re.fullmatch(r"[A-Za-z]{3}", value) for value in present)
        add("currency_field", 0.94 if valid_codes else 0.75, meaning="currency_code")
        profile["value_distribution"] = dict(Counter(value.upper() for value in present))
    if (any(term in name for term in ("order", "transaction", "invoice", "sale"))
            and any(term in name for term in ("key", "id", "number", "code"))):
        add("transaction_entity_key", 0.94, meaning="transaction_identifier")
    return candidates


def revenue_role_bindings(source: dict, confirmed=None, proposed=None) -> dict:
    """Keep dataset-specific bindings local, separate from the reusable pattern."""
    if isinstance(confirmed, str):
        try:
            confirmed = json.loads(confirmed)
        except json.JSONDecodeError:
            confirmed = {}
    confirmed = confirmed if isinstance(confirmed, dict) else {}
    proposed = proposed if isinstance(proposed, dict) else {}
    profiles = {item["name"]: item for item in source.get("column_profiles", [])}
    bindings = {}
    for role in ("transaction_monetary_value", "transaction_status", "business_event_timestamp", "transaction_entity_key"):
        manual = confirmed.get(role)
        suggestion = proposed.get(role)
        if manual:
            entry = {"column": manual} if isinstance(manual, str) else dict(manual) if isinstance(manual, dict) else {}
            entry.update({"authority": "human", "confidence": 1.0, "evidence": ["Human confirmed this semantic role for this dataset"]})
        else:
            matches = [(column, candidate) for column, profile in profiles.items()
                       for candidate in profile.get("business_role_candidates", [])
                       if candidate.get("role") == role and candidate.get("confidence", 0) >= 0.90]
            if len(matches) == 1:
                column, candidate = matches[0]
                entry = {**candidate, "column": column, "authority": "profile"}
            elif isinstance(suggestion, dict) and suggestion.get("column") in profiles:
                # A model suggestion is reviewable mapping evidence, never Human confirmation.
                entry = {**suggestion, "authority": "model"}
            else:
                continue
        column = entry.get("column")
        if column not in profiles:
            continue
        entry["datatype"] = profiles[column].get("datatype")
        entry["numeric_parse_ratio"] = profiles[column].get("numeric_parse_ratio", 0)
        entry["date_parse_ratio"] = profiles[column].get("date_parse_ratio", 0)
        if role == "transaction_monetary_value":
            entry.setdefault("meaning", "transaction_value" if manual else "unknown_monetary_value")
            entry.setdefault("basis", "unspecified")
            entry.setdefault("scale", "transaction_total" if manual else "unknown")
            entry.setdefault("value_grain", source.get("data_grain", "unknown"))
            if entry.get("meaning") == "transaction_value" and entry.get("scale") == "unknown" and source.get("data_grain") == "order_level":
                entry["scale"] = "transaction_total"
                entry["evidence"] = [*entry.get("evidence", []), "an explicit revenue measure at a complete unique transaction grain"]
        if role == "business_event_timestamp":
            entry.setdefault("meaning", "transaction" if manual else "unknown")
        bindings[role] = entry
    return bindings


def _grain_profile(columns: list[str], rows: list[dict], total_rows: int) -> tuple[str, float, list[str]]:
    names = {normalize_name(column): column for column in columns}
    order_key = next((names[key] for key in ("orderid", "ordernumber", "transactionid", "receiptno") if key in names), None)
    item_fields = [names[key] for key in names if key in {
        "productid", "itemid", "orderitemid", "lineid", "detailid", "productsku", "sku", "variantid"
    }]
    event_fields = [names[key] for key in names if key in {
        "eventid", "eventtype", "eventtime", "eventtimestamp", "statuschangedat", "historyid"
    }]
    if event_fields and order_key:
        repeated = defaultdict(set)
        for row in rows:
            key = str(row.get(order_key) or "").strip()
            event = next((str(row.get(field) or "").strip() for field in event_fields
                          if str(row.get(field) or "").strip()), "")
            if key and event:
                repeated[key].add(event)
        if any(len(values) > 1 for values in repeated.values()):
            return "event_level", 0.97, [f"multiple {event_fields[0]} values observed for one {order_key}",
                                           f"entity key present: {order_key}"]
    if not order_key and any(key in names for key in ("customerid", "clientid", "buyerid")):
        return "customer_level", 0.90, ["customer key present", "no order-level transaction key found"]
    if order_key and item_fields:
        repeated_with_different_items = False
        products_by_order: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            key = str(row.get(order_key) or "").strip()
            item = next((str(row.get(field) or "").strip() for field in item_fields if str(row.get(field) or "").strip()), "")
            if key and item:
                products_by_order[key].add(item)
        repeated_with_different_items = any(len(values) > 1 for values in products_by_order.values())
        if repeated_with_different_items:
            return "item_level", 0.97, [f"multiple {item_fields[0]} values observed for one {order_key}",
                                         f"order key present: {order_key}"]
    if order_key and total_rows:
        values = [str(row.get(order_key) or "").strip() for row in rows if str(row.get(order_key) or "").strip()]
        unique_ratio = len(set(values)) / len(values) if values else 0.0
        if unique_ratio == 1.0 and len(values) == total_rows:
            return "order_level", 0.98, [f"{order_key} is complete and unique across profiled rows"]
        # Repeated IDs alone, including exact row copies, cannot prove the grain:
        # a repeated item/event may be valid and needs an explicit dedup policy.
    return "unknown", 0.0, ["available schema and row profile do not establish a reliable grain"]


def profile_source(schema: dict, data_dir: Path | None) -> dict:
    columns = list(schema.get("columns", []))
    source_name = str(schema.get("original_name", schema.get("name", "")))
    sample_rows: list[dict] = []
    counts: Counter = Counter()
    missing: Counter = Counter()
    whitespace: Counter = Counter()
    uniques: dict[str, set[str]] = {column: set() for column in columns}
    numeric_values: dict[str, list[float]] = {column: [] for column in columns}
    invalid_numeric: Counter = Counter()
    invalid_date: Counter = Counter()
    all_values: dict[str, list[str]] = {column: [] for column in columns}
    date_format_counts: dict[str, Counter] = {column: Counter() for column in columns}
    number_format_counts: dict[str, Counter] = {column: Counter() for column in columns}
    status_counts: Counter = Counter()
    business_state_counts = {column: Counter() for column in columns
                             if any(term in normalize_name(column) for term in ("status", "state", "currency"))}
    order_id_counts: Counter = Counter()
    order_id_values: set[str] = set()
    exact_row_fingerprints: Counter = Counter()
    all_rows_for_grain: list[dict] = []
    configured_path = schema.get("profile_path")
    path = Path(configured_path) if configured_path else (
        data_dir / "custom_csv" / str(schema.get("name", "")) if data_dir else None
    )

    if path and path.is_file():
        if path.suffix.casefold() == ".json":
            raw_json = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw_json, dict):
                raw_json = raw_json.get("data", raw_json.get("rows", []))
            source_rows = raw_json if isinstance(raw_json, list) else []
        else:
            stream = path.open(encoding="utf-8-sig", newline="")
            source_rows = csv.DictReader(stream)
        try:
            for row in source_rows:
                counts["rows"] += 1
                if len(sample_rows) < SAMPLE_LIMIT:
                    sample_rows.append(dict(row))
                if len(all_rows_for_grain) < PROFILE_ROW_LIMIT:
                    all_rows_for_grain.append(dict(row))
                exact_row_fingerprints[tuple(str(row.get(column) or "").strip() for column in columns)] += 1
                for column in columns:
                    raw_value = str(row.get(column) or "")
                    if raw_value != raw_value.strip():
                        whitespace[column] += 1
                    value = raw_value.strip()
                    if column in business_state_counts and value:
                        business_state_counts[column][value] += 1
                    if len(all_values[column]) < 500:
                        all_values[column].append(value)
                    if not value:
                        missing[column] += 1
                        continue
                    if date_kind := _looks_date(value):
                        date_format_counts[column][date_kind] += 1
                    normalized_column = normalize_name(column)
                    date_name_tokens = ("date", "time", "timestamp", "datetime")
                    is_date_column = any(token in normalized_column for token in date_name_tokens)
                    if is_date_column and not date_kind:
                        invalid_date[column] += 1
                    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", value):
                        number_format_counts[column]["comma_grouped_multiple" if value.count(",") > 1 else "comma_grouped_single"] += 1
                    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", value):
                        number_format_counts[column]["dot_grouped_multiple" if value.count(".") > 1 else "dot_grouped_single"] += 1
                    elif re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                        number_format_counts[column]["plain_decimal"] += 1
                    else:
                        number_format_counts[column]["other"] += 1
                    if len(uniques[column]) < PROFILE_ROW_LIMIT:
                        uniques[column].add(value)
                    if len(numeric_values[column]) < PROFILE_ROW_LIMIT and (number := _number(value)) is not None:
                        numeric_values[column].append(number)
                    looks_numeric = any(token in normalized_column for token in
                                        ("amount", "revenue", "price", "fare", "quantity", "qty", "distance", "duration", "meter", "kilometer"))
                    if looks_numeric and _number(value) is None:
                        invalid_numeric[column] += 1
                    if normalize_name(column) in {"status", "orderstatus", "orderstate", "state"} or normalize_name(column).endswith("status"):
                        status_counts[value] += 1
                    if normalized_column in {"orderid", "ordernumber", "transactionid", "receiptno"}:
                        order_id_counts[value] += 1
                        if len(order_id_values) < PROFILE_ROW_LIMIT:
                            order_id_values.add(value)
        finally:
            if path.suffix.casefold() != ".json":
                stream.close()

    row_count = counts["rows"]
    column_profiles = []
    for column in columns:
        samples = list(dict.fromkeys(str(row.get(column) or "").strip() for row in sample_rows))[:5]
        sample_for_type = all_values[column]
        inferred_type = _infer_type(sample_for_type)
        present_count = max(0, row_count - missing[column])
        unique_count = len(uniques[column])
        unique_ratio = unique_count / present_count if present_count else 0.0
        profile = {
            "name": column,
            "normalized_name": normalize_name(column),
            "datatype": inferred_type,
            "sample_values": samples,
            "row_count": row_count,
            "null_count": missing[column],
            "null_ratio": missing[column] / row_count if row_count else 0.0,
            "unique_count": unique_count,
            "unique_ratio": unique_ratio,
            "min": min(numeric_values[column]) if numeric_values[column] else None,
            "max": max(numeric_values[column]) if numeric_values[column] else None,
            "invalid_numeric_count": invalid_numeric[column],
            "invalid_date_count": invalid_date[column],
            "trim_whitespace_count": whitespace[column],
            "candidate_key": bool(row_count and missing[column] == 0 and unique_ratio == 1.0),
            "semantic_candidates": _semantic_candidates(column, all_values[column], inferred_type),
        }
        profile["business_role_candidates"] = _business_role_candidates(column, all_values[column], profile)
        if column in business_state_counts:
            profile["value_distribution"] = dict(business_state_counts[column])
        if date_format_counts[column]:
            profile["date_format_candidates"] = dict(date_format_counts[column])
        if profile["normalized_name"] in {"distancemeter", "distancemeters"}:
            profile["unit_candidates"] = [{"unit": "meter", "confidence": 0.99, "evidence": ["column name explicitly includes meter"]}]
        elif profile["normalized_name"] in {"distancekm", "distancekilometer", "distancekilometers"}:
            profile["unit_candidates"] = [{"unit": "kilometer", "confidence": 0.99, "evidence": ["column name explicitly includes kilometer"]}]
        values = [value for value in all_values[column] if value]
        number_formats = number_format_counts[column]
        separator_formats = {key: count for key, count in number_formats.items()
                             if key in {"comma_grouped_single", "comma_grouped_multiple",
                                        "dot_grouped_single", "dot_grouped_multiple"}}
        separator_total = sum(separator_formats.values())
        separator_kinds = set(separator_formats)
        # A uniform multi-group example establishes that this separator is a
        # grouping mark for the column. Single-group values in the same column
        # then follow that observed convention; they are not independently
        # ambiguous decimals. A lone value such as "1.234" remains ambiguous.
        repeated_grouping = (
            "comma_grouped_multiple" in separator_kinds
            and separator_kinds <= {"comma_grouped_single", "comma_grouped_multiple"}
        ) or (
            "dot_grouped_multiple" in separator_kinds
            and separator_kinds <= {"dot_grouped_single", "dot_grouped_multiple"}
        )
        if separator_total and repeated_grouping:
            profile["number_format_candidate"] = "thousands"
            profile["number_format_evidence"] = [
                "the same separator forms repeated three-digit groups in observed values",
                "single-group values in this column follow the observed grouping convention",
            ]
        elif separator_total:
            profile["number_format_candidate"] = "ambiguous_separator"
            profile["number_format_evidence"] = ["a single separator could represent either a decimal or thousands grouping"]
        elif values and number_formats and set(number_formats) == {"plain_decimal"}:
            profile["number_format_candidate"] = "decimal"
            profile["number_format_evidence"] = ["sample values use an unambiguous decimal/integer representation"]
        date_formats = date_format_counts[column]
        if date_formats:
            if set(date_formats).issubset({"iso", "iso_datetime"}):
                profile["date_format_candidate"] = "iso"
            elif len(date_formats) == 1 and next(iter(date_formats)) in {"day_first", "month_first"}:
                profile["date_format_candidate"] = next(iter(date_formats))
            profile["date_format_evidence"] = [f"profiled date formats: {dict(date_formats)}"]
        column_profiles.append(profile)

    grain, grain_confidence, grain_evidence = _grain_profile(columns, all_rows_for_grain, row_count)
    if grain == "unknown":
        # Use identifier semantics and row relationships, without extending the
        # legacy dictionary each time a different key spelling is encountered.
        entity_columns = [column for column in column_profiles if any(
            item.get("role") == "transaction_entity_key" for item in column.get("business_role_candidates", []))]
        if len(entity_columns) == 1:
            entity = entity_columns[0]
            if entity.get("candidate_key"):
                grain, grain_confidence = "order_level", 0.94
                grain_evidence = ["a transaction entity key is complete and unique in the local profile"]
            else:
                line_columns = [column for column in columns if any(term in normalize_name(column)
                    for term in ("line", "item", "product")) and column != entity["name"]]
                rows_by_entity = defaultdict(set)
                for row in all_rows_for_grain:
                    key = str(row.get(entity["name"]) or "").strip()
                    if key and line_columns:
                        rows_by_entity[key].add(tuple(str(row.get(column) or "").strip() for column in line_columns))
                if any(len(values) > 1 for values in rows_by_entity.values()):
                    grain, grain_confidence = "item_level", 0.94
                    grain_evidence = ["different line/item identifiers occur for the same transaction entity"]
    stem = Path(source_name).stem
    tokens = [token for token in re.split(r"[_\-\s]+", stem) if token]
    generic_prefixes = {"orders", "order", "data", "dataset", "export", "fact", "sales"}
    suffix_tokens = tokens[1:] if tokens and normalize_name(tokens[0]) in generic_prefixes else []
    generic_suffixes = {"test", "sample", "cleaned", "copy", "final", "part", "batch", "with", "without",
                        "lines", "items", "customers", "products", "events", "orders", "data"}
    platform_candidate = None
    if len(suffix_tokens) == 1 and normalize_name(suffix_tokens[0]) not in generic_suffixes:
        platform_candidate = suffix_tokens[0]

    duplicate_rows = sum(count - 1 for count in exact_row_fingerprints.values() if count > 1)
    return {
        "name": str(schema.get("name", source_name)),
        "source_name": source_name,
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample_rows,
        "column_profiles": column_profiles,
        "status_counts": dict(status_counts),
        "duplicate_order_id_count": sum(count - 1 for count in order_id_counts.values() if count > 1),
        "order_id_values_sample": sorted(order_id_values),
        "exact_duplicate_row_count": duplicate_rows,
        "profiled_rows_for_grain": len(all_rows_for_grain),
        "profiled_rows_for_format_analysis": row_count,
        "profile_truncated": row_count > PROFILE_ROW_LIMIT,
        "data_grain": grain,
        "data_grain_confidence": grain_confidence,
        "data_grain_evidence": grain_evidence,
        "source_identity_candidate": {
            "value": platform_candidate,
            "confidence": 0.93 if platform_candidate else 0.0,
            "evidence": [f"single source suffix in filename {source_name}"] if platform_candidate else [],
        },
    }


def run(state: dict) -> dict:
    try:
        schemas = json.loads(os.getenv("FLOWFORGE_CUSTOM_CSV_SCHEMAS", "[]"))
    except json.JSONDecodeError:
        schemas = []
    data_dir = Path(os.getenv("FLOWFORGE_DATA_DIR", "")) if os.getenv("FLOWFORGE_DATA_DIR") else None
    profile_schemas = []
    if schemas:
        for schema in schemas:
            profiled = dict(schema)
            if data_dir:
                profiled["profile_path"] = str(data_dir / "custom_csv" / str(schema.get("name", "")))
            profile_schemas.append(profiled)
    elif data_dir and data_dir.is_dir():
        # Profile every local tabular input generically; do not special-case brands or filenames.
        for path in sorted(data_dir.iterdir()):
            if path.suffix.casefold() not in {".csv", ".json"} or not path.is_file():
                continue
            try:
                if path.suffix.casefold() == ".json":
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                    rows = payload.get("data", payload.get("rows", [])) if isinstance(payload, dict) else payload
                    columns = list(rows[0]) if isinstance(rows, list) and rows and isinstance(rows[0], dict) else []
                else:
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        columns = next(csv.reader(stream), [])
                if columns:
                    profile_schemas.append({"name": path.name, "original_name": path.name,
                                             "columns": columns, "profile_path": str(path)})
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, csv.Error):
                continue
    profiles = [profile_source(schema, data_dir) for schema in profile_schemas]
    return {
        "data_profile": {"sources": profiles, "profile_version": 1},
        "default_rules": dict(DEFAULT_RULES),
        "schema_inspection_status": "complete" if profile_schemas else "no_uploaded_csvs",
    }
