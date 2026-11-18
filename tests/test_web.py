import tempfile
import unittest
from pathlib import Path

from finautomation.web import WebRuns, parse_form


class WebRunsTest(unittest.TestCase):
    def test_demo_returns_small_summary_and_writes_full_report(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            runs = WebRuns(Path(temporary), root / "examples" / "demo")
            try:
                run_id = runs.submit({
                    "mode": "demo",
                    "settings": {"filters": [{"field": "ebitda_m", "op": ">=", "value": 0.20}]},
                })
            finally:
                runs.pool.shutdown()
            state = runs.state(run_id)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["metrics"]["ingestion"]["with_quotes"], 8)
            self.assertEqual(state["metrics"]["screener"]["passed_instruments"], 7)
            self.assertEqual(len(state["screener"]), 8)
            self.assertNotIn("report", state)
            self.assertEqual(runs.history()[0]["id"], run_id)
            self.assertTrue((runs.output / run_id / "report.html").is_file())

    def test_csv_mode_requires_both_inputs(self):
        with self.assertRaisesRegex(ValueError, "отчётности и котировок"):
            parse_form({"mode": "csv", "tickers": "ticker,market\nAAPL,yahoo", "settings": {}})
