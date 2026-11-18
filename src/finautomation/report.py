"""Local HTML output. All source text is escaped before rendering."""

import json
from collections import defaultdict
from html import escape

from .forecast import MODEL_LABELS
from .inputs import PERIOD_COLUMNS
from .metrics import SCENARIO_FIELDS

LABELS = {
    "revenue": "Выручка", "revenue_yoy": "Рост выручки год к году", "gross_margin": "Валовая маржа",
    "operating_margin": "Операционная маржа", "net_margin": "Чистая маржа", "ebit": "EBIT",
    "ebitda": "EBITDA", "ebitda_m": "Маржа EBITDA", "adjusted_ebitda": "Adjusted EBITDA",
    "adjusted_ebitda_m": "Маржа Adjusted EBITDA", "profit_before_tax": "Прибыль до налога",
    "income_tax_expense": "Расход по налогу", "effective_tax_rate": "Эффективная ставка налога",
    "roe": "ROE", "roa": "ROA", "cr": "Текущая ликвидность", "qr": "Быстрая ликвидность",
    "de": "Долг / капитал", "debt": "Долг", "net_debt": "Чистый долг",
    "interest_coverage": "Покрытие процентов", "operating_cf": "Операционный денежный поток",
    "fcf": "Свободный денежный поток", "cash_conversion": "OCF / EBITDA", "price": "Цена закрытия",
    "return_1m": "Доходность за 1 месяц", "return_3m": "Доходность за 3 месяца",
    "return_12m": "Доходность за 12 месяцев", "market_cap": "Капитализация", "pe_fy": "P/E FY",
    "pe_ttm": "P/E TTM", "pb": "P/B", "ev": "Стоимость предприятия", "ev_ebitda": "EV/EBITDA",
}
STATUSES = {
    "ok": "Рассчитано", "missing_inputs": "Недостаточно входных данных",
    "zero_denominator": "Нулевой знаменатель", "nonpositive_denominator": "Прибыль до налога отрицательная",
    "not_applicable_period": "Для этой частоты не применяется",
    "incomplete_ttm_window": "Нет четырёх последовательных кварталов",
    "nonfinite_result": "Результат вне числового диапазона", "not_evaluable": "Недостаточно данных для оценки",
    "missing_value": "Поле отсутствует", "invalid_number": "Некорректное число",
    "nonfinite_number": "Неконечное число", "no_close_at_cutoff": "Нет закрытия до даты отсечки",
    "missing_prior_year_period": "Нет сопоставимого периода прошлого года",
    "missing_prior_ytd": "Нет предыдущего накопительного периода того же финансового года",
    "ambiguous_period": "Несколько отчётных дат в одном месяце",
    "insufficient_contiguous_history": "Короткий или прерванный ряд",
    "nonfinite_prediction": "Прогноз вне числового диапазона",
    "pass": "Критерий выполнен", "fail": "Критерий не выполнен",
    "missing_fy_period": "Нет завершённого финансового года к дате квартала",
    "missing_fy_profit": "В годовой отчётности отсутствует чистая прибыль",
    "zero_fy_profit": "Чистая прибыль за финансовый год равна нулю",
    "missing_quarter_cap": "Недоступна капитализация на конец квартала",
    "nonpositive_close": "Цена закрытия должна быть положительной",
}
PERCENTAGES = {
    "revenue_yoy", "gross_margin", "operating_margin", "net_margin", "ebitda_m", "adjusted_ebitda_m",
    "effective_tax_rate", "roe", "roa", "return_1m", "return_3m", "return_12m", "forecast_growth",
}
SCREEN_FIELDS = ("revenue", "ebitda", "ebitda_m", "pe_fy", "pe_ttm", "ev_ebitda", "de", "cr", "effective_tax_rate")
TAX_FIELDS = ("profit_before_tax", "income_tax_expense", "effective_tax_rate")

CSS = """
:root{color-scheme:light;font:15px/1.5 Inter,system-ui,sans-serif;color:#1f2937;background:#f4f6fa}
body{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1,h2,h3,h4{line-height:1.2;color:#142342}h1{font-size:2.5rem;margin:0}h2{font-size:1.5rem}
a{color:#174aa1;text-underline-offset:.18em}a:focus-visible,summary:focus-visible{outline:2px solid #174aa1;outline-offset:3px}
section,header,article{background:white;border:1px solid #dae0ea;border-radius:12px;padding:20px;margin:0 0 16px}
.summary{color:#4b5870}.warning{border-left:4px solid #b06918;padding:8px 12px;background:#fff5df}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;border-bottom:1px solid #e3e8ef;text-align:left;vertical-align:top}
th{background:#f4f7fc;white-space:nowrap}td:nth-child(2){min-width:70px}td{overflow-wrap:anywhere}
details{margin:.35em 0}details.period{border-top:1px solid #dae0ea;padding:10px 0}
summary{cursor:pointer;color:#174aa1;font-weight:600}.inputs{padding-left:1.25rem;font-size:.9rem}
nav ul{display:flex;flex-wrap:wrap;gap:8px 24px;padding-left:18px}.status{color:#5d4c32}
@media(prefers-color-scheme:dark){
  :root{color-scheme:dark;color:#edf1fc;background:#101725}
  h1,h2,h3,h4{color:#eef2fd}
  a,summary{color:#aab6ff}a:focus-visible,summary:focus-visible{outline-color:#aab6ff}
  section,header,article{background:#182235;border-color:#34415a}
  .summary{color:#acb9d1}.warning{border-color:#ffc783;background:#443422}.status{color:#f2cea8}
  th{background:#202c43}th,td{border-color:#34415a}details.period{border-color:#34415a}
}
"""


def html(value):
    return escape(str(value), quote=True)


def display(value, field=""):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2%}" if field in PERCENTAGES else f"{value:.8g}"
    return html(value)


def table(headers, rows):
    columns = "".join(f'<th scope="col">{html(header)}</th>' for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{columns}</tr></thead><tbody>{body}</tbody></table></div>'


def details_inputs(inputs):
    items = []
    for key, value in inputs.items():
        name = html(key.split(". ", 1)[-1])
        if "inputs" in value:
            info = f'{html(value["formula"])} = {display(value["value"])}'
            if value["status"] != "ok":
                info += f' · {html(STATUSES.get(value["status"], value["status"]))}'
            items.append(f'<li>{name} · {info}{details_inputs(value["inputs"])}</li>')
            continue
        parts = [f'{name} = {display(value["value"])}']
        if value.get("source_field") and value["source_field"] != key.split(". ", 1)[-1]:
            parts.append(f'поле источника {html(value["source_field"])}')
        if value["raw"] is not None:
            parts.append(f'исходное значение {html(value["raw"])}')
        if value.get("scale") not in (None, 1):
            parts.append(f'масштаб ×{display(value["scale"])}')
        parts.extend(html(value[name]) for name in ("date", "source_ref") if value[name])
        if value.get("source_period_end") and value["source_period_end"] != value["date"]:
            parts.append(f'дата периода в источнике {html(value["source_period_end"])}')
        if value["row"]:
            parts.append(f'строка {value["row"]}')
        if value["retrieved_at"]:
            parts.append(f'получено {html(value["retrieved_at"])}')
        if value["reason"]:
            parts.append(html(STATUSES.get(value["reason"], value["reason"])))
        items.append("<li>" + " · ".join(parts) + "</li>")
    return '<ul class="inputs">' + "".join(items) + "</ul>" if items else ""


def formula_table(period, fields):
    rows = []
    for name in fields:
        calc = period.calculations[name]
        status = "" if calc.status == "ok" else html(STATUSES.get(calc.status, calc.status))
        inputs = details_inputs(calc.inputs)
        formula = html(calc.formula)
        if inputs:
            formula = f"<details><summary>{formula}</summary>{inputs}</details>"
        rows.append([html(LABELS[name]), display(calc.value, name), formula, status])
    return table(("Показатель", "Значение", "Формула и входные данные", "Статус"), rows)


def scenario_table(row):
    statuses = json.loads(row["statuses"])
    formulas = json.loads(row["formulas"])
    return table(("Показатель", "База", "Сценарий", "Формула", "Статус"), (
        [html(LABELS[field]), display(row[f"base_{field}"], field), display(row[f"scenario_{field}"], field),
         html(formulas[field]),
         html(STATUSES.get(statuses[field]["status"], statuses[field]["status"]))]
        for field in SCENARIO_FIELDS
    ))


def pe_summary(comparison):
    ttm_periods = ", ".join(json.loads(comparison["ttm_periods"]))
    status = STATUSES.get(comparison["reason"], comparison["reason"])
    attr = ""
    if comparison["alert"]:
        status = "Сигнал расхождения"
        attr = ' class="warning"'
    elif not status:
        status = "В пределах порога"
    threshold = f'{comparison["threshold"]:.0%}'
    return (
        f'<h4>Сравнение P/E при общей капитализации</h4>'
        f'<p{attr}>{html(status)}. Расхождение {display(comparison["deviation"], "forecast_growth")}, '
        f'порог {threshold}. Квартал {display(comparison["date"])}, '
        f'цена на {display(comparison["quote_date"])}, капитализация {display(comparison["market_cap"])}.</p>'
        + table(("Методика", "P/E", "Периоды чистой прибыли"), (
            ("P/E FY", display(comparison["pe_fy_at_quarter"]), display(comparison["fy_period_end"])),
            ("P/E TTM", display(comparison["pe_ttm_at_quarter"]), display(ttm_periods or None)),
        ))
    )


def make_report(results, screened, scenarios, forecasts, stats, settings):
    by_instrument = defaultdict(list)
    for period in results:
        by_instrument[period.statement.instrument].append(period)
    ids = {
        (item.source, item.ticker, item.currency): f"issuer-{index}"
        for index, item in enumerate(by_instrument, start=1)
    }
    screen_rows = []
    for row in screened:
        anchor = ids[row["source"], row["ticker"], row["currency"]]
        screen_rows.append([
            f'<a href="#{anchor}">{html(row["ticker"])}</a>',
            html(row["source"].upper()), html(row["currency"]),
            f'{html(row["period_type"])} · {html(row["period_end"])}',
            *(display(row[field], field) for field in SCREEN_FIELDS),
        ])
    summary = stats["ingestion"]
    filters = ", ".join(f"{LABELS.get(rule.field, 'Эффективная ставка налога')} {rule.operator} "
                        f"{rule.value:g}" for rule in settings.filters)
    parameters = html(filters or "Все инструменты с отчётностью")
    table_html = table(("Тикер", "Площадка", "Валюта", "Период", *(LABELS[field] for field in SCREEN_FIELDS)),
                       screen_rows) if screen_rows else "<p>По заданным условиям совпадений нет.</p>"

    nav = []
    cards = []

    def key(row):
        return tuple(row[field] for field in PERIOD_COLUMNS)

    scenarios_by_period = {key(row): row for row in scenarios}
    latest_forecasts = {(row["source"], row["ticker"], row["currency"], row["period_type"]): row
                        for row in forecasts if row["kind"] == "next"}
    for instrument, periods in by_instrument.items():
        identity = (instrument.source, instrument.ticker, instrument.currency)
        anchor = ids[identity]
        title = html(f"{instrument.ticker} · {instrument.source.upper()} · {instrument.currency}")
        nav.append(f'<li><a href="#{anchor}">{title}</a></li>')
        blocks = [f'<article id="{anchor}"><h3>{title}</h3>']
        comparison = next((period.pe_comparison for period in periods if period.pe_comparison), None)
        if comparison:
            blocks.append(pe_summary(comparison))
        latest = [latest_forecasts[(*identity, frequency)] for frequency in ("Q", "FY")
                  if (*identity, frequency) in latest_forecasts]
        blocks.append("<h4>Прогноз выручки</h4>" + table(
            ("Период", "Целевая дата", "Прогноз", "Метод", "Naive", "Дата Naive", "Рост", "Статус"),
            ([html(row["period_type"]), html(row["target_period_end"]), display(row["forecast"]),
              display(MODEL_LABELS.get(row["model"])), display(row["naive"]), display(row["naive_period_end"]),
              display(row["forecast_growth"], "forecast_growth"),
              html(STATUSES.get(row["reason"] or row["status"], row["reason"] or row["status"]))] for row in latest),
        ))
        for index, period in enumerate(reversed(periods)):
            current = period.statement
            opened = " open" if index == 0 else ""
            blocks.append(
                f'<details class="period"{opened}><summary>{html(current.period_type)} · {current.period_end}</summary>'
                + formula_table(period, (field for field in period.calculations if field not in TAX_FIELDS))
                + "<h4>Налоги</h4>" + formula_table(period, TAX_FIELDS)
                + "<h4>Сценарий</h4>" + scenario_table(scenarios_by_period[key(current.columns())])
                + "</details>"
            )
        cards.append("".join(blocks) + '<p><a href="#top">К скринеру</a></p></article>')

    forecast = stats["forecast"]
    score_line = (
        f'MAPE модели {display(forecast["mape_model"], "forecast_growth")}, '
        f'Naive {display(forecast["mape_naive"], "forecast_growth")}. '
        f'Допустимых точек {forecast["eligible_points"]}, исключено {forecast["excluded_points"]}. '
        f'{html(STATUSES[forecast["status"]])}.'
    )
    frequency_rows = []
    for kind, label in (("Q", "Квартал"), ("FY", "Год")):
        result = forecast["by_period_type"][kind]
        frequency_rows.append((
            label, display(result["mape_model"], "forecast_growth"),
            display(result["mape_linear_reference"], "forecast_growth"), display(result["mape_naive"], "forecast_growth"),
            html(result["eligible_points"]), html(STATUSES[result["status"]]),
        ))
    frequency_scores = table(("Период", "MAPE модели", "Базовый тренд", "MAPE Naive", "Точек", "Статус"), frequency_rows)
    if stats["synthetic_demo"]:
        input_type = "Учебный синтетический набор"
    elif stats["financials_mode"] == "yahoo":
        input_type = "Отчётность Yahoo Finance"
    elif stats["financials_mode"] == "mixed":
        input_type = "Отчётность из CSV и Yahoo Finance"
    else:
        input_type = "Отчёт по входным данным"
    changes = settings.scenario
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Опекс · финансовый анализ</title><style>{CSS}</style></head>
<body id="top"><header><h1>Опекс</h1><p class="summary">{input_type} · {html(stats["started_at"])}</p>
<a href="parse_log.csv">Журнал обработки</a> · <a href="issuer_metrics.csv">Показатели CSV</a> ·
<a href="formula_trace.csv">След расчёта CSV</a></header>
<main><section><h2>Скринер</h2><p>{parameters}. Бумаг в списке {summary["requested"]},
обработано {summary["processed"]}, прошли отбор {stats["screener"]["passed_instruments"]}.</p>{table_html}</section>
<section><h2>Проверка прогноза</h2><p>{score_line}</p>{frequency_scores}
<a href="forecast_next_period.csv">Прогноз и контрольные точки CSV</a></section>
<section><h2>Параметры сценария</h2><p>Выручка {changes['r']:+.2%}, маржа EBITDA {changes['m'] * 100:+g} п. п.,
долг {display(changes['delta_d'])} в валюте отчётности.</p></section>
<nav aria-label="Карточки эмитентов"><ul>{''.join(nav)}</ul></nav>{''.join(cards)}</main></body></html>
"""
