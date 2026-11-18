"""CSV input contracts and normalization shared by the CLI and quote adapters."""

import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from .periods import month_index

FLOW_FIELDS = (
    "revenue", "cogs", "ebit", "da", "net_income", "profit_before_tax",
    "income_tax_expense", "ebitda_adjustments", "interest_expense", "operating_cf",
    "capex",
)
FINANCIAL_FIELDS = (*FLOW_FIELDS, "equity", "assets", "current_assets", "current_liabilities",
    "inventory", "cash", "debt", "shares",
)
EXPENSES = {"cogs", "da", "interest_expense", "capex"}
INSTRUMENT_COLUMNS = ("source", "ticker", "currency")
PERIOD_COLUMNS = (*INSTRUMENT_COLUMNS, "period_type", "period_end")
PROVENANCE_COLUMNS = ("source_ref", "retrieved_at")
FINANCIAL_COLUMNS = (*PERIOD_COLUMNS, *FINANCIAL_FIELDS, *PROVENANCE_COLUMNS)
QUOTE_COLUMNS = (*INSTRUMENT_COLUMNS, "date", "close", *PROVENANCE_COLUMNS, "close_source_field", "price_scale")
LOG_COLUMNS = ("row", *INSTRUMENT_COLUMNS, "stage", "status", "reason", "input_file")


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
    scale: float = 1.0
    source_field: str | None = None
    formula: str | None = None
    operands: tuple["Observation", ...] = ()
    source_period_end: date | None = None

    def trace(self):
        trace = {
            "field": self.field,
            "source_field": (self.source_field or self.field) if not self.formula else None,
            "value": self.value,
            "raw": self.raw,
            "scale": self.scale if not self.formula else None,
            "source_ref": self.source_ref,
            "retrieved_at": self.retrieved_at,
            "row": self.row,
            "date": self.observed_on,
            "reason": self.reason,
        }
        if self.source_period_end is not None:
            trace["source_period_end"] = self.source_period_end
        if self.formula:
            trace.update(
                formula=self.formula, status=self.reason or "ok",
                inputs={f"{index}. {item.field}": item.trace() for index, item in enumerate(self.operands, 1)},
            )
        return trace


@dataclass(frozen=True)
class Statement:
    instrument: Instrument
    period_type: str
    period_end: date
    fields: dict[str, Observation]
    fiscal_year: str = ""
    fiscal_quarter: int | None = None
    flow_basis: str = "period"

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
            **self.instrument.columns(), "date": self.day, "close": self.close.raw,
            "source_ref": self.close.source_ref, "retrieved_at": self.close.retrieved_at,
            "close_source_field": self.close.source_field or "close", "price_scale": self.close.scale,
        }


def log_entry(stage, status, reason="", *, row=None, source="", ticker="", currency="", input_file=""):
    return {
        "row": row, "source": source, "ticker": ticker, "currency": currency,
        "stage": stage, "status": status, "reason": reason, "input_file": input_file,
    }


def read_csv(path: Path, required: tuple[str, ...]):
    """Ignore extra named columns while reporting malformed data rows individually."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        header = stream.readline()
        delimiter = ";" if ";" in header else ","
        stream.seek(0)
        reader = csv.reader(iter(stream.readline, ""), delimiter=delimiter, strict=True)
        try:
            names = next(reader, [])
        except csv.Error as error:
            raise ValueError(f"{path.name}: некорректный заголовок CSV") from error
        if len(names) != len(set(names)) or not set(required).issubset(names):
            raise ValueError(f"{path.name}: нужны уникальные колонки {', '.join(required)}")
        line_offset = 0
        while True:
            start_line = line_offset + reader.line_num + 1
            position = stream.tell()
            try:
                values = next(reader)
            except StopIteration:
                break
            except csv.Error:
                # A missing quote can consume subsequent rows. Resume one physical line later.
                stream.seek(position)
                stream.readline()
                line_offset = start_line
                reader = csv.reader(iter(stream.readline, ""), delimiter=delimiter, strict=True)
                yield start_line, {**dict.fromkeys(names), None: "Нарушен синтаксис CSV"}
                continue
            if values:
                row = dict.fromkeys(names)
                row.update(zip(names, values))
                if len(values) > len(names):
                    row[None] = "Лишние значения в строке"
                yield start_line, row


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
            reason = record.get(None, "Нужны ticker и market moex/yahoo")
            log.append(log_entry("tickers", "invalid_row", reason, **context))
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


def scale_of(record, field):
    raw = (record.get(field) or "").strip()
    if not raw:
        return 1.0
    value, _ = number(raw)
    if value is None or value <= 0:
        raise ValueError(f"{field}: ожидается положительный конечный множитель")
    return value


def fiscal_period(record, kind, day):
    basis = (record.get("flow_basis") or "").strip().lower() or "period"
    year = (record.get("fiscal_year") or "").strip()
    quarter = (record.get("fiscal_quarter") or "").strip()
    if basis not in {"period", "ytd"} or (basis == "ytd" and kind != "Q"):
        raise ValueError("flow_basis должен быть period или ytd (только для Q)")
    if year and not re.fullmatch(r"[0-9]{4}", year):
        raise ValueError("fiscal_year должен иметь формат YYYY")
    if quarter and (quarter not in {"1", "2", "3", "4"} or kind != "Q"):
        raise ValueError("fiscal_quarter для Q должен быть от 1 до 4")
    if basis == "ytd" and not (year and quarter):
        raise ValueError("Для ytd обязательны fiscal_year и fiscal_quarter")
    if kind == "FY":
        year = year or f"{day.year:04d}"
    return year, int(quarter) if quarter else None, basis


def input_rows(paths, required):
    for path in paths:
        for row, record in read_csv(path, required):
            yield path, row, record


def read_records(paths, requested, log, *, statements, seen=None):
    if isinstance(paths, Path):
        paths = (paths,)
    stage = "financials" if statements else "quotes"
    required = PERIOD_COLUMNS if statements else (*INSTRUMENT_COLUMNS, "date", "close")
    grouped = defaultdict(list)
    for path, row, record in input_rows(paths, required):
        context = {"row": row, "input_file": path.name, **{key: record.get(key) or "" for key in INSTRUMENT_COLUMNS}}
        try:
            if None in record:
                raise ValueError(record[None])
            instrument = parse_instrument(record)
            pair = (instrument.source, instrument.ticker)
            if pair not in requested:
                continue
            if seen is not None:
                seen.add(pair)
            context.update(instrument.columns())
            observed_on = iso_date(record["period_end" if statements else "date"])
            source_period_end = record.get("source_period_end") if statements else None
            if source_period_end:
                source_period_end = iso_date(source_period_end)
            else:
                source_period_end = None
            source_ref = (record.get("source_ref") or "").strip() or path.name
            retrieved_at = (record.get("retrieved_at") or "").strip()
            if retrieved_at:
                iso_date(retrieved_at)
            scales = {
                "amount": scale_of(record, "amount_scale"),
                "shares": scale_of(record, "shares_scale"),
            } if statements else {"price": scale_of(record, "price_scale")}

            def observe(field):
                raw = record.get(field)
                value, reason = number(raw)
                scale = scales["price"] if not statements else scales["shares" if field == "shares" else "amount"]
                if value is not None and scale != 1:
                    value = float(Decimal(str(value)) * Decimal(str(scale)))
                    if not math.isfinite(value):
                        value, reason = None, "nonfinite_number"
                if value is not None and field in EXPENSES:
                    value = abs(value)
                if value is not None and field == "close" and value <= 0:
                    value, reason = None, "nonpositive_close"
                if reason and (reason != "missing_value" or field == "close"):
                    log.append(log_entry(stage, reason, field, **context))
                source_field = (record.get(f"{field}_source_field") or "").strip() or field
                return Observation(field, value, raw, source_ref, retrieved_at, row, observed_on, reason,
                                   scale, source_field, source_period_end=source_period_end)

            if statements:
                kind = (record["period_type"] or "").strip().upper()
                if kind not in {"Q", "FY"}:
                    raise ValueError("period_type должен быть Q или FY")
                if not 2 <= observed_on.year <= 9998:
                    raise ValueError("Год отчётного периода должен быть от 0002 до 9998")
                fiscal_year, quarter, basis = fiscal_period(record, kind, observed_on)
                fields = {field: observe(field) for field in FINANCIAL_FIELDS}
                item = Statement(instrument, kind, observed_on, fields, fiscal_year, quarter, basis)
                key = (instrument, kind, observed_on)
                values = (fiscal_year, quarter, basis, *(field.value for field in fields.values()))
            else:
                close = observe("close")
                item = Quote(instrument, observed_on, close)
                key = (instrument, observed_on)
                values = (close.value,)
            grouped[key].append((item, values, context))
        except ValueError as error:
            log.append(log_entry(stage, "invalid_row", str(error), **context))

    accepted = []
    conflicts = set()
    for key, entries in grouped.items():
        item, _, _ = entries[0]
        if len({values for _, values, _ in entries}) > 1:
            conflicts.add(key)
            for _, _, context in entries:
                log.append(log_entry(stage, "conflict", "Разные значения для одного ключа", **context))
            continue
        if statements and not any(field.value is not None for field in item.fields.values()):
            log.append(log_entry(stage, "missing_values", "Нет числовых полей отчётности", **entries[0][2]))
        elif statements or item.close.value is not None:
            accepted.append(item)
        for _, _, context in entries[1:]:
            log.append(log_entry(stage, "duplicate", "Повтор записи", **context))
    return normalize_quarters(accepted, conflicts, log) if statements else accepted


def quarter_flow(current, prior, quarter):
    if quarter == 1:
        return replace(current, formula="Q1 = YTD1", operands=(current,))
    operand = prior or Observation(current.field, None, None, "", "", reason="missing_prior_ytd")
    value = None
    reason = current.reason or operand.reason
    if current.value is not None and operand.value is not None:
        value = float(Decimal(str(current.value)) - Decimal(str(operand.value)))
        if not math.isfinite(value):
            value, reason = None, "nonfinite_number"
    label = "FY" if quarter == 4 else f"YTD{quarter}"
    return replace(current, value=value, raw=None, reason=reason, formula=f"Q{quarter} = {label} − YTD{quarter - 1}",
                   operands=(current, operand))


def normalize_quarters(statements, conflicts, log):
    """Turn YTD flows into discrete quarters using the same fiscal year and instrument."""
    fiscal_slots = defaultdict(list)
    source_keys = set(conflicts)
    for item in statements:
        source_keys.add((item.instrument, item.period_type, item.period_end))
        if item.flow_basis == "ytd":
            fiscal_slots[item.instrument, item.fiscal_year, item.fiscal_quarter].append(item)

    def context(item):
        return {"row": item.fields["revenue"].row, **item.instrument.columns()}

    for key, candidates in fiscal_slots.items():
        if len(candidates) > 1:
            for item in candidates:
                log.append(log_entry("financials", "conflict", f"Несколько дат для FY {key[1]}, Q{key[2]}",
                                     **context(item)))

    def preceding_quarter(item, quarter):
        candidates = fiscal_slots[item.instrument, item.fiscal_year, quarter - 1]
        if len(candidates) == 1 and month_index(item.period_end) - month_index(candidates[0].period_end) == 3:
            return candidates[0]
        return None

    def convert(item, quarter, previous):
        fields = dict(item.fields)
        for field in FLOW_FIELDS:
            fields[field] = quarter_flow(fields[field], previous.fields[field] if previous else None, quarter)
        return replace(item, period_type="Q", fields=fields, fiscal_quarter=quarter, flow_basis="period")

    normalized = []
    for item in statements:
        if item.flow_basis == "ytd":
            quarter = item.fiscal_quarter
            if len(fiscal_slots[item.instrument, item.fiscal_year, quarter]) > 1:
                continue
            previous = preceding_quarter(item, quarter) if quarter > 1 else None
            if quarter > 1 and previous is None:
                log.append(log_entry("financials", "missing_prior_ytd", f"Для Q{quarter} нужен YTD{quarter - 1}",
                                     **context(item)))
            normalized.append(convert(item, quarter, previous))
            continue
        normalized.append(item)
        if item.period_type == "FY" and (item.instrument, "Q", item.period_end) not in source_keys:
            previous = preceding_quarter(item, 4)
            if previous is not None:
                normalized.append(convert(item, 4, previous))
    return normalized


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
