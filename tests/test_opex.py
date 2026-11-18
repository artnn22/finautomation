import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from finautomation.config import load_settings
from finautomation.financials import fetch_financials, read_period_dates
from finautomation.forecast import forecast_revenue
from finautomation.inputs import FINANCIAL_FIELDS, Instrument, Observation, Quote, Statement, parse_tickers, read_records
from finautomation.main import main
from finautomation.metrics import analyze, scenario
from finautomation.quotes import fetch_online, moex_quotes, yahoo_quotes


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "demo"
VERIFICATION = ROOT / "examples" / "verification"
FIXTURES = Path(__file__).with_name("fixtures")
INSTRUMENT = Instrument("moex", "CHECK", "RUB")


def statement(day, kind="Q", instrument=INSTRUMENT, **values):
    day = date.fromisoformat(day)
    fields = {field: Observation(field, values.get(field), values.get(field), "report.csv", "", 2, day)
              for field in FINANCIAL_FIELDS}
    return Statement(instrument, kind, day, fields)


def quote(day, close, instrument=INSTRUMENT):
    day = date.fromisoformat(day)
    observation = Observation("close", close, close, "quotes.csv", "", 2, day)
    return Quote(instrument, day, observation)


def columns(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter=";"))


class CalculationsTest(unittest.TestCase):
    def test_control_values_cover_all_metrics_for_financial_year(self):
        previous = statement("2023-12-31", "FY", revenue=400)
        current = statement(
            "2024-12-31", "FY", revenue=500, cogs=300, ebit=100, da=20, net_income=70,
            profit_before_tax=90, income_tax_expense=18, ebitda_adjustments=5, interest_expense=10,
            operating_cf=115, capex=15, equity=200, assets=1000, current_assets=120,
            current_liabilities=60, inventory=30, cash=75, debt=175, shares=10,
        )
        prices = [quote(day, price) for day, price in (
            ("2023-12-29", 5), ("2024-09-30", 10), ("2024-11-29", 16), ("2024-12-30", 20),
            ("2025-01-02", 999),
        )]
        periods, _ = analyze([previous, current], prices)
        result = periods[-1]
        expected = {
            "revenue": 500, "revenue_yoy": 0.25, "gross_margin": 0.4, "operating_margin": 0.2,
            "net_margin": 0.14, "ebit": 100, "ebitda": 120, "ebitda_m": 0.24,
            "adjusted_ebitda": 125, "adjusted_ebitda_m": 0.25, "profit_before_tax": 90,
            "income_tax_expense": 18, "effective_tax_rate": 0.2, "roe": 0.35, "roa": 0.07,
            "cr": 2, "qr": 1.5, "de": 0.875, "net_debt": 100, "interest_coverage": 10,
            "operating_cf": 115, "fcf": 100, "cash_conversion": 115 / 120, "price": 20,
            "return_1m": 0.25, "return_3m": 1, "return_12m": 3,
            "market_cap": 200, "pe_fy": 200 / 70, "pb": 1, "ev": 300, "ev_ebitda": 2.5,
        }
        self.assertEqual(set(result.calculations), set(expected) | {"pe_ttm"})
        for metric, value in expected.items():
            with self.subTest(metric=metric):
                self.assertAlmostEqual(result.calculations[metric].value, value)
                self.assertEqual(result.calculations[metric].status, "ok")
        self.assertEqual(result.calculations["pe_ttm"].status, "not_applicable_period")

    def test_ebitda_tax_provenance_and_scenario(self):
        entries = [statement("2024-12-31", "FY", revenue=500, ebit=100, da=20, ebitda_adjustments=0,
                             net_income=70, profit_before_tax=90, income_tax_expense=18,
                             cash=100, debt=200, operating_cf=115, capex=15, shares=10, equity=0)]
        results, _ = analyze(entries, [quote("2024-12-30", 20), quote("2025-01-02", 99)])
        period = results[0]
        row = period.columns()
        self.assertEqual(row["ebitda"], 120)
        self.assertEqual(row["adjusted_ebitda"], 120)
        self.assertEqual(row["effective_tax_rate"], 0.2)
        self.assertAlmostEqual(row["pe_fy"], 200 / 70)
        self.assertEqual(row["price"], 20)
        self.assertEqual(period.calculations["roe"].status, "zero_denominator")
        trace = period.calculations["price"].trace()
        self.assertEqual(json.loads(trace["inputs"])["1. close"]["date"], "2024-12-30")
        adjusted = scenario(period, {"r": 0.1, "m": -0.02, "delta_d": 10})
        self.assertAlmostEqual(adjusted["scenario_ebitda"], 121)
        self.assertAlmostEqual(adjusted["scenario_net_debt"], 110)
        self.assertAlmostEqual(adjusted["scenario_fcf"], 101)
        self.assertAlmostEqual(adjusted["scenario_ev_ebitda"], 310 / 121)
        self.assertEqual(period.columns(), row)

    def test_missing_fields_never_substitute_other_currency_or_source(self):
        report = statement("2024-12-31", revenue=0, ebit=10, da=5, profit_before_tax=-3,
                           income_tax_expense=1)
        wrong_price = quote("2024-12-31", 10, Instrument("yahoo", "CHECK", "RUB"))
        wrong_currency = quote("2024-12-31", 11, Instrument("moex", "CHECK", "USD"))
        results, _ = analyze([report], [wrong_price, wrong_currency])
        row = results[0].calculations
        self.assertIsNone(row["price"].value)
        self.assertIsNone(row["adjusted_ebitda"].value)
        self.assertEqual(row["adjusted_ebitda"].missing, ("ebitda_adjustments",))
        self.assertEqual(row["ebitda_m"].status, "zero_denominator")
        self.assertEqual(row["effective_tax_rate"].status, "nonpositive_denominator")

    def test_price_returns_use_period_end_as_calendar_anchor(self):
        report = statement("2024-08-31")
        quotes = [quote("2024-07-29", 20), quote("2024-07-31", 25),
                  quote("2024-08-29", 30), quote("2024-09-02", 99)]
        periods, _ = analyze([report], quotes)
        row = periods[0].columns()
        self.assertEqual(row["price"], 30)
        self.assertAlmostEqual(row["return_1m"], 0.20)
        self.assertIsNone(row["return_3m"])

    def test_pe_ttm_alert_threshold_use_common_quarter_cap_and_require_full_window(self):
        quarters = [statement(day, net_income=30, shares=10)
                    for day in ("2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31")]
        quotes = [quote("2023-12-29", 10), quote("2024-12-30", 30)]
        old = statement("2023-12-31", "FY", net_income=100, shares=10)
        rows, alerts = analyze([*quarters, old], quotes)
        self.assertEqual(rows[0].columns()["pe_fy"], 1)
        self.assertEqual(rows[-1].columns()["pe_ttm"], 2.5)
        self.assertEqual(alerts, [])  # |120 - 100| / 100 == 20%
        self.assertEqual(rows[-1].pe_comparison["deviation"], 0.2)
        self.assertFalse(rows[-1].pe_comparison["alert"])
        old = statement("2023-12-31", "FY", net_income=99, shares=10)
        _, alerts = analyze([*quarters, old], quotes)
        self.assertEqual(len(alerts), 1)
        self.assertAlmostEqual(alerts[0]["pe_fy_at_quarter"], 300 / 99)
        self.assertAlmostEqual(alerts[0]["pe_ttm_at_quarter"], 2.5)
        rows, alerts = analyze([quarters[0], *quarters[2:], old], quotes)
        self.assertEqual(rows[-1].calculations["pe_ttm"].status, "incomplete_ttm_window")
        self.assertEqual(alerts, [])


class ForecastTest(unittest.TestCase):
    def test_quarterly_seasonality_is_selected_using_only_earlier_points(self):
        seasons = (250, 60, 100, 30)
        dates = ("03-31", "06-30", "09-30", "12-31")
        series = [statement(f"{2020 + index // 4}-{dates[index % 4]}",
                            revenue=100 + 10 * index + seasons[index % 4]) for index in range(13)]
        rows, stats = forecast_revenue(series, 4)
        held_out = rows[-2]
        self.assertEqual(held_out["model"], "seasonal_trend")
        self.assertAlmostEqual(held_out["forecast"], held_out["y_true"])
        self.assertNotAlmostEqual(held_out["linear_reference"], held_out["y_true"])
        changed = [*series[:-1], statement("2023-03-31", revenue=999)]
        changed_rows, _ = forecast_revenue(changed, 4)
        self.assertEqual(changed_rows[-2]["model"], held_out["model"])
        self.assertEqual(changed_rows[-2]["forecast"], held_out["forecast"])
        self.assertLess(stats["mape_model"], stats["mape_linear_reference"])

    def test_microsoft_history_compares_three_methods_on_the_same_quarters(self):
        path = ROOT / "examples" / "forecast" / "msft_quarterly.csv"
        with path.open(encoding="utf-8", newline="") as source:
            series = [statement(row["period_end"], revenue=int(row["revenue_million_usd"]))
                      for row in csv.DictReader(source)]
        _, stats = forecast_revenue(series, 4)
        self.assertEqual(stats["eligible_points"], 24)
        self.assertLess(stats["mape_model"], stats["mape_linear_reference"])
        self.assertLess(stats["mape_model"], stats["mape_naive"])

    def test_target_is_excluded_from_its_own_fit(self):
        dates = ("2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31")
        series = [statement(day, revenue=value) for day, value in zip(dates, (10, 20, 30, 40, 500))]
        rows, stats = forecast_revenue(series, 4)
        self.assertAlmostEqual(rows[-2]["forecast"], 50)
        self.assertEqual(rows[-2]["naive"], 40)
        self.assertEqual(rows[-2]["y_true"], 500)
        self.assertEqual(stats["eligible_points"], 1)
        self.assertEqual(stats["by_period_type"]["Q"]["eligible_points"], 1)
        self.assertEqual(stats["by_period_type"]["FY"]["status"], "not_evaluable")
        self.assertEqual(rows[-1]["target_period_end"], date(2025, 3, 31))
        self.assertIsNone(rows[-1]["y_true"])
        self.assertTrue(rows[-1]["mape_excluded"])

    def test_gap_breaks_history_and_nonpositive_actual_is_excluded(self):
        series = [statement(day, revenue=value) for day, value in (
            ("2024-03-31", 10), ("2024-06-30", 20), ("2024-09-30", 0), ("2025-03-31", 40)
        )]
        rows, stats = forecast_revenue(series, 2)
        self.assertAlmostEqual(rows[2]["forecast"], 30)
        self.assertTrue(rows[2]["mape_excluded"])
        self.assertEqual(rows[2]["mape_exclusion_reason"], "nonpositive_actual")
        self.assertIsNone(rows[3]["forecast"])
        self.assertEqual(rows[-1]["status"], "not_evaluable")
        self.assertEqual(stats["eligible_points"], 0)


class QuotesTest(unittest.TestCase):
    def test_moex_shape_currency_and_pagination(self):
        response = json.loads((FIXTURES / "moex_sber.json").read_text())
        page = response["history"]
        first = {"history": {**page, "data": page["data"][:1]},
                 "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 2, 1]]}}
        second = {"history": {**page, "data": page["data"][1:]},
                  "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[1, 2, 1]]}}
        with patch("finautomation.quotes.get_json", side_effect=[first, second]) as fetch:
            values = moex_quotes("SBER", date(2024, 12, 24), date(2024, 12, 30), board="TQBR", timeout=10)
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0].instrument.currency, "RUB")
        self.assertEqual(values[0].close.value, 264.34)
        self.assertTrue(fetch.call_args_list[-1].args[0].endswith("&start=1"))

    def test_yahoo_shape_exchange_date_close_and_currency(self):
        response = json.loads((FIXTURES / "yahoo_aapl.json").read_text())
        record = response["chart"]["result"][0]
        day = datetime.fromtimestamp(record["timestamp"][0], UTC).date()
        with patch("finautomation.quotes.get_json", return_value=response) as fetch:
            values = yahoo_quotes("AAPL", day, day, timeout=10)
        self.assertIn("finance.yahoo.com/v8/finance/chart/AAPL?", fetch.call_args.args[0])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].instrument.currency, "USD")
        self.assertEqual(values[0].close.value, record["indicators"]["quote"][0]["close"][0])

    def test_source_error_and_currency_mismatch_are_logged(self):
        settings = load_settings(EXAMPLES / "config.yaml")
        report = statement("2024-12-31")
        log = []
        with patch("finautomation.quotes.get_json", side_effect=URLError("offline")):
            self.assertEqual(fetch_online([report], settings, log), [])
        self.assertIn("source_unavailable", {row["status"] for row in log})

        log = []
        mismatched = quote("2024-12-30", 10, Instrument("moex", "CHECK", "USD"))
        with patch("finautomation.quotes.moex_quotes", return_value=[mismatched]):
            self.assertEqual(fetch_online([report], settings, log), [])
        self.assertIn("currency_mismatch", {row["status"] for row in log})


class InputsTest(unittest.TestCase):
    def test_malformed_csv_row_preserves_following_rows_and_multiline_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickers, financials = root / "tickers.csv", root / "financials.csv"
            tickers.write_text('ticker,market\n"bad,moex\nONE,moex\n', encoding="utf-8")
            financials.write_text(
                'source,ticker,currency,period_type,period_end,revenue,source_ref\n'
                'moex,ONE,RUB,FY,2023-12-31,10,"Annual report\npage 7"\n'
                'moex,ONE,RUB,FY,2024-12-31,20,Report\n',
                encoding="utf-8",
            )
            log = []
            requested = parse_tickers(tickers, log)
            statements = read_records(financials, requested, log, statements=True)
            self.assertEqual(requested, {("moex", "ONE"): 3})
            self.assertEqual([(row["row"], row["status"]) for row in log], [(2, "invalid_row")])
            self.assertEqual(statements[0].fields["revenue"].source_ref, "Annual report\npage 7")
            self.assertEqual(statements[1].fields["revenue"].row, 4)


class OnlineFinancialsTest(unittest.TestCase):
    @staticmethod
    def series(name, *amounts, ticker="CHECK"):
        records = [
            {"asOfDate": day, "periodType": duration, "currencyCode": currency,
             "reportedValue": {"raw": raw, "fmt": "formatted"}}
            for day, duration, currency, raw in amounts
        ]
        return {"meta": {"type": [name], "symbol": [ticker]}, name: records}

    def test_downloaded_statements_feed_pipeline_and_use_exact_price_cutoff(self):
        day = "2025-09-30"
        statements = [
            self.series("quarterlyTotalRevenue", (day, "3M", "USD", 500),
                        (day, "12M", "USD", 999)),
            self.series("quarterlyOperatingIncome", (day, "3M", "USD", 100)),
            self.series("quarterlyDepreciationAndAmortization", (day, "3M", "USD", 20)),
            self.series("quarterlyTaxProvision", (day, "3M", "USD", 18)),
            self.series("quarterlyPretaxIncome", (day, "3M", "USD", 90)),
            self.series("quarterlyOrdinarySharesNumber", (day, "3M", "USD", 10)),
            self.series("annualTotalRevenue", (day, "12M", "USD", 2200)),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tickers.csv").write_text("ticker,market\nCHECK,yahoo\nSBER,moex\n")
            (root / "quotes.csv").write_text(
                "source,ticker,currency,date,close\n"
                "yahoo,CHECK,USD,2025-09-26,20\nyahoo,CHECK,USD,2025-09-30,99\n"
            )
            (root / "dates.csv").write_text(
                "ticker,currency,period_type,source_period_end,period_end\nCHECK,USD,Q,2025-09-30,2025-09-27\n"
            )
            with patch("finautomation.financials.get_json",
                       return_value={"timeseries": {"result": statements, "error": None}}) as fetch:
                result = main([
                    "run", "--tickers", str(root / "tickers.csv"), "--online-financials",
                    "--period-dates", str(root / "dates.csv"), "--quotes", str(root / "quotes.csv"),
                    "--config", str(EXAMPLES / "config.yaml"), "--out", str(root / "runs"),
                ])
            self.assertEqual(result, 0)
            fetch.assert_called_once()
            (output,) = (root / "runs").iterdir()
            quarterly, annual = columns(output / "issuer_metrics.csv")
            self.assertEqual((quarterly["period_type"], quarterly["period_end"], quarterly["price"]),
                             ("Q", "2025-09-27", "20.0"))
            self.assertEqual(float(quarterly["ebitda"]), 120)
            self.assertEqual(float(quarterly["effective_tax_rate"]), 0.2)
            self.assertEqual(float(quarterly["market_cap"]), 200)
            self.assertEqual(float(annual["revenue"]), 2200)
            traces = columns(output / "formula_trace.csv")
            revenue = next(row for row in traces if row["period_type"] == "Q" and row["metric"] == "revenue")
            raw = json.loads(revenue["inputs"])["1. revenue"]
            self.assertEqual((raw["source_field"], raw["source_period_end"], raw["raw"]),
                             ("quarterlyTotalRevenue", day, "500"))
            self.assertIn("requires_csv", {row["status"] for row in columns(output / "parse_log.csv")})
            self.assertEqual(json.loads((output / "metrics.json").read_text())["financials_mode"], "yahoo")
            self.assertTrue((output / "inputs" / "period_dates.csv").is_file())

    def test_conflicts_wrong_symbol_and_missing_fields_do_not_fabricate_values(self):
        requested = {("yahoo", "OTHER"): 2, ("yahoo", "CHECK"): 3}
        payloads = [
            {"timeseries": {"error": None, "result": [
                self.series("annualTotalRevenue", ("2024-12-31", "12M", "USD", 10)),
            ]}},
            {"timeseries": {"error": None, "result": [
                self.series("annualTotalRevenue", ("2024-12-31", "12M", "USD", 50),
                            ("2024-12-31", "12M", "USD", 55), ("2024-12-31", "12M", "CAD", 65),
                            ("2025-12-31", "12M", "USD", 70), ("2026-12-31", "12M", "USD", 80)),
                self.series("annualTaxProvision", ("2025-12-31", "12M", "USD", None)),
            ]}},
        ]
        log = []
        with patch("finautomation.financials.get_json", side_effect=payloads):
            rows = fetch_financials(requested, load_settings(EXAMPLES / "config.yaml"), date(2026, 6, 30), {}, log)
        self.assertEqual([(row["ticker"], row["currency"], row["revenue"]) for row in rows],
                         [("CHECK", "CAD", 65), ("CHECK", "USD", 70)])
        self.assertNotIn("income_tax_expense", rows[1])
        self.assertEqual({entry["status"] for entry in log}, {"conflict", "ok", "source_unavailable"})

    def test_conflicting_report_dates_block_period(self):
        requested = {("yahoo", "CHECK"): 2}
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "dates.csv"
            file.write_text(
                "ticker,currency,period_type,source_period_end,period_end\n"
                "CHECK,USD,Q,2025-09-30,2025-09-27\nCHECK,USD,Q,2025-09-30,2025-09-28\n"
            )
            log = []
            dates = read_period_dates(file, requested, log)
            payload = {"timeseries": {"error": None, "result": [
                self.series("quarterlyTotalRevenue", ("2025-09-30", "3M", "USD", 500)),
            ]}}
            with patch("finautomation.financials.get_json", return_value=payload):
                rows = fetch_financials(requested, load_settings(EXAMPLES / "config.yaml"), date(2026, 6, 30),
                                        dates, log)
            self.assertEqual(rows, [])
            self.assertIn("conflict", {entry["status"] for entry in log})

    def test_local_reports_take_precedence_and_mixed_snapshots_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickers = root / "tickers.csv"
            financials = root / "financials.csv"
            quotes = root / "quotes.csv"
            tickers.write_text("ticker,market\nCHECK,yahoo\nLOCAL,yahoo\nSBER,moex\n")
            financials.write_text(
                "source,ticker,currency,period_type,period_end,revenue\n"
                "moex,SBER,RUB,FY,2024-12-31,10\nyahoo,LOCAL,USD,FY,2024-12-31,20\n"
            )
            quotes.write_text(
                "source,ticker,currency,date,close\n"
                "moex,SBER,RUB,2024-12-30,9\nyahoo,LOCAL,USD,2024-12-30,8\nyahoo,CHECK,USD,2024-12-30,7\n"
            )
            payload = {"timeseries": {"error": None, "result": [
                self.series("annualTotalRevenue", ("2024-12-31", "12M", "USD", 30)),
            ]}}
            base_args = [
                "run", "--tickers", str(tickers), "--quotes", str(quotes), "--config", str(EXAMPLES / "config.yaml"),
            ]
            with patch("finautomation.financials.get_json", return_value=payload) as fetch:
                self.assertEqual(main([*base_args, "--financials", str(financials), "--online-financials",
                                       "--out", str(root / "mixed")]), 0)
            fetch.assert_called_once()
            (mixed,) = (root / "mixed").iterdir()
            stats = json.loads((mixed / "metrics.json").read_text())
            self.assertEqual(stats["financials_mode"], "mixed")
            self.assertEqual(stats["ingestion"]["with_quotes"], 3)
            self.assertEqual(main([
                *base_args, "--financials", str(mixed / "inputs" / "financials.csv"),
                "--financials", str(mixed / "inputs" / "financials_yahoo.csv"), "--out", str(root / "replayed"),
            ]), 0)
            (replay,) = (root / "replayed").iterdir()
            self.assertEqual((mixed / "issuer_metrics.csv").read_bytes(), (replay / "issuer_metrics.csv").read_bytes())


class CliTest(unittest.TestCase):
    def invoke(self, folder, tickers, financials, quotes, *, synthetic=True, config=EXAMPLES / "config.yaml"):
        process = subprocess.run(
            [sys.executable, "-m", "finautomation.main", "run", "--tickers", str(tickers),
             "--financials", str(financials), "--quotes", str(quotes), "--config", str(config),
             "--out", str(folder), *(["--synthetic-demo"] if synthetic else [])],
            capture_output=True, text=True, check=False,
        )
        return process, sorted(folder.glob("20*"))

    def test_demo_and_new_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, count, passed in (("tickers.csv", 8, 7), ("new_tickers.csv", 3, 3)):
                process, (output,) = self.invoke(
                    root / name, EXAMPLES / name, EXAMPLES / "financials.csv", EXAMPLES / "quotes.csv"
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                stats = json.loads((output / "metrics.json").read_text())
                self.assertEqual(stats["ingestion"]["with_quotes"], count)
                self.assertEqual(stats["screener"]["passed_instruments"], passed)
                self.assertEqual(stats["forecast"]["status"], "pass")
                self.assertTrue(columns(output / "formula_trace.csv"))
                self.assertIn("Учебный синтетический набор", (output / "report.html").read_text())
                if name == "tickers.csv":
                    self.assertEqual(stats["screener"]["status"], "pass")
                    self.assertEqual([row["ticker"] for row in columns(output / "alerts.csv")], ["DEMO01"])

    def test_real_list_with_saved_public_quotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, (output,) = self.invoke(
                Path(temporary), VERIFICATION / "tickers.csv", VERIFICATION / "financials.csv",
                VERIFICATION / "quotes.csv", config=VERIFICATION / "config.yaml", synthetic=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads((output / "metrics.json").read_text())
            self.assertEqual(stats["ingestion"]["coverage"], 1)
            self.assertEqual(stats["screener"]["passed_instruments"], 3)
            self.assertEqual(stats["forecast"]["eligible_points"], 2)
            self.assertEqual(stats["forecast"]["status"], "pass")
            rows = {(row["ticker"], row["period_end"]): row for row in columns(output / "issuer_metrics.csv")}
            apple = rows["AAPL", "2025-09-27"]
            self.assertEqual(float(apple["ebitda"]), 144_748_000_000)
            self.assertAlmostEqual(float(apple["effective_tax_rate"]), 20719 / 132729)
            sber = rows["SBER", "2024-12-31"]
            self.assertEqual(float(sber["price"]), 279.43)
            self.assertAlmostEqual(float(sber["effective_tax_rate"]), 454.3 / 2034.6)

    def test_ytd_quarters_scales_and_formula_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickers, financials, prices = (root / name for name in ("tickers.csv", "financials.csv", "quotes.csv"))
            tickers.write_text("ticker,market\nONE,moex\n")
            financials.write_text(
                "source;ticker;currency;period_type;period_end;flow_basis;fiscal_year;fiscal_quarter;"
                "amount_scale;shares_scale;revenue;ebit;da;income_tax_expense;cash;shares;"
                "revenue_source_field;source_ref;retrieved_at\n"
                "moex;ONE;RUB;Q;2024-09-30;ytd;2025;1;1000000;1000;100;20;-5;-3;7;2;"
                "Sales;quarter1;2024-11-11\n"
                "moex;ONE;RUB;Q;2024-12-31;ytd;2025;2;1000;1000;240000;50000;-9000;5000;17000;2;"
                "Net sales;quarter2;2025-02-11\n"
                "moex;ONE;RUB;Q;2025-03-31;ytd;2025;3;1000000;1000;390;80;-15;7;11;3;"
                "Sales;quarter3;2025-05-11\n"
                "moex;ONE;RUB;FY;2025-06-30;;2025;;1000000;1000;560;130;-25;11;19;4;"
                "Sales;annual;2025-08-11\n"
            )
            prices.write_text("source,ticker,currency,date,close,price_scale\nmoex,ONE,RUB,2025-06-30,700,0.01\n")
            process, (output,) = self.invoke(root / "runs", tickers, financials, prices)
            self.assertEqual(process.returncode, 0, process.stderr)
            rows = columns(output / "issuer_metrics.csv")
            quarters = [row for row in rows if row["period_type"] == "Q"]
            self.assertEqual([float(row["revenue"]) for row in quarters], [1e8, 1.4e8, 1.5e8, 1.7e8])
            self.assertEqual([float(row["ebitda"]) for row in quarters], [25e6, 34e6, 36e6, 60e6])
            self.assertEqual([float(row["income_tax_expense"]) for row in quarters], [-3e6, 8e6, 2e6, 4e6])
            self.assertEqual(float(quarters[-1]["market_cap"]), 28_000)
            annual = next(row for row in rows if row["period_type"] == "FY")
            self.assertEqual(float(annual["revenue"]), 5.6e8)
            trail = [row for row in columns(output / "formula_trace.csv") if row["period_type"] == "Q"]
            q2 = next(row for row in trail if row["metric"] == "revenue" and row["period_end"] == "2024-12-31")
            derivation = json.loads(q2["inputs"])["1. revenue"]
            self.assertEqual(derivation["formula"], "Q2 = YTD2 − YTD1")
            newer, older = list(derivation["inputs"].values())
            self.assertEqual((newer["source_field"], newer["raw"], newer["scale"]), ("Net sales", "240000", 1000))
            self.assertEqual((older["source_ref"], older["retrieved_at"]), ("quarter1", "2024-11-11"))
            self.assertIn("масштаб ×1000", (output / "report.html").read_text())

    def test_ytd_never_crosses_fiscal_year_or_regenerates_conflicting_q4(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickers, financials, prices = (root / name for name in ("tickers.csv", "financials.csv", "quotes.csv"))
            tickers.write_text("ticker,market\nONE,moex\n")
            financials.write_text(
                "source,ticker,currency,period_type,period_end,flow_basis,fiscal_year,fiscal_quarter,revenue,equity\n"
                "moex,ONE,RUB,Q,2024-03-31,ytd,2023,1,10,1\n"
                "moex,ONE,RUB,Q,2024-06-30,ytd,2024,2,50,2\n"
                "moex,ONE,RUB,Q,2024-09-30,ytd,2024,3,80,3\n"
                "moex,ONE,RUB,Q,2024-12-31,period,2024,4,32,4\n"
                "moex,ONE,RUB,Q,2024-12-31,period,2024,4,33,4\n"
                "moex,ONE,RUB,FY,2024-12-31,period,2024,,110,4\n"
            )
            prices.write_text(
                "source,ticker,currency,date,close\nmoex,ONE,RUB,2024-06-28,12\n\n"
                "moex,ONE,RUB,2024-06-30,15\nmoex,ONE,RUB,2024-06-30,\n"
            )
            process, (output,) = self.invoke(root / "runs", tickers, financials, prices)
            self.assertEqual(process.returncode, 0, process.stderr)
            rows = {(row["period_type"], row["period_end"]): row for row in columns(output / "issuer_metrics.csv")}
            self.assertEqual(rows["Q", "2024-06-30"]["revenue"], "")
            self.assertEqual(float(rows["Q", "2024-06-30"]["price"]), 12)
            self.assertEqual(float(rows["Q", "2024-09-30"]["revenue"]), 30)
            self.assertNotIn(("Q", "2024-12-31"), rows)
            log = columns(output / "parse_log.csv")
            self.assertTrue(any(row["status"] == "missing_prior_ytd" for row in log))
            self.assertEqual({row["row"] for row in log if row["stage"] == "quotes" and row["status"] == "conflict"},
                             {"4", "5"})

    def test_invalid_and_conflicting_rows_leave_other_instruments_usable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickers, financials, prices = (root / name for name in ("tickers.csv", "financials.csv", "quotes.csv"))
            tickers.write_text("\ufeffticker,market\nONE,moex\nONE,moex\nTWO,moex\nERR,wrong\n")
            financials.write_text(
                "source;ticker;currency;period_type;period_end;revenue;ebit;da;source_ref\n"
                "moex;ONE;RUB;FY;2024-12-31;50;10;-5;<script>alert(1)</script>\n"
                "moex;TWO;RUB;FY;2024-12-31;50;10;5;report\n"
                "moex;TWO;RUB;FY;2024-12-31;51;10;5;report\n"
                "moex;ONE;RUB;FY;2024-12-99;51;10;5;report\n"
            )
            prices.write_text("source,ticker,currency,date,close\nmoex,ONE,RUB,2024-12-30,99\n")
            result, (output,) = self.invoke(root / "runs", tickers, financials, prices)
            self.assertEqual(result.returncode, 0, result.stderr)
            metrics = columns(output / "issuer_metrics.csv")
            self.assertEqual([row["ticker"] for row in metrics], ["ONE"])
            self.assertEqual(metrics[0]["ebitda"], "15.0")
            stats = json.loads((output / "metrics.json").read_text())
            self.assertEqual(stats["ingestion"]["processed"], 1)
            self.assertEqual(stats["ingestion"]["coverage"], 0.5)
            statuses = {row["status"] for row in columns(output / "parse_log.csv")}
            self.assertTrue({"invalid_row", "duplicate", "conflict", "missing_financials"} <= statuses)
            report = (output / "report.html").read_text()
            self.assertNotIn("<script>", report)
            self.assertIn("&lt;script&gt;", report)

    def test_no_valid_statements_still_produces_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickers = root / "tickers.csv"
            tickers.write_text("ticker,market\nMISSING,moex\n")
            result, (output,) = self.invoke(root / "runs", tickers, EXAMPLES / "financials.csv", EXAMPLES / "quotes.csv")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("missing_financials", {row["status"] for row in columns(output / "parse_log.csv")})


if __name__ == "__main__":
    unittest.main()
