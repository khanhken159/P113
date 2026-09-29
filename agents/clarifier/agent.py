import csv
import math
import os
import re
import unicodedata
from collections import Counter
from typing import Any
from pathlib import Path

def plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text.casefold().replace("đ", "d")).encode("ascii", "ignore").decode()


def is_money_column(column: str) -> bool:
    name = re.sub(r"[^a-z0-9]", "", plain(column))
    return any(term in name for term in ("amount", "price", "revenue", "fare", "giatridon", "paid"))


def currency_column(schema: dict) -> str | None:
    return next((column for column in schema.get("columns", [])
                 if re.sub(r"[^a-z]", "", column.casefold()) in
                 {"currency", "currencycode", "currencyunit"}), None)


def valid_rate(value) -> bool:
    try:
        rate_text = str(value).strip()
        return (not re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", rate_text)
                and math.isfinite(float(rate_text)) and float(rate_text) > 0)
    except (TypeError, ValueError):
        return False


def llm_question_needs_followup(question: str, request: str, schemas: list[dict], answers: dict) -> bool:
    """Do not repeat data checks already answered by deterministic preflight."""
    normalized_question = plain(question)
    normalized_request = plain(request + " " + str(answers.get("additional_context", "")))
    append = answers.get("operation") == "append" or (
        len(schemas) > 1 and any(term in normalized_request for term in
                                 ("gop don hang", "gop cac dong", "noi cac dong", "append", "union", "stack"))
    )
    if append and any(term in normalized_question for term in ("join key", "join column", "khoa join", "khoa nao", "cot id")):
        return False
    requests_source_breakdown = any(term in normalized_request for term in
                                    ("tung nguon", "moi nguon", "per source", "each source", "per file", "each file"))
    if append and requests_source_breakdown and any(term in normalized_question for term in
                                                    ("ten nguon", "source name", "label", "ten hien thi")):
        return False
    # The deterministic scanner parses every amount and asks about any ambiguous separator.
    if any(term in normalized_question for term in ("dinh dang so", "dau phay", "dau cham", "decimal separator", "thousands separator", "number format")):
        return False
    return True


def custom_csv_questions(request: str, schemas: list[dict], answers: dict) -> tuple[list[str], list[dict]]:
    lower = plain(request + " " + str(answers.get("additional_context", "")))
    questions, fields = [], []
    money_schemas = [schema for schema in schemas if any(is_money_column(column) for column in schema.get("columns", []))]
    data_root = os.getenv("FLOWFORGE_DATA_DIR", "")
    rows_by_file = {}
    if data_root:
        for schema in schemas:
            path = Path(data_root) / "custom_csv" / schema["name"]
            if path.is_file():
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    rows_by_file[schema["name"]] = list(csv.DictReader(stream))
    if rows_by_file and any(term in lower for term in ("join", "merge", "ghep")):
        from agents.csv_quality import header_key
        compact_request = header_key(plain(request))
        requested_join_keys = answers.get("join_keys") or {}
        for fact_schema in schemas:
            fact_rows = rows_by_file.get(fact_schema["name"], [])
            for dimension_schema in schemas:
                if dimension_schema["name"] == fact_schema["name"]:
                    continue
                dimension_rows = rows_by_file.get(dimension_schema["name"], [])
                if not fact_rows or len(dimension_rows) > len(fact_rows):
                    continue
                shared = [column for column in fact_schema.get("columns", [])
                          if column in dimension_schema.get("columns", [])]
                mentioned = [column for column in shared
                             if header_key(column) in compact_request]
                existing = (requested_join_keys.get(dimension_schema.get("original_name", dimension_schema["name"]))
                            if isinstance(requested_join_keys, dict) else requested_join_keys)
                if len(mentioned) != 1 or existing:
                    continue
                key = mentioned[0]
                duplicate_key = any(count > 1 for value, count in Counter(
                    str(row.get(key) or "").strip() for row in dimension_rows
                ).items() if value)
                if not duplicate_key:
                    continue
                extra_keys = [column for column in shared if column != key
                              and header_key(column).endswith(("id", "code", "key"))]
                resolving_column = next((column for column in extra_keys
                                         if len({(str(row.get(key) or "").strip(),
                                                   str(row.get(column) or "").strip())
                                                 for row in dimension_rows}) == len(dimension_rows)), None)
                if resolving_column:
                    name = dimension_schema.get("original_name", dimension_schema["name"])
                    questions.append(
                        f"{key} không duy nhất trong {name}; ghép thêm {resolving_column} để xác định đúng bản ghi không?"
                    )
                    fields.append({"key": "join_keys", "file": name, "type": "text"})
                    break
            if questions and fields and fields[-1].get("key") == "join_keys":
                break
    has_amount = bool(money_schemas)
    asks_money = has_amount and any(word in lower for word in
                                    ("doanh thu", "revenue", "amount", "tong tien", "total amount", "gross"))
    if (len(schemas) > 1 and answers.get("operation") != "append"
            and not any(word in lower for word in
                        ("gop don hang", "noi cac dong", "append", "union", "stack", "combine", "join", "merge", "ghep"))):
        questions.append("Bạn muốn nối các dòng dữ liệu của các file (UNION ALL)? Nếu muốn JOIN, hãy sửa yêu cầu và nêu rõ các cột khóa.")
        fields.append({"key": "operation", "type": "select", "options": ["append"]})
    if asks_money:
        explicit_status = any(word in lower for word in
                              ("tat ca trang thai", "moi trang thai", "all statuses", "completed", "delivered",
                               "hoan tat", "hoan thanh", "thanh cong", "huy", "cancelled", "returned"))
        if not explicit_status and answers.get("status_scope") not in {"all", "completed"}:
            questions.append("Doanh thu tính mọi đơn, hay chỉ đơn hoàn tất/giao thành công?")
            fields.append({"key": "status_scope", "type": "select", "options": ["all", "completed"]})
        currencies = answers.get("currency_by_file") or {}
        for schema in money_schemas:
            name = schema.get("original_name", schema["name"])
            currency_field = currency_column(schema)
            if currency_field:
                rows = rows_by_file.get(schema["name"], [])
                if any(not str(row.get(currency_field) or "").strip() for row in rows):
                    missing = str((answers.get("missing_currency_by_file") or {}).get(name, "")).upper().strip()
                    if not re.fullmatch(r"[A-Z]{3}", missing):
                        questions.append(f"{name} có dòng thiếu currency. Đơn vị tiền của các dòng đó là gì?")
                        fields.append({"key": "missing_currency_by_file", "file": name, "type": "currency"})
                continue
            code = str(currencies.get(name, "")).upper().strip()
            if not re.fullmatch(r"[A-Z]{3}", code):
                questions.append(f"Đơn vị tiền của cột doanh thu trong {name} là gì (mã ISO, ví dụ VND)?")
                fields.append({"key": "currency_by_file", "file": name, "type": "currency"})
        target = str(answers.get("target_currency", "")).upper().strip()
        if not re.fullmatch(r"[A-Z]{3}", target):
            questions.append("Bạn muốn doanh thu đầu ra dùng đơn vị tiền nào (ví dụ VND)?")
            fields.append({"key": "target_currency", "type": "currency"})
        if re.fullmatch(r"[A-Z]{3}", target):
            requested_currency_rates = set()
            for schema in money_schemas:
                name = schema.get("original_name", schema["name"])
                currency_field = currency_column(schema)
                if currency_field:
                    rows = rows_by_file.get(schema["name"], [])
                    codes = {str(row.get(currency_field) or "").upper().strip() for row in rows}
                    codes.discard("")
                    missing = str((answers.get("missing_currency_by_file") or {}).get(name, "")).upper().strip()
                    if missing:
                        codes.add(missing)
                    for source_code in sorted(codes):
                        if source_code == target or source_code in requested_currency_rates:
                            continue
                        requested_currency_rates.add(source_code)
                        if not valid_rate((answers.get("currency_rates") or {}).get(source_code)):
                            questions.append(f"1 {source_code} đổi thành bao nhiêu {target}? Nhập tỷ giá không có dấu phân tách hàng nghìn.")
                            fields.append({"key": "currency_rates", "file": source_code, "type": "rate"})
                    continue
                code = str(currencies.get(name, "")).upper().strip()
                if re.fullmatch(r"[A-Z]{3}", code) and code != target:
                    rate = (answers.get("conversion_rates") or {}).get(name)
                    if not valid_rate(rate):
                        questions.append(f"1 {code} từ {name} đổi thành bao nhiêu {target}? Nhập tỷ giá dạng 25000 hoặc 0.00004, không dùng dấu phân tách hàng nghìn.")
                        fields.append({"key": "conversion_rates", "file": name, "type": "rate"})
        if data_root:
            from agents.money import parse_amount
            formats = answers.get("number_format_by_file") or {}
            currency_formats = answers.get("number_format_by_currency") or {}
            for schema in money_schemas:
                name = schema.get("original_name", schema["name"])
                rows = rows_by_file.get(schema["name"], [])
                currency_field = currency_column(schema)
                amount_column = next((column for column in schema.get("columns", []) if is_money_column(column)), None)
                if not amount_column:
                    continue
                if any(not str(row.get(amount_column) or "").strip() for row in rows):
                    policy = (answers.get("missing_amount_policy") or {}).get(name)
                    if policy not in {"exclude", "reject"}:
                        questions.append(f"{name} có giá trị tiền trống. Bỏ các dòng này khỏi doanh thu hay dừng để sửa dữ liệu?")
                        fields.append({"key": "missing_amount_policy", "file": name,
                                       "type": "select", "options": ["exclude", "reject"]})
                for row in rows:
                    code = (str(row.get(currency_field) or "").upper().strip()
                            if currency_field else str(currencies.get(name, "")).upper().strip())
                    if not code and currency_field:
                        code = str((answers.get("missing_currency_by_file") or {}).get(name, "")).upper().strip()
                    if not re.fullmatch(r"[A-Z]{3}", code):
                        continue
                    try:
                        number_format = (currency_formats.get(code) if currency_field else None) or formats.get(name, "")
                        parse_amount(row.get(amount_column), code, number_format)
                    except ValueError as error:
                        if "Ambiguous money value" in str(error):
                            if currency_field:
                                questions.append(f"Trong {name} ({code}), giá trị {row.get(amount_column)!r} dùng dấu phân cách hàng nghìn hay thập phân?")
                                fields.append({"key": "number_format_by_currency", "file": code,
                                               "type": "select", "options": ["thousands", "decimal"]})
                                break
                            questions.append(f"Trong {name}, dấu phân cách ở giá trị {row.get(amount_column)!r} là hàng nghìn hay thập phân?")
                            fields.append({"key": "number_format_by_file", "file": name,
                                           "type": "select", "options": ["thousands", "decimal"]})
                        break
    if rows_by_file:
        from agents.csv_quality import conflicting_order_ids, cross_file_conflicting_order_ids, find_column
        asks_status_metrics = any(term in lower for term in (
            "completed orders", "cancelled orders", "canceled orders", "completed trips",
            "cancelled trips", "canceled trips", "completion rate", "cancellation rate",
            "so don hoan thanh", "so don huy", "so chuyen hoan thanh", "so chuyen huy",
            "completed_count", "cancelled_count", "completion_rate", "cancellation_rate",
        ))
        if asks_status_metrics:
            completed_values = answers.get("completed_status_values_by_file") or {}
            cancelled_values = answers.get("cancelled_status_values_by_file") or {}
            known_completed = {"complete", "completed", "delivered", "done"}
            known_cancelled = {"cancelled", "canceled", "returned"}
            for schema in schemas:
                status_column = find_column(schema.get("columns", []), {"status", "orderstatus", "tripstatus", "ridestatus"})
                if not status_column:
                    continue
                name = schema.get("original_name", schema["name"])
                counts = Counter(str(row.get(status_column) or "").strip() for row in rows_by_file.get(schema["name"], []))
                values = [(value, count) for value, count in counts.items() if value]
                unknown = [(value, count) for value, count in values
                           if value.casefold() not in known_completed | known_cancelled]
                if unknown and not completed_values.get(name):
                    shown = ", ".join(f"{value} ({count})" for value, count in sorted(unknown))
                    questions.append(
                        f"{name}: các status chưa phân loại là {shown}. Nhập chính xác giá trị nào là completed, phân tách bằng dấu phẩy; nhập none nếu không có giá trị nào?"
                    )
                    fields.append({"key": "completed_status_values_by_file", "file": name, "type": "text"})
                if unknown and any(term in lower for term in ("cancelled", "canceled", "huy", "cancellation_rate")) and not cancelled_values.get(name):
                    shown = ", ".join(f"{value} ({count})" for value, count in sorted(unknown))
                    questions.append(
                        f"{name}: trong các status chưa phân loại ({shown}), giá trị nào là cancelled, phân tách bằng dấu phẩy; nhập none nếu không có giá trị nào?"
                    )
                    fields.append({"key": "cancelled_status_values_by_file", "file": name, "type": "text"})
        requests_time_group = any(term in lower for term in
                                  ("day", "daily", "month", "monthly", "week", "weekly", "ngay", "thang", "tuan"))
        date_formats = answers.get("date_format_by_file") or {}
        if requests_time_group:
            for schema in schemas:
                date_column = find_column(schema.get("columns", []),
                                          {"date", "orderdate", "createdat", "saledate", "timestamp", "datetime", "movementdate"})
                if not date_column:
                    continue
                name = schema.get("original_name", schema["name"])
                dates = [str(row.get(date_column) or "").strip()
                         for row in rows_by_file.get(schema["name"], [])]
                ambiguous_dates = []
                zoned_dates = False
                for value in dates:
                    match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s|$)", value)
                    if match:
                        first, second = int(match.group(1)), int(match.group(2))
                        if 1 <= first <= 12 and 1 <= second <= 12 and first != second:
                            ambiguous_dates.append(value)
                    if re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", value, re.I):
                        zoned_dates = True
                if ambiguous_dates and date_formats.get(name) not in {"day_first", "month_first"}:
                    questions.append(f"{name} có ngày như {ambiguous_dates[0]!r}; ngày/tháng hay tháng/ngày?")
                    fields.append({"key": "date_format_by_file", "file": name, "type": "select",
                                   "options": ["day_first", "month_first"]})
                if zoned_dates and not answers.get("report_timezone"):
                    questions.append(f"{name} có timestamp kèm múi giờ. Bạn muốn nhóm ngày/tháng theo múi giờ nào?")
                    fields.append({"key": "report_timezone", "type": "text"})
        cross_file_conflicts = cross_file_conflicting_order_ids(schemas, rows_by_file)
        scope = answers.get("duplicate_scope")
        if cross_file_conflicts and scope not in {"global", "per_file"}:
            questions.append(f"ID {', '.join(cross_file_conflicts[:5])} xuất hiện ở nhiều file với dữ liệu khác nhau. ID này là chung cho mọi file hay chỉ duy nhất trong từng file?")
            fields.append({"key": "duplicate_scope", "type": "select",
                           "options": ["global", "per_file"]})
        conflicts = conflicting_order_ids(schemas, rows_by_file, "per_file" if scope == "per_file" else "global")
        if cross_file_conflicts and scope not in {"global", "per_file"}:
            conflicts = []
        if conflicts and answers.get("duplicate_resolution") not in {"keep_first", "keep_latest", "reject"}:
            questions.append(f"Đơn hàng {', '.join(conflicts[:5])} có cùng ID nhưng ngày/tiền/trạng thái khác nhau. Chọn quy tắc xử lý hoặc sửa file.")
            fields.append({"key": "duplicate_resolution", "type": "select",
                           "options": ["reject", "keep_first", "keep_latest"]})
        if any(term in lower for term in ("join", "merge", "ghep")):
            facts = [schema for schema in money_schemas if find_column(schema.get("columns", []),
                     {"customerid", "khachhangid"})]
            dimensions = [schema for schema in schemas if schema not in money_schemas and find_column(
                          schema.get("columns", []), {"customerid", "khachhangid"})]
            if len(facts) == 1 and len(dimensions) == 1:
                fact, dimension = facts[0], dimensions[0]
                fact_key = find_column(fact["columns"], {"customerid", "khachhangid"})
                dimension_key = find_column(dimension["columns"], {"customerid", "khachhangid"})
                fact_ids = {row.get(fact_key) for row in rows_by_file.get(fact["name"], [])}
                dimension_ids = [row.get(dimension_key) for row in rows_by_file.get(dimension["name"], [])]
                duplicates = sorted(item for item, count in Counter(dimension_ids).items() if count > 1 and item)
                orphans = sorted(item for item in fact_ids - set(dimension_ids) if item)
                if duplicates and answers.get("join_duplicate_policy") not in {"keep_first", "reject"}:
                    questions.append(f"{dimension.get('original_name', dimension['name'])} có khóa khách hàng trùng: {', '.join(duplicates[:5])}. Chọn cách xử lý.")
                    fields.append({"key": "join_duplicate_policy", "type": "select", "options": ["reject", "keep_first"]})
                if orphans and answers.get("orphan_policy") not in {"keep_unknown", "drop", "reject"}:
                    questions.append(f"Đơn hàng tham chiếu khách hàng không có trong bảng JOIN: {', '.join(orphans[:5])}. Giữ dưới nhóm Unknown, bỏ dòng, hay dừng?")
                    fields.append({"key": "orphan_policy", "type": "select",
                                   "options": ["keep_unknown", "drop", "reject"]})
        if any(term in lower for term in ("join", "merge", "ghep")):
            compact_request = header_key(plain(request))
            join_key_answers = answers.get("join_keys") or {}
            needs_join_key_answer = any(field.get("key") == "join_keys" for field in fields)
            if not needs_join_key_answer or join_key_answers:
                for fact_schema in schemas:
                    fact_rows = rows_by_file.get(fact_schema["name"], [])
                    for dimension_schema in schemas:
                        if fact_schema["name"] == dimension_schema["name"]:
                            continue
                        dimension_rows = rows_by_file.get(dimension_schema["name"], [])
                        if not fact_rows or len(dimension_rows) > len(fact_rows):
                            continue
                        shared = [column for column in fact_schema.get("columns", [])
                                  if column in dimension_schema.get("columns", [])]
                        mentioned = [column for column in shared
                                     if header_key(column) in compact_request]
                        if any(header_key(column) in {"customerid", "khachhangid"} for column in mentioned):
                            continue
                        dimension_name = dimension_schema.get("original_name", dimension_schema["name"])
                        supplied = (join_key_answers.get(dimension_name) or join_key_answers.get(dimension_schema["name"])) \
                            if isinstance(join_key_answers, dict) else join_key_answers
                        if supplied:
                            requested = [part.strip() for part in re.split(r",|;|\+|\band\b|\bvà\b", str(supplied), flags=re.I)
                                         if part.strip()]
                            by_key = {header_key(column): column for column in shared}
                            keys = [by_key[header_key(column)] for column in requested if header_key(column) in by_key]
                        else:
                            keys = mentioned
                        if len(keys) != len(mentioned) and mentioned:
                            continue
                        if not keys:
                            candidates = [column for column in shared
                                          if header_key(column).endswith(("id", "sku", "code", "key"))]
                            keys = candidates if len(candidates) == 1 else []
                        if not keys:
                            continue
                        right_key_rows = [tuple(str(row.get(key) or "").strip() for key in keys)
                                          for row in dimension_rows]
                        right_key_set = {key for key in right_key_rows if all(key)}
                        if len(right_key_set) < len([key for key in right_key_rows if all(key)]) \
                                and answers.get("join_duplicate_policy") not in {"keep_first", "reject"}:
                            questions.append(f"JOIN {dimension_name} có khóa {', '.join(keys)} trùng nhau. Chọn giữ bản đầu hay dừng để sửa dữ liệu?")
                            fields.append({"key": "join_duplicate_policy", "type": "select",
                                           "options": ["reject", "keep_first"]})
                        fact_key_rows = [tuple(str(row.get(key) or "").strip() for key in keys)
                                         for row in fact_rows]
                        orphans = sorted({key for key in fact_key_rows if all(key) and key not in right_key_set})
                        if orphans and answers.get("orphan_policy") not in {"keep_unknown", "drop", "reject"}:
                            examples = [" + ".join(key) for key in orphans[:5]]
                            questions.append(f"{len(orphans)} khóa trong {fact_schema.get('original_name', fact_schema['name'])} không có trong {dimension_name} ({', '.join(examples)}). Giữ Unknown, bỏ dòng hay dừng?")
                            fields.append({"key": "orphan_policy", "type": "select",
                                           "options": ["keep_unknown", "drop", "reject"]})
                        break
                    if any(field.get("key") == "orphan_policy" for field in fields):
                        break
    return questions, fields


def run(state: dict[str, Any]) -> dict[str, Any]:
    """Render only unresolved items explicitly approved by Ambiguity Resolver."""
    resolution = state.get("ambiguity_resolution") or {}
    questions = resolution.get("questions_for_user", [])
    fields = resolution.get("clarification_fields", [])
    if not isinstance(questions, list):
        questions = []
    if not isinstance(fields, list):
        fields = []
    return {
        "needs_clarification": bool(questions),
        "clarification_questions": questions,
        "clarification_fields": fields,
        "ambiguity_resolution": resolution,
        "resolved_business_rules": resolution.get("resolved_business_rules", {}),
        "status": "clarification_required" if questions else "ready_for_planning",
    }
