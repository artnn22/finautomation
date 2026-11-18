"""Chronological revenue forecasting and like-for-like evaluation."""

import math
from collections import Counter, defaultdict
from statistics import mean

from sklearn.linear_model import LinearRegression

from .periods import month_index, shift_months

FORECAST_COLUMNS = (
    "target_period_end", "kind", "history_count", "y_true", "forecast", "naive",
    "naive_period_end", "model", "linear_reference", "error", "forecast_growth", "mape_excluded",
    "mape_exclusion_reason", "status", "reason",
)
MODEL_LABELS = {
    "linear_trend": "Линейный тренд",
    "recent_trend": "Тренд по недавней истории",
    "seasonal_trend": "Тренд с квартальной сезонностью",
}
VALIDATION_MINIMUM = 3
VALIDATION_WINDOW = 8


def fit_trend(history, *, seasonal=False):
    def features(index):
        if seasonal:
            return [index, *(int(index % 4 == quarter) for quarter in (1, 2, 3))]
        return [index]

    scale = max(abs(value) for value in history) or 1
    model = LinearRegression().fit([features(i) for i in range(len(history))], [value / scale for value in history])
    result = float(model.predict([features(len(history))])[0]) * scale
    return result if math.isfinite(result) else None


def select_model(history, frequency, minimum):
    reference = fit_trend(history)
    recent_window = max(minimum, 4)
    candidates = [("linear_trend", minimum, fit_trend)]
    if len(history) >= recent_window + 1 + VALIDATION_MINIMUM:
        candidates.append(("recent_trend", recent_window + 1, lambda values: fit_trend(values[-recent_window:])))
    seasonal_minimum = max(minimum, 8)
    if frequency == "Q" and len(history) >= seasonal_minimum + VALIDATION_MINIMUM:
        candidates.append(("seasonal_trend", seasonal_minimum, lambda values: fit_trend(values, seasonal=True)))
    selected = candidates[0]
    if len(candidates) > 1:
        start = max(len(history) - VALIDATION_WINDOW, max(required for _, required, _ in candidates))
        losses = []
        for index in range(start, len(history)):
            actual = history[index]
            if actual <= 0:
                continue
            predictions = [fit(history[:index]) for _, _, fit in candidates]
            errors = [abs(value - actual) / actual for value in predictions if value is not None]
            if len(errors) == len(candidates) and all(map(math.isfinite, errors)):
                losses.append(errors)
        if len(losses) >= VALIDATION_MINIMUM:
            scores = [mean(column) for column in zip(*losses, strict=True)]
            lowest = min(scores)
            best = next(index for index, error in enumerate(scores) if error - lowest <= 1e-10)
            selected = candidates[best]
    name, _, fitted = selected
    predicted = reference if name == "linear_trend" else fitted(history)
    return (name, predicted, reference) if predicted is not None else ("linear_trend", reference, reference)


def forecast_row(statement, target, history, minimum, *, evaluation, last_observed, actual=None, ambiguous=False):
    naive_period_end, naive = last_observed
    enough_history = len(history) >= minimum
    method, predicted, reference = "", None, None
    if enough_history and not ambiguous:
        method, predicted, reference = select_model(history, statement.period_type, minimum)
    if ambiguous:
        reason = "ambiguous_period"
    elif not enough_history:
        reason = "insufficient_contiguous_history"
    elif predicted is None:
        reason = "nonfinite_prediction"
    else:
        reason = ""
    error = predicted - actual if predicted is not None and actual is not None else None
    growth = predicted / naive - 1 if predicted is not None and naive not in (None, 0) else None
    if growth is not None and not math.isfinite(growth):
        growth = None
    if not evaluation:
        exclusion = "future_period"
    elif actual is None:
        exclusion = "missing_actual"
    elif actual <= 0:
        exclusion = "nonpositive_actual"
    else:
        exclusion = reason
    status = "not_evaluable" if reason else "ok"
    return {
        **statement.instrument.columns(), "period_type": statement.period_type, "target_period_end": target,
        "kind": "backtest" if evaluation else "next", "history_count": len(history), "y_true": actual,
        "forecast": predicted, "naive": naive, "naive_period_end": naive_period_end,
        "model": method if predicted is not None else "", "linear_reference": reference,
        "error": error if error is None or math.isfinite(error) else None,
        "forecast_growth": growth, "mape_excluded": bool(exclusion), "mape_exclusion_reason": exclusion,
        "status": status, "reason": reason,
    }


def forecast_revenue(statements, minimum):
    grouped = defaultdict(list)
    for item in statements:
        grouped[item.instrument, item.period_type].append(item)
    rows = []
    for (_, frequency), series in sorted(grouped.items()):
        step = 3 if frequency == "Q" else 12
        buckets = defaultdict(list)
        for item in series:
            buckets[month_index(item.period_end)].append(item)
        history = []
        previous = None
        latest = None
        last_observed = (None, None)
        for month, candidates in sorted(buckets.items()):
            latest = max(candidates, key=lambda item: item.period_end)
            actual = latest.fields["revenue"].value if len(candidates) == 1 else None
            if previous is None or month != previous + step:
                history.clear()
            rows.append(forecast_row(latest, latest.period_end, history, minimum,
                                     evaluation=True, last_observed=last_observed, actual=actual,
                                     ambiguous=len(candidates) != 1))
            if actual is None:
                history.clear()
            else:
                history.append(actual)
                last_observed = (latest.period_end, actual)
            previous = month
        target = shift_months(latest.period_end, step, preserve_month_end=True)
        rows.append(forecast_row(latest, target, history, minimum, evaluation=False, last_observed=last_observed))
    return rows, score(rows, minimum)


def score(rows, minimum):
    backtests = [row for row in rows if row["kind"] == "backtest"]
    totals = Counter(row["period_type"] for row in backtests)
    scored = defaultdict(list)
    for row in backtests:
        if row["mape_excluded"]:
            continue
        actual = row["y_true"]
        model_ape = abs(row["forecast"] - actual) / actual
        naive_ape = abs(row["naive"] - actual) / actual
        reference = row["linear_reference"]
        linear_ape = abs(reference - actual) / actual if reference is not None else None
        if math.isfinite(model_ape) and math.isfinite(naive_ape):
            scored[row["period_type"]].append((model_ape, naive_ape, linear_ape))
        else:
            row["mape_excluded"] = True
            row["mape_exclusion_reason"] = "nonfinite_error"

    def summary(points, total):
        count = len(points)
        model = mean(item[0] for item in points) if count else None
        naive = mean(item[1] for item in points) if count else None
        linear = mean(item[2] for item in points) if count and all(
            item[2] is not None and math.isfinite(item[2]) for item in points
        ) else None
        status = ("pass" if model <= 0.20 or model < naive else "fail") if count else "not_evaluable"
        return {
            "mape_model": model, "mape_naive": naive, "eligible_points": count,
            "mape_linear_reference": linear,
            "excluded_points": total - count, "status": status,
        }

    return {
        "model": "adaptive_linear", "minimum_training_periods": minimum, "threshold_mape": 0.20,
        "selected_models": dict(Counter(row["model"] for row in rows if row["kind"] == "next" and row["model"])),
        **summary([point for points in scored.values() for point in points], len(backtests)),
        "by_period_type": {kind: summary(scored[kind], totals[kind]) for kind in ("Q", "FY")},
    }
