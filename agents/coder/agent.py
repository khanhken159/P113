import json
import re
from pathlib import Path

from agents.llm import generate_text
from agents.prompt_loader import load_system_prompt

ROOT = Path(__file__).resolve().parents[2]
NORMALIZED_SQL_TYPES = {
    "date": "TIMESTAMP", "amount": "DOUBLE", "distance": "DOUBLE",
    "duration": "DOUBLE", "value": "DOUBLE", "revenue": "DOUBLE",
}

PIPELINE_PATH = (
    ROOT
    / "generated"
    / "generated_pipeline.py"
)


def _custom_sql_aggregation(pipeline_code: str, sql_query: dict[str, str] | None = None) -> str:
    """Replace the pandas aggregation block with the multi-output SQL runtime."""
    start = pipeline_code.index("if group_columns:\n    grouped = data.groupby")
    end = pipeline_code.index("OUT.mkdir(parents=True, exist_ok=True)", start)
    replacement = '''
from agents.coder.runtime import execute_output_specs
SPEC["sql_by_output"] = __SQL_BY_OUTPUT_LITERAL__
result = execute_output_specs(
    data=data,
    spec=SPEC,
    root=ROOT,
    output_dir=OUT,
    optimize_query=optimize_query,
    run_mode=os.environ.get("FLOWFORGE_SQL_RUN_MODE", "baseline"),
)

'''.replace("__SQL_BY_OUTPUT_LITERAL__", repr(sql_query or {}))
    return pipeline_code[:start] + replacement + pipeline_code[end:]

def _sql_contract(plan: dict, output_spec: dict | None = None) -> dict:
    if plan.get("dataset_mode") == "generic_csv":
        from agents.coder.runtime import _default_query
        columns = set()
        columns.update(plan.get("group_dimensions", []))
        for item in plan.get("files", []):
            if item.get("name") == plan.get("base_file") or plan.get("union_files"):
                columns.update(item.get("fields", {}).keys())
                columns.update(item.get("constants", {}).keys())
            columns.update(item.get("dimensions", {}).keys())
        for item in plan.get("union_files", []):
            columns.update(item.get("fields", {}))
            columns.update(item.get("constants", {}))
        columns.update(item.get("name") for item in plan.get("derived_fields", []) if item.get("name"))
        spec = output_spec or {}
        columns.update(spec.get("column_mapping", {}).values())
        columns.update(spec.get("column_mapping", {}).keys())
        for window in spec.get("window_specs", []):
            columns.update(window.get("partition_by", []))
            columns.update(item if isinstance(item, str) else item.get("field", "") for item in window.get("order_by", []))
            if window.get("value_field"):
                columns.add(window["value_field"])
        for derived in spec.get("derived_metrics", []):
            if derived.get("name"):
                columns.add(derived["name"])
            columns.update(derived.get("inputs", []))
        group_by = spec.get("group_by", plan.get("group_by"))
        group_dimensions = spec.get("group_dimensions", plan.get("group_dimensions", []))
        metrics = spec.get("metrics", plan.get("metrics", []))
        columns.update(group_dimensions)
        is_rows = spec.get("kind") in {"rows", "table", "row_level"}
        window_names = [item.get("name") for item in spec.get("window_specs", []) if item.get("name")]
        default_columns = ((list(spec.get("column_mapping", {})) + window_names) if is_rows else
                           ((["report_period"] if group_by != "all" else []) + group_dimensions + metrics + window_names))
        output_columns = spec.get("columns") or default_columns
        return {
            "table": "flowforge_data",
            "columns": sorted(columns),
            "output_columns": output_columns,
            "column_types": {column: NORMALIZED_SQL_TYPES[column] for column in columns if column in NORMALIZED_SQL_TYPES},
            "preprocessing_complete": True,
            "preprocessed_fields": sorted(column for column in columns if column in NORMALIZED_SQL_TYPES),
            "projection_only": is_rows and not spec.get("window_specs") and not spec.get("post_window_filters"),
            "projection_mapping": {target: spec.get("column_mapping", {}).get(target, target) for target in output_columns} if is_rows else {},
        }

    output_columns = ["report_period"]
    if plan.get("group_by") == "day_and_month":
        output_columns.extend(["daily_revenue", "monthly_revenue", "completed_orders"])
    else:
        output_columns.extend(["total_revenue", "completed_orders"])
    if plan.get("include_channel_revenue"):
        output_columns.extend(f"{source}_revenue" for source in plan.get("sources", []))
    return {
        "table": "flowforge_orders",
        "columns": ["report_period", "revenue", "order_id", "source"],
        "output_columns": output_columns,
    }


def _default_sql(plan: dict, output_spec: dict | None = None) -> str:
    if plan.get("dataset_mode") == "generic_csv":
        from agents.coder.runtime import _default_query
        return _default_query(plan, output_spec or {})[0]
        output_spec = output_spec or {}
        if output_spec.get("kind") in {"rows", "table", "row_level"}:
            mapping = output_spec.get("column_mapping", {})
            output_columns = output_spec.get("columns") or list(mapping)
            if not output_columns:
                raise ValueError(f"Row-level output {output_spec.get('name')} must declare output columns")
            select_parts = [
                f'"{str(mapping.get(column, column)).replace(chr(34), chr(34) * 2)}" AS "{str(column).replace(chr(34), chr(34) * 2)}"'
                for column in output_columns
            ]
            return "SELECT " + ", ".join(select_parts) + " FROM flowforge_data"
        group_by = output_spec.get("group_by", plan.get("group_by"))
        group_dimensions = output_spec.get("group_dimensions", plan.get("group_dimensions", []))
        metrics = output_spec.get("metrics", plan.get("metrics", []))
        period_expression = f"date_trunc('{group_by}', \"date\")" if group_by in {"day", "week", "month"} else None
        select_parts = ([f'{period_expression} AS "report_period"'] if period_expression else [])
        select_parts.extend(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in group_dimensions)
        for metric in metrics:
            if metric == "record_count":
                expression = "COUNT(*)"
            elif metric == "distinct_order_count":
                expression = "COUNT(DISTINCT id)"
            elif metric == "completed_count":
                values = ", ".join("'" + value.replace("'", "''") + "'" for value in plan.get("completed_status_values", []))
                expression = f"COUNT(*) FILTER (WHERE status IN ({values}))" if values else "0"
            elif metric == "cancelled_count":
                values = ", ".join("'" + value.replace("'", "''") + "'" for value in plan.get("cancelled_status_values", []))
                expression = f"COUNT(*) FILTER (WHERE status IN ({values}))" if values else "0"
            elif metric == "total_amount":
                expression = "SUM(amount)"
            elif metric == "min_amount":
                expression = "MIN(amount)"
            elif metric == "max_amount":
                expression = "MAX(amount)"
            elif metric == "median_amount":
                expression = "median(amount)"
            elif metric == "average_amount":
                expression = "AVG(amount)"
            elif metric == "average_distance":
                expression = "COALESCE(AVG(distance), 0)"
            elif metric == "average_duration":
                expression = "COALESCE(AVG(duration), 0)"
            elif metric == "sum_value":
                expression = "SUM(value)"
            elif metric in {"completion_rate", "cancellation_rate"}:
                status_values = plan.get("completed_status_values", []) if metric == "completion_rate" else plan.get("cancelled_status_values", [])
                values = ", ".join("'" + value.replace("'", "''") + "'" for value in status_values)
                expression = f"COUNT(*) FILTER (WHERE lower(trim(status)) IN ({values})) * 1.0 / NULLIF(COUNT(*), 0)" if values else "NULL"
            elif metric == "customer_lifetime_value":
                expression = "SUM(amount)"
            elif metric == "running_revenue":
                expression = "SUM(SUM(amount)) OVER (ORDER BY report_period ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
            elif metric == "customer_rank":
                expression = "RANK() OVER (ORDER BY SUM(amount) DESC)"
            else:
                raise ValueError(f"Unsupported SQL metric: {metric}")
            select_parts.append(f'{expression} AS "{metric}"')
        query = "SELECT " + ", ".join(select_parts) + " FROM flowforge_data"
        if period_expression or group_dimensions:
            group_sql = ", ".join(part for part in (["1"] if period_expression else []) +
                                   [f'"{column.replace(chr(34), chr(34) * 2)}"' for column in group_dimensions])
            query += " GROUP BY " + group_sql + " ORDER BY " + group_sql
        return query

    channel_selects = []
    if plan.get("include_channel_revenue"):
        for source in plan.get("sources", []):
            escaped = source.replace("'", "''")
            channel_selects.append(f"SUM(revenue) FILTER (WHERE source = '{escaped}') AS {source}_revenue")
    if plan.get("group_by") == "day_and_month":
        daily_columns = [
            "report_period",
            "SUM(revenue) AS daily_revenue",
            "COUNT(order_id) AS completed_orders",
            *channel_selects,
        ]
        select_sql = ", ".join(daily_columns)
        channel_names = ", ".join(f'daily."{source}_revenue"' for source in plan.get("sources", []))
        extra = f", {channel_names}" if channel_names else ""
        return (
            "WITH daily AS (SELECT " + select_sql + " FROM flowforge_orders GROUP BY report_period), "
            "monthly AS (SELECT SUBSTR(report_period, 1, 7) AS report_month, "
            "SUM(revenue) AS monthly_revenue FROM flowforge_orders GROUP BY 1) "
            "SELECT daily.report_period, daily.daily_revenue, monthly.monthly_revenue, "
            "daily.completed_orders" + extra + " FROM daily JOIN monthly "
            "ON SUBSTR(daily.report_period, 1, 7) = monthly.report_month "
            "ORDER BY daily.report_period"
        )
    select_parts = [
        "report_period",
        "SUM(revenue) AS total_revenue",
        "COUNT(order_id) AS completed_orders",
        *channel_selects,
    ]
    return "SELECT " + ", ".join(select_parts) + " FROM flowforge_orders GROUP BY report_period ORDER BY report_period"


def _validate_sql_source_relations(sql: str, contract_table: str) -> None:
    cte_names = {
        name.casefold()
        for name in re.findall(r"(?:WITH|,)\s*([A-Za-z_][\w]*)\s+AS\s*\(", sql, re.IGNORECASE)
    }
    relations = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*|\"[^\"]+\"|'[^']+'|`[^`]+`)", sql, re.IGNORECASE)
    normalized_relations = {name.strip("\"'`").casefold() for name in relations}
    if contract_table.casefold() not in normalized_relations:
        raise ValueError(f"SQL must query the registered table {contract_table}")
    unexpected_relations = normalized_relations - cte_names - {contract_table.casefold()}
    if unexpected_relations:
        raise ValueError(f"SQL referenced unregistered table(s): {', '.join(sorted(unexpected_relations))}")


def _validate_generated_sql(sql: str, contract: dict) -> None:
    """Bind the query against an empty typed table before building the runner."""
    import duckdb

    table = contract["table"]
    if table not in {"flowforge_orders", "flowforge_data"}:
        raise ValueError("SQL contract contains an unsupported relation")
    if ";" in sql or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("SQL must be exactly one read-only SELECT or WITH query")
    if re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|COPY|ATTACH|DETACH|PRAGMA|CALL|INSTALL|LOAD|EXPORT|IMPORT)\b", sql, re.IGNORECASE):
        raise ValueError("SQL contains a disallowed non-read-only operation")
    if re.search(r"\b(read_csv|read_csv_auto|read_json|read_json_auto|read_parquet|parquet_scan|csv_scan|sqlite_scan|postgres_scan|glob)\s*\(", sql, re.IGNORECASE):
        raise ValueError("SQL may only read the runner's registered input relation")
    _validate_sql_source_relations(sql, table)
    if contract.get("projection_only"):
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="duckdb")
        # Explicit rows projections are an interface operation, not permission
        # to clean, filter, fill, aggregate or reinterpret the bound values.
        forbidden = (exp.With, exp.Join, exp.Where, exp.Group, exp.Having, exp.Qualify,
                     exp.Limit, exp.Union, exp.Intersect, exp.Except, exp.Distinct)
        if not isinstance(tree, exp.Select) or any(tree.find(kind) for kind in forbidden):
            raise ValueError("Rows projection contains an unplanned relational operation")
        if len(list(tree.find_all(exp.Select))) != 1:
            raise ValueError("Rows projection must read prepared values directly")
        expressions = tree.expressions
        targets = contract["output_columns"]
        if len(expressions) != len(targets):
            raise ValueError("Rows projection must preserve every declared output column")
        for target, expression in zip(targets, expressions):
            value = expression.this if isinstance(expression, exp.Alias) else expression
            if not isinstance(value, exp.Column) or value.name != contract["projection_mapping"][target]:
                raise ValueError(f"Rows projection {target!r} must preserve its mapped prepared value; extra transformations are unplanned")

    quote = lambda name: '"' + str(name).replace('"', '""') + '"'
    types = {**NORMALIZED_SQL_TYPES, **contract.get("column_types", {})}
    column_definitions = ", ".join(
        f"{quote(column)} {types.get(column, 'VARCHAR')}"
        for column in contract["columns"]
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"CREATE TABLE {quote(table)} ({column_definitions})")
        cursor = connection.execute(
            f"SELECT * FROM ({sql}) AS flowforge_sql_preflight LIMIT 0"
        )
        actual_columns = [description[0] for description in cursor.description]
        expected_columns = contract["output_columns"]
        if actual_columns != expected_columns:
            raise ValueError(
                "SQL output columns must match the plan in name and order; "
                f"expected {expected_columns}, got {actual_columns}"
            )
    finally:
        connection.close()


def _validated_output_query(sql: str, plan: dict, output_spec: dict) -> str:
    """Validate one output query, using a safe direct projection if a rows query drifts.

    A row projection has no business computation for the LLM to choose once its
    schema mapping is validated. The deterministic projection also prevents a
    second UNION/JOIN/filter over a relation the runner has already prepared.
    """
    contract = _sql_contract(plan, output_spec)
    try:
        _validate_generated_sql(sql, contract)
        return sql
    except Exception:
        if not contract.get("projection_only"):
            raise
        fallback = _default_sql(plan, output_spec)
        _validate_generated_sql(fallback, contract)
        return fallback


def _generate_sql_with_llm(state: dict, plan: dict, provider: str, output_spec: dict | None = None,
                           validation_feedback: dict | None = None) -> str:
    """Ask the model for one read-only SQL query, never for executable Python."""
    contract = _sql_contract(plan, output_spec)
    context = {
        "request": state.get("request", plan.get("request", "")),
        "dataset_mode": plan.get("dataset_mode", "orders"),
        "plan": plan,
        "output_spec": output_spec,
        "sql_contract": contract,
        "validation_feedback": validation_feedback,
    }
    response = generate_text(
        provider,
        json.dumps(context, ensure_ascii=False, indent=2),
        system_prompt=load_system_prompt("prompt_coder"),
    ).strip()
    fenced = re.fullmatch(r"```(?:sql)?\s*(.*?)\s*```", response, re.DOTALL | re.IGNORECASE)
    sql = fenced.group(1).strip() if fenced else response
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ValueError("SQL Coder returned an empty query")
    if ";" in sql:
        raise ValueError("SQL Coder must return exactly one SQL statement")
    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("SQL Coder must return a SELECT or WITH query")
    if re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|COPY|ATTACH|DETACH|PRAGMA|CALL|INSTALL|LOAD|EXPORT|IMPORT)\b", sql, re.IGNORECASE):
        raise ValueError("SQL Coder returned a disallowed non-read-only statement")
    if re.search(r"\b(read_csv|read_csv_auto|read_json|read_json_auto|read_parquet|parquet_scan|csv_scan|sqlite_scan|postgres_scan|glob)\s*\(", sql, re.IGNORECASE):
        raise ValueError("SQL Coder may only query the runner's registered data table")
    contract_table = contract["table"]
    _validate_sql_source_relations(sql, contract_table)
    baseline_path = ROOT / "generated" / "sql_baseline.sql"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(sql + ";\n", encoding="utf-8")
    return sql


PIPELINE_TEMPLATE = '''"""Runtime pipeline assembled from the Planner plan and SQL Coder query."""

import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.sql_optimizer import optimize_query
DATA = Path(os.environ.get("FLOWFORGE_DATA_DIR", ROOT / "test-data"))
OUT = ROOT / "output"

SPEC = __PLAN_LITERAL__
OPTIMIZATION_RUNS = []

def execute_optimized_sql(sql, frame):
    con = duckdb.connect(":memory:")
    con.register("flowforge_orders", frame)
    sql_mode = os.environ.get("FLOWFORGE_SQL_RUN_MODE", "baseline")
    selected_sql_path = ROOT / "generated" / "generated_query.sql"
    if sql_mode == "optimize":
        result = optimize_query(con, sql)
        sql = result["sql"]
    elif sql_mode == "selected" and selected_sql_path.exists():
        sql = selected_sql_path.read_text(encoding="utf-8").strip().rstrip(";")
        result = {"optimized": False, "optimizer_status": "SELECTED_SQL"}
    else:
        result = {"optimized": False, "optimizer_status": "BASELINE_ONLY"}
    if sql_mode == "baseline":
        baseline_path = ROOT / "generated" / "sql_baseline.sql"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(sql.rstrip().rstrip(";") + ";\\n", encoding="utf-8")
    if sql_mode != "selected":
        selected_sql_path.write_text(sql, encoding="utf-8")
        OPTIMIZATION_RUNS.append({"sql": sql, **result})
    return con.execute(sql).df()


def parse_mixed_date(series):
    """
    Xử lý nhiều định dạng ngày trong cùng một cột.

    Ví dụ:
    01/06/2025
    2025-06-01
    02-06-2025
    2025/06/04
    2025-06-01T09:15:00
    """

    return pd.to_datetime(
        series.astype("string").str.strip(),
        format="mixed",
        dayfirst=True,
        errors="coerce",
    )


def parse_revenue(value):
    """
    Chuyển nhiều định dạng tiền về số.

    Ví dụ:
    1.075.000
    1,250,000
    780,000đ
    1250000 VND
    """

    if pd.isna(value):
        return pd.NA

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return float(value)

    text = str(value).strip()

    if not text:
        return pd.NA

    is_negative = text.startswith("-")

    # Chỉ giữ lại các chữ số.
    digits = re.sub(
        r"[^0-9]",
        "",
        text,
    )

    if not digits:
        return pd.NA

    amount = float(digits)

    if is_negative:
        amount = -amount

    return amount


def clean_common_columns(df):
    """
    Làm sạch các cột sau khi đã đổi
    về schema chung.
    """

    df["order_id"] = (
        df["order_id"]
        .astype("string")
        .str.strip()
    )

    df["order_status"] = (
        df["order_status"]
        .astype("string")
        .str.lower()
        .str.strip()
    )

    df["revenue"] = (
        df["revenue"]
        .apply(parse_revenue)
    )

    df["revenue"] = pd.to_numeric(
        df["revenue"],
        errors="coerce",
    )

    return df


def normalize_shopee():
    file_path = (
        DATA
        / "orders_shopee.csv"
    )

    if not file_path.exists():
        raise FileNotFoundError(
            f"Không tìm thấy {file_path}"
        )

    df = pd.read_csv(file_path)

    required_columns = {
        "order_id",
        "order_date",
        "total_amount",
        "status",
    }

    missing_columns = (
        required_columns
        - set(df.columns)
    )

    if missing_columns:
        raise ValueError(
            "Shopee thiếu cột: "
            f"{sorted(missing_columns)}"
        )

    df = df.rename(columns={
        "status": "order_status",
        "total_amount": "revenue",
    })

    df["order_date"] = parse_mixed_date(
        df["order_date"]
    )

    df["source"] = "shopee"

    return clean_common_columns(df)


def normalize_tiki():
    file_path = (
        DATA
        / "orders_tiki.csv"
    )

    if not file_path.exists():
        raise FileNotFoundError(
            f"Không tìm thấy {file_path}"
        )

    df = pd.read_csv(file_path)

    required_columns = {
        "id",
        "created_at",
        "amount",
        "order_status",
    }

    missing_columns = (
        required_columns
        - set(df.columns)
    )

    if missing_columns:
        raise ValueError(
            "Tiki thiếu cột: "
            f"{sorted(missing_columns)}"
        )

    df = df.rename(columns={
        "id": "order_id",
        "created_at": "order_date",
        "amount": "revenue",
    })

    df["order_date"] = parse_mixed_date(
        df["order_date"]
    )

    df["source"] = "tiki"

    return clean_common_columns(df)


def normalize_website():
    file_path = (
        DATA
        / "orders_website.json"
    )

    if not file_path.exists():
        raise FileNotFoundError(
            f"Không tìm thấy {file_path}"
        )

    payload = json.loads(
        file_path.read_text(
            encoding="utf-8-sig"
        )
    )

    # Hỗ trợ hai kiểu JSON:
    # [{...}, {...}]
    # {"orders": [{...}, {...}]}
    if isinstance(payload, dict):
        records = payload.get(
            "orders",
            [],
        )
    else:
        records = payload

    if not isinstance(records, list):
        raise ValueError(
            "Website JSON phải là list "
            "hoặc object có trường orders"
        )

    df = pd.DataFrame(records)

    required_columns = {
        "OrderID",
        "OrderDate",
        "GrandTotal",
        "Status",
    }

    missing_columns = (
        required_columns
        - set(df.columns)
    )

    if missing_columns:
        raise ValueError(
            "Website thiếu cột: "
            f"{sorted(missing_columns)}"
        )

    df = df.rename(columns={
        "OrderID": "order_id",
        "OrderDate": "order_date",
        "Status": "order_status",
        "GrandTotal": "revenue",
    })

    df["order_date"] = parse_mixed_date(
        df["order_date"]
    )

    df["source"] = "website"

    return clean_common_columns(df)


def create_period_column(df):
    group_by = SPEC["group_by"]

    if group_by in {"day", "day_and_month"}:
        df["report_period"] = (
            df["order_date"]
            .dt.strftime("%Y-%m-%d")
        )

    elif group_by == "week":
        week_number = (
            (
                df["order_date"].dt.day
                - 1
            )
            // 7
            + 1
        ).astype(str)

        df["report_period"] = (
            df["order_date"]
            .dt.strftime("%Y-%m")
            + "-W"
            + week_number
        )

    elif group_by == "month":
        df["report_period"] = (
            df["order_date"]
            .dt.strftime("%Y-%m")
        )

    else:
        raise ValueError(
            "group_by chỉ được là "
            "day, week hoặc month"
        )

    return df


def main():
    source_map = {
        "shopee": normalize_shopee,
        "tiki": normalize_tiki,
        "website": normalize_website,
    }

    unknown_sources = [
        source
        for source in SPEC["sources"]
        if source not in source_map
    ]

    if unknown_sources:
        raise ValueError(
            "Nguồn không được hỗ trợ: "
            f"{unknown_sources}"
        )

    frames = [
        source_map[source]()
        for source in SPEC["sources"]
    ]

    orders = pd.concat(
        frames,
        ignore_index=True,
    )

    input_rows = len(orders)

    valid_statuses = [
        str(status)
        .lower()
        .strip()
        for status
        in SPEC["valid_statuses"]
    ]

    # Chỉ giữ trạng thái hợp lệ.
    valid = orders[
        orders["order_status"].isin(
            valid_statuses
        )
    ].copy()

    rows_after_status_filter = len(valid)

    # Bỏ dòng thiếu ID, ngày hoặc doanh thu.
    valid = valid.dropna(
        subset=[
            "order_id",
            "order_date",
            "revenue",
        ]
    )

    # Bỏ ID rỗng.
    valid = valid[
        valid["order_id"].str.len() > 0
    ]

    # Bỏ doanh thu âm.
    valid = valid[
        valid["revenue"] >= 0
    ]

    rows_before_dedup = len(valid)

    # Bỏ mã đơn trùng.
    if SPEC[
        "remove_duplicate_order_ids"
    ]:
        valid = valid.drop_duplicates(
            subset=["order_id"],
            keep="first",
        )

    duplicate_rows_removed = (
        rows_before_dedup
        - len(valid)
    )

    invalid_rows_removed = (
        rows_after_status_filter
        - rows_before_dedup
    )

    excluded_status_rows = (
        input_rows
        - rows_after_status_filter
    )

    if valid.empty:
        raise ValueError(
            "Không còn dữ liệu hợp lệ "
            "sau bước tiền xử lý"
        )

    valid = create_period_column(
        valid
    )

    query = __SQL_QUERY_LITERAL__
    result = execute_optimized_sql(query, valid)

    OUT.mkdir(
        parents=True,
        exist_ok=True,
    )

    generated = ROOT / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    if os.environ.get("FLOWFORGE_SQL_RUN_MODE", "baseline") != "selected":
        (generated / "generated_query.sql").write_text(
            "\\n\\n".join(
                f"-- {item['optimizer_status']}\\n{item['sql']}"
                for item in OPTIMIZATION_RUNS
            ),
            encoding="utf-8",
        )
        (generated / "sql_optimization_report.json").write_text(
            json.dumps(OPTIMIZATION_RUNS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    output_path = (
        OUT
        / "fct_daily_revenue.csv"
    )

    result.to_csv(
        output_path,
        index=False,
    )

    print(
        f"Input rows: {input_rows}"
    )

    print(
        "Excluded status rows: "
        f"{excluded_status_rows}"
    )

    print(
        "Invalid rows removed: "
        f"{invalid_rows_removed}"
    )

    print(
        "Duplicate rows removed: "
        f"{duplicate_rows_removed}"
    )

    print(
        f"Valid rows: {len(valid)}"
    )

    print(
        "Pipeline completed with "
        f"{len(result)} report rows"
    )


if __name__ == "__main__":
    main()
'''


def run(state: dict) -> dict:
    generated = ROOT / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    for stale_artifact in (
        generated / "generated_query.sql",
        generated / "sql_baseline.sql",
        generated / "generated_queries.json",
        generated / "sql_baselines.json",
        generated / "output_manifest.json",
        generated / "sql_optimization_report.json",
    ):
        stale_artifact.unlink(missing_ok=True)

    plan = state.get(
        "plan",
        {},
    )
    provider = state.get("provider", "mock")
    if plan.get("dataset_mode") == "generic_csv":
        output_specs = plan.get("output_specs") or [{
            "name": "fct_daily_revenue", "file_name": "fct_daily_revenue.csv", "format": "csv",
            "group_by": plan.get("group_by", "all"),
            "group_dimensions": plan.get("group_dimensions", []), "metrics": plan.get("metrics", []),
        }]
        sql_query = {}
        try:
            for output_spec in output_specs:
                output_name = output_spec["name"]
                output_sql = (_generate_sql_with_llm(state, plan, provider, output_spec)
                              if provider in {"openai", "gemini"}
                              else _default_sql(plan, output_spec))
                try:
                    output_sql = _validated_output_query(output_sql, plan, output_spec)
                except Exception as validation_error:
                    if provider not in {"openai", "gemini"}:
                        raise
                    output_sql = _generate_sql_with_llm(state, plan, provider, output_spec,
                        {"previous_sql": output_sql, "error": str(validation_error),
                         "instruction": "Correct only the contract violation; preserve the validated plan and prepared input values."})
                    output_sql = _validated_output_query(output_sql, plan, output_spec)
                sql_query[output_name] = output_sql
        except Exception as error:
            return {"status": "failed", "error": f"SQL Coder output validation failed: {type(error).__name__}: {error}"}
        plan["sql_by_output"] = sql_query
        primary_output = output_specs[0]
        plan["primary_output_file"] = primary_output["file_name"]
        plan["primary_output_format"] = primary_output.get("format", "csv")
        plan["output_columns"] = _sql_contract(plan, primary_output)["output_columns"]

    else:
        sql_query = None
        if provider in {"openai", "gemini"}:
            try:
                sql_query = _generate_sql_with_llm(state, plan, provider)
            except Exception as error:
                return {"status": "failed", "error": f"SQL Coder could not generate a valid query: {error}"}
        if sql_query is None:
            sql_query = _default_sql(plan)
            baseline_path = ROOT / "generated" / "sql_baseline.sql"
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(sql_query + ";\n", encoding="utf-8")
        try:
            _validate_generated_sql(sql_query, _sql_contract(plan))
        except Exception as error:
            return {"status": "failed", "error": f"SQL Coder query validation failed: {type(error).__name__}: {error}"}

    if plan.get("dataset_mode") == "generic_csv":
        custom_template = '"""Runtime pipeline for arbitrary CSV datasets, assembled with SQL Coder output."""\nfrom pathlib import Path\nimport pandas as pd\n\nROOT = Path(__file__).resolve().parents[1]\nDATA = ROOT / "data" / "custom_csv"\nOUT = ROOT / "output"\nSPEC = __PLAN_LITERAL__\n\ndef numeric(series):\n    cleaned = series.astype("string").str.replace(",", "", regex=False).str.replace(r"[^0-9.-]", "", regex=True)\n    return pd.to_numeric(cleaned, errors="coerce")\n\nfile_specs = {item["name"]: item for item in SPEC["files"]}\nframes = {name: pd.read_csv(DATA / name) for name in file_specs}\nbase_name = SPEC["base_file"]\nbase_spec = file_specs[base_name]\nif SPEC.get("union_files"):\n    parts = []\n    all_fields = set()\n    all_dimensions = set()\n    for source in SPEC["union_files"]:\n        source_frame = frames[source["name"]]\n        part = pd.DataFrame(index=source_frame.index)\n        for semantic, column in source.get("fields", {}).items():\n            if column in source_frame.columns:\n                part[semantic] = source_frame[column]\n                all_fields.add(semantic)\n        for dimension, value in source.get("constants", {}).items():\n            part[dimension] = value\n            all_dimensions.add(dimension)\n        if "distance" in part and source.get("distance_factor", 1) != 1:\n            part["distance"] = numeric(part["distance"]) * source["distance_factor"]\n        parts.append(part)\n    frames[base_name] = pd.concat(parts, ignore_index=True, sort=False)\n    base_spec = {"fields": {key: key for key in all_fields},\n                 "dimensions": {key: key for key in all_dimensions}}\ndata = frames[base_name].copy()\n\nfor join in SPEC.get("joins", []):\n    if join["left_file"] != base_name:\n        raise ValueError("Only joins directly from the fact CSV are supported")\n    right_name = join["right_file"]\n    right_spec = file_specs[right_name]\n    right_frame = frames[right_name]\n    right_key = join["right_key"]\n    if right_frame[right_key].duplicated().any():\n        raise ValueError(f"Join key {right_key} in {right_name} is not unique")\n    dimension_map = right_spec.get("dimensions", {})\n    selected = list(dict.fromkeys([right_key, *dimension_map.values()]))\n    right_frame = right_frame[selected].copy()\n    rename = {source: dimension for dimension, source in dimension_map.items() if source != right_key}\n    right_frame = right_frame.rename(columns=rename)\n    data = data.merge(\n        right_frame,\n        how=join["how"],\n        left_on=join["left_key"],\n        right_on=right_key,\n        validate="many_to_one",\n        suffixes=("", "__dimension"),\n    )\n    if join["left_key"] != right_key and right_key in data.columns:\n        data = data.drop(columns=[right_key])\n\nnormalized = pd.DataFrame(index=data.index)\nfor semantic, column in base_spec.get("fields", {}).items():\n    if column in data.columns:\n        normalized[semantic] = data[column]\nfor dimension, column in base_spec.get("dimensions", {}).items():\n    if column in data.columns:\n        normalized[dimension] = data[column]\nfor join in SPEC.get("joins", []):\n    for dimension in file_specs[join["right_file"]].get("dimensions", {}):\n        if dimension in data.columns:\n            normalized[dimension] = data[dimension]\n\nif "date" in normalized:\n    date_values = normalized["date"].astype("string").str.strip()\n    iso_dates = date_values.str.match(r"^\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}", na=False)\n    parsed_dates = pd.Series(pd.NaT, index=normalized.index, dtype="datetime64[ns]")\n    parsed_dates.loc[iso_dates] = pd.to_datetime(date_values.loc[iso_dates], errors="coerce", dayfirst=False, format="mixed")\n    parsed_dates.loc[~iso_dates] = pd.to_datetime(date_values.loc[~iso_dates], errors="coerce", dayfirst=True, format="mixed")\n    normalized["date"] = parsed_dates\nfor semantic in ("amount", "distance", "duration"):\n    if semantic in normalized:\n        normalized[semantic] = numeric(normalized[semantic])\nif "status" in normalized:\n    normalized["status"] = normalized["status"].astype("string").str.lower().str.strip()\nif "id" in normalized:\n    normalized = normalized.drop_duplicates(subset=["id"], keep="first")\n\ninclude_statuses = set(SPEC.get("include_status_values", []))\nexclude_statuses = set(SPEC.get("exclude_status_values", []))\nif "status" in normalized:\n    if include_statuses:\n        normalized = normalized[normalized["status"].isin(include_statuses)]\n    if exclude_statuses:\n        normalized = normalized[~normalized["status"].isin(exclude_statuses)]\n\ndata = normalized\nif SPEC["group_by"] != "all":\n    if "date" not in data or data["date"].notna().sum() == 0:\n        raise ValueError("No usable date values were found in the mapped fact CSV")\n    if SPEC["group_by"] == "day":\n        data["report_period"] = data["date"].dt.strftime("%Y-%m-%d")\n    elif SPEC["group_by"] == "week":\n        data["report_period"] = data["date"].dt.to_period("W").astype(str)\n    else:\n        data["report_period"] = data["date"].dt.strftime("%Y-%m")\n    data = data.dropna(subset=["report_period"])\n    group_columns = ["report_period"] + SPEC["group_dimensions"]\nelse:\n    group_columns = list(SPEC["group_dimensions"])\n\nif group_columns:\n    grouped = data.groupby(group_columns, dropna=False, sort=True)\n    groups = [(key if isinstance(key, tuple) else (key,), part) for key, part in grouped]\nelse:\n    groups = [((), data)]\n\nrows = []\nfor keys, part in groups:\n    row = dict(zip(group_columns, keys))\n    for metric in SPEC["metrics"]:\n        if metric == "record_count":\n            row[metric] = int(len(part))\n        elif metric == "completed_count":\n            row[metric] = int(part["status"].isin(SPEC["completed_status_values"]).sum()) if "status" in part else 0\n        elif metric == "cancelled_count":\n            row[metric] = int(part["status"].isin(SPEC["cancelled_status_values"]).sum()) if "status" in part else 0\n        elif metric == "total_amount":\n            row[metric] = float(part["amount"].sum()) if "amount" in part else 0.0\n        elif metric == "average_amount":\n            row[metric] = float(part["amount"].mean()) if "amount" in part and part["amount"].notna().any() else 0.0\n        elif metric == "average_distance":\n            row[metric] = float(part["distance"].mean()) if "distance" in part and part["distance"].notna().any() else 0.0\n        elif metric == "average_duration":\n            row[metric] = float(part["duration"].mean()) if "duration" in part and part["duration"].notna().any() else 0.0\n    rows.append(row)\n\nresult = pd.DataFrame(rows, columns=SPEC["output_columns"])\nOUT.mkdir(parents=True, exist_ok=True)\nresult.to_csv(OUT / "fct_daily_revenue.csv", index=False)\nprint(f"Input rows: {len(data)}")\nprint(f"Pipeline completed with {len(result)} result rows")\n'
        custom_template = custom_template.replace(
            "SPEC = __PLAN_LITERAL__\n",
            "SPEC = __PLAN_LITERAL__\nROW_PROJECTION_ONLY = bool(SPEC.get('output_specs')) and all(\n"
            "    output.get('kind') in {'rows', 'table', 'row_level'} for output in SPEC['output_specs']\n"
            ")\n",
        )
        date_start = custom_template.index('if "date" in normalized:\n')
        date_end = custom_template.index('for semantic in ("amount", "distance", "duration"):\n', date_start)
        date_code = '''if "date" in normalized:
    date_values = normalized["date"].astype("string").str.strip()
    date_format_by_file = SPEC.get("date_format_by_file", {})
    report_timezone = str(SPEC.get("report_timezone") or "").strip()
    source_files = normalized.get("_source_file", pd.Series(base_name, index=normalized.index))
    def parse_date(value, source_name):
        if pd.isna(value) or not str(value).strip():
            return pd.NaT
        date_format = date_format_by_file.get(source_name, "day_first")
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=date_format == "day_first",
                                format="ISO8601" if date_format == "iso" else "mixed")
        if pd.isna(parsed):
            return pd.NaT
        if parsed.tzinfo is None:
            if not report_timezone:
                return parsed
            return parsed.tz_localize(report_timezone, ambiguous="NaT", nonexistent="NaT").tz_localize(None)
        if not report_timezone:
            return parsed.tz_localize(None)
        return parsed.tz_convert(report_timezone).tz_localize(None)
    normalized["date"] = pd.Series(
        (parse_date(value, source_name) for value, source_name in zip(date_values, source_files)),
        index=normalized.index, dtype="datetime64[ns]",
    )
'''
        custom_template = custom_template[:date_start] + date_code + custom_template[date_end:]
        custom_template = custom_template.replace(
            "from pathlib import Path\nimport pandas as pd\n",
            "import json\nimport os\nimport sys\nfrom pathlib import Path\nimport pandas as pd\nimport duckdb\n",
        ).replace(
            "ROOT = Path(__file__).resolve().parents[1]\nDATA =",
            "ROOT = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(ROOT))\nfrom agents.sql_optimizer import optimize_query\nDATA =",
        )
        custom_template = custom_template.replace(
            "from agents.sql_optimizer import optimize_query\n",
            "from agents.sql_optimizer import optimize_query\nfrom agents.money import parse_amount_series\n",
        ).replace(
            'def numeric(series):\n    cleaned = series.astype("string").str.replace(",", "", regex=False).str.replace(r"[^0-9.-]", "", regex=True)\n    return pd.to_numeric(cleaned, errors="coerce")\n',
            'def numeric(series, number_format=""):\n    return parse_numeric_series(series, number_format)\n',
        ).replace(
            'numeric(part["distance"]) * source["distance_factor"]',
            'numeric(part["distance"], SPEC.get("number_format_by_file", {}).get(source["name"], "")) * source["distance_factor"]',
        ).replace(
            "pd.read_csv(DATA / name) for name in file_specs",
            "pd.read_csv(DATA / name, dtype=str) for name in file_specs",
        ).replace(
            "frames = {name: pd.read_csv(DATA / name, dtype=str) for name in file_specs}\n",
            "used_source_names = list(dict.fromkeys([item['name'] for item in SPEC.get('union_files', [])] or [SPEC['base_file'], *[join['right_file'] for join in SPEC.get('joins', [])]]))\n"
            "SPEC['loaded_source_names'] = used_source_names\n"
            "frames = {name: pd.read_csv(DATA / name, dtype=str) for name in used_source_names}\n",
        ).replace(
            '        parts.append(part)\n',
            '        if "status" in part:\n'
            '            part["status"] = part["status"].astype("string").str.casefold().str.strip()\n'
            '            source_status_mapping = {str(key).casefold().strip(): str(value).casefold().strip() for key, value in source.get("status_mapping", {}).items()}\n'
            '            part["status"] = part["status"].map(lambda value: source_status_mapping.get(str(value).casefold().strip(), value))\n'
            '        if "amount" in part:\n'
            '            source_name = source["name"]\n'
            '            currency_field = SPEC["currency_columns"].get(source_name)\n'
            '            part["amount"] = parse_amount_series_for_output(part["amount"], SPEC.get("currency_by_file", {}).get(source_name, ""), SPEC.get("conversion_rates", {}).get(source_name, 1.0), SPEC.get("number_format_by_file", {}).get(source_name, ""), SPEC.get("missing_amount_policy", {}).get(source_name, "exclude"), source_frame[currency_field] if currency_field else None, SPEC.get("target_currency", ""), SPEC.get("currency_rates", {}), SPEC.get("missing_currency_by_file", {}).get(source_name, ""), SPEC.get("number_format_by_currency", {}), allow_numeric_without_currency=ROW_PROJECTION_ONLY)\n'
            '        part["_source_file"] = source["name"]\n'
            '        parts.append(part)\n',
        ).replace(
            'for semantic in ("amount", "distance", "duration"):\n',
            'if not SPEC.get("union_files") and "amount" in normalized:\n'
            '    currency_field = SPEC["currency_columns"].get(base_name)\n'
            '    normalized["amount"] = parse_amount_series_for_output(normalized["amount"], SPEC.get("currency_by_file", {}).get(base_name, ""), SPEC.get("conversion_rates", {}).get(base_name, 1.0), SPEC.get("number_format_by_file", {}).get(base_name, ""), SPEC.get("missing_amount_policy", {}).get(base_name, "exclude"), data[currency_field] if currency_field else None, SPEC.get("target_currency", ""), SPEC.get("currency_rates", {}), SPEC.get("missing_currency_by_file", {}).get(base_name, ""), SPEC.get("number_format_by_currency", {}), allow_numeric_without_currency=ROW_PROJECTION_ONLY)\n'
            'if not SPEC.get("union_files") and "distance" in normalized and SPEC.get("distance_factor", 1) != 1:\n'
            '    normalized["distance"] = numeric(normalized["distance"], SPEC.get("number_format_by_file", {}).get(base_name, "")) * SPEC["distance_factor"]\n'
            'for semantic in ("distance", "duration", "value"):\n',
        ).replace(
            'for semantic in ("distance", "duration", "value"):\n'
            '    if semantic in normalized:\n'
            '        normalized[semantic] = numeric(normalized[semantic])\n',
            'source_names = normalized.get("_source_file")\n'
            'for semantic in ("amount", "distance", "duration", "value"):\n'
            '    if semantic not in normalized or semantic == "amount":\n'
            '        continue\n'
            '    normalized[semantic] = normalized[semantic].astype(object)\n'
            '    if source_names is None:\n'
            '        normalized[semantic] = numeric(normalized[semantic], SPEC.get("number_format_by_file", {}).get(base_name, ""))\n'
            '    else:\n'
            '        for source_name, row_indices in source_names.groupby(source_names).groups.items():\n'
            '            normalized.loc[row_indices, semantic] = numeric(normalized.loc[row_indices, semantic], SPEC.get("number_format_by_file", {}).get(source_name, ""))\n',
        ).replace(
            'result.to_csv(OUT / "fct_daily_revenue.csv", index=False)\n',
            'if SPEC.get("primary_output_format", "csv") == "csv":\n'
            '    result.to_csv(OUT / SPEC.get("primary_output_file", "fct_daily_revenue.csv"), index=False)\n',
        )
        custom_template = custom_template.replace(
            "from agents.money import parse_amount_series\n",
            "from agents.money import parse_amount_series, parse_numeric_series, parse_amount_series_for_output\nfrom agents.csv_quality import resolve_duplicate_orders, deduplicate_by_keys, apply_filters\nfrom agents.derived_fields import apply_derived_fields\n",
        ).replace(
            '    right_key = join["right_key"]\n',
            '    right_key = join["right_key"]\n'
            '    right_keys = join.get("right_keys", [right_key])\n'
            '    left_keys = join.get("left_keys", [join["left_key"]])\n',
        ).replace(
            '    selected = list(dict.fromkeys([right_key, *dimension_map.values()]))\n',
            '    selected = list(dict.fromkeys([*right_keys, *dimension_map.values()]))\n',
        ).replace(
            '        left_on=join["left_key"],\n        right_on=right_key,\n',
            '        left_on=left_keys,\n        right_on=right_keys,\n',
        ).replace(
            '    if join["left_key"] != right_key and right_key in data.columns:\n'
            '        data = data.drop(columns=[right_key])\n',
            '    duplicate_right_keys = [right for left, right in zip(left_keys, right_keys)\n'
            '                            if left != right and right in data.columns]\n'
            '    if duplicate_right_keys:\n'
            '        data = data.drop(columns=duplicate_right_keys)\n',
        ).replace(
            '    if right_frame[right_key].duplicated().any():\n'
            '        raise ValueError(f"Join key {right_key} in {right_name} is not unique")\n',
            '    if right_frame[right_keys].duplicated().any():\n'
            '        if SPEC.get("join_duplicate_policy") == "keep_first":\n'
            '            right_frame = right_frame.drop_duplicates(subset=right_keys, keep="first")\n'
            '        else:\n'
            '            raise ValueError(f"Join key {right_keys} in {right_name} is not unique")\n',
        ).replace(
            '    data = data.merge(\n',
            '    valid_keys = set(map(tuple, right_frame[right_keys].itertuples(index=False, name=None)))\n'
            '    unmatched = ~data[left_keys].apply(tuple, axis=1).isin(valid_keys)\n'
            '    if unmatched.any():\n'
            '        if SPEC.get("orphan_policy") == "drop":\n'
            '            data = data.loc[~unmatched].copy()\n'
            '        elif SPEC.get("orphan_policy") != "keep_unknown":\n'
            '            raise ValueError(f"Orphan keys in {right_name}: {data.loc[unmatched, left_keys].head().to_dict(orient=\'records\')}")\n'
            '    data = data.merge(\n',
        ).replace(
            '    if join["left_key"] != right_key and right_key in data.columns:\n',
            '    if SPEC.get("orphan_policy") == "keep_unknown":\n'
            '        for dimension in dimension_map:\n'
            '            if dimension in data.columns:\n'
            '                data[dimension] = data[dimension].fillna("Unknown")\n'
            '    if join["left_key"] != right_key and right_key in data.columns:\n',
        ).replace(
            '    normalized = normalized.drop_duplicates(subset=["id"], keep="first")\n',
            '    if SPEC.get("deduplicate_ids", False):\n'
            '        normalized = resolve_duplicate_orders(normalized, SPEC.get("duplicate_resolution", "ask"), SPEC.get("duplicate_scope", "global"))\n',
        ).replace(
            'normalized = pd.DataFrame(index=data.index)\n',
            'normalized = pd.DataFrame(index=data.index)\n'
            'if "_source_file" in data:\n'
            '    normalized["_source_file"] = data["_source_file"]\n',
        )
        custom_template = custom_template.replace(
            'data = frames[base_name].copy()\n',
            'data = frames[base_name].copy()\n'
            'if not SPEC.get("union_files"):\n'
            '    data["_source_file"] = base_name\n',
        ).replace(
            'for join in SPEC.get("joins", []):\n    for dimension in file_specs[join["right_file"]].get("dimensions", {}):\n',
            'for field, value in base_spec.get("constants", {}).items():\n'
            '    normalized[field] = value\n'
            'for join in SPEC.get("joins", []):\n    for dimension in file_specs[join["right_file"]].get("dimensions", {}):\n',
        ).replace(
            'if "id" in normalized:\n'
            '    if SPEC.get("deduplicate_ids", False):\n'
            '        normalized = resolve_duplicate_orders(normalized, SPEC.get("duplicate_resolution", "ask"), SPEC.get("duplicate_scope", "global"))\n',
            'if SPEC.get("deduplicate_ids", False):\n'
            '    normalized = resolve_duplicate_orders(normalized, SPEC.get("duplicate_resolution", "ask"), SPEC.get("duplicate_scope", "global"))\n'
            'elif SPEC.get("deduplicate_policy") == "exact_duplicates_only":\n'
            '    normalized = normalized.drop_duplicates()\n'
            'elif SPEC.get("deduplicate_keys"):\n'
            '    normalized = deduplicate_by_keys(normalized, SPEC["deduplicate_keys"], SPEC.get("duplicate_resolution", "ask"))\n',
        ).replace(
            'data = normalized\nif SPEC["group_by"] != "all":\n',
            'normalized = apply_filters(normalized, SPEC.get("filters", []))\n'
            'normalized = apply_derived_fields(normalized, SPEC.get("derived_fields", []))\n'
            'data = normalized\nif SPEC["group_by"] != "all":\n',
        ).replace(
            'if "status" in normalized:\n'
            '    normalized["status"] = normalized["status"].astype("string").str.lower().str.strip()\n',
            'if "status" in normalized:\n'
            '    normalized["status"] = normalized["status"].astype("string").str.lower().str.strip()\n'
            '    status_mapping = {str(key).casefold().strip(): str(value).casefold().strip() for key, value in SPEC.get("status_mapping", {}).items()}\n'
            '    normalized["status"] = normalized["status"].map(lambda value: status_mapping.get(str(value).casefold().strip(), value))\n',
        ).replace(
            'if "date" in normalized:\n',
            'for text_field in normalized.columns:\n'
            '    if text_field in {"amount", "distance", "duration", "value"}:\n'
            '        continue\n'
            '    normalized[text_field] = normalized[text_field].astype("string").str.strip().replace("", pd.NA)\n'
            'if "date" in normalized:\n',
        ).replace(
            'source_names = normalized.get("_source_file")\n',
            'for field, default in SPEC.get("field_defaults", {}).items():\n'
            '    if field in normalized:\n'
            '        normalized[field] = normalized[field].astype("string").str.strip().replace("", pd.NA).fillna(default)\n'
            'source_names = normalized.get("_source_file")\n',
        )
        grouping_start = custom_template.index('if SPEC["group_by"] != "all":\n', custom_template.index("data = normalized\n"))
        grouping_marker = "if group_columns:\n    grouped = data.groupby(group_columns, dropna=False, sort=True)"
        grouping_end = custom_template.index(grouping_marker, grouping_start)
        custom_template = custom_template[:grouping_start] + grouping_marker + custom_template[grouping_end + len(grouping_marker):]
        pipeline_code = custom_template.replace("__PLAN_LITERAL__", repr(plan))
        pipeline_code = _custom_sql_aggregation(pipeline_code, sql_query)
        PIPELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PIPELINE_PATH.write_text(pipeline_code, encoding="utf-8")
        artifacts = dict(state.get("artifacts", {}))
        artifacts["pipeline"] = str(PIPELINE_PATH)
        artifacts["sql_baseline"] = str(ROOT / "generated" / "sql_baseline.sql")
        artifacts["sql"] = str(ROOT / "generated" / "generated_query.sql")
        artifacts["sql_optimization_report"] = str(ROOT / "generated" / "sql_optimization_report.json")
        return {"artifacts": artifacts, "status": "coded"}

    required_fields = [
        "sources",
        "valid_statuses",
        "group_by",
        "include_channel_revenue",
        "remove_duplicate_order_ids",
        "drop_missing_revenue",
    ]

    missing_fields = [
        field
        for field in required_fields
        if field not in plan
    ]

    if missing_fields:
        return {
            "status": "failed",
            "error": (
                "Plan thiếu trường: "
                + ", ".join(
                    missing_fields
                )
            ),
        }

    pipeline_code = (
        PIPELINE_TEMPLATE.replace(
            "__PLAN_LITERAL__",
            repr(plan),
        ).replace("__SQL_QUERY_LITERAL__", repr(sql_query))
    )

    PIPELINE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    PIPELINE_PATH.write_text(
        pipeline_code,
        encoding="utf-8",
    )

    artifacts = dict(
        state.get(
            "artifacts",
            {},
        )
    )

    artifacts["pipeline"] = str(
        PIPELINE_PATH
    )
    artifacts["sql_baseline"] = str(ROOT / "generated" / "sql_baseline.sql")
    artifacts["sql"] = str(ROOT / "generated" / "generated_query.sql")
    artifacts["sql_optimization_report"] = str(ROOT / "generated" / "sql_optimization_report.json")

    return {
        "artifacts": artifacts,
        "status": "coded",
    }
