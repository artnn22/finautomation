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
from finautomation.forecast import forecast_revenue
from finautomation.inputs import FINANCIAL_FIELDS, Instrument, Observation, Quote, Statement
from finautomation.metrics import analyze, scenario
from finautomation.quotes import fetch_online, moex_quotes, yahoo_quotes


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "demo"
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
        old = statement("2023-12-31", "FY", net_income=99, shares=10)
        _, alerts = analyze([*quarters, old], quotes)
        self.assertEqual(len(alerts), 1)
        self.assertAlmostEqual(alerts[0]["pe_fy_at_quarter"], 300 / 99)
        self.assertAlmostEqual(alerts[0]["pe_ttm_at_quarter"], 2.5)
        rows, alerts = analyze([quarters[0], *quarters[2:], old], quotes)
        self.assertEqual(rows[-1].calculations["pe_ttm"].status, "incomplete_ttm_window")
        self.assertEqual(alerts, [])


class ForecastTest(unittest.TestCase):
    def test_target_is_excluded_from_its_own_fit(self):
        dates = ("2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31")
        series = [statement(day, revenue=value) for day, value in zip(dates, (10, 20, 30, 40, 500))]
        rows, stats = forecast_revenue(series, 4)
        self.assertAlmostEqual(rows[-2]["forecast"], 50)
        self.assertEqual(rows[-2]["naive"], 40)
        self.assertEqual(rows[-2]["y_true"], 500)
        self.assertEqual(stats["eligible_points"], 1)
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


class CliTest(unittest.TestCase):
    def invoke(self, folder, tickers, financials, quotes, *, synthetic=True):
        process = subprocess.run(
            [sys.executable, "-m", "finautomation.main", "run", "--tickers", str(tickers),
             "--financials", str(financials), "--quotes", str(quotes), "--config", str(EXAMPLES / "config.yaml"),
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
