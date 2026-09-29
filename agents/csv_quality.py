"""Shared CSV quality checks for Clarifier and generated pipelines."""

import re
from datetime import date

import pandas as pd


def apply_filters(frame: pd.DataFrame, filters: list[dict]) -> pd.DataFrame:
    result = frame
    operators = {"=", "==", "eq", "!=", "<>", "ne", ">", "gt", ">=", "gte", "<", "lt", "<=", "lte",
                 "in", "not_in", "contains", "is_null", "not_null"}
    for item in filters:
        field = str(item.get("field", ""))
        operation = str(item.get("operator", "")).casefold().strip()
        value = item.get("value")
        if field not in result.columns or operation not in operators:
            raise ValueError(f"Unsupported or unmapped filter: {item}")
        column = result[field]
        if operation in {"is_null", "not_null"}:
            mask = column.isna() if operation == "is_null" else column.notna()
        elif operation in {"=", "==", "eq", "!=", "<>", "ne", "in", "not_in"}:
            if pd.api.types.is_numeric_dtype(column):
                comparable = pd.to_numeric(column, errors="coerce")
                if operation in {"in", "not_in"}:
                    values = pd.to_numeric(pd.Series(value if isinstance(value, list) else [value]), errors="coerce").tolist()
                    mask = comparable.isin(values)
                    if operation == "not_in":
                        mask = ~mask
                else:
                    target = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
                    mask = comparable.eq(target) if operation in {"=", "==", "eq"} else comparable.ne(target)
            else:
                comparable = column.astype("string").str.casefold()
                if operation in {"in", "not_in"}:
                    values = value if isinstance(value, list) else [value]
                    mask = comparable.isin([str(candidate).casefold() for candidate in values])
                else:
                    target = str(value).casefold()
                    mask = comparable.eq(target) if operation in {"=", "==", "eq"} else comparable.ne(target)
                if operation == "not_in":
                    mask = ~mask
        elif operation in {">", "gt", ">=", "gte", "<", "lt", "<=", "lte"}:
            comparable = column
            target = value
            if not pd.api.types.is_numeric_dtype(column) and "date" in field.casefold():
                comparable = pd.to_datetime(column, errors="coerce")
                target = pd.to_datetime(value, errors="coerce")
            else:
                comparable = pd.to_numeric(column, errors="coerce")
                target = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
            mask = {
                ">": comparable.gt, "gt": comparable.gt,
                ">=": comparable.ge, "gte": comparable.ge,
                "<": comparable.lt, "lt": comparable.lt,
                "<=": comparable.le, "lte": comparable.le,
            }[operation](target)
        else:
            mask = column.astype("string").str.casefold().str.contains(str(value).casefold(), regex=False, na=False)
        result = result.loc[mask.fillna(False)]
    return result.copy()


def header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def find_column(columns, choices: set[str]) -> str | None:
    return next((column for column in columns if header_key(column) in choices), None)


def canonical_date(value: str) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            return raw
    match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", raw)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return raw
    return raw


def conflicting_order_ids(schemas: list[dict], rows_by_file: dict[str, list[dict]],
                          scope: str = "global") -> list[str]:
    seen = {}
    conflicts = set()
    for schema in schemas:
        columns = schema.get("columns", [])
        id_column = find_column(columns, {"orderid", "madonhang", "receiptno", "id"})
        amount_column = find_column(columns, {"amount", "amountraw", "totalamount", "giatridon", "paid", "price", "revenue"})
        date_column = find_column(columns, {"orderdate", "ngaydat", "saledate", "createdat", "date"})
        status_column = find_column(columns, {"status", "orderstatus"})
        if not id_column or not amount_column:
            continue
        for row in rows_by_file.get(schema["name"], []):
            order_id = str(row.get(id_column) or "").strip()
            if not order_id:
                continue
            fingerprint = (
                canonical_date(row.get(date_column)) if date_column else "",
                str(row.get(amount_column) or "").strip(),
                str(row.get(status_column) or "").casefold().strip() if status_column else "",
            )
            key = (schema["name"], order_id) if scope == "per_file" else order_id
            if key in seen and fingerprint != seen[key]:
                conflicts.add(order_id)
            seen.setdefault(key, fingerprint)
    return sorted(conflicts)


def cross_file_conflicting_order_ids(schemas: list[dict], rows_by_file: dict[str, list[dict]]) -> list[str]:
    """Find identifiers with different fact rows in more than one uploaded file."""
    by_id: dict[str, dict[str, set[tuple]]] = {}
    for schema in schemas:
        columns = schema.get("columns", [])
        id_column = find_column(columns, {"orderid", "madonhang", "receiptno", "id"})
        amount_column = find_column(columns, {"amount", "amountraw", "totalamount", "giatridon", "paid", "price", "revenue"})
        date_column = find_column(columns, {"orderdate", "ngaydat", "saledate", "createdat", "date"})
        status_column = find_column(columns, {"status", "orderstatus"})
        if not id_column or not amount_column:
            continue
        for row in rows_by_file.get(schema["name"], []):
            order_id = str(row.get(id_column) or "").strip()
            if order_id:
                fingerprint = (
                    canonical_date(row.get(date_column)) if date_column else "",
                    str(row.get(amount_column) or "").strip(),
                    str(row.get(status_column) or "").casefold().strip() if status_column else "",
                )
                by_id.setdefault(order_id, {}).setdefault(schema["name"], set()).add(fingerprint)
    return sorted(order_id for order_id, files in by_id.items()
                  if len(files) > 1 and len(set.union(*files.values())) > 1)


def resolve_duplicate_orders(frame: pd.DataFrame, policy: str = "ask",
                             scope: str = "global") -> pd.DataFrame:
    if "id" not in frame:
        return frame
    subset = ["_source_file", "id"] if scope == "per_file" and "_source_file" in frame else ["id"]
    duplicate = frame[frame["id"].notna() & frame.duplicated(subset=subset, keep=False)]
    if duplicate.empty:
        return frame
    compared = [column for column in ("date", "amount", "status") if column in frame]
    for order_id, group in duplicate.groupby(subset, sort=False):
        if any(group[column].astype("string").fillna("<missing>").nunique() > 1 for column in compared):
            if policy not in {"keep_first", "keep_latest"}:
                raise ValueError(f"Conflicting rows for order ID {order_id}; clarify duplicate resolution")
    if policy == "keep_latest" and "date" in frame:
        return frame.sort_values("date", na_position="first").drop_duplicates(subset=subset, keep="last").sort_index()
    return frame.drop_duplicates(subset=subset, keep="first")


def deduplicate_by_keys(frame: pd.DataFrame, keys: list[str], policy: str = "ask") -> pd.DataFrame:
    """Remove duplicate records only when the selected key policy is unambiguous."""
    keys = list(dict.fromkeys(key for key in keys if key in frame.columns))
    if not keys:
        raise ValueError("Deduplication keys are not present in the normalized data")
    duplicate = frame[frame.duplicated(subset=keys, keep=False)]
    if duplicate.empty:
        return frame
    compared = [column for column in frame.columns if column not in keys and not column.startswith("_")]
    for key_value, group in duplicate.groupby(keys, sort=False, dropna=False):
        if any(group[column].astype("string").fillna("<missing>").nunique() > 1 for column in compared):
            if policy not in {"keep_first", "keep_latest"}:
                raise ValueError(f"Conflicting rows for composite key {key_value}; clarify duplicate resolution")
    if policy == "keep_latest":
        order_column = "date" if "date" in frame.columns else None
        if order_column:
            return frame.sort_values(order_column, na_position="first").drop_duplicates(subset=keys, keep="last").sort_index()
        raise ValueError("keep_latest deduplication requires a normalized date field")
    return frame.drop_duplicates(subset=keys, keep="first")
