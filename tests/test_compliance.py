from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from agent.checks import semantic_errors
from agent.compliance import CompetitionBook, structural_errors, validate_competition_inputs
from agent.ledger import Ledger, apply_official_book, load_ledger, save_ledger
from agent.util import PlanError, TAIPEI


def valid_plan():
    """A deterministic hold book: 20 positions, 20% cash, disjoint benchmark."""
    tickers = [str(2300 + index) for index in range(20)]
    document = {
        "schema_version": "4.2", "doc_type": "D-Plan", "team_id": "TEAM_TEST",
        "trade_date": "2026-10-27",
        "sources": [{"source_id": "S1", "authority": "twse", "url": "https://www.twse.com.tw/", "content_as_of": "2026-10-23T13:30:00+08:00"}],
        "observations": [{"obs_id": "O1", "source_ref": ["S1"], "statement": "交易所個股收盤價為100元", "values": {"close": 100.0}}],
        "market_view": {"basis_refs": ["O1"], "logic": "本次維持已確認的既有現金與股票配置，等待新增公開證據。", "regime": "neutral", "stance": "neutral", "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.15, 0.24]}, "counter_evidence": None},
        "inferences": [{"inf_id": "I1", "premise_refs": ["O1"], "logic": "沒有新增足以抵銷交易成本的證據，因此維持現有股票與現金配置。", "counter_evidence": None}],
        "decisions": [], "orders": [],
        "no_trade_decisions": [{"ticker": ticker, "reason_refs": ["I1"]} for ticker in tickers],
        "agent_metadata": {"model_provider": "openai", "model_version": "recorded-test-model", "run_started_at": "2026-10-27T07:00:00+08:00", "run_completed_at": "2026-10-27T07:01:00+08:00", "code_version": "sha256:test"},
    }
    context = {"held": {ticker: 40000 for ticker in tickers}, "prices": {ticker: 100.0 for ticker in tickers}, "pre_nav": 100000000, "cash": "20000000", "commission": 0.001425, "tax": 0.003,
               "universe": tickers, "benchmarks": {"ETF": {str(3000 + i): 0.04 for i in range(10)}}, "benchmark_etf_ids": ["ETF"], "active_share_definition_confirmed": True, "active_share_definition": "top10_normalized", "as_of": "2026-10-27T07:02:00+08:00", "pricing_date": "2026-10-23", "mode": "competition"}
    return document, context


class SemanticSafetyTests(unittest.TestCase):
    def test_schema_invalid_returns_errors_without_keyerror(self):
        errors, _ = semantic_errors({"orders": [{"ticker": "2330"}]}, {})
        self.assertTrue(errors)

    def test_structural_check_needs_no_synthetic_portfolio_context(self):
        document, _context = valid_plan()
        self.assertEqual(structural_errors(document), [])
        document["orders"] = [{"ticker": "2300", "side": "BUY", "shares": 1000, "decision_ref": "D1"}]
        self.assertTrue(any("不存在" in error for error in structural_errors(document)))

    def test_invalid_ledger_context_is_reported(self):
        document, context = valid_plan()
        context["cash"] = "NaN"
        self.assertTrue(any("context" in error for error in semantic_errors(document, context)[0]))

    def test_valid_hold_and_no_trade_book(self):
        document, context = valid_plan()
        self.assertEqual(semantic_errors(document, context)[0], [])

    def test_missing_active_share_fails_closed(self):
        document, context = valid_plan()
        context["benchmarks"] = {}
        self.assertTrue(any("Active Share" in error for error in semantic_errors(document, context)[0]))
        context["mode"] = "shadow"
        errors, warnings = semantic_errors(document, context)
        self.assertEqual(errors, [])
        self.assertTrue(warnings)

    def test_future_source_and_inverted_run_times(self):
        document, context = valid_plan()
        document["sources"][0]["content_as_of"] = "2026-10-27T08:00:00+08:00"
        document["agent_metadata"]["run_started_at"] = "2026-10-27T07:03:00+08:00"
        errors, _ = semantic_errors(document, context)
        self.assertTrue(any("可得" in error for error in errors))
        self.assertTrue(any("起始" in error for error in errors))

    def test_data_cutoff_and_local_validation_time_are_distinct(self):
        document, context = valid_plan()
        context["as_of"] = "2026-10-27T07:01:30+08:00"
        context["validation_time"] = "2026-10-27T07:03:00+08:00"
        document["agent_metadata"]["run_completed_at"] = "2026-10-27T07:02:00+08:00"
        # Rendering finishes after the information freeze, before local
        # validation. This is not a claim of organizer receipt or new data.
        self.assertEqual(semantic_errors(document, context)[0], [])
        document["sources"][0]["content_as_of"] = "2026-10-27T07:01:31+08:00"
        self.assertTrue(any("可得" in error for error in semantic_errors(document, context)[0]))
        document["sources"][0]["content_as_of"] = "2026-10-23T13:30:00+08:00"
        document["agent_metadata"]["run_completed_at"] = "2026-10-27T07:04:00+08:00"
        self.assertTrue(any("驗證" in error for error in semantic_errors(document, context)[0]))

    def test_invalid_calendar_timestamp_does_not_crash(self):
        document, context = valid_plan()
        document["sources"][0]["content_as_of"] = "2026-99-99T07:00:00+08:00"
        self.assertTrue(semantic_errors(document, context)[0])

    def test_broken_reference_and_universe_are_rejected(self):
        document, context = valid_plan()
        document["inferences"][0]["premise_refs"] = ["O2"]
        context["universe"] = ["2330"]
        errors, _ = semantic_errors(document, context)
        self.assertTrue(any("不存在" in error for error in errors))
        self.assertTrue(any("股票池" in error for error in errors))

    def test_nonfinite_values_rejected(self):
        document, context = valid_plan()
        document["observations"][0]["values"]["close"] = float("nan")
        self.assertTrue(any("有限" in error for error in semantic_errors(document, context)[0]))

    def test_missing_and_zero_price_block(self):
        document, context = valid_plan()
        context["prices"]["2300"] = 0
        self.assertTrue(any("定價" in error for error in semantic_errors(document, context)[0]))

    def test_fake_source_rejected_for_competition(self):
        document, context = valid_plan()
        document["sources"][0]["url"] = "https://news.invalid/fabricated"
        self.assertTrue(any("範例" in error for error in semantic_errors(document, context)[0]))


class OfficialBookTests(unittest.TestCase):
    def test_record_does_not_change_ledger_or_submit(self):
        with tempfile.TemporaryDirectory() as temporary:
            book = CompetitionBook(Path(temporary))
            document, context = valid_plan()
            record = book.record_plan("run-1", document, context)
            self.assertEqual(record["state"], "GENERATED")
            self.assertEqual(book.get_ledger().positions, {})
            self.assertFalse(book.get_ledger().reconciled)
            book.reconcile({"as_of": "2026-10-23", "cash": context["cash"], "positions": context["held"]}, date(2026, 10, 23))
            book.transition("run-1", "VALIDATED")
            with self.assertRaisesRegex(PlanError, "收據"):
                book.transition("run-1", "SUBMITTED")
            book.transition("run-1", "SUBMITTED", {"receipt_id": "official-1"})
            self.assertEqual(book.get_plan("run-1")["state"], "SUBMITTED")
            self.assertEqual(book.get_ledger().positions, context["held"])
            with self.assertRaisesRegex(PlanError, "官方帳本"):
                book.transition("run-1", "SETTLED")
            raw = {"as_of": "2026-10-27", "cash": "20000000", "positions": context["held"]}
            ledger = book.reconcile(raw, date(2026, 10, 27))
            self.assertTrue(ledger.reconciled)
            self.assertEqual(book.get_plan("run-1")["state"], "RECONCILED")
            self.assertEqual(book.reconcile(raw, date(2026, 10, 27)).cash, ledger.cash)

    def test_repeat_is_idempotent_but_frozen_decision_cannot_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            book = CompetitionBook(Path(temporary))
            document, context = valid_plan()
            first = book.record_plan("run-1", document, context)
            self.assertEqual(book.record_plan("run-1", document, context)["digest"], first["digest"])
            document["market_view"]["logic"] += "新增敘述。"
            with self.assertRaisesRegex(PlanError, "不可覆寫"):
                book.record_plan("run-1", document, context)

    def test_bad_official_snapshot_does_not_mutate_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "book.json"
            ledger = Ledger(Decimal("100"), {"2330": 1000})
            for positions, cash in (({"2330": 1000.5}, "200"), ({"2330": -1000}, "200"), ({"2330": 1000}, "NaN")):
                path.write_text(json.dumps({"as_of": "2026-10-23", "cash": cash, "positions": positions}), encoding="utf-8")
                with self.assertRaises(PlanError):
                    apply_official_book(ledger, date(2026, 10, 23), path)
                self.assertEqual(ledger.cash, Decimal("100"))
                self.assertEqual(ledger.positions, {"2330": 1000})

    def test_ledger_optional_path_roundtrip_and_separation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "separate.json"
            ledger = Ledger(Decimal("100.00"), {"2330": 1000})
            save_ledger(ledger, path)
            self.assertEqual(load_ledger(1, path).positions, ledger.positions)
            self.assertEqual(load_ledger(50, Path(temporary) / "other.json").positions, {})
            ledger.positions["2330"] = 1000.5
            with self.assertRaises(PlanError):
                save_ledger(ledger, path)
            self.assertEqual(load_ledger(1, path).positions, {"2330": 1000})


class CompetitionGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.rows = [{"ticker": str(2300 + i), "market": "twse" if i < 100 else "tpex"} for i in range(150)]
        universe = self.root / "universe.csv"
        with universe.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "market"])
            writer.writeheader()
            writer.writerows(self.rows)
        benchmark = self.root / "benchmarks.csv"
        benchmark.write_text("etf_id,ticker,weight\n" + "".join(f"ETF,{3000+i},0.04\n" for i in range(10)), encoding="utf-8")
        self.settings = {"team_id": "TEAM_TEST", "initial_capital": 1000000000, "commission_rate": 0.001425, "tax_rate": 0.003, "lot": 1000, "universe_as_of": "2026-07-31"}
        self.runtime = {"competition": {"official_universe_path": str(universe), "universe_manifest": {"source_url": "https://www.esunbank.com/competition/universe", "confirmed": True, "sha256": hashlib.sha256(universe.read_bytes()).hexdigest(), "as_of": "2026-07-31"},
                                        "benchmarks_path": str(benchmark), "benchmarks_manifest": {"source_url": "https://www.twse.com.tw/etf", "confirmed": True, "sha256": hashlib.sha256(benchmark.read_bytes()).hexdigest(), "as_of": "2026-10-23", "etf_ids": ["ETF"], "definition": "top10_normalized"},
                                        "schedule_confirmed": True, "first_trade_date": "2026-10-27", "last_trade_date": "2026-11-30", "allow_degraded": False}}
        self.benchmarks = {"ETF": {str(3000+i): 0.04 for i in range(10)}}
        self.ledger = Ledger(Decimal("1000000000"), {}, last_settled_date="2026-10-23", reconciled=True)
        self.research = {"provider": "codex", "model": "actual-model", "success": True, "new_research": True, "synthetic": False, "completed_at": "2026-10-27T07:00:00+08:00"}

    def gate(self):
        return validate_competition_inputs(self.settings, self.runtime, self.rows, self.benchmarks, self.ledger, date(2026, 10, 27), datetime(2026, 10, 27, 8, 0, tzinfo=TAIPEI), research_run=self.research, pricing_date=date(2026, 10, 23))

    def test_confirmed_inputs_pass(self):
        self.assertIsNone(self.gate())

    def test_formal_happy_path_guard_generated_validated_submitted_reconciled(self):
        """The formal path can succeed with complete, explicitly supplied inputs.

        Provider metadata here is a test double, never a live-research claim.
        No exchange call or model account is needed by the test.
        """
        document, context = valid_plan()
        context["cash"] = "200000000"
        context["held"] = {ticker: 400000 for ticker in context["held"]}
        context["pre_nav"] = 1000000000
        context["universe"] = [row["ticker"] for row in self.rows]
        book = CompetitionBook(self.root / "formal")
        before = {"as_of": "2026-10-23", "cash": context["cash"], "positions": context["held"]}
        book.reconcile(before, date(2026, 10, 23))
        self.ledger = book.get_ledger()
        self.assertIsNone(self.gate())
        self.assertEqual(book.record_plan("formal-run", document, context)["state"], "GENERATED")
        self.assertEqual(book.transition("formal-run", "VALIDATED")["state"], "VALIDATED")
        self.assertEqual(book.transition("formal-run", "SUBMITTED", "official-fixture-receipt")["state"], "SUBMITTED")
        after = {**before, "as_of": "2026-10-27", "run_id": "formal-run"}
        book.reconcile(after, date(2026, 10, 27))
        self.assertEqual(book.get_plan("formal-run")["state"], "RECONCILED")
        self.assertEqual(book.get_ledger().cash, Decimal(context["cash"]))

    def test_unknown_actual_model_cannot_be_reported_as_real(self):
        self.research["model"] = "codex-default-unreported"
        with self.assertRaisesRegex(PlanError, "實際模型"):
            self.gate()

    def test_unconfirmed_and_stale_benchmark_block(self):
        self.runtime["competition"]["benchmarks_manifest"]["confirmed"] = False
        self.runtime["competition"]["benchmarks_manifest"]["as_of"] = "2026-10-22"
        with self.assertRaises(PlanError) as error:
            self.gate()
        self.assertIn("尚未", str(error.exception))
        self.assertIn("過期", str(error.exception))

    def test_mock_research_and_unreconciled_ledger_block(self):
        self.research["provider"] = "mock"
        self.ledger.reconciled = False
        with self.assertRaises(PlanError) as error:
            self.gate()
        self.assertIn("Mock", str(error.exception))
        self.assertIn("主辦方", str(error.exception))

    def test_hash_mismatch_and_different_runtime_universe_block(self):
        self.rows[0] = {"ticker": "9999", "market": "twse"}
        self.runtime["competition"]["universe_manifest"]["sha256"] = "0" * 64
        with self.assertRaises(PlanError) as error:
            self.gate()
        self.assertIn("SHA-256", str(error.exception))
        self.assertIn("執行中的股票池", str(error.exception))


if __name__ == "__main__":
    unittest.main()
