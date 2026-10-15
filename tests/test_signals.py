from __future__ import annotations

import copy
import json
import math
import unittest
from datetime import date, timedelta
from decimal import Decimal

from agent.config import load_strategy
from agent.portfolio import _select_names, active_share, construct, estimate
from agent.signals import fuse, quant_signals, research_signal, reverse_research_candidates
from agent.util import PlanError


def histories():
    start = date(2026, 1, 1)
    days = [start + timedelta(days=i) for i in range(100) if (start + timedelta(days=i)).weekday() < 5][:65]
    return {str(2300 + n): [{"date": day.isoformat(), "close": 50 * (1 + (n + 1) / 10000) ** i,
                              "volume": 1000000 * (2 if i >= 60 else 1), "turnover": 80000000,
                              "source_ids": ["fixture-bars"]} for i, day in enumerate(days)] for n in range(5)}


def thesis(**overrides):
    value = {"id": "TH1", "ticker": "2300", "claim": "Synthetic test claim",
             "updated_at": "2026-04-01T13:30:00+08:00", "source_ids": ["S1"], "status": "active",
             "evidence_strength": "high", "market_recognition": "medium",
             "fundamental_surprise": {"baseline_type": "company_guidance_proxy", "baseline": "prior guidance",
                                      "observation": "new public update", "source_ids": ["S1"]},
             "catalysts": [{"description": "synthetic scheduled report", "expected_at": "2026-04-06",
                            "source_ids": ["S1"], "status": "confirmed"}],
             "scenarios": [{"name": "bear", "downside": -0.3, "assumptions": ["delay"]},
                           {"name": "bull", "upside": 0.4, "assumptions": ["execution"]}],
             "adversarial_reviewed": True}
    value.update(overrides)
    return value


class QuantSignalTest(unittest.TestCase):
    def test_independent_horizons_and_relative_metrics(self):
        result = quant_signals(histories(), "2026-04-02", {str(2300 + n): "industry" for n in range(5)})
        row = result["2304"]
        for n in (5, 10, 20, 60):
            self.assertAlmostEqual(row[f"momentum_{n}"], 1.0005 ** n - 1)
        self.assertGreater(row["relative_strength_20"], 0)
        self.assertGreater(row["industry_relative_momentum_20"], 0)
        self.assertGreater(row["volume_surge"], 0)
        self.assertEqual(row["downside_volatility_20"], 0)
        self.assertEqual(row["ranking_method"], "heuristic_default_momentum_20_percentile")

    def test_future_observations_and_late_publication_excluded(self):
        fixture = histories()
        prior = quant_signals(fixture, "2026-04-02")
        fixture["2300"] += [{"date": "2026-04-03", "close": 999999},
                             {"date": "2026-04-02", "available_at": "2026-04-04T01:00:00+08:00", "close": 123456}]
        self.assertEqual(prior, quant_signals(fixture, "2026-04-02"))

    def test_intraday_cutoff_excludes_same_day_close(self):
        rows = {"1": [{"date": "2026-04-01", "close": 10}, {"date": "2026-04-02", "close": 20}]}
        row = quant_signals(rows, "2026-04-02T09:00:00+08:00")["1"]
        self.assertEqual(row["latest_date"], "2026-04-01")

    def test_missing_nan_inf_are_json_safe(self):
        rows = {"missing": [{"date": "2026-04-01", "close": float("nan")}],
                "bad": [{"date": "2026-04-01", "close": 1, "turnover": float("inf")}], "empty": []}
        result = quant_signals(rows, "2026-04-02")
        json.dumps(result, allow_nan=False)
        self.assertFalse(result["missing"]["valid"])
        self.assertIsNone(result["empty"]["momentum_60"])

    def test_reverse_research_has_honest_trigger_and_respects_coverage(self):
        q = {"2300": {"as_of": "2026-04-02", "valid": True, "score": 0.8, "volume_surge": 1.0}}
        self.assertEqual(reverse_research_candidates(q, [thesis()]), [])
        task = reverse_research_candidates(q, [])[0]
        self.assertEqual(task["triggers"], ["volume_surge"])
        self.assertIn("unknown", task["question"])

    def test_stale_or_zero_trading_bars_cannot_be_fresh_quant_buys(self):
        fixture = histories()
        fixture["2300"][-1]["volume"] = 0
        result = quant_signals(fixture, "2026-04-02")
        self.assertFalse(result["2300"]["valid"])
        self.assertFalse(fuse(result, [], "Q0")["2300"]["eligible"])
        stale = quant_signals(histories(), "2026-05-01")
        self.assertTrue(all(not row["valid"] and row["stale"] for row in stale.values()))


class ResearchFusionTest(unittest.TestCase):
    def setUp(self):
        self.q = {"2300": {"ticker": "2300", "as_of": "2026-04-02", "valid": True,
                           "score": 0.6, "momentum_20": 0.08, "relative_strength_20": 0.03,
                           "source_ids": ["S_PRICE"]}}

    def test_research_intermediates_and_proxy_label(self):
        r = research_signal(thesis(), "2026-04-02")
        self.assertTrue(r["surprise_verifiable"])
        self.assertEqual(r["surprise_baseline_type"], "company_guidance_proxy")
        self.assertEqual(r["stage"], "confirmed_acceleration")
        self.assertNotIn("probability", r)

    def test_thesis_evidence_versions_survive_signal_fusion(self):
        r = research_signal(thesis(source_versions={"S1": 2}), "2026-04-02")
        d = fuse(self.q, [r], "H1")["2300"]
        self.assertEqual(d["source_refs"], [{"source_id": "S1", "version": 2, "thesis_id": "TH1"}])
        self.assertEqual(d["research"][0]["source_versions"], {"S1": 2})

    def test_missing_sources_watchlist_cannot_promote(self):
        r = research_signal(thesis(source_ids=[]), "2026-04-02")
        self.assertFalse(r["eligible"])
        d = fuse({}, [r], "R0")["2300"]
        self.assertFalse(d["eligible"])

    def test_research_schema_recognition_and_watchlist_gate(self):
        crowded = research_signal(thesis(market_recognition="crowded"), "2026-04-02")
        self.assertEqual(crowded["stage"], "crowded")
        discovery = research_signal(thesis(status="watchlist", market_recognition="recognized"), "2026-04-02")
        self.assertEqual(discovery["stage"], "early_discovery")
        self.assertFalse(fuse({}, [discovery], "R0")["2300"]["eligible"])

    def test_cancelled_or_unsourced_catalysts_do_not_receive_priority(self):
        r = research_signal(thesis(catalysts=[{"expected_at": "2026-04-03", "status": "cancelled", "source_ids": ["S1"]},
                                             {"expected_at": "2026-04-03", "status": "confirmed", "source_ids": []}]), "2026-04-02")
        self.assertEqual(r["catalyst_proximity"], "undated")

    def test_q0_independent_hybrids_evidence_gated_and_scenario_sized(self):
        r = research_signal(thesis(), "2026-04-02")
        q0 = fuse(self.q, [], "Q0")
        self.assertEqual(q0, fuse(self.q, [r], "Q0"))
        h1, h2 = fuse(self.q, [r], "H1")["2300"], fuse(self.q, [r], "H2")["2300"]
        self.assertGreater(h1["score"], q0["2300"]["score"])
        self.assertLessEqual(h2["max_weight"], 0.005 / 0.3)
        self.assertEqual(set(h1["source_ids"]), {"S_PRICE", "S1"})

    def test_legacy_baseline_preserves_order_without_imputing_missing_scores(self):
        q = {str(i): {**self.q["2300"], "baseline_score": value}
             for i, value in enumerate((-2.0, 0.0, 3.0, None))}
        original = copy.deepcopy(q)
        d = fuse(q, [], "Q0", config={"quant_ranking": "legacy_baseline"})
        self.assertEqual([d[str(i)]["score"] for i in range(3)], [0, 0.5, 1])
        self.assertFalse(d["3"]["eligible"])
        self.assertEqual(original, q)

    def test_research_first_can_enter_without_price_breakout(self):
        r = research_signal(thesis(market_recognition="low"), "2026-04-02")
        d = fuse({}, [r], "H1")["2300"]
        self.assertTrue(d["eligible"])
        self.assertLessEqual(d["max_weight"], 0.02)

    def test_broken_thesis_prevents_hybrid_automatic_addition(self):
        r = research_signal(thesis(status="invalidated"), "2026-04-02")
        self.assertFalse(fuse(self.q, [r], "H2")["2300"]["eligible"])
        self.assertTrue(fuse(self.q, [r], "Q0")["2300"]["eligible"])

    def test_future_or_naive_research_rejected(self):
        with self.assertRaises(ValueError):
            research_signal(thesis(updated_at="2026-04-03T09:00:00+08:00"), "2026-04-02")
        with self.assertRaises(ValueError):
            research_signal(thesis(updated_at="2026-04-01T09:00:00"), "2026-04-02")
        with self.assertRaises(ValueError):
            fuse(self.q, [], "Q0", config={"as_of": "2026-04-01"})


class PortfolioRiskTest(unittest.TestCase):
    def setUp(self):
        self.portfolio = copy.deepcopy(load_strategy()["portfolio"])
        self.names = [str(2300 + i) for i in range(40)]
        self.prices = {t: 80.0 for t in self.names}
        self.scores = {t: 1 - i / 45 for i, t in enumerate(self.names)}
        self.metrics = {t: {"avg_turnover": 80000000, "volatility_20": 0.02} for t in self.names}
        self.industries = {t: str(i % 6) for i, t in enumerate(self.names)}

    def test_costs_and_individual_sector_caps_after_nav_erosion(self):
        decisions = {t: {"eligible": True, "max_weight": 0.02 if i == 0 else 0.09}
                     for i, t in enumerate(self.names)}
        original = copy.deepcopy(self.portfolio)
        plan = construct(self.scores, self.metrics, self.prices, self.industries, {}, Decimal("1000000000"),
                         self.portfolio, {}, "neutral", 0.001425, 0.003, decisions)
        self.assertEqual(original, self.portfolio)
        for ticker, shares in plan.positions_after.items():
            self.assertLessEqual(shares * self.prices[ticker] / float(plan.nav_after), decisions[ticker]["max_weight"])
        self.assertGreaterEqual(len(plan.positions_after), 20)
        self.assertGreater(plan.cash_after / plan.nav_after, Decimal("0.02"))
        self.assertLessEqual(max(plan.diagnostics["sector_weights"].values()), self.portfolio["max_sector_weight"])
        self.assertLess(plan.nav_after, Decimal("1000000000"))

    def test_negligible_score_improvement_does_not_evict_incumbent(self):
        incumbent = self.names[24]
        held = {t: 1000 for t in self.names[:23] + [incumbent]}
        self.scores[incumbent] = self.scores[self.names[23]] - 0.001
        selected, _ = _select_names(self.scores, self.metrics, self.prices, 1e9, held, self.portfolio, 24, self.industries)
        self.assertIn(incumbent, selected)

    def test_pairwise_correlation_limit_does_not_cap_the_entire_market_as_one_cluster(self):
        names = ["2330"] + [t for t in self.names if t != "2330"]
        scores = {t: 100.0 if t == "2330" else 1.0 for t in names}
        prices = {t: 80.0 for t in names}
        metrics = {t: {"avg_turnover": 80000000, "correlations": {other: 0.99 for other in names if other != t}} for t in names}
        industries = {t: str(i % 6) for i, t in enumerate(names)}
        self.portfolio["max_correlated_weight"] = 0.15
        plan = construct(scores, metrics, prices, industries, {}, Decimal("1000000000"), self.portfolio,
                         {}, "neutral", 0.001425, 0.003)
        weights = [shares * prices[t] / float(plan.nav_after) for t, shares in plan.positions_after.items()]
        self.assertLessEqual(sum(sorted(weights, reverse=True)[:2]), 0.15)
        self.assertGreater(sum(weights), 0.75)
        self.assertEqual(plan.diagnostics["correlation_constraint_scope"], "pairwise_not_full_connected_component")

    def test_active_share_conventions_and_missing_benchmark_fail_closed(self):
        ours = {f"{i:04}": 0.04 for i in range(20)}
        bench = {f"{i:04}": 0.04 for i in range(10, 30)}
        self.assertAlmostEqual(active_share(ours, bench, "top10_normalized"), 1)
        self.assertAlmostEqual(active_share(ours, bench, "top10_raw"), 0.4)
        self.assertAlmostEqual(active_share(ours, bench, "top15_normalized"), 2 / 3)
        self.assertAlmostEqual(active_share(ours, bench, "top15_raw"), 0.4)
        self.portfolio["require_benchmarks"] = True
        with self.assertRaises(PlanError):
            construct(self.scores, self.metrics, self.prices, self.industries, {}, Decimal("1000000000"),
                      self.portfolio, {}, "neutral", 0.001425, 0.003)

    def test_oversell_negative_cash_and_day_trade_rejected(self):
        for positions, cash, orders in [
            ({"2300": 1000}, 1000000, [{"ticker": "2300", "side": "SELL", "shares": 2000}]),
            ({}, 0, [{"ticker": "2300", "side": "BUY", "shares": 1000}]),
            ({"2300": 1000}, 1000000, [{"ticker": "2300", "side": "SELL", "shares": 1000},
                                       {"ticker": "2300", "side": "BUY", "shares": 1000}])]:
            with self.assertRaises(PlanError):
                estimate(Decimal(cash), positions, orders, self.prices, Decimal("0.001425"), Decimal("0.003"))


if __name__ == "__main__":
    unittest.main()
