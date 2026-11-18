"""YAML configuration for the screener, scenario, forecast and quote sources."""

import math
import operator
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

COMPARISONS = {
    "<": operator.lt, "<=": operator.le, "==": operator.eq,
    "!=": operator.ne, ">=": operator.ge, ">": operator.gt,
}
FILTER_FIELDS = {"pe_fy", "ev_ebitda", "de", "cr", "ebitda_m", "effective_tax_rate", "etr"}


@dataclass(frozen=True)
class Filter:
    field: str
    operator: str
    value: float

    def matches(self, row):
        actual = row["effective_tax_rate" if self.field == "etr" else self.field]
        return actual is not None and COMPARISONS[self.operator](actual, self.value)


@dataclass(frozen=True)
class Settings:
    filters: tuple[Filter, ...]
    scenario: dict[str, float]
    min_train_periods: int
    moex_boards: dict[str, str]
    quote_timeout: float


def mapping(value, allowed, name):
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError(f"{name}: ожидаются ключи из списка {', '.join(sorted(allowed))}")
    return value


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: ожидается конечное число")
    try:
        value = float(value)
    except OverflowError as error:
        raise ValueError(f"{name}: ожидается конечное число") from error
    if not math.isfinite(value):
        raise ValueError(f"{name}: ожидается конечное число")
    return value


def load_settings(path: Path):
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    root = mapping({} if data is None else data, {"filters", "scenario", "forecast", "quotes"}, "config")
    filters = []
    rules = root.get("filters", [])
    if not isinstance(rules, list):
        raise ValueError("filters: ожидается список")
    for rule in rules:
        rule = mapping(rule, {"field", "op", "value"}, "filter")
        field, op = rule.get("field"), rule.get("op")
        if not isinstance(field, str) or field not in FILTER_FIELDS:
            raise ValueError("filter.field: неизвестный показатель")
        if not isinstance(op, str) or op not in COMPARISONS:
            raise ValueError("filter.op: неизвестный оператор сравнения")
        filters.append(Filter(field, op, finite(rule.get("value"), "filter.value")))

    scenario = mapping(root.get("scenario", {}), {"r", "m", "delta_d"}, "scenario")
    scenario = {key: finite(scenario.get(key, 0), f"scenario.{key}") for key in ("r", "m", "delta_d")}
    forecast = mapping(root.get("forecast", {}), {"min_train_periods"}, "forecast")
    minimum = forecast.get("min_train_periods", 4)
    if type(minimum) is not int or minimum < 2:
        raise ValueError("forecast.min_train_periods: ожидается целое число от 2")
    quotes = mapping(root.get("quotes", {}), {"timeout_seconds", "moex_boards"}, "quotes")
    timeout = finite(quotes.get("timeout_seconds", 10), "quotes.timeout_seconds")
    if timeout <= 0:
        raise ValueError("quotes.timeout_seconds: требуется положительное число")
    boards = quotes.get("moex_boards", {})
    if not isinstance(boards, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]+", value)
        for key, value in boards.items()
    ):
        raise ValueError("quotes.moex_boards: ожидается соответствие тикеров кодам режимов торгов")
    return Settings(tuple(filters), scenario, minimum, {key.upper(): value for key, value in boards.items()}, timeout)
