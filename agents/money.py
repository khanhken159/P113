"""Strict parsing of monetary CSV values before SQL aggregation."""

import re
from decimal import Decimal, InvalidOperation

import pandas as pd


def parse_amount(value, currency: str, number_format: str = "") -> float | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    raw = str(value).strip().replace("\u00a0", "").replace(" ", "")
    markers = re.findall(r"VND|USD|EUR|₫|\$|€", raw, flags=re.I)
    marker_currency = {"₫": "VND", "$": "USD", "€": "EUR"}
    for marker in markers:
        actual = marker_currency.get(marker, marker.upper())
        if actual != currency:
            raise ValueError(f"Money value {value!r} uses {actual}, but the confirmed input unit is {currency}")
    raw = re.sub(r"^(?:VND|USD|EUR|₫|\$|€)", "", raw, flags=re.I)
    raw = re.sub(r"(?:VND|USD|EUR|₫|\$|€)$", "", raw, flags=re.I)
    if not re.fullmatch(r"-?\d[\d.,]*", raw):
        raise ValueError(f"Invalid money value {value!r}; specify its number format before calculating revenue")
    if any(char in raw for char in ".,"):
        last = max(raw.rfind("."), raw.rfind(","))
        suffix = raw[last + 1:]
        parts = re.split(r"[.,]", raw.lstrip("-"))
        single_separator = len(parts) == 2
        zero_decimal_currency = currency in {"VND", "JPY", "KRW"}
        if single_separator and zero_decimal_currency and not number_format:
            raise ValueError(f"Ambiguous money value {value!r}; clarify decimal/thousands format")
        if single_separator and number_format == "thousands" and len(suffix) < 3:
            if zero_decimal_currency:
                raw = str(Decimal(raw.replace(",", ".")) * 1000)
            else:
                # A one- or two-digit suffix is a decimal for currencies that
                # support minor units; `thousands` applies to three-digit groups.
                raw = raw.replace(",", ".")
        elif single_separator and number_format == "thousands":
            raw = raw.replace(".", "").replace(",", "")
        elif single_separator and number_format == "decimal":
            raw = raw.replace(",", ".")
        elif all(len(part) == 3 for part in parts[1:]):
            if zero_decimal_currency or len(parts) > 2:
                raw = raw.replace(".", "").replace(",", "")
            else:
                raise ValueError(f"Ambiguous money value {value!r}; clarify decimal/thousands format")
        elif len(suffix) in {1, 2}:
            decimal_separator = raw[last]
            other = "," if decimal_separator == "." else "."
            raw = raw.replace(other, "")
            raw = raw.replace(decimal_separator, ".")
        else:
            raise ValueError(f"Ambiguous money value {value!r}; clarify decimal/thousands format")
    try:
        number = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"Invalid money value {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"Invalid money value {value!r}")
    return float(number)


def parse_amount_series(series: pd.Series, currency: str, rate: float = 1.0,
                        number_format: str = "", missing_policy: str = "exclude",
                        currency_values: pd.Series | None = None, target_currency: str = "",
                        currency_rates: dict | None = None, missing_currency: str = "",
                        number_format_by_currency: dict | None = None) -> pd.Series:
    if currency_values is None and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Input currency is missing or invalid")
    if rate <= 0:
        raise ValueError("Currency conversion rate must be positive")
    if missing_policy not in {"exclude", "reject"}:
        raise ValueError("Missing amount policy must be confirmed")
    if missing_policy == "reject" and (series.isna() | series.astype("string").str.strip().eq("")).any():
        raise ValueError("Missing amount values must be corrected before calculating revenue")
    if currency_values is None:
        return series.map(lambda value: None if (parsed := parse_amount(value, currency, number_format)) is None else parsed * rate)
    if not re.fullmatch(r"[A-Z]{3}", target_currency):
        raise ValueError("Target currency is missing or invalid")
    rates = currency_rates or {}
    converted = []
    for amount, raw_currency in zip(series, currency_values):
        code = str(raw_currency).upper().strip() if not pd.isna(raw_currency) else ""
        code = code or missing_currency
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("A row has no valid currency; clarify its unit before calculating revenue")
        row_rate = 1.0 if code == target_currency else float(rates.get(code, 0))
        if not 0 < row_rate < float("inf"):
            raise ValueError(f"A positive conversion rate from {code} to {target_currency} is required")
        row_format = (number_format_by_currency or {}).get(code, number_format)
        parsed = parse_amount(amount, code, row_format)
        converted.append(None if parsed is None else parsed * row_rate)
    return pd.Series(converted, index=series.index)


def parse_number(value, number_format: str = "") -> float | None:
    """Parse a scalar numeric value without guessing malformed text as zero."""
    if pd.isna(value) or str(value).strip() == "":
        return None
    raw = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw) and number_format != "thousands":
        return float(raw)
    if not re.fullmatch(r"-?\d[\d.,]*", raw):
        return None
    if number_format not in {"", "thousands", "decimal"}:
        raise ValueError(f"Unknown number format {number_format!r}")
    separators = {separator for separator in ".," if separator in raw}
    if not separators:
        return float(raw)
    parts = re.split(r"[.,]", raw.lstrip("-"))
    if len(parts) > 2 and len(separators) == 1:
        if all(len(part) == 3 for part in parts[1:]):
            if number_format == "decimal":
                return None
            normalized = raw.replace(".", "").replace(",", "")
            return float(normalized)
        if number_format == "decimal" and len(parts[-1]) in {1, 2}:
            decimal_separator = raw[-(len(parts[-1]) + 1)]
            thousands_separator = "," if decimal_separator == "." else "."
            normalized = raw.replace(thousands_separator, "").replace(decimal_separator, ".")
            return float(normalized)
        if number_format == "thousands":
            return float(raw.replace(".", "").replace(",", ""))
        return None
    if len(separators) == 2:
        decimal_separator = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousands_separator = "," if decimal_separator == "." else "."
        suffix = raw.rsplit(decimal_separator, 1)[-1]
        if len(suffix) not in {1, 2} and number_format != "decimal":
            return None
        normalized = raw.replace(thousands_separator, "").replace(decimal_separator, ".")
        return float(normalized)
    separator = next(iter(separators))
    if number_format == "thousands":
        return float(raw.replace(separator, ""))
    if number_format == "decimal":
        return float(raw.replace(separator, "."))
    suffix = raw.rsplit(separator, 1)[-1]
    if len(suffix) in {1, 2}:
        return float(raw.replace(separator, "."))
    if len(suffix) == 3:
        return None
    return None


def parse_numeric_series(series: pd.Series, number_format: str = "") -> pd.Series:
    """Vector wrapper; malformed numeric values become null for validation/exclusion."""
    return series.map(lambda value: parse_number(value, number_format))


def parse_amount_series_for_output(
    series: pd.Series,
    currency: str,
    rate: float = 1.0,
    number_format: str = "",
    missing_policy: str = "exclude",
    currency_values: pd.Series | None = None,
    target_currency: str = "",
    currency_rates: dict | None = None,
    missing_currency: str = "",
    number_format_by_currency: dict | None = None,
    allow_numeric_without_currency: bool = False,
) -> pd.Series:
    """Parse row-output values numerically when no currency conversion is requested.

    A detail projection may normalize an amount-like field without aggregating
    it or converting its currency. In that case the numeric notation is needed,
    but a currency code is not. Aggregate monetary calculations continue to use
    the strict currency-aware parser.
    """
    if (allow_numeric_without_currency and currency_values is None
            and not re.fullmatch(r"[A-Z]{3}", str(currency or "").upper())):
        return parse_numeric_series(series, number_format)
    return parse_amount_series(
        series, str(currency or "").upper(), rate, number_format, missing_policy,
        currency_values, target_currency, currency_rates, missing_currency,
        number_format_by_currency,
    )
