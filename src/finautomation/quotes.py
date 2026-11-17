"""Daily close adapters. Both return the same records as an offline quote file."""

import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from http.client import HTTPException
from urllib.parse import quote as urlquote
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .inputs import Instrument, Observation, Quote, iso_date, log_entry, number
from .periods import shift_months

MOEX_BASE = "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards"


class QuoteSourceError(Exception):
    pass


def get_json(url, timeout):
    request = Request(url, headers={"User-Agent": "Opex/0.1 (historical quotes)"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def today():
    return datetime.now(ZoneInfo("Europe/Moscow")).date()


def valid_currency(currency):
    return (isinstance(currency, str) and len(currency) == 3 and currency.isascii()
            and currency.isalpha() and currency.isupper())


def quote_record(source, ticker, currency, day, raw, ref, retrieved_at):
    if not valid_currency(currency):
        return None
    value, _ = number(raw)
    if value is None or value <= 0:
        return None
    instrument = Instrument(source, ticker, currency)
    return Quote(instrument, day, Observation("close", value, raw, ref, retrieved_at, observed_on=day))


def moex_quotes(ticker, start, end, *, board, timeout):
    base = f"{MOEX_BASE}/{board}/securities/{urlquote(ticker, safe='')}.json"
    query = {"from": start.isoformat(), "till": end.isoformat(), "iss.meta": "off"}
    reference = f"{base}?{urlencode(query)}"
    candidates = defaultdict(list)
    offset = 0
    retrieved_at = today().isoformat()
    try:
        while True:
            data = get_json(f"{reference}&start={offset}", timeout)
            page = data["history"]
            for values in page["data"]:
                row = dict(zip(page["columns"], values, strict=True))
                if row["SECID"] != ticker or row["BOARDID"] != board:
                    continue
                day = iso_date(row["TRADEDATE"])
                currency = {"SUR": "RUB"}.get(row["CURRENCYID"], row["CURRENCYID"])
                item = quote_record("moex", ticker, currency, day, row["CLOSE"], reference, retrieved_at)
                if item and start <= day <= end:
                    candidates[(item.instrument, day)].append((str(row.get("TRADINGSESSION")) == "3", item))
            if not data["history.cursor"]["data"]:
                break
            cursor = dict(zip(data["history.cursor"]["columns"], data["history.cursor"]["data"][0], strict=True))
            next_offset = cursor["INDEX"] + cursor["PAGESIZE"]
            if next_offset >= cursor["TOTAL"]:
                break
            if next_offset <= offset or not page["data"]:
                raise QuoteSourceError("MOEX ISS вернул некорректную пагинацию")
            offset = next_offset
    except (OSError, HTTPException, ValueError, KeyError, TypeError) as error:
        raise QuoteSourceError(f"MOEX ISS: {error}") from error

    result = []
    for entries in candidates.values():
        aggregate = [item for is_aggregate, item in entries if is_aggregate]
        selected = aggregate or [item for _, item in entries]
        if len({item.close.value for item in selected}) > 1:
            raise QuoteSourceError(f"MOEX ISS: несколько цен закрытия за {selected[0].day}")
        result.append(selected[0])
    return result


def unix_day(day):
    return int(datetime.combine(day, time.min, tzinfo=UTC).timestamp())


def yahoo_quotes(ticker, start, end, *, timeout):
    # Yahoo uses Unix timestamps and its end date is exclusive. Fetch padding
    # around UTC boundaries and keep sessions by the exchange's local date.
    symbol = urlquote(ticker, safe="")
    query = urlencode({
        "period1": unix_day(start - timedelta(days=1)),
        "period2": unix_day(end + timedelta(days=2)),
        "interval": "1d",
    })
    reference = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
    try:
        chart = get_json(reference, timeout)["chart"]
        if chart["error"]:
            raise QuoteSourceError(f"Yahoo Finance: {chart['error'].get('description', 'ошибка Chart API')}")
        if not chart["result"]:
            return []
        payload = chart["result"][0]
        meta = payload["meta"]
        if meta["symbol"].upper() != ticker:
            raise QuoteSourceError("Yahoo Finance: ответ содержит другой тикер")
        currency = meta["currency"]
        if not valid_currency(currency):
            raise QuoteSourceError("Yahoo Finance: валюта отсутствует или цена указана в дробных единицах")
        zone = ZoneInfo(meta["exchangeTimezoneName"])
        timestamps = payload.get("timestamp") or []
        if not timestamps:
            return []
        closes = payload["indicators"]["quote"][0]["close"]
        result = []
        retrieved_at = today().isoformat()
        for timestamp, raw in zip(timestamps, closes, strict=True):
            day = datetime.fromtimestamp(timestamp, zone).date()
            if start <= day <= end:
                item = quote_record("yahoo", ticker, currency, day, raw, reference, retrieved_at)
                if item:
                    result.append(item)
        return result
    except (OSError, HTTPException, ValueError, KeyError, TypeError, IndexError) as error:
        raise QuoteSourceError(f"Yahoo Finance: {error}") from error


def fetch_online(statements, settings, log):
    grouped = defaultdict(list)
    result = []
    for item in statements:
        grouped[item.instrument.source, item.instrument.ticker].append(item)
    for (source, ticker), reports in sorted(grouped.items()):
        required = {item.instrument for item in reports}
        period_start = min(item.period_end for item in reports)
        first_cutoff = shift_months(period_start, -12)
        start = shift_months(period_start, -13)
        end = min(max(item.period_end for item in reports), today())
        adapter = moex_quotes if source == "moex" else yahoo_quotes
        options = {"timeout": settings.quote_timeout}
        if source == "moex":
            options["board"] = settings.moex_boards.get(ticker, "TQBR")

        context = {"source": source, "ticker": ticker}
        quotes = []
        try:
            if start <= end:
                quotes = adapter(ticker, start, end, **options)
                covered = {quote.instrument for quote in quotes if quote.day <= first_cutoff}
                if required - covered:
                    # Suspended or sparsely traded securities may have an older last close.
                    lower_bound = date(1997, 1, 1) if source == "moex" else date(1900, 1, 1)
                    if lower_bound < start:
                        quotes.extend(adapter(ticker, lower_bound, start - timedelta(days=1), **options))
        except QuoteSourceError as error:
            log.append(log_entry("online_quotes", "source_unavailable", str(error), **context))
        matched = [quote for quote in quotes if quote.instrument in required]
        result.extend(matched)
        currencies = {item.instrument.currency for item in quotes}
        have = {item.instrument for item in matched}
        for instrument in sorted(required):
            if instrument in have:
                count = sum(item.instrument == instrument for item in matched)
                log.append(log_entry("online_quotes", "ok", f"Дневных цен: {count}", **instrument.columns()))
            elif currencies:
                reason = f"Источник вернул валюты: {', '.join(sorted(currencies))}"
                log.append(log_entry("online_quotes", "currency_mismatch", reason, **instrument.columns()))
            else:
                log.append(log_entry("online_quotes", "no_quotes", "Нет котировок в диапазоне", **instrument.columns()))
    return sorted(result, key=lambda item: (item.instrument, item.day))
