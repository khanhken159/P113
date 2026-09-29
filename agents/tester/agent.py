"""Execute generated pipelines and validate technical and request-level contracts."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pandas as pd

from agents.requirement_contract import canonical_metric, revenue_status_filter_is_covered

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "generated"
PIPELINE = GENERATED / "generated_pipeline.py"
REPORT = GENERATED / "test_report.json"
PLAN_PATH = GENERATED / "pipeline_plan.json"
OUTPUT_DIR = ROOT / "output"
DEFAULT_OUTPUT = OUTPUT_DIR / "fct_daily_revenue.csv"


def monitor_process(process: subprocess.Popen, samples: dict) -> None:
    try:
        import psutil

        monitored = psutil.Process(process.pid)
        while process.poll() is None:
            try:
                samples["cpu_seconds"] = monitored.cpu_times().user + monitored.cpu_times().system
                samples["peak_memory_mb"] = max(
                    samples.get("peak_memory_mb", 0.0),
                    monitored.memory_info().rss / (1024 * 1024),
                )
            except Exception:
                break
            time.sleep(0.05)
    except (ImportError, Exception):
        return


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except (OSError, json.JSONDecodeError):
        return default


def _file_key(value: str) -> tuple[str, str | None]:
    path = Path(str(value or "")).name.casefold()
    suffix = Path(path).suffix
    return (Path(path).stem, suffix or None)


def _finite_numeric(frame: pd.DataFrame, columns: list[str]) -> bool:
    if not columns:
        return True
    raw = frame[columns]
    values = raw.apply(pd.to_numeric, errors="coerce")
    non_null = raw.notna()
    if (non_null & values.isna()).any().any():
        return False
    for column in columns:
        present = values[column].dropna()
        if not present.map(lambda value: math.isfinite(float(value))).all():
            return False
    return True


def _label_key(value: str) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _output_field_for_runtime(spec: dict, runtime_field: str) -> str:
    """Resolve a normalized grouping/metric field to its declared output label."""
    mapping = spec.get("column_mapping", {})
    match = next((label for label, runtime in mapping.items()
                  if _label_key(runtime) == _label_key(runtime_field)), None)
    if match is not None:
        return match
    return next((column for column in spec.get("columns", [])
                 if _label_key(column) == _label_key(runtime_field)), runtime_field)


def _grouping_columns(spec: dict, plan: dict, frame: pd.DataFrame | None = None) -> list[str]:
    dimensions = list(spec.get("group_dimensions", plan.get("group_dimensions", [])))
    columns = [_output_field_for_runtime(spec, dimension) for dimension in dimensions]
    group_by = spec.get("group_by", plan.get("group_by", "all"))
    declared_columns = set(spec.get("columns") or (
        (["report_period"] if group_by != "all" else [])
        + dimensions
        + list(spec.get("metrics", plan.get("metrics", [])))
    ))
    available_columns = set(frame.columns) if frame is not None else declared_columns
    # Some aggregate contracts expose the time key as an explicit dimension
    # (for example, a mapped `date` field); older/default contracts expose it
    # as `report_period`. Validate the key the output contract actually names.
    if (group_by != "all"
            and "report_period" in declared_columns
            and "report_period" in available_columns
            and "report_period" not in columns):
        columns.insert(0, "report_period")
    return columns


def _numeric_output_fields(spec: dict, frame: pd.DataFrame) -> list[str]:
    """Use the output contract to find numeric metric aliases and derived values."""
    mapping = spec.get("column_mapping", {})
    fields = []
    for metric in spec.get("metrics", []):
        field = next((label for label, runtime in mapping.items()
                      if canonical_metric(runtime) == canonical_metric(metric) and label in frame.columns), metric)
        if field in frame.columns:
            fields.append(field)
    fields.extend(item.get("name") for item in spec.get("derived_metrics", [])
                  if item.get("name") in frame.columns)
    return list(dict.fromkeys(fields))


def _validate_generic(plan: dict, manifest: dict, output_dir: Path) -> tuple[dict, dict]:
    contract = plan.get("requirement_contract", {})
    outputs = manifest.get("outputs", []) if isinstance(manifest, dict) else []
    expected_outputs = plan.get("output_specs") or []
    if not expected_outputs and plan.get("primary_output_file"):
        expected_outputs = [{"file_name": plan["primary_output_file"], "columns": plan.get("output_columns", [])}]

    paths = [output_dir / str(item.get("file_name", "")) for item in expected_outputs]
    existing = [(item, path) for item, path in zip(expected_outputs, paths) if path.is_file()]
    frames: dict[str, pd.DataFrame] = {}
    read_ok = len(existing) == len(expected_outputs)
    column_checks = {}
    finite_checks = {}
    unique_checks = {}
    for spec, path in existing:
        try:
            frame = pd.read_json(path) if path.suffix.casefold() == ".json" else pd.read_csv(path)
            frames[path.name] = frame
            expected_columns = spec.get("columns") or (
                (["report_period"] if spec.get("group_by", plan.get("group_by", "all")) != "all" else [])
                + spec.get("group_dimensions", plan.get("group_dimensions", []))
                + spec.get("metrics", plan.get("metrics", []))
            )
            column_checks[path.name] = set(expected_columns).issubset(frame.columns)
            metric_fields = _numeric_output_fields(spec, frame)
            finite_checks[path.name] = _finite_numeric(frame, metric_fields)
            grouping = _grouping_columns(spec, plan, frame)
            unique_checks[path.name] = (not grouping or
                                        (set(grouping).issubset(frame.columns) and
                                         not frame.duplicated(grouping).any()))
        except Exception:
            read_ok = False
            column_checks[path.name] = False
            finite_checks[path.name] = False
            unique_checks[path.name] = False

    requested_sources = {str(item).casefold() for item in contract.get("requested_sources", [])}
    used_sources = {str(item).casefold() for item in manifest.get("used_sources", [])} if isinstance(manifest, dict) else set()
    sources_match = not requested_sources or requested_sources == used_sources

    # The request may use a declared output label while the runtime manifest
    # reports the normalized metric slot. Resolve labels through each output
    # contract before comparing them.
    requested_metrics = set()
    output_metric_bindings = {}
    for spec in expected_outputs:
        output_metric_bindings.update({
            _label_key(label): canonical_metric(runtime)
            for label, runtime in spec.get("column_mapping", {}).items()
        })
    for item in contract.get("requested_metrics", []):
        requested_metrics.add(output_metric_bindings.get(_label_key(item), canonical_metric(item)))
    plan_specs_by_file = {_file_key(item.get("file_name", ""))[0]: item for item in expected_outputs}
    generated_metrics = set()
    for output in outputs:
        spec = plan_specs_by_file.get(_file_key(output.get("file_name", ""))[0], {})
        output_columns = set(output.get("columns", []))
        generated_metrics.update(canonical_metric(metric) for metric in output.get("metrics", []))
        generated_metrics.update(
            canonical_metric(metric.get("name", ""))
            for metric in spec.get("derived_metrics", [])
            if metric.get("name") in output_columns
        )
    metrics_match = not requested_metrics or requested_metrics == generated_metrics

    requested_output_files = contract.get("requested_output_files", [])
    generated_file_names = [item.get("file_name", "") for item in outputs]
    if requested_output_files:
        def same_output(requested: str, generated: str) -> bool:
            request_stem, request_suffix = _file_key(requested)
            generated_stem, generated_suffix = _file_key(generated)
            return request_stem == generated_stem and (request_suffix is None or request_suffix == generated_suffix)
        outputs_match = (len(requested_output_files) == len(generated_file_names)
                         and all(any(same_output(requested, generated) for generated in generated_file_names)
                                 for requested in requested_output_files)
                         and all(any(same_output(requested, generated) for requested in requested_output_files)
                                 for generated in generated_file_names))
    else:
        outputs_match = bool(outputs)

    requested_groups = contract.get("requested_groupings", [])
    requested_filters = contract.get("requested_filters", [])
    revenue_semantics = plan.get("revenue_semantics")
    effective_requested_filters = [
        predicate for predicate in requested_filters
        if not revenue_status_filter_is_covered(predicate, revenue_semantics)
    ]
    filters_match = effective_requested_filters == (manifest.get("applied_filters", []) if isinstance(manifest, dict) else [])
    actual_groups = {
        (str(item.get("group_by", "all")).casefold(),
         frozenset(str(value).casefold() for value in item.get("group_dimensions", [])))
        for item in outputs
        if item.get("kind") not in {"rows", "table", "row_level"}
    }
    if requested_groups:
        grouping_match = True
        output_by_file = {_file_key(item.get("file_name", ""))[0]: item for item in outputs}
        spec_by_file = {_file_key(item.get("file_name", ""))[0]: item for item in expected_outputs}
        for group in requested_groups:
            requested_file = _file_key(group.get("output_file", ""))[0]
            candidates = ([requested_file] if requested_file else list(output_by_file))
            matched = False
            for file_key in candidates:
                actual = output_by_file.get(file_key)
                spec = spec_by_file.get(file_key)
                if actual is None or spec is None:
                    continue
                actual_grain = str(actual.get("group_by", "all")).casefold()
                expected_grain = str(group.get("time_grain") or "all").casefold()
                if actual_grain != expected_grain:
                    continue
                mapping = spec.get("column_mapping", {})
                expected_dimensions = set()
                for dimension in group.get("dimensions", []):
                    label = next((label for label in mapping
                                  if _label_key(label) == _label_key(dimension)), None)
                    expected_dimensions.add(_label_key(mapping[label] if label is not None else dimension))
                actual_dimensions = {_label_key(value) for value in actual.get("group_dimensions", [])}
                if expected_dimensions == actual_dimensions:
                    matched = True
                    break
            if not matched:
                grouping_match = False
                break
    else:
        grouping_match = True
    reference_semantics_match = bool(outputs) and all(item.get("semantic_reference_passed") is True for item in outputs)

    all_expected_outputs_present = len(existing) == len(expected_outputs) and bool(expected_outputs)
    all_columns = bool(column_checks) and all(column_checks.values())
    all_numeric_finite = bool(finite_checks) and all(finite_checks.values())
    all_group_keys_unique = bool(unique_checks) and all(unique_checks.values())
    checks = {
        "all_requested_outputs_exist_and_read": all_expected_outputs_present and read_ok,
        "output_columns_complete": all_columns,
        "numeric_metrics_are_finite": all_numeric_finite,
        "group_keys_are_unique": all_group_keys_unique,
        "requested_sources_equal_used_sources": sources_match,
        "requested_metrics_equal_generated_metrics": metrics_match,
        "requested_outputs_equal_generated_outputs": outputs_match,
        "requested_grouping_matches_actual_grouping": grouping_match,
        "requested_filters_equal_applied_filters": filters_match,
        "sql_results_match_semantic_reference": reference_semantics_match,
        "no_requested_metric_lost": requested_metrics.issubset(generated_metrics),
        "no_requested_source_skipped": requested_sources.issubset(used_sources),
    }
    details = {
        "requested_sources": sorted(requested_sources),
        "used_sources": sorted(used_sources),
        "requested_metrics": sorted(requested_metrics),
        "generated_metrics": sorted(generated_metrics),
        "requested_outputs": requested_output_files,
        "generated_outputs": generated_file_names,
        "requested_groupings": requested_groups,
        "actual_groupings": [{"time_grain": item.get("group_by", "all"),
                               "dimensions": item.get("group_dimensions", [])} for item in outputs],
        "output_checks": {
            "columns": column_checks,
            "numeric_finite": finite_checks,
            "group_keys_unique": unique_checks,
        },
        "semantic_correctness": all(checks[key] for key in (
            "requested_sources_equal_used_sources",
            "requested_metrics_equal_generated_metrics",
            "requested_outputs_equal_generated_outputs",
            "requested_grouping_matches_actual_grouping",
            "requested_filters_equal_applied_filters",
            "sql_results_match_semantic_reference",
            "no_requested_metric_lost",
            "no_requested_source_skipped",
        )),
        "technical_correctness": all(checks[key] for key in (
            "all_requested_outputs_exist_and_read", "output_columns_complete",
            "numeric_metrics_are_finite", "group_keys_are_unique",
        )),
    }
    return checks, details


def _validate_builtin(output: Path, plan: dict) -> tuple[dict, dict]:
    checks = {
        "output_file_exists": output.is_file(),
        "expected_columns": False,
        "numeric_metrics_are_finite": False,
        "has_result_rows": False,
        "report_period_is_unique": False,
    }
    details = {}
    if output.is_file():
        try:
            frame = pd.read_csv(output)
            revenue_columns = (["daily_revenue", "monthly_revenue"]
                               if {"daily_revenue", "monthly_revenue"}.issubset(frame.columns)
                               else ["total_revenue"])
            required = {"report_period", "completed_orders", *revenue_columns}
            checks["expected_columns"] = required.issubset(frame.columns)
            metrics = [column for column in [*revenue_columns, "completed_orders"] if column in frame.columns]
            checks["numeric_metrics_are_finite"] = _finite_numeric(frame, metrics)
            checks["has_result_rows"] = len(frame) > 0
            checks["report_period_is_unique"] = frame["report_period"].nunique(dropna=False) == len(frame) if "report_period" in frame else False
            details = {"row_count": len(frame), "columns": list(frame.columns)}
        except Exception as error:
            details = {"read_error": f"{type(error).__name__}: {error}"}
    return checks, details


def run(state: dict) -> dict:
    plan = _read_json(PLAN_PATH, {}) or state.get("plan", {}) or {}
    generic_csv = plan.get("dataset_mode") == "generic_csv"
    specs = plan.get("output_specs", []) if generic_csv else []
    paths = [OUTPUT_DIR / str(spec.get("file_name", "")) for spec in specs]
    for path in paths or [DEFAULT_OUTPUT]:
        path.unlink(missing_ok=True)
    manifest_path = GENERATED / "output_manifest.json"
    manifest_path.unlink(missing_ok=True)

    stdout, stderr = "", ""
    runtime_seconds = None
    measured_cpu_seconds = None
    measured_peak_memory_mb = None
    exit_code = None
    if not PIPELINE.is_file():
        stderr = "Missing generated/generated_pipeline.py"
    else:
        started = time.perf_counter()
        sql_mode = "selected" if state.get("optimization_phase") in {"verify_candidate", "verify_fallback"} else "baseline"
        process_env = os.environ.copy()
        process_env["FLOWFORGE_SQL_RUN_MODE"] = sql_mode
        try:
            process = subprocess.Popen(
                [sys.executable, str(PIPELINE)], cwd=ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=process_env,
            )
            samples: dict = {}
            monitor = threading.Thread(target=monitor_process, args=(process, samples), daemon=True)
            monitor.start()
            try:
                stdout, stderr = process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                stderr = (stderr or "") + "\nPipeline exceeded the 60 second Tester timeout."
            monitor.join(timeout=1)
            runtime_seconds = time.perf_counter() - started
            measured_cpu_seconds = samples.get("cpu_seconds")
            measured_peak_memory_mb = samples.get("peak_memory_mb")
            exit_code = process.returncode
        except Exception as error:
            runtime_seconds = time.perf_counter() - started
            stderr = f"Tester could not execute pipeline: {type(error).__name__}: {error}"

    if generic_csv:
        checks, details = _validate_generic(plan, _read_json(manifest_path, {}) or {}, OUTPUT_DIR)
        checks["pipeline_exit_code"] = exit_code == 0
        details["pipeline_exit_code"] = exit_code
        semantic = details["semantic_correctness"] and checks["pipeline_exit_code"]
        technical = details["technical_correctness"] and checks["pipeline_exit_code"]
    else:
        checks, details = _validate_builtin(DEFAULT_OUTPUT, plan)
        checks["pipeline_exit_code"] = exit_code == 0
        semantic = all(checks.values())
        technical = all(checks.values())

    passed = all(checks.values())
    metrics = {
        "runtime_seconds": round(runtime_seconds, 6) if runtime_seconds is not None else None,
        "cpu_seconds": measured_cpu_seconds,
        "peak_memory_mb": measured_peak_memory_mb,
        "cost_usd": 0.0 if state.get("provider", "mock") == "mock" else None,
        "cost_note": "Mock provider has no API charge" if state.get("provider", "mock") == "mock" else "Provider usage and pricing are not exposed by the current LLM adapter",
        "correctness": sum(checks.values()) / len(checks) if checks else 0.0,
        "semantic_correctness": semantic,
        "technical_correctness": technical,
    }
    report = {
        "agent": "tester",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "details": details,
        "metrics": metrics,
        "stdout": stdout,
        "stderr": stderr,
    }
    GENERATED.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts = dict(state.get("artifacts", {}))
    artifacts["test_report"] = str(REPORT)
    if paths:
        artifacts["outputs"] = [str(path) for path in paths]
        artifacts["output_manifest"] = str(manifest_path)
        artifacts["output"] = str(paths[0])
    else:
        artifacts["output"] = str(DEFAULT_OUTPUT)
    result = {
        "artifacts": artifacts,
        "test_status": report["status"],
        "performance_metrics": metrics,
        "status": "tested",
    }
    phase = state.get("optimization_phase")
    if phase in {None, "baseline"}:
        result.update({"baseline_test_status": report["status"], "baseline_metrics": metrics})
    elif phase == "verify_candidate":
        result.update({"candidate_test_status": report["status"], "candidate_metrics": metrics})
    elif phase == "verify_fallback":
        result.update({"fallback_test_status": report["status"], "fallback_metrics": metrics})
    return result
