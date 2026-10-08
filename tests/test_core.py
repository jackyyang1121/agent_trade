from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from decimal import Decimal

from agent.calendar_tw import closed_dates, default_trade_date, previous_trading_day
from agent.checks import schema_errors, semantic_errors
from agent.config import SCHEMA_PATH, enabled_factors, load_prospectus, load_strategy
from agent.dplan import render_dplan
from agent.portfolio import (
    active_share,
    construct,
    estimate,
    fit_shares,
    judge_regime,
    repair_active_share,
)
from agent.util import TAIPEI, money, target_shares


ROOT_SCHEMA = SCHEMA_PATH


class FormulaTest(unittest.TestCase):
    def test_target_shares_matches_guide(self):
        self.assertEqual(target_shares(0.23, 102_000_000, 1480), 15_000)
        self.assertEqual(target_shares(0.045, 102_000_000, 42.5), 108_000)

    def test_weight_roundtrip_respects_cap(self):
        for shares in (1000, 15000, 86000, 390000):
            fitted, weight = fit_shares(shares, 1_000_000_000, 2550, 0.22)
            self.assertLessEqual(weight, 0.22)
            self.assertEqual(target_shares(weight, 1_000_000_000, 2550), fitted)
            self.assertGreater(fitted, 0)

    def test_fees(self):
        cash, nav, positions, buy, sell = estimate(
            Decimal("1000000000"),
            {},
            [{"ticker": "2300", "side": "BUY", "shares": 1000}],
            {"2300": 100},
            Decimal("0.001425"),
            Decimal("0.003"),
        )
        self.assertEqual(buy, Decimal("100000"))
        self.assertEqual(sell, Decimal("0"))
        self.assertEqual(cash, money(Decimal("1000000000") - Decimal("100000") - Decimal("142.50")))
        self.assertEqual(positions["2300"], 1000)
        self.assertLess(nav, Decimal("1000000000"))


class CalendarTest(unittest.TestCase):
    def test_trading_notes_are_not_holidays(self):
        closed = closed_dates(
            [
                {"Name": "中華民國開國紀念日", "Date": "1150101"},
                {"Name": "國曆新年開始交易日", "Date": "1150102"},
                {"Name": "農曆春節前最後交易日", "Date": "1150211"},
            ]
        )
        self.assertIn(date(2026, 1, 1), closed)
        self.assertNotIn(date(2026, 1, 2), closed)
        self.assertNotIn(date(2026, 2, 11), closed)

    def test_evening_of_oct_8_plans_oct_12(self):
        closed = {date(2026, 10, 9), date(2026, 10, 26)}
        planned = default_trade_date(datetime(2026, 10, 8, 21, 30, tzinfo=TAIPEI), closed)
        self.assertEqual(planned, date(2026, 10, 12))
        self.assertEqual(previous_trading_day(planned, closed), date(2026, 10, 8))
        morning = default_trade_date(datetime(2026, 10, 12, 8, 0, tzinfo=TAIPEI), closed)
        self.assertEqual(morning, date(2026, 10, 12))


class ActiveShareTest(unittest.TestCase):
    def test_repair_moves_away_from_clone(self):
        weights = {f"{2300 + i:04d}": 0.04 for i in range(10)}
        bench = {"CLONE": dict(weights)}
        eligible = [f"{2300 + i:04d}" for i in range(25)]
        scores = {ticker: float(25 - index) for index, ticker in enumerate(eligible)}
        repaired = repair_active_share(weights, scores, eligible, bench, 0.28, lambda _ticker: 0.09)
        self.assertGreaterEqual(active_share(repaired, bench["CLONE"]), 0.2)
        self.assertLess(active_share(weights, bench["CLONE"]), 0.01)


class PlanTest(unittest.TestCase):
    def test_opening_book_passes_schema_and_semantics(self):
        strategy = load_strategy()
        prospectus = load_prospectus()
        factors = enabled_factors(strategy, prospectus)
        enabled = [fid for fid, _weight, _params in factors]
        tickers = [f"{2300 + i:04d}" for i in range(40)]
        prices = {ticker: 80.0 for ticker in tickers}
        scores = {ticker: float(len(tickers) - index) for index, ticker in enumerate(tickers)}
        metrics = {
            ticker: {
                "close": 80.0,
                "ret_60": 0.12,
                "ret_20": 0.03,
                "pe": 15.0,
                "pb": 2.0,
                "dividend_yield": 2.0,
                "rev_yoy": 18.0,
                "avg_turnover": 80_000_000.0,
                "vol_20": 0.02,
            }
            for ticker in tickers
        }
        components = {ticker: {fid: 0.4 for fid in enabled} for ticker in tickers}
        industries = {ticker: str(index % 6) for index, ticker in enumerate(tickers)}
        names = {ticker: f"測試{ticker}" for ticker in tickers}
        markets = {ticker: "twse" for ticker in tickers}
        market = {
            "taiex_close": 20000.0,
            "taiex_chg_pct": -0.2,
            "taiex_ret_5": 0.001,
            "taiex_ret_20": 0.002,
            "semi_chg_pct": -0.4,
            "foreign_bn": -2.0,
            "trust_bn": 1.0,
            "us": {
                "SOX": {"chg_pct": -0.3, "source_key": "us_sox"},
                "NDX": {"chg_pct": 0.1, "source_key": "us_ndx"},
                "SPX": {"chg_pct": 0.05, "source_key": "us_spx"},
            },
        }
        regime, _score, counter = judge_regime(market, strategy["regime_rules"])
        plan = construct(
            scores=scores,
            metrics=metrics,
            prices=prices,
            industries=industries,
            held={},
            cash=Decimal("1000000000"),
            portfolio=strategy["portfolio"],
            benchmarks={},
            regime=regime,
            commission=0.001425,
            tax=0.003,
        )
        self.assertGreaterEqual(len(plan.positions_after), 20)
        self.assertLessEqual(len(plan.positions_after), 30)
        self.assertEqual(plan.intent, "increase")
        now = datetime(2026, 10, 8, 21, 0, tzinfo=TAIPEI)
        sources = []
        for key, authority, url in (
            ("twse", "twse", "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date=20261008&type=ALLBUT0999"),
            ("valuation", "twse", "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"),
            ("rev_twse", "mops", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"),
            ("bfi", "twse", "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate=20261008&type=day"),
            ("us_sox", "vendor", "https://query1.finance.yahoo.com/v8/finance/chart/%5ESOX?range=10d&interval=1d"),
            ("us_ndx", "vendor", "https://query1.finance.yahoo.com/v8/finance/chart/%5ENDX?range=10d&interval=1d"),
            ("us_spx", "vendor", "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=10d&interval=1d"),
        ):
            sources.append(
                {
                    "key": key,
                    "authority": authority,
                    "name": key,
                    "url": url,
                    "content_as_of": "2026-10-08T13:30:00+08:00",
                    "fetched_at": "2026-10-08T21:00:00+08:00",
                }
            )
        document = render_dplan(
            prospectus=prospectus,
            market=market,
            names=names,
            markets=markets,
            prices=prices,
            metrics=metrics,
            scores=scores,
            components=components,
            enabled=enabled,
            held={},
            plan=plan,
            regime=regime,
            counter=counter,
            sources=sources,
            team_id="TEAM_TEST",
            trade_date="2026-10-12",
            model_provider="xai",
            model_version="grok-4.7",
            code_version="v1.0.0-test",
            started=now,
            finished=now,
            http_calls=3,
            band=float(strategy["portfolio"]["rebalance_band"]),
        )
        payload = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
        errors, warnings = semantic_errors(
            payload,
            {
                "held": {},
                "prices": prices,
                "pre_nav": plan.pre_nav,
                "cash": Decimal("1000000000"),
                "commission": 0.001425,
                "tax": 0.003,
                "benchmarks": {},
                "active_share_min": 0.2,
            },
        )
        self.assertEqual(errors, [], "\n".join(errors))
        self.assertTrue(warnings)
        self.assertTrue(payload["orders"])
        self.assertEqual(payload["market_view"]["posture"]["net_exposure_intent"], "increase")

    def test_official_example_matches_schema(self):
        example = json.loads((ROOT_SCHEMA.parent / "(範例)D-Plan_TEAM_042_2026-10-27.json").read_text(encoding="utf-8"))
        self.assertEqual(schema_errors(example), [])


if __name__ == "__main__":
    unittest.main()
