"""Yahoo Finance statements normalized into the same input format as local reports."""

from collections import defaultdict
from datetime import timedelta
from http.client import HTTPException
from urllib.parse import quote, urlencode

from .inputs import FINANCIAL_COLUMNS, FINANCIAL_FIELDS, iso_date, log_entry, number, read_csv
from .quotes import get_json, unix_day, valid_currency

YAHOO_FIELDS = {
    "TotalRevenue": "revenue",
    "CostOfRevenue": "cogs",
    "OperatingIncome": "ebit",
    "DepreciationAndAmortization": "da",
    "NetIncome": "net_income",
    "PretaxIncome": "profit_before_tax",
    "TaxProvision": "income_tax_expense",
    "InterestExpense": "interest_expense",
    "OperatingCashFlow": "operating_cf",
    "CapitalExpenditure": "capex",
    "StockholdersEquity": "equity",
    "TotalAssets": "assets",
    "CurrentAssets": "current_assets",
    "CurrentLiabilities": "current_liabilities",
    "Inventory": "inventory",
    "CashAndCashEquivalents": "cash",
    "TotalDebt": "debt",
    "OrdinarySharesNumber": "shares",
}
YAHOO_SERIES = {
    prefix + name: (kind, duration, field)
    for prefix, kind, duration in (("annual", "FY", "12M"), ("quarterly", "Q", "3M"))
    for name, field in YAHOO_FIELDS.items()
}
ONLINE_FINANCIAL_COLUMNS = (
    *FINANCIAL_COLUMNS, "source_period_end", *(f"{field}_source_field" for field in FINANCIAL_FIELDS),
)
DATE_COLUMNS = ("ticker", "currency", "period_type", "source_period_end", "period_end")


def read_period_dates(path, requested, log):
    dates = {}
    for line, row in read_csv(path, DATE_COLUMNS):
        ticker = (row["ticker"] or "").strip().upper()
        currency = (row["currency"] or "").strip().upper()
        kind = (row["period_type"] or "").strip().upper()
        context = {"row": line, "source": "yahoo", "ticker": ticker, "currency": currency}
        if None in row or not ticker or not valid_currency(currency) or kind not in {"Q", "FY"}:
            log.append(log_entry("financial_dates", "invalid_row", "Нужны тикер, валюта и период Q/FY", **context))
            continue
        if ("yahoo", ticker) not in requested:
            continue
        try:
            key = (ticker, currency, kind, iso_date(row["source_period_end"]))
            corrected = iso_date(row["period_end"])
        except ValueError as error:
            log.append(log_entry("financial_dates", "invalid_row", str(error), **context))
            continue
        if key in dates and dates[key] != corrected:
            dates[key] = None
            log.append(log_entry("financial_dates", "conflict", f"Разные отчётные даты для {key[-1]}", **context))
        else:
            dates[key] = corrected
    return dates


def yahoo_financials(ticker, *, timeout, day, period_dates, log, context):
    reference = f"https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{quote(ticker, safe='')}"
    query = urlencode({"type": ",".join(YAHOO_SERIES), "period1": 0, "period2": unix_day(day + timedelta(days=1))})
    payload = get_json(f"{reference}?{query}", timeout)["timeseries"]
    if payload["error"]:
        raise ValueError("Yahoo Finance вернул ошибку запроса отчётности")
    grouped = defaultdict(dict)
    conflicts = set()
    for series in payload["result"] or ():
        name, = series["meta"]["type"]
        if name not in YAHOO_SERIES:
            continue
        if series["meta"]["symbol"] != [ticker]:
            raise ValueError("Yahoo Finance вернул отчётность другого тикера")
        kind, duration, field = YAHOO_SERIES[name]
        for point in series.get(name) or ():
            if not point or point.get("periodType") != duration:
                continue
            try:
                source_end = iso_date(point.get("asOfDate"))
            except ValueError:
                log.append(log_entry("online_financials", "invalid_date", name, **context))
                continue
            currency = point.get("currencyCode")
            if not valid_currency(currency):
                log.append(log_entry("online_financials", "invalid_currency", name, **context))
                continue
            key = (currency, kind, source_end)
            raw = (point.get("reportedValue") or {}).get("raw")
            value, reason = number(raw)
            if isinstance(raw, bool) or reason or (field == "shares" and value <= 0):
                if raw is not None:
                    log.append(log_entry("online_financials", "invalid_value", f"{name}, {source_end}", **context))
                continue
            previous = grouped[key].get(field)
            if previous is not None and float(previous) != value:
                conflicts.add(key)
                log.append(log_entry("online_financials", "conflict", f"{name}, {source_end}",
                                     currency=currency, **context))
                continue
            grouped[key][field] = raw
            grouped[key][f"{field}_source_field"] = name

    records = []
    for (currency, kind, source_end), values in sorted(grouped.items()):
        if (currency, kind, source_end) in conflicts:
            continue
        if not any(field in values for field in FINANCIAL_FIELDS if field != "shares"):
            continue
        exact_end = period_dates.get((ticker, currency, kind, source_end), source_end)
        if exact_end is None or exact_end > day:
            continue
        records.append({
            "source": "yahoo", "ticker": ticker, "currency": currency,
            "period_type": kind, "period_end": exact_end, "source_period_end": source_end,
            "source_ref": reference, "retrieved_at": day, **values,
        })
    return records


def fetch_financials(requested, settings, day, period_dates, log):
    records = []
    for (source, ticker), line in requested.items():
        context = {"row": line, "source": source, "ticker": ticker}
        if source != "yahoo":
            log.append(log_entry("online_financials", "requires_csv",
                                 "Для MOEX передайте отчётность через --financials", **context))
            continue
        try:
            statements = yahoo_financials(ticker, timeout=settings.quote_timeout, day=day,
                                         period_dates=period_dates, log=log, context=context)
        except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError) as error:
            log.append(log_entry("online_financials", "source_unavailable", str(error), **context))
            continue
        status = "ok" if statements else "no_financials"
        log.append(log_entry("online_financials", status, f"Загружено периодов: {len(statements)}", **context))
        records.extend(statements)
    return records
