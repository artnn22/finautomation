"""Command-line entry point and orchestration for one reproducible run."""

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml

from .config import load_settings
from .forecast import FORECAST_COLUMNS, forecast_revenue
from .inputs import (
    INSTRUMENT_COLUMNS, LOG_COLUMNS, PERIOD_COLUMNS, QUOTE_COLUMNS, log_entry, parse_tickers,
    read_records, write_csv,
)
from .metrics import (
    ALERT_COLUMNS, METRIC_COLUMNS, SCENARIO_COLUMNS, TRACE_COLUMNS, analyze, json_cell, scenario,
)
from .quotes import fetch_online
from .report import make_report

SCREEN_COLUMNS = (*PERIOD_COLUMNS, *METRIC_COLUMNS, "filters")


def arguments(argv):
    parser = argparse.ArgumentParser(prog="finautomation", description="Опекс · финансовый анализ эмитентов")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run", help="Рассчитать показатели и собрать локальный HTML-отчёт")
    command.add_argument("--tickers", required=True, type=Path, help="Список бумаг CSV")
    command.add_argument("--financials", required=True, type=Path, help="Подготовленная отчётность CSV")
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--quotes", type=Path, help="Подготовленные котировки CSV")
    source.add_argument("--online", action="store_true", help="Загрузить дневные цены с указанных площадок")
    command.add_argument("--config", required=True, type=Path, help="Параметры запуска YAML")
    command.add_argument("--out", required=True, type=Path, help="Каталог для отдельных папок запусков")
    command.add_argument("--synthetic-demo", action="store_true", help="Пометить учебные данные как синтетические")
    return parser.parse_args(argv)


def screen(results, settings):
    latest = {}
    for item in results:
        statement = item.statement
        key = (statement.instrument, statement.period_type)
        if key not in latest or latest[key].statement.period_end < statement.period_end:
            latest[key] = item
    filters = [asdict(rule) for rule in settings.filters]
    rows = [item.columns() for item in latest.values()]
    screened = [dict(row, filters=json_cell(filters)) for row in rows
                if all(rule.matches(row) for rule in settings.filters)]
    passed = len({tuple(row[key] for key in INSTRUMENT_COLUMNS) for row in screened})
    return screened, {
        "filters": filters, "candidate_rows": len(rows), "excluded_rows": len(rows) - len(screened),
        "passed_rows": len(screened), "passed_instruments": passed,
    }


def ingestion_summary(requested, results, log):
    grouped = defaultdict(list)
    for item in results:
        instrument = item.statement.instrument
        grouped[instrument.source, instrument.ticker].append(item)
    matched = 0
    for (source, ticker), row in requested.items():
        items = grouped.get((source, ticker), [])
        priced = [item for item in items if item.calculations["price"].value is not None]
        if priced:
            matched += 1
            status, reason = "ok", f"Периодов с котировками: {len(priced)} из {len(items)}"
        elif items:
            status, reason = "missing_quotes", "Нет цены на отчётную дату или ранее в той же валюте и площадке"
        else:
            status, reason = "missing_financials", "Нет пригодных строк отчётности"
        log.append(log_entry("ingestion", status, reason, row=row, source=source, ticker=ticker))
    total = len(requested)
    coverage = matched / total if total else None
    return {
        "requested": total, "processed": len(grouped), "with_quotes": matched, "coverage": coverage,
        "threshold": 0.70, "status": ("pass" if coverage >= 0.70 else "fail") if total else "not_evaluable",
    }


def snapshot(args, destination):
    folder = destination / "inputs"
    folder.mkdir()
    files = {
        "tickers": (args.tickers, folder / "tickers.csv"),
        "financials": (args.financials, folder / "financials.csv"),
        "config": (args.config, folder / "config.yaml"),
    }
    if args.quotes is not None:
        files["quotes"] = (args.quotes, folder / "quotes.csv")
    for source, target in files.values():
        shutil.copyfile(source, target)
    return {key: target for key, (_, target) in files.items()}


def run_pipeline(args, destination, started):
    clock = time.monotonic()
    files = snapshot(args, destination)
    log = []
    settings = load_settings(files["config"])
    requested = parse_tickers(files["tickers"], log)
    statements = read_records(files["financials"], requested, log, statements=True)
    if args.online:
        files["quotes"] = destination / "inputs" / "quotes.csv"
        fetched = fetch_online(statements, settings, log)
        write_csv(files["quotes"], QUOTE_COLUMNS, (quote.columns() for quote in fetched))
    quotes = read_records(files["quotes"], requested, log, statements=False)
    results, alerts = analyze(statements, quotes)
    forecast, forecast_stats = forecast_revenue(statements, settings.min_train_periods)
    screened, screen_stats = screen(results, settings)
    scenarios = [scenario(item, settings.scenario) for item in results]
    ingestion = ingestion_summary(requested, results, log)
    stats = {
        "version": version("finautomation"), "started_at": started.isoformat(timespec="seconds"),
        "quote_mode": "online" if args.online else "csv", "synthetic_demo": args.synthetic_demo,
        "ingestion": ingestion, "screener": screen_stats, "forecast": forecast_stats,
        "elapsed_seconds": round(time.monotonic() - clock, 3),
    }
    log.append(log_entry("run", "complete", f'Обработка данных: {stats["elapsed_seconds"]:.3f} с'))
    log.sort(key=lambda row: (str(row["stage"]), row["row"] or 0))
    outputs = (
        ("parse_log.csv", LOG_COLUMNS, log),
        ("issuer_metrics.csv", (*PERIOD_COLUMNS, *METRIC_COLUMNS), (item.columns() for item in results)),
        ("formula_trace.csv", (*PERIOD_COLUMNS, *TRACE_COLUMNS), (row for item in results for row in item.traces())),
        ("screener.csv", SCREEN_COLUMNS, screened),
        ("alerts.csv", (*INSTRUMENT_COLUMNS, *ALERT_COLUMNS), alerts),
        ("scenario.csv", (*PERIOD_COLUMNS, *SCENARIO_COLUMNS), scenarios),
        ("forecast_next_period.csv", (*INSTRUMENT_COLUMNS, "period_type", *FORECAST_COLUMNS), forecast),
    )
    for name, columns, rows in outputs:
        write_csv(destination / name, columns, rows)
    (destination / "metrics.json").write_text(
        json.dumps(stats, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / "report.html").write_text(
        make_report(results, screened, scenarios, alerts, forecast, stats, settings), encoding="utf-8"
    )
    return ingestion


def main(argv=None):
    args = arguments(argv)
    started = datetime.now(ZoneInfo("Europe/Moscow"))
    destination = args.out / f"{started:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".opex-", dir=args.out) as temporary:
            staging = Path(temporary)
            summary = run_pipeline(args, staging, started)
            staging.rename(destination)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Ошибка запуска: {error}", file=sys.stderr)
        return 2
    print(f'Обработано {summary["processed"]} из {summary["requested"]}, с котировками {summary["with_quotes"]}')
    print(destination.resolve())
    return 0 if summary["processed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
