from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal

from agent.evaluation import ForwardTracker
from agent.evaluation.forward import attribute_missed_opportunity
from agent.util import PlanError


class ForwardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = [(date(2026, 10, 5) + timedelta(days=i)).isoformat()
                         for i in range(30) if (date(2026, 10, 5) + timedelta(days=i)).weekday() < 5]
        self.tracker = ForwardTracker(self.tmp.name, initial_capital=1_000_000,
                                      strategy_version="test-v1", trading_sessions=self.sessions,
                                      universe=["2300", "2301"])

    def freeze(self, variant="Q0", **kwargs):
        arguments = dict(trade_date="2026-10-05", as_of="2026-10-02T21:00:00+08:00",
                         orders=[{"ticker": "2300", "side": "BUY", "shares": 1000}],
                         prices={"2300": 100.0, "2301": 50.0},
                         candidates=[{"ticker": "2301", "rejection_reason": "cash_constraint"}],
                         industries={"2300": "電子", "2301": "金融"}, benchmark=100)
        arguments.update(kwargs)
        return self.tracker.freeze(variant, **arguments)

    @staticmethod
    def quotes(close=110, volume=100_000):
        return {"2300": {"volume": volume, "value": 105 * volume, "close": close},
                "2301": {"volume": 100_000, "value": 55 * 100_000, "close": 55}}

    def test_generated_orders_do_not_fill_and_freeze_is_immutable(self):
        frozen = self.freeze()
        self.assertEqual(self.tracker.ledger("Q0").cash, Decimal("1000000.00"))
        self.assertEqual(self.tracker.ledger("Q0").positions, {})
        self.assertEqual(self.freeze(), frozen)
        with self.assertRaisesRegex(PlanError, "不可覆寫"):
            self.freeze(prices={"2300": 102, "2301": 50})
        self.assertEqual(self.tracker.ledger("Q0").pending["prices"]["2300"], 100)

    def test_session_vwap_costs_and_idempotent_settlement(self):
        self.freeze()
        settled = self.tracker.settle("Q0", "2026-10-05", self.quotes(), benchmark=102)
        self.assertEqual(settled["fills"][0]["price"], 105)
        self.assertEqual(settled["fills"][0]["commission"], 149.63)
        self.assertEqual(settled["cash"], 894850.37)
        self.assertEqual(settled["nav"], 1004850.37)
        repeated = self.tracker.settle("Q0", "2026-10-05", {}, benchmark=999)
        self.assertEqual(repeated, settled)
        self.assertEqual(len(self.tracker.ledger("Q0").nav_history), 1)
        self.assertEqual(self.tracker.ledger("H1").positions, {})
        self.assertEqual(self.tracker.ledger("H2").cash, Decimal("1000000.00"))
        self.assertAlmostEqual(settled["industry_exposure"]["電子"], 110000 / 1004850.37)
        self.assertAlmostEqual(settled["concentration_hhi"], (110000 / 1004850.37) ** 2)

    def test_sell_tax_and_missing_execution_are_not_fills(self):
        self.freeze()
        self.tracker.settle("Q0", "2026-10-05", self.quotes())
        self.tracker.freeze("Q0", "2026-10-06", "2026-10-05T21:00:00+08:00",
                            [{"ticker": "2300", "side": "SELL", "shares": 1000}], {"2300": 110})
        sold = self.tracker.settle("Q0", "2026-10-06", self.quotes())
        self.assertEqual(sold["fills"][0]["tax"], 315.0)
        self.assertEqual(sold["fills"][0]["commission"], 149.63)
        self.assertEqual(self.tracker.ledger("Q0").positions, {})
        self.freeze("H1")
        stopped = self.tracker.settle("H1", "2026-10-05", self.quotes(volume=0))
        self.assertEqual(stopped["fills"][0]["status"], "UNFILLED")
        self.assertEqual(stopped["cash"], 1000000)
        self.assertEqual(stopped["trading_cost"], 0)

    def test_no_future_information_or_late_freeze(self):
        for metadata in [
            {"sources": [{"published_at": "2026-10-05T00:00:00+08:00"}]},
            {"fetched_at": "2026-10-03T01:00:00+08:00"},
        ]:
            with self.assertRaisesRegex(PlanError, "未來資料"):
                self.freeze(metadata=metadata)
        with self.assertRaisesRegex(PlanError, "截止"):
            self.freeze(as_of="2026-10-05T09:01:00+08:00")
        with self.assertRaises(PlanError):
            self.freeze(metadata={"published_at": "2026-10-02T12:00:00"})

    def test_predictions_mature_only_after_trading_sessions_and_keep_missing(self):
        self.freeze()
        result = self.tracker.evaluate()["portfolios"]["Q0"]
        self.assertEqual({item["status"] for item in result["predictions"]}, {"PENDING"})
        for day in self.sessions[:5]:
            self.tracker.settle("Q0", day, self.quotes(), benchmark=100)
        result = self.tracker.evaluate()["portfolios"]["Q0"]
        statuses = {item["horizon_sessions"]: item["status"] for item in result["predictions"]}
        self.assertEqual(statuses, {1: "MATURED", 5: "MATURED", 10: "PENDING", 20: "PENDING"})
        missed = result["missed_opportunities"][0]
        self.assertEqual(missed["category"], "Portfolio Constraint")
        self.assertFalse(missed["hindsight_sources_used"])
        self.assertAlmostEqual(result["predictions"][0]["return"], .1)
        self.freeze("H1")
        self.tracker.settle("H1", "2026-10-05", {})
        prediction = self.tracker.evaluate()["portfolios"]["H1"]["predictions"][0]
        self.assertEqual(prediction["status"], "MISSING_DATA")
        self.assertIsNone(prediction["return"])

    def test_variant_and_version_isolation(self):
        self.freeze("H2")
        self.tracker.settle("H2", "2026-10-05", self.quotes())
        other = ForwardTracker(self.tmp.name, initial_capital=1_000_000, strategy_version="test-v2")
        self.assertEqual(other.evaluate()["portfolios"]["H2"]["settled_sessions"], 0)
        with self.assertRaisesRegex(PlanError, "新版本"):
            ForwardTracker(self.tmp.name, initial_capital=2_000_000, strategy_version="test-v1")
        self.assertEqual(self.tracker.evaluate()["portfolios"]["Q0"]["settled_sessions"], 0)

    def test_stale_marks_disclosed_and_overselling_blocked(self):
        self.freeze()
        self.tracker.settle("Q0", "2026-10-05", self.quotes())
        stale = self.tracker.settle("Q0", "2026-10-06", {})
        self.assertEqual(stale["stale_marks"], ["2300"])
        self.assertEqual(stale["marking_status"], "STALE_MARKS")
        with self.assertRaisesRegex(PlanError, "超賣"):
            self.tracker.freeze("Q0", "2026-10-07", "2026-10-06T21:00:00+08:00",
                                [{"ticker": "2300", "side": "SELL", "shares": 2000}], {"2300": 110})
        self.assertEqual(self.tracker.ledger("Q0").positions["2300"], 1000)

    def test_concurrent_settlements_charge_exactly_once(self):
        self.freeze()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: self.tracker.settle("Q0", "2026-10-05", self.quotes()), range(2)))
        self.assertEqual(outcomes[0], outcomes[1])
        ledger = self.tracker.ledger("Q0")
        self.assertEqual(ledger.positions, {"2300": 1000})
        self.assertEqual(ledger.cash, Decimal("894850.37"))
        self.assertEqual(len(ledger.nav_history), 1)

    def test_lookup_checkpoint_and_report(self):
        self.assertIsNone(self.tracker.frozen("Q0", "2026-10-05"))
        original = self.freeze()
        self.assertEqual(self.tracker.pending_dates("Q0"), ["2026-10-05"])
        self.assertEqual(self.tracker.frozen("Q0", "2026-10-05"), original)
        settled = self.tracker.settle("Q0", "2026-10-05", self.quotes())
        self.assertEqual(settled["run_id"], original["run_id"])
        self.assertEqual(self.tracker.pending_dates("Q0"), [])
        report = self.tracker.write_report()
        self.assertIn("前瞻實驗績效", report.read_text(encoding="utf-8"))
        self.assertIn("Q0", report.read_text(encoding="utf-8"))

    def test_new_buy_requires_both_execution_and_marking_data(self):
        self.freeze()
        quotes = self.quotes()
        quotes["2300"].pop("close")
        settled = self.tracker.settle("Q0", "2026-10-05", quotes)
        self.assertEqual(settled["fills"][0]["reason"], "MISSING_CLOSING_PRICE")
        self.assertEqual(self.tracker.ledger("Q0").positions, {})
        self.assertEqual(settled["trading_cost"], 0)

    def test_no_hindsight_attribution_when_snapshot_has_no_explanation(self):
        result = attribute_missed_opportunity({"ticker": "2300"})
        self.assertEqual(result["category"], "UNDETERMINED")
        self.assertFalse(result["hindsight_sources_used"])

    def test_cannot_skip_pending_session_or_rewrite_old_ledger(self):
        self.freeze()
        with self.assertRaisesRegex(PlanError, "跳過"):
            self.tracker.settle("Q0", "2026-10-06", self.quotes())
        self.assertIsNone(self.tracker.ledger("Q0").last_settled_date)
        self.tracker.settle("Q0", "2026-10-05", self.quotes())
        with self.assertRaises(PlanError):
            self.tracker.freeze("Q0", "2026-10-05", "2026-10-02T21:00:00+08:00", [], {"2300": 100})


if __name__ == "__main__":
    unittest.main()
