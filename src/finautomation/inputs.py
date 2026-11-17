"""CSV input contracts and normalization shared by the CLI and quote adapters."""

import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

FINANCIAL_FIELDS = (
    "revenue", "cogs", "ebit", "da", "net_income", "profit_before_tax",
    "income_tax_expense", "ebitda_adjustments", "interest_expense", "operating_cf",
    "capex", "equity", "assets", "current_assets", "current_liabilities",
    "inventory", "cash", "debt", "shares",
)
EXPENSES = {"cogs", "da", "interest_expense", "capex"}
INSTRUMENT_COLUMNS = ("source", "ticker", "currency")
PERIOD_COLUMNS = (*INSTRUMENT_COLUMNS, "period_type", "period_end")
PROVENANCE_COLUMNS = ("source_ref", "retrieved_at")
FINANCIAL_COLUMNS = (*PERIOD_COLUMNS, *FINANCIAL_FIELDS, *PROVENANCE_COLUMNS)
QUOTE_COLUMNS = (*INSTRUMENT_COLUMNS, "date", "close", *PROVENANCE_COLUMNS)
LOG_COLUMNS = ("row", *INSTRUMENT_COLUMNS, "stage", "status", "reason")


@dataclass(frozen=True, order=True)
class Instrument:
    source: str
    ticker: str
    currency: str

    def columns(self):
        return dict(zip(INSTRUMENT_COLUMNS, (self.source, self.ticker, self.currency)))


@dataclass(frozen=True)
class Observation:
    field: str
    value: float | None
    raw: str | float | None
    source_ref: str
    retrieved_at: str
    row: int | None = None
    observed_on: date | None = None
    reason: str = ""

    def trace(self):
        return {
            "field": self.field,
            "value": self.value,
            "raw": self.raw,
            "source_ref": self.source_ref,
            "retrieved_at": self.retrieved_at,
            "row": self.row,
            "date": self.observed_on,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Statement:
    instrument: Instrument
    period_type: str
    period_end: date
    fields: dict[str, Observation]

    def columns(self):
        return {
            **self.instrument.columns(),
            "period_type": self.period_type,
            "period_end": self.period_end,
        }


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    day: date
    close: Observation

    def columns(self):
        return {
            **self.instrument.columns(), "date": self.day, "close": self.close.value,
            "source_ref": self.close.source_ref, "retrieved_at": self.close.retrieved_at,
        }


def log_entry(stage, status, reason="", *, row=None, source="", ticker="", currency=""):
    return {
        "row": row, "source": source, "ticker": ticker, "currency": currency,
        "stage": stage, "status": status, "reason": reason,
    }


def read_csv(path: Path, required: tuple[str, ...]):
    """Ignore extra named columns while reporting malformed data rows individually."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        header = stream.readline()
        delimiter = ";" if ";" in header else ","
        stream.seek(0)
        reader = csv.DictReader(stream, delimiter=delimiter)
        names = reader.fieldnames or []
        if len(names) != len(set(names)) or not set(required).issubset(names):
            raise ValueError(f"{path.name}: нужны уникальные колонки {', '.join(required)}")
        start_line = 2
        for row in reader:
            yield start_line, row
            start_line = reader.line_num + 1


def iso_date(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("ожидается дата YYYY-MM-DD")
    return date.fromisoformat(raw)


def number(raw):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "missing_value"
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None, "invalid_number"
    if not math.isfinite(value):
        return None, "nonfinite_number"
    return value, ""


def parse_tickers(path, log):
    requested = {}
    for row, record in read_csv(path, ("ticker", "market")):
        ticker = (record["ticker"] or "").strip().upper()
        source = (record["market"] or "").strip().lower()
        context = dict(row=row, ticker=ticker, source=source)
        key = (source, ticker)
        if None in record or not ticker or source not in {"moex", "yahoo"}:
            log.append(log_entry("tickers", "invalid_row", "Нужны ticker и market moex/yahoo", **context))
        elif key in requested:
            log.append(log_entry("tickers", "duplicate", "Повтор пары market/ticker", **context))
        else:
            requested[key] = row
    return requested


def parse_instrument(record):
    source = (record["source"] or "").strip().lower()
    ticker = (record["ticker"] or "").strip().upper()
    currency = (record["currency"] or "").strip().upper()
    if not ticker or source not in {"moex", "yahoo"} or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Нужны source moex/yahoo, ticker и трёхбуквенная валюта")
    return Instrument(source, ticker, currency)


def read_records(path, requested, log, *, statements):
    stage = "financials" if statements else "quotes"
    required = PERIOD_COLUMNS if statements else (*INSTRUMENT_COLUMNS, "date", "close")
    grouped = defaultdict(list)
    for row, record in read_csv(path, required):
        context = {"row": row, **{key: record.get(key) or "" for key in INSTRUMENT_COLUMNS}}
        try:
            if None in record:
                raise ValueError("Лишние значения в строке")
            instrument = parse_instrument(record)
            if (instrument.source, instrument.ticker) not in requested:
                continue
            context.update(instrument.columns())
            observed_on = iso_date(record["period_end" if statements else "date"])
            source_ref = (record.get("source_ref") or "").strip() or path.name
            retrieved_at = (record.get("retrieved_at") or "").strip()
            if retrieved_at:
                iso_date(retrieved_at)

            def observe(field):
                raw = record.get(field)
                value, reason = number(raw)
                if value is not None and field in EXPENSES:
                    value = abs(value)
                if value is not None and field == "close" and value <= 0:
                    value, reason = None, "nonpositive_close"
                if reason and (reason != "missing_value" or field == "close"):
                    log.append(log_entry(stage, reason, field, **context))
                return Observation(field, value, raw, source_ref, retrieved_at, row, observed_on, reason)

            if statements:
                kind = (record["period_type"] or "").strip().upper()
                if kind not in {"Q", "FY"}:
                    raise ValueError("period_type должен быть Q или FY")
                fields = {field: observe(field) for field in FINANCIAL_FIELDS}
                item = Statement(instrument, kind, observed_on, fields)
                key = (instrument, kind, observed_on)
                values = tuple(field.value for field in fields.values())
            else:
                close = observe("close")
                if close.value is None:
                    continue
                item = Quote(instrument, observed_on, close)
                key = (instrument, observed_on)
                values = (close.value,)
            grouped[key].append((item, values, context))
        except ValueError as error:
            log.append(log_entry(stage, "invalid_row", str(error), **context))

    accepted = []
    for entries in grouped.values():
        item, _, _ = entries[0]
        if len({values for _, values, _ in entries}) > 1:
            for _, _, context in entries:
                log.append(log_entry(stage, "conflict", "Разные значения для одного ключа", **context))
            continue
        accepted.append(item)
        for _, _, context in entries[1:]:
            log.append(log_entry(stage, "duplicate", "Повтор записи", **context))
    return accepted


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
