"""Formal daily contract tests with injected services, never live research.

The adapter doubles below deliberately emulate successfully verified live
services to exercise the production branch. All values/content are unit-test
data in temporary directories; no model, website or actual contest is called.
The production ``--fixture`` prohibition is kept intact.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agent.checks import schema_errors, semantic_errors
from agent.compliance import CompetitionBook
from agent.config import load_settings
from agent.knowledge import ResearchStore
from agent.orchestration.configuration import load_runtime
from agent.orchestration.market_data import MarketSnapshot
from agent.util import TAIPEI, PlanError, target_shares
from scripts.create_demo_fixture import build_fixture

workflow = importlib.import_module("agent.orchestration.daily")


class DailyCompetitionContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.day = date(2026, 10, 28)
        self.pricing = date(2026, 10, 27)
        self.started = datetime(2026, 10, 28, 8, 0, 0, 100000, tzinfo=TAIPEI)
        self.completed = datetime(2026, 10, 28, 8, 1, 0, 300000, tzinfo=TAIPEI)
        self.version = "sha256:dailycompetitioncontract"
        self.settings = load_settings()
        self.settings["team_id"] = "TEAM_UNIT_TEST"
        self.runtime = load_runtime()
        self.runtime["llm"].update(provider="codex_cli", model="requested-model-test-double")

        raw = build_fixture(self.day)
        # The trusted adapter contract is injected; these are never passed as a
        # --fixture or represented as an actual download in a user-facing run.
        def adapter_value(value):
            if isinstance(value, dict):
                return {key: False if key == "synthetic" else adapter_value(child) for key, child in value.items()}
            if isinstance(value, list):
                return [adapter_value(child) for child in value]
            return value
        raw = adapter_value(raw)
        for key, source in raw["prepared"]["source_material"].items():
            source["url"] = ("https://www.tpex.org.tw/" if key == "tpex" else
                             "https://mops.twse.com.tw/" if key.startswith("rev_") else
                             "https://www.twse.com.tw/")
            source["name"] = f"UNIT TEST adapter double: {key}; never downloaded"
        self.snapshot = MarketSnapshot(self.day, self.pricing, self.started.isoformat(), raw["prepared"],
                                       raw["histories"], raw["universe"], synthetic=False, http_calls=0)
        self.snapshot.validate()
        self.ticker = max(raw["prepared"]["scores"], key=raw["prepared"]["scores"].get)

        universe = self.root / "official-universe-test-double.csv"
        with universe.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "market", "industry"])
            writer.writeheader()
            writer.writerows(raw["universe"])
        benchmark = self.root / "benchmark-test-double.csv"
        benchmark.write_text("etf_id,ticker,weight\n" + "".join(f"ETF_TEST,{9000 + i},0.04\n" for i in range(10)), encoding="utf-8")
        universe_manifest = self.root / "universe.manifest.json"
        universe_manifest.write_text(json.dumps({"source_url": "https://www.esunbank.com/", "confirmed": True,
                                                "sha256": hashlib.sha256(universe.read_bytes()).hexdigest(),
                                                "as_of": "2026-07-31"}), encoding="utf-8")
        benchmark_manifest = self.root / "benchmark.manifest.json"
        benchmark_manifest.write_text(json.dumps({"source_url": "https://www.twse.com.tw/", "confirmed": True,
                                                 "sha256": hashlib.sha256(benchmark.read_bytes()).hexdigest(),
                                                 "as_of": self.pricing.isoformat(), "etf_ids": ["ETF_TEST"],
                                                 "definition": "top10_normalized"}), encoding="utf-8")
        self.runtime["competition"].update(official_universe_path=str(universe), universe_manifest=str(universe_manifest),
                                            benchmarks_path=str(benchmark), benchmarks_manifest=str(benchmark_manifest),
                                            schedule_confirmed=True, first_trade_date="2026-10-27",
                                            last_trade_date="2026-11-30", allow_degraded=False)
        self.book = CompetitionBook(self.state / "competition", self.settings["initial_capital"])
        self.before = {"as_of": self.pricing.isoformat(), "cash": "1000000000.00", "positions": {}}
        self.book.reconcile(self.before, self.pricing)

    def research_adapter_double(self, runtime, state_dir, universe, quant, as_of, sources):
        """Return a recorded-provider-shaped test double, without invoking GPT."""
        self.assertEqual(runtime["llm"]["provider"], "codex_cli")
        store = ResearchStore(state_dir / "research.sqlite3")
        try:
            source_id = "SOURCE_UNIT_TEST_DOUBLE"
            store.put_source({"id": source_id, "url": "https://mops.twse.com.tw/mops/web/t05st01",
                              "title": "UNIT TEST source double; never downloaded", "publisher": "Unit test double",
                              "source_type": "corporate", "authority": "mops", "legal_basis": "public",
                              "evidence_tier": "official", "extraction_status": "extracted", "synthetic": False,
                              "published_at": "2026-10-27T15:00:00+08:00", "content_as_of": "2026-10-27T15:00:00+08:00",
                              "fetched_at": "2026-10-28T00:00:00.123456+00:00",
                              "content": "UNIT TEST DOUBLE ONLY: proxy metric 10 versus observation 20; not a real company announcement."})
            store.upsert_thesis({"id": "THESIS_UNIT_TEST_DOUBLE", "ticker": self.ticker,
                                 "claim": "UNIT TEST DOUBLE ONLY: test the evidence chain; no live investment claim.",
                                 "source_ids": [source_id], "counter_source_ids": [], "synthetic": False,
                                 "status": "early_validation", "evidence_strength": "high", "market_recognition": "emerging",
                                 "unknowns": ["Only a service double, no actual company finding"],
                                 "catalysts": [], "risks": [], "next_tasks": [], "scenarios": [],
                                 "fundamental_surprise": {"baseline_type": "historical_proxy", "baseline": 10,
                                                          "observation": 20, "source_ids": [source_id]},
                                 "updated_at": "2026-10-28T08:00:00.200000+08:00"})
        finally:
            store.close()
        return {"metadata": {"provider": "codex_cli", "model": "observed-model-test-double", "success": True,
                              "used_llm": True, "new_research": True, "synthetic": False,
                              "started_at": self.started.isoformat(), "completed_at": "2026-10-28T08:01:00.123456+08:00",
                              "test_double_only": True}, "theses": [], "sources": [], "tasks": [],
                "limitations": ["UNIT TEST DOUBLE: no GPT invocation, no live research or website verification occurred."]}

    def injected_services(self, clock=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(workflow, "load_settings", side_effect=lambda: copy.deepcopy(self.settings)))
        stack.enter_context(patch.object(workflow, "load_runtime", side_effect=lambda _path: copy.deepcopy(self.runtime)))
        stack.enter_context(patch.object(workflow, "strategy_version", return_value=self.version))
        stack.enter_context(patch.object(workflow, "get_snapshot", side_effect=lambda *_args: copy.deepcopy(self.snapshot)))
        research = stack.enter_context(patch.object(workflow, "perform_research", side_effect=self.research_adapter_double))
        calls = 0
        def observed_clock():
            nonlocal calls
            calls += 1
            return self.started if calls <= 2 else self.completed
        stack.enter_context(patch.object(workflow, "taipei_now", side_effect=clock or observed_clock))
        stack.enter_context(patch("requests.get", side_effect=AssertionError("Contract tests must not use the network")))
        stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("Contract tests must not invoke a model")))
        return research

    def test_daily_formal_happy_path_validates_without_submitting_or_settling(self):
        research = self.injected_services()
        summary = workflow.daily(mode="competition", trade_date=self.day, state_dir=self.state, live=True)
        self.assertEqual(research.call_count, 1)
        portfolio = summary["portfolios"]["H2"]
        self.assertEqual(portfolio["status"], "VALIDATED")
        path = Path(portfolio["path"])
        self.assertEqual(path.name, f"D-Plan_TEAM_UNIT_TEST_{self.day.isoformat()}.json")
        document = json.loads(path.read_text(encoding="utf-8"))
        context = json.loads(path.with_suffix(".context.json").read_text(encoding="utf-8"))
        self.assertEqual(schema_errors(document), [])
        self.assertEqual(semantic_errors(document, context)[0], [])
        self.assertEqual(document["agent_metadata"]["model_provider"], "openai")
        self.assertEqual(document["agent_metadata"]["model_version"], "observed-model-test-double")
        self.assertEqual(document["agent_metadata"]["code_version"], self.version)
        started = datetime.fromisoformat(document["agent_metadata"]["run_started_at"])
        completed = datetime.fromisoformat(document["agent_metadata"]["run_completed_at"])
        self.assertEqual(started, self.started)
        self.assertEqual(completed, self.completed)
        self.assertGreater((completed - started).total_seconds(), 60)
        self.assertTrue(document["orders"])
        decisions = {row["decision_id"]: row for row in document["decisions"]}
        for order in document["orders"]:
            decision = decisions[order["decision_ref"]]
            target = target_shares(decision["target_weight"], context["pre_nav"], context["prices"][order["ticker"]])
            delta = target - context["held"].get(order["ticker"], 0)
            self.assertEqual(order["shares"], abs(delta))
            self.assertEqual(order["side"], "BUY" if delta > 0 else "SELL")
        source_ids = {row["source_id"] for row in document["sources"] if "UNIT TEST source double" in row.get("name", "")}
        self.assertTrue(source_ids)
        self.assertTrue(any(source_ids.intersection(row["source_ref"]) for row in document["observations"]))
        self.assertTrue(all(row["fetched_at"].endswith("+08:00") for row in document["sources"] if row.get("fetched_at")))
        record = self.book.get_plan(summary["run_id"])
        self.assertEqual(record["state"], "VALIDATED")
        self.assertIsNone(record["receipt"])
        self.assertEqual(self.book.get_ledger().cash, Decimal(self.before["cash"]))
        self.assertEqual(self.book.get_ledger().positions, {})
        self.assertEqual(self.book.get_ledger().last_settled_date, self.pricing.isoformat())
        self.assertEqual(list(self.state.rglob("forward.sqlite3")), [])
        with sqlite3.connect(self.book.path) as db:
            self.assertEqual(db.execute("SELECT state FROM events ORDER BY id").fetchall(), [("GENERATED",), ("VALIDATED",)])
        repeated = workflow.daily(mode="competition", trade_date=self.day, state_dir=self.state, live=True)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["run_id"], summary["run_id"])
        self.assertEqual(research.call_count, 1)
        self.assertEqual(self.book.get_ledger().cash, Decimal(self.before["cash"]))

    def test_computation_crossing_deadline_does_not_validate_official_plan(self):
        deadline_before = datetime(2026, 10, 28, 8, 54, 59, 500000, tzinfo=TAIPEI)
        deadline_after = datetime(2026, 10, 28, 8, 55, 0, 100000, tzinfo=TAIPEI)
        count = 0
        def clock():
            nonlocal count
            count += 1
            # Initial start, snapshot invocation and post-research decision
            # remain within the window; render/final validation cross it.
            return deadline_before if count <= 3 else deadline_after
        self.injected_services(clock)
        with self.assertRaisesRegex(PlanError, "收件時間窗"):
            workflow.daily(mode="competition", trade_date=self.day, state_dir=self.state, live=True)
        with sqlite3.connect(self.book.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM plans WHERE state='VALIDATED'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE state IN ('SUBMITTED','SETTLED')").fetchone()[0], 0)
        self.assertEqual(self.book.get_ledger().cash, Decimal(self.before["cash"]))
        self.assertEqual(list(self.state.rglob("forward.sqlite3")), [])


if __name__ == "__main__":
    unittest.main()
