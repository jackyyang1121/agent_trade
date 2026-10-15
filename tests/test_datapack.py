from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from agent.compliance import CompetitionBook
from agent.datapack import parse_datapack
from agent.ledger import load_ledger, save_ledger
from agent.util import PlanError


def settled_pack():
    return {"schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_TEST",
            "trade_date": "2026-10-28", "as_of_date": "2026-10-27", "generated_at": "2026-10-28T07:00:00+08:00",
            "missing": [], "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": ["2301"]},
            "account": {"cash": 200000, "nav_tradable": 300000, "aum": 345000,
                        "receivable_cash": 5000, "cash_ratio_tradable": 2 / 3, "holdings_count": 1},
            "holdings": [{"code": "2330", "shares": 1000, "receivable_shares": 200, "close_price": 100, "tradable_weight": 1 / 3},
                         {"code": "2300", "shares": 0, "receivable_shares": 400, "close_price": 50, "tradable_weight": None}],
            "compliance": {"violations": 1, "notices": 2, "incidents": [
                {"date": "2026-10-27", "check_code": "weight", "status": "warn", "message": "UNIT TEST"}]}}


def initial_pack():
    raw = settled_pack()
    raw.update(trade_date="2026-10-27", as_of_date=None, generated_at="2026-10-27T07:00:00+08:00",
               prev_day_orders={"outcome": "none", "void_reasons": [], "excluded_codes": []}, holdings=[])
    raw["account"] = {"cash": 1000000000, "nav_tradable": 1000000000, "aum": 1000000000,
                      "receivable_cash": 0, "cash_ratio_tradable": 1, "holdings_count": 0}
    return raw


class DataPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_receivables_are_preserved_but_never_become_cash_or_sellable_shares(self):
        raw = settled_pack()
        snapshot = parse_datapack(raw, date(2026, 10, 27))
        self.assertEqual(snapshot.cash, Decimal(200000))
        self.assertEqual(snapshot.positions, {"2330": 1000})
        self.assertEqual(snapshot.nav_tradable, Decimal(300000))
        self.assertEqual(snapshot.aum, Decimal(345000))
        book = CompetitionBook(self.root)
        ledger = book.reconcile(raw, date(2026, 10, 27))
        self.assertEqual(ledger.nav_on({"2330": 100}), snapshot.nav_tradable)
        self.assertEqual(ledger.official_data, raw)
        self.assertEqual(raw, settled_pack())
        self.assertEqual(ledger.official_data["prev_day_orders"]["excluded_codes"], ["2301"])
        self.assertEqual(ledger.official_data["compliance"]["notices"], 2)
        path = self.root / "local-copy.json"
        save_ledger(ledger, path)
        self.assertEqual(load_ledger(1, path).official_data, raw)

    def test_first_day_has_true_cash_without_inventing_a_settlement_date(self):
        raw = initial_pack()
        book = CompetitionBook(self.root)
        ledger = book.reconcile(raw, date(2026, 10, 23))
        self.assertEqual(ledger.cash, Decimal(1000000000))
        self.assertEqual(ledger.positions, {})
        self.assertIsNone(ledger.last_settled_date)
        self.assertIsNone(ledger.official_data["as_of_date"])
        self.assertTrue(ledger.reconciled)
        self.assertEqual(book.get_ledger().official_data, raw)

    def test_missing_settlement_or_prices_never_replace_a_known_book(self):
        book = CompetitionBook(self.root)
        before = book.reconcile(initial_pack(), date(2026, 10, 23))
        for missing in ("prev_settlement", "prev_close"):
            with self.subTest(missing=missing):
                raw = settled_pack()
                raw["missing"] = [missing]
                if missing == "prev_settlement":
                    raw.update(as_of_date=None, account=None, holdings=None)
                    raw["prev_day_orders"]["outcome"] = "unknown"
                with self.assertRaises(PlanError):
                    book.reconcile(raw, date(2026, 10, 27))
                self.assertEqual(book.get_ledger().official_data, before.official_data)
                self.assertEqual(book.get_ledger().cash, before.cash)

    def test_invalid_versions_dates_values_and_ratios_are_rejected(self):
        mutations = [lambda d: d.update(schema_version="4.3"),
                     lambda d: d.update(as_of_date="2026-10-26"),
                     lambda d: d.update(generated_at="2026-10-28T07:00:00"),
                     lambda d: d.update(generated_at="2026-10-26T07:00:00+08:00"),
                     lambda d: d["account"].update(cash=float("nan")),
                     lambda d: d["account"].update(cash_ratio_tradable=1),
                     lambda d: d["account"].update(holdings_count=2),
                     lambda d: d["account"].update(aum=300000),
                     lambda d: d["holdings"][0].update(shares=-1),
                     lambda d: d["holdings"][0].update(close_price=None),
                     lambda d: d["prev_day_orders"].update(outcome="unknown")]
        for mutate in mutations:
            raw = settled_pack(); mutate(raw)
            with self.subTest(raw=raw), self.assertRaises(PlanError):
                parse_datapack(raw, date(2026, 10, 27))

    def test_initial_data_cannot_hide_real_positions(self):
        raw = settled_pack(); raw["as_of_date"] = None
        with self.assertRaisesRegex(PlanError, "首次全現金"):
            parse_datapack(raw, date(2026, 10, 27))

    def test_voided_batch_is_reconciled_without_claiming_it_executed(self):
        from tests.test_compliance import valid_plan
        doc, context = valid_plan()
        book = CompetitionBook(self.root)
        book.reconcile({"as_of": "2026-10-23", "cash": context["cash"], "positions": context["held"]}, date(2026, 10, 23))
        book.record_plan("voided-test", doc, context)
        book.transition("voided-test", "VALIDATED")
        book.transition("voided-test", "SUBMITTED", "unit-test-receipt")
        raw = settled_pack()
        raw["prev_day_orders"] = {"outcome": "voided", "void_reasons": ["compliance_fail"], "excluded_codes": []}
        book.reconcile(raw, date(2026, 10, 27))
        self.assertEqual(book.get_plan("voided-test")["state"], "RECONCILED")
        with sqlite3.connect(book.path) as db:
            states = [row[0] for row in db.execute("SELECT state FROM events ORDER BY id")]
        self.assertIn("VOIDED", states)
        self.assertNotIn("SETTLED", states)

    def test_first_day_gate_checks_team_trade_date_and_true_initial_capital(self):
        from tests.test_compliance import CompetitionGateTests
        case = CompetitionGateTests(); case.setUp()
        self.addCleanup(case.doCleanups)
        case.ledger = CompetitionBook(self.root).reconcile(initial_pack(), date(2026, 10, 23))
        self.assertIsNone(case.gate())
        for field, value in (("team_id", "OTHER_TEAM"), ("trade_date", "2026-10-28"),
                             ("generated_at", "2026-10-27T08:01:00+08:00")):
            with self.subTest(field=field):
                original = copy.deepcopy(case.ledger.official_data)
                case.ledger.official_data[field] = value
                with self.assertRaises(PlanError):
                    case.gate()
                case.ledger.official_data = original
        case.ledger.cash -= Decimal(1)
        with self.assertRaisesRegex(PlanError, "與官方 DataPack"):
            case.gate()
