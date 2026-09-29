"""Small deterministic derived-field operator set used by generated pipelines."""

from __future__ import annotations

import pandas as pd


def apply_derived_fields(frame: pd.DataFrame, specifications: list[dict]) -> pd.DataFrame:
    result = frame.copy()
    for spec in specifications:
        name = str(spec.get("name", "")).strip()
        operation = str(spec.get("operation", "")).casefold().strip()
        if not name:
            raise ValueError("Derived field needs a name")
        if operation in {"date_diff", "datetime_difference"}:
            start = spec.get("start_field") or (spec.get("inputs") or [None, None])[0]
            end = spec.get("end_field") or (spec.get("inputs") or [None, None])[1]
            unit = str(spec.get("unit", "second")).casefold().rstrip("s")
            divisors = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
            if start not in result or end not in result or unit not in divisors:
                raise ValueError(f"Unsupported or unproven date difference for {name}")
            start_values = pd.to_datetime(result[start], errors="coerce", utc=True)
            end_values = pd.to_datetime(result[end], errors="coerce", utc=True)
            result[name] = (end_values - start_values).dt.total_seconds() / divisors[unit]
            continue

        left = spec.get("left_field") or (spec.get("inputs") or [None])[0]
        right = spec.get("right_field") or ((spec.get("inputs") or [None, None])[1])
        if operation in {"copy", "identity"}:
            if left not in result:
                raise ValueError(f"Derived field {name} refers to missing input {left}")
            result[name] = result[left]
        elif operation in {"add", "subtract", "multiply", "divide"}:
            if left not in result:
                raise ValueError(f"Derived field {name} refers to missing input {left}")
            left_values = pd.to_numeric(result[left], errors="coerce")
            if right is None and operation in {"multiply", "divide"}:
                factor = float(spec.get("factor", spec.get("divisor", 0)))
                if operation == "multiply":
                    result[name] = left_values * factor
                elif factor:
                    result[name] = left_values / factor
                else:
                    raise ValueError(f"Derived field {name} divides by zero or has no divisor")
            else:
                if right not in result:
                    raise ValueError(f"Derived field {name} refers to missing input {right}")
                right_values = pd.to_numeric(result[right], errors="coerce")
                if operation == "add":
                    result[name] = left_values + right_values
                elif operation == "subtract":
                    result[name] = left_values - right_values
                elif operation == "multiply":
                    result[name] = left_values * right_values
                else:
                    result[name] = left_values / right_values.where(right_values.ne(0))
        else:
            raise ValueError(f"Unsupported derived-field operation {operation!r} for {name}")
    return result
