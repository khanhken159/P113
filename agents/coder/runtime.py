"""Shared runtime for executing a validated set of SQL output specifications."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pandas as pd
from pandas.testing import assert_frame_equal


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _metric_expression(metric: str, spec: dict) -> str:
    if metric == "record_count":
        return "COUNT(*)"
    if metric == "distinct_order_count":
        return "COUNT(DISTINCT id)"
    if metric in {"completed_count", "cancelled_count", "completion_rate", "cancellation_rate"}:
        completed = metric in {"completed_count", "completion_rate"}
        statuses = spec.get("completed_status_values", []) if completed else spec.get("cancelled_status_values", [])
        if not statuses:
            raise ValueError(f"{metric} requires explicit status values")
        values = ", ".join(_sql_literal(value.casefold()) for value in statuses)
        numerator = f"COUNT(*) FILTER (WHERE lower(trim(status)) IN ({values}))"
        return f"({numerator}) * 1.0 / NULLIF(COUNT(*), 0)" if metric.endswith("_rate") else numerator
    expressions = {
        "total_amount": "SUM(amount)",
        "customer_lifetime_value": "SUM(amount)",
        "average_amount": "AVG(amount)",
        "min_amount": "MIN(amount)",
        "max_amount": "MAX(amount)",
        "median_amount": "median(amount)",
        "average_distance": "AVG(distance)",
        "average_duration": "AVG(duration)",
        "sum_value": "SUM(value)",
        "running_revenue": "SUM(SUM(amount)) OVER (ORDER BY report_period ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        "customer_rank": "RANK() OVER (ORDER BY SUM(amount) DESC)",
    }
    if metric not in expressions:
        raise ValueError(f"Unsupported SQL metric: {metric}")
    return expressions[metric]


def _window_expression(window: dict) -> tuple[str, str]:
    name = str(window.get("name", "")).strip()
    function = str(window.get("function", "")).upper().strip()
    allowed = {"ROW_NUMBER", "RANK", "LAG", "LEAD", "SUM", "AVG", "COUNT", "MIN", "MAX"}
    if not name or function not in allowed:
        raise ValueError(f"Unsupported or unnamed window function: {window}")
    partition = window.get("partition_by", [])
    partition_sql = f"PARTITION BY {', '.join(_quote(field) for field in partition)} " if partition else ""
    order_items = window.get("order_by", [])
    requires_order = function in {"ROW_NUMBER", "RANK", "LAG", "LEAD"} or bool(window.get("frame"))
    if requires_order and not order_items:
        raise ValueError(f"Window function {name} requires deterministic order_by fields")
    order_sql_parts = []
    for item in order_items:
        if isinstance(item, str):
            field, direction, nulls = item, "ASC", ""
        else:
            field = str(item.get("field", ""))
            direction = str(item.get("direction", "ASC")).upper()
            nulls = str(item.get("nulls", "")).upper()
        if direction not in {"ASC", "DESC"} or nulls not in {"", "FIRST", "LAST"}:
            raise ValueError(f"Invalid window ordering in {name}: {item}")
        order_sql_parts.append(f"{_quote(field)} {direction}" + (f" NULLS {nulls}" if nulls else ""))
    order_sql = ("ORDER BY " + ", ".join(order_sql_parts)) if order_sql_parts else ""
    arguments = ""
    value_field = window.get("value_field")
    if function in {"LAG", "LEAD", "SUM", "AVG", "COUNT", "MIN", "MAX"}:
        if function == "COUNT" and not value_field:
            arguments = "COUNT(*)"
        elif not value_field:
            raise ValueError(f"Window function {name} requires value_field")
        else:
            field_sql = _quote(value_field)
            if window.get("aggregate_input"):
                if function not in {"SUM", "COUNT"}:
                    raise ValueError(f"Window function {name} cannot aggregate grouped input with {function}")
                aggregate_function = str(window.get("aggregate_function", "SUM")).upper()
                if aggregate_function == "COUNT_DISTINCT":
                    aggregate_input = f"COUNT(DISTINCT {field_sql})"
                elif aggregate_function in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
                    aggregate_input = f"{aggregate_function}({field_sql})"
                else:
                    raise ValueError(f"Window function {name} has an unsupported grouped aggregate")
                arguments = f"{function}({aggregate_input})"
            elif function in {"LAG", "LEAD"}:
                arguments = (f"{function}({field_sql}, {int(window.get('offset', 1))}"
                             + (f", {_sql_literal(window['default_value'])}" if window.get("default_value") is not None else "")
                             + ")")
            else:
                arguments = f"{function}({field_sql})"
    else:
        arguments = f"{function}()"
    frame = str(window.get("frame", "")).strip().upper()
    if frame:
        if not re.fullmatch(r"(?:ROWS|RANGE) BETWEEN (?:UNBOUNDED PRECEDING|\d+ PRECEDING) AND (?:CURRENT ROW|\d+ FOLLOWING|UNBOUNDED FOLLOWING)", frame):
            raise ValueError(f"Unsupported window frame for {name}: {frame}")
    window_parts = " ".join(part for part in (partition_sql.strip(), order_sql, frame) if part)
    return f"{arguments} OVER ({window_parts}) AS {_quote(name)}", name


def _default_query(spec: dict, output: dict) -> tuple[str, list[str]]:
    metrics = output.get("metrics", spec.get("metrics", []))
    dimensions = output.get("group_dimensions", spec.get("group_dimensions", []))
    grain = output.get("group_by", spec.get("group_by", "all"))
    window_specs = output.get("window_specs", [])
    if output.get("kind") in {"rows", "table", "row_level"}:
        mapping = output.get("column_mapping", {})
        columns = output.get("columns") or list(mapping)
        if not columns:
            raise ValueError(f"Row-level output {output.get('name')} must declare its output columns")
        windows = [_window_expression(window) for window in window_specs]
        window_by_name = {name: expression for expression, name in windows}
        internal_columns = list(dict.fromkeys([*columns, *window_by_name, *[item.get("field") for item in output.get("post_window_filters", []) if item.get("field")]]))
        selections = [window_by_name[column] if column in window_by_name else
                      f'{_quote(mapping.get(column, column))} AS {_quote(column)}' for column in columns]
        selections.extend(expression for expression, name in windows if name not in columns)
        inner_query = "SELECT " + ", ".join(selections) + " FROM flowforge_data"
        post_filters = output.get("post_window_filters", [])
        if post_filters:
            predicate_sql = []
            operators = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "in": "IN", "not_in": "NOT IN"}
            for predicate in post_filters:
                field, operator, value = predicate.get("field"), predicate.get("operator"), predicate.get("value")
                if field not in internal_columns or operator not in operators:
                    raise ValueError(f"Unsupported post-window predicate: {predicate}")
                values = value if isinstance(value, list) else [value]
                right = "(" + ", ".join(_sql_literal(item) for item in values) + ")" if operator in {"in", "not_in"} else _sql_literal(value)
                predicate_sql.append(f"{_quote(field)} {operators[operator]} {right}")
            outer_projection = ", ".join(_quote(column) for column in columns)
            return f"SELECT {outer_projection} FROM ({inner_query}) AS flowforge_windowed WHERE " + " AND ".join(predicate_sql), list(columns)
        return inner_query, list(columns)
    period_expression = f"date_trunc({_sql_literal(grain)}, {_quote('date')})" if grain in {"day", "week", "month"} else None
    mapping = output.get("column_mapping", {})
    requested_columns = output.get("columns", [])

    def output_name(internal: str) -> str:
        return next((target for target, role in mapping.items()
                     if role == internal and (not requested_columns or target in requested_columns)), internal)

    selections = ([f'{period_expression} AS {_quote("report_period")}'] if period_expression else [])
    selections.extend(f'{_quote(column)} AS {_quote(output_name(column))}' for column in dimensions)
    selections.extend(f'{_metric_expression(metric, spec)} AS {_quote(output_name(metric))}' for metric in metrics)
    metric_output_names = {output_name(metric) for metric in metrics}
    input_window_columns, post_window_columns = [], []
    for source_window in window_specs:
        window = dict(source_window)
        if window.get("value_field") in metrics:
            window["value_field"] = output_name(window["value_field"])
        if window.get("aggregate_input") and window.get("value_field") not in metric_output_names:
            input_window_columns.append(_window_expression(window))
        else:
            # Windows over a grouped metric run in the outer query, after
            # aggregate aliases exist. This is the generic denominator/rank
            # stage; it does not depend on a metric's business name.
            window["aggregate_input"] = False
            post_window_columns.append(_window_expression(window))
    selections.extend(expression for expression, _ in input_window_columns)
    if not selections:
        raise ValueError("Output specification has no dimensions or metrics")
    base_query = "SELECT " + ", ".join(selections) + " FROM flowforge_data"
    groups = (["1"] if period_expression else []) + [_quote(column) for column in dimensions]
    if groups:
        group_sql = ", ".join(groups)
        base_query += " GROUP BY " + group_sql + " ORDER BY " + group_sql

    derived_metrics = output.get("derived_metrics", [])
    base_columns = ((["report_period"] if period_expression else [])
                    + [output_name(column) for column in dimensions]
                    + [output_name(metric) for metric in metrics]
                    + [name for _, name in input_window_columns]
                    + [name for _, name in post_window_columns])
    columns = requested_columns or (base_columns + [item.get("name") for item in derived_metrics])
    query = base_query
    if post_window_columns:
        query = "SELECT *, " + ", ".join(expression for expression, _ in post_window_columns) \
                + f" FROM ({query}) AS flowforge_windowed"
    if derived_metrics:
        derived_by_name = {item.get("name"): item for item in derived_metrics}
        projections = []
        for column in columns:
            definition = derived_by_name.get(column)
            if not definition:
                if column not in base_columns:
                    raise ValueError(f"Output column {column!r} has no aggregate or derived binding")
                projections.append(_quote(column))
                continue
            operation = str(definition.get("operation", "")).casefold()
            inputs = definition.get("inputs", [])
            if not isinstance(inputs, list) or not inputs:
                raise ValueError(f"Derived metric {column!r} requires ordered inputs")
            values = [_quote(value) for value in inputs]
            if operation == "divide":
                expression = f"{values[0]} / NULLIF({values[1]}, 0)" if len(values) == 2 else None
            elif operation in {"add", "subtract", "multiply"} and len(values) == 2:
                operator = {"add": "+", "subtract": "-", "multiply": "*"}[operation]
                expression = f"{values[0]} {operator} {values[1]}"
            elif operation in {"copy", "identity"} and len(values) == 1:
                expression = values[0]
            else:
                expression = None
            if expression is None:
                raise ValueError(f"Derived metric {column!r} has an unsupported operation or input count")
            projections.append(f"{expression} AS {_quote(column)}")
        query = "SELECT " + ", ".join(projections) + f" FROM ({query}) AS flowforge_aggregated"
    return query, list(columns)


def _write_output(frame: pd.DataFrame, path: Path, file_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if file_format == "json":
        path.write_text(frame.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
    elif file_format == "csv":
        frame.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported output format: {file_format}")


def _assert_semantic_equivalence(actual: pd.DataFrame, expected: pd.DataFrame, columns: list[str], output_name: str) -> None:
    actual = actual.reindex(columns=columns)
    expected = expected.reindex(columns=columns)
    if len(actual) != len(expected):
        raise ValueError(f"SQL semantic validation failed for {output_name}: row count {len(actual)} != reference {len(expected)}")
    sort_columns = list(columns)
    actual = actual.sort_values(sort_columns, na_position="first", kind="stable").reset_index(drop=True)
    expected = expected.sort_values(sort_columns, na_position="first", kind="stable").reset_index(drop=True)
    try:
        assert_frame_equal(actual, expected, check_dtype=False, check_exact=False, rtol=1e-9, atol=1e-9)
    except AssertionError as error:
        raise ValueError(f"SQL semantic validation failed for {output_name}: generated result differs from the planned reference query") from error


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def execute_output_specs(
    *, data: pd.DataFrame, spec: dict, root: Path, output_dir: Path,
    optimize_query, run_mode: str,
) -> pd.DataFrame:
    outputs = spec.get("output_specs") or [{
        "name": "fct_daily_revenue", "file_name": "fct_daily_revenue.csv", "format": "csv",
        "group_by": spec.get("group_by", "all"),
        "group_dimensions": spec.get("group_dimensions", []), "metrics": spec.get("metrics", []),
    }]
    generated = root / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    selected_path = generated / "generated_queries.json"
    baselines_path = generated / "sql_baselines.json"
    report_path = generated / "sql_optimization_report.json"
    selected_queries = _read_json(selected_path)
    baseline_queries: dict[str, str] = {}
    run_reports: list[dict] = []
    manifest_outputs: list[dict] = []
    results: list[pd.DataFrame] = []

    con = duckdb.connect(":memory:")
    registered = False
    try:
        for output in outputs:
            name = str(output["name"])
            output_data = data
            grain = output.get("group_by", spec.get("group_by", "all"))
            if grain != "all" and output.get("filter_invalid_dates", True) and "date" in data.columns:
                output_data = data.loc[data["date"].notna()].copy()
            if registered:
                con.unregister("flowforge_data")
            con.register("flowforge_data", output_data)
            registered = True

            default_query, default_columns = _default_query(spec, output)
            query = spec.get("sql_by_output", {}).get(name, default_query)
            if run_mode == "selected":
                query = selected_queries.get(name, query)
                outcome = {"optimized": False, "optimizer_status": "SELECTED_SQL"}
            elif run_mode == "optimize":
                outcome = optimize_query(con, query)
                query = outcome["sql"]
            else:
                outcome = {"optimized": False, "optimizer_status": "BASELINE_ONLY"}
            columns = output.get("columns") or default_columns
            reference = con.execute(default_query).df()

            def execute_and_check(sql: str) -> pd.DataFrame:
                candidate = con.execute(sql).df()
                missing_columns = [column for column in columns if column not in candidate.columns]
                if missing_columns:
                    raise ValueError(f"SQL output {name} is missing contract columns: {missing_columns}")
                candidate = candidate.reindex(columns=columns)
                _assert_semantic_equivalence(candidate, reference, columns, name)
                return candidate

            try:
                frame = execute_and_check(query)
            except Exception as candidate_error:
                if query == default_query:
                    raise
                rejected_query = query
                query = default_query
                frame = execute_and_check(query)
                outcome = {
                    **outcome,
                    "optimized": False,
                    "optimizer_status": "SEMANTIC_FALLBACK",
                    "rejected_sql": rejected_query,
                    "fallback_reason": str(candidate_error),
                }

            if run_mode != "selected":
                if run_mode == "baseline":
                    baseline_queries[name] = query
                selected_queries[name] = query
                run_reports.append({"output": name, "file_name": output["file_name"], **outcome, "sql": query})
            file_format = output.get("format", Path(output["file_name"]).suffix.lstrip(".") or "csv").lower()
            _write_output(frame, output_dir / output["file_name"], file_format)
            results.append(frame)
            manifest_outputs.append({
                "name": name,
                "file_name": output["file_name"],
                "format": file_format,
                "kind": output.get("kind", "aggregate"),
                "columns": list(frame.columns),
                "row_count": len(frame),
                "group_by": grain,
                "group_dimensions": output.get("group_dimensions", spec.get("group_dimensions", [])),
                "metrics": output.get("metrics", spec.get("metrics", [])),
                "semantic_reference_passed": True,
            })
    finally:
        con.close()

    if run_mode == "baseline":
        baselines_path.write_text(json.dumps(baseline_queries, ensure_ascii=False, indent=2), encoding="utf-8")
        first_sql = next(iter(baseline_queries.values()), "")
        (generated / "sql_baseline.sql").write_text(first_sql.rstrip().rstrip(";") + ";\n", encoding="utf-8")
    if run_mode != "selected":
        selected_path.write_text(json.dumps(selected_queries, ensure_ascii=False, indent=2), encoding="utf-8")
        first_sql = next(iter(selected_queries.values()), "")
        (generated / "generated_query.sql").write_text(first_sql.rstrip().rstrip(";") + ";\n", encoding="utf-8")
        report_path.write_text(json.dumps(run_reports, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded_sources = spec.get("loaded_source_names") or spec.get("source_files_used", [])
    source_specs = {item.get("name"): item for item in spec.get("files", [])}
    used_sources = [source_specs.get(name, {}).get("original_name", name) for name in loaded_sources]
    manifest = {
        "requested_sources": spec.get("requirement_contract", {}).get("requested_sources", []),
        "planned_sources": spec.get("source_files_used", []),
        "used_sources": used_sources,
        "applied_filters": spec.get("filters", []),
        "outputs": manifest_outputs,
    }
    (generated / "output_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return results[0] if results else pd.DataFrame()
