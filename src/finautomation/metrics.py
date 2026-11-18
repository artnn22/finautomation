"""Financial formulas, period matching and P/E divergence."""

import json
import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal

from .inputs import Observation, Statement
from .periods import month_index, shift_months

METRIC_COLUMNS = (
    "revenue", "revenue_yoy", "gross_margin", "operating_margin", "net_margin", "ebit",
    "ebitda", "ebitda_m", "adjusted_ebitda", "adjusted_ebitda_m", "profit_before_tax",
    "income_tax_expense", "effective_tax_rate", "roe", "roa", "cr", "qr", "de",
    "net_debt", "interest_coverage", "operating_cf", "fcf", "cash_conversion", "price",
    "return_1m", "return_3m", "return_12m", "market_cap", "pe_fy", "pe_ttm", "pb",
    "ev", "ev_ebitda",
)
TRACE_COLUMNS = ("metric", "formula", "value", "status", "missing_fields", "inputs")
PE_ALERT_THRESHOLD = Decimal("0.20")
ALERT_COLUMNS = (
    "date", "fy_period_end", "ttm_periods", "quote_date", "market_cap",
    "pe_fy_at_quarter", "pe_ttm_at_quarter", "deviation", "threshold",
)
SCENARIO_FIELDS = (
    "revenue", "ebitda_m", "ebitda", "debt", "net_debt", "operating_cf", "fcf", "ev_ebitda",
)
SCENARIO_COLUMNS = (
    "r", "m", "delta_d", *(f"{prefix}_{field}" for field in SCENARIO_FIELDS
                           for prefix in ("base", "scenario")), "formulas", "statuses",
)


def json_cell(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


class NotApplicable(ValueError):
    pass


@dataclass
class Calculation:
    name: str
    formula: str
    value: float | None
    status: str
    missing: tuple[str, ...]
    inputs: dict

    def trace(self):
        return {
            "metric": self.name, "formula": self.formula, "value": self.value, "status": self.status,
            "missing_fields": json_cell(self.missing), "inputs": json_cell(self.inputs),
        }

    def as_input(self):
        return {
            "formula": self.formula, "value": self.value, "status": self.status,
            "missing_fields": self.missing, "inputs": self.inputs,
        }


def evaluate(name, formula, fn, inputs, *, status=None):
    values = [item.value for item in inputs]
    missing = tuple(dict.fromkeys(
        field for item in inputs if item.value is None
        for field in (item.missing if isinstance(item, Calculation) and item.missing
                      else (item.name if isinstance(item, Calculation) else item.field,))
    ))
    provenance = {
        f"{index + 1}. {item.name if isinstance(item, Calculation) else item.field}":
        item.as_input() if isinstance(item, Calculation) else item.trace()
        for index, item in enumerate(inputs)
    }
    if status:
        return Calculation(name, formula, None, status, missing, provenance)
    if missing:
        derived_status = next(
            (item.status for item in inputs if isinstance(item, Calculation)
             and item.value is None and item.status != "missing_inputs"), None
        )
        return Calculation(name, formula, None, derived_status or "missing_inputs", missing, provenance)
    try:
        result = fn(*values)
        if not math.isfinite(result):
            return Calculation(name, formula, None, "nonfinite_result", (), provenance)
    except ZeroDivisionError:
        return Calculation(name, formula, None, "zero_denominator", (), provenance)
    except NotApplicable as error:
        return Calculation(name, formula, None, str(error), (), provenance)
    return Calculation(name, formula, result, "ok", (), provenance)


class QuoteHistory:
    def __init__(self, quotes):
        self.quotes = sorted(quotes, key=lambda quote: quote.day)
        self.dates = [quote.day for quote in self.quotes]

    def close_at(self, day):
        index = bisect_right(self.dates, day) - 1
        if index < 0:
            return Observation("close", None, None, "", "", observed_on=day, reason="no_close_at_cutoff")
        return self.quotes[index].close


def ratio(a, b):
    return a / b


def tax_rate(tax, profit):
    if profit < 0:
        raise NotApplicable("nonpositive_denominator")
    return tax / profit


def ttm_multiple(cap, *incomes):
    total = sum(Decimal(str(value)) for value in incomes)
    if total == 0:
        raise ZeroDivisionError
    return float(Decimal(str(cap)) / total)


def periods_by_month(statements):
    """Treat ambiguous dates in the same fiscal month as unavailable for windows."""
    buckets = defaultdict(list)
    for statement in statements:
        buckets[month_index(statement.period_end)].append(statement)
    return {month: entries[0] for month, entries in buckets.items() if len(entries) == 1}


def quarterly_window(quarters, period):
    end = month_index(period.period_end)
    window = [quarters.get(end + offset) for offset in (-9, -6, -3, 0)]
    return window if all(window) else []


def calculate_period(statement, history, same_frequency, quarters):
    fields = statement.fields
    results = {}

    def calc(name, formula, fn, *keys, status=None):
        inputs = [results[key] if key in results else fields[key] for key in keys]
        results[name] = evaluate(name, formula, fn, inputs, status=status)

    def direct(name, field):
        calc(name, field, lambda value: value, field)

    for field in ("revenue", "ebit", "profit_before_tax", "income_tax_expense", "operating_cf"):
        direct(field, field)
    prior = same_frequency.get(month_index(statement.period_end) - 12)
    previous_revenue = Observation("revenue_prior", None, None, "", "", reason="missing_prior_year_period")
    if prior:
        previous_revenue = replace(prior.fields["revenue"], field="revenue_prior",
                                   source_field=prior.fields["revenue"].source_field or "revenue")
    fields = {
        **fields,
        "revenue_prior": previous_revenue,
        "close": history.close_at(statement.period_end),
    }
    calc("revenue_yoy", "revenue / revenue_prior − 1", lambda a, b: a / b - 1, "revenue", "revenue_prior")
    calc("gross_margin", "(revenue − cogs) / revenue", lambda a, b: (a - b) / a, "revenue", "cogs")
    calc("operating_margin", "ebit / revenue", ratio, "ebit", "revenue")
    calc("net_margin", "net_income / revenue", ratio, "net_income", "revenue")
    calc("ebitda", "ebit + da", lambda a, b: a + b, "ebit", "da")
    calc("adjusted_ebitda", "ebitda + ebitda_adjustments", lambda a, b: a + b, "ebitda", "ebitda_adjustments")
    calc("ebitda_m", "ebitda / revenue", ratio, "ebitda", "revenue")
    calc("adjusted_ebitda_m", "adjusted_ebitda / revenue", ratio, "adjusted_ebitda", "revenue")
    calc("effective_tax_rate", "income_tax_expense / profit_before_tax при PBT > 0", tax_rate,
         "income_tax_expense", "profit_before_tax")
    calc("roe", "net_income / equity", ratio, "net_income", "equity")
    calc("roa", "net_income / assets", ratio, "net_income", "assets")
    calc("cr", "current_assets / current_liabilities", ratio, "current_assets", "current_liabilities")
    calc("qr", "(current_assets − inventory) / current_liabilities",
         lambda a, b, c: (a - b) / c, "current_assets", "inventory", "current_liabilities")
    calc("de", "debt / equity", ratio, "debt", "equity")
    calc("net_debt", "debt − cash", lambda a, b: a - b, "debt", "cash")
    calc("interest_coverage", "ebit / interest_expense", ratio, "ebit", "interest_expense")
    calc("fcf", "operating_cf − capex", lambda a, b: a - b, "operating_cf", "capex")
    calc("cash_conversion", "operating_cf / ebitda", ratio, "operating_cf", "ebitda")
    calc("price", "последнее close не позже period_end", lambda value: value, "close")
    for months in (1, 3, 12):
        historical = f"close_{months}m"
        original = history.close_at(shift_months(statement.period_end, -months))
        fields[historical] = replace(original, field=historical, source_field=original.source_field or "close")
        calc(f"return_{months}m", f"price / {historical} − 1", lambda a, b: a / b - 1, "price", historical)
    calc("market_cap", "price × shares", lambda a, b: a * b, "price", "shares")
    fy = statement.period_type == "FY"
    calc("pe_fy", "market_cap / net_income FY", ratio, "market_cap", "net_income",
         status=None if fy else "not_applicable_period")
    window = quarterly_window(quarters, statement) if not fy else []
    results["pe_ttm"] = evaluate(
        "pe_ttm", "market_cap / сумма net_income за 4 последовательных квартала",
        ttm_multiple,
        [results["market_cap"], *(item.fields["net_income"] for item in window)],
        status="not_applicable_period" if fy else (None if window else "incomplete_ttm_window"),
    )
    calc("pb", "market_cap / equity", ratio, "market_cap", "equity")
    calc("ev", "market_cap + net_debt", lambda a, b: a + b, "market_cap", "net_debt")
    calc("ev_ebitda", "ev / ebitda", ratio, "ev", "ebitda")
    return results


@dataclass
class PeriodResult:
    statement: Statement
    calculations: dict[str, Calculation]
    pe_comparison: dict | None = None

    def columns(self):
        return {
            **self.statement.columns(),
            **{name: self.calculations[name].value for name in METRIC_COLUMNS},
        }

    def traces(self):
        return [{**self.statement.columns(), **self.calculations[name].trace()} for name in METRIC_COLUMNS]


def compare_pe(quarter, years, quarters):
    statement = quarter.statement
    cap = quarter.calculations["market_cap"].value
    ttm = quarter.calculations["pe_ttm"]
    last_fy = next((year for year in reversed(years) if year.period_end <= statement.period_end), None)
    window = quarterly_window(quarters, statement)
    quote = quarter.calculations["price"].inputs["1. close"]
    comparison = {
        **statement.instrument.columns(), "date": statement.period_end,
        "fy_period_end": last_fy.period_end if last_fy else None,
        "ttm_periods": json_cell([item.period_end for item in window]),
        "quote_date": quote["date"] if quote["value"] is not None else None,
        "market_cap": cap, "pe_fy_at_quarter": None, "pe_ttm_at_quarter": ttm.value,
        "deviation": None, "threshold": float(PE_ALERT_THRESHOLD), "reason": "", "alert": False,
    }
    ni = last_fy.fields["net_income"].value if last_fy else None
    if not last_fy:
        fy_reason = "missing_fy_period"
    elif ni is None:
        fy_reason = "missing_fy_profit"
    elif ni == 0:
        fy_reason = "zero_fy_profit"
    elif cap is None:
        fy_reason = "missing_quarter_cap"
    else:
        fy_ratio = float(Decimal(str(cap)) / Decimal(str(ni)))
        fy_reason = "" if math.isfinite(fy_ratio) else "nonfinite_result"
        if not fy_reason:
            comparison["pe_fy_at_quarter"] = fy_ratio
    if ttm.value is None or fy_reason:
        comparison["reason"] = fy_reason or ttm.status
        return comparison

    # Compare at the boundary before converting the percentage back to float.
    common_cap = Decimal(str(cap))
    ttm_income = sum(Decimal(str(item.fields["net_income"].value)) for item in window)
    numerator = abs(ttm_income - Decimal(str(ni)))
    denominator = abs(Decimal(str(ni)))
    if abs(common_cap) < abs(ttm_income) * Decimal("0.000001"):
        numerator *= abs(common_cap)
        denominator *= abs(ttm_income) * Decimal("0.000001")
    deviation = float(numerator / denominator)
    if math.isfinite(deviation):
        comparison["deviation"] = deviation
        comparison["alert"] = numerator > PE_ALERT_THRESHOLD * denominator
    else:
        comparison["reason"] = "nonfinite_result"
    return comparison


def analyze(statements, quotes):
    statements_by_instrument = defaultdict(list)
    quotes_by_instrument = defaultdict(list)
    for statement in statements:
        statements_by_instrument[statement.instrument].append(statement)
    for quote in quotes:
        quotes_by_instrument[quote.instrument].append(quote)

    results, alerts = [], []
    for instrument, records in sorted(statements_by_instrument.items()):
        records = sorted(records, key=lambda item: (item.period_end, item.period_type))
        years = [item for item in records if item.period_type == "FY"]
        quarters = [item for item in records if item.period_type == "Q"]
        month_maps = {"FY": periods_by_month(years), "Q": periods_by_month(quarters)}
        history = QuoteHistory(quotes_by_instrument[instrument])
        rows = [
            PeriodResult(item, calculate_period(item, history, month_maps[item.period_type], month_maps["Q"]))
            for item in records
        ]
        results.extend(rows)
        current = next((row for row in reversed(rows) if row.statement.period_type == "Q"), None)
        if current is not None:
            comparison = compare_pe(current, years, month_maps["Q"])
            current.pe_comparison = comparison
            if comparison["alert"]:
                alerts.append({key: comparison[key] for key in (*instrument.columns(), *ALERT_COLUMNS)})
    return results, alerts


def scenario(period, changes):
    base = {**period.statement.fields, **period.calculations}
    for key, value in changes.items():
        base[key] = Observation(key, value, value, "config.yaml", "")
    changed = {}

    def calc(name, formula, fn, *keys):
        changed[name] = evaluate(name, formula, fn, [base[key] for key in keys])

    calc("revenue", "revenue × (1 + r)", lambda a, b: a * (1 + b), "revenue", "r")
    calc("ebitda_m", "ebitda_m + m", lambda a, b: a + b, "ebitda_m", "m")
    changed["ebitda"] = evaluate(
        "ebitda", "scenario.revenue × scenario.ebitda_m", lambda a, b: a * b,
        [changed["revenue"], changed["ebitda_m"]],
    )
    calc("debt", "debt + delta_d", lambda a, b: a + b, "debt", "delta_d")
    changed["net_debt"] = evaluate(
        "net_debt", "scenario.debt − cash", lambda a, b: a - b, [changed["debt"], base["cash"]],
    )
    changed["operating_cf"] = evaluate(
        "operating_cf", "operating_cf + (scenario.ebitda − ebitda)", lambda a, b, c: a + (b - c),
        [base["operating_cf"], changed["ebitda"], base["ebitda"]],
    )
    changed["fcf"] = evaluate(
        "fcf", "scenario.operating_cf − capex", lambda a, b: a - b,
        [changed["operating_cf"], base["capex"]],
    )
    changed["ev_ebitda"] = evaluate(
        "ev_ebitda", "(market_cap + scenario.net_debt) / scenario.ebitda", lambda a, b, c: (a + b) / c,
        [base["market_cap"], changed["net_debt"], changed["ebitda"]],
    )
    return {
        **period.statement.columns(), **changes,
        **{f"base_{field}": base[field].value for field in SCENARIO_FIELDS},
        **{f"scenario_{field}": changed[field].value for field in SCENARIO_FIELDS},
        "formulas": json_cell({name: item.formula for name, item in changed.items()}),
        "statuses": json_cell({name: {"status": item.status, "missing_fields": item.missing}
                               for name, item in changed.items()}),
    }
