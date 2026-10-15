"""離線端到端驗收：合成行情與 Mock，絕不啟動 Codex 或存取外網。"""
from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch

from agent.checks import schema_errors, semantic_errors
from agent.config import load_settings
from agent.evaluation import ForwardTracker
from agent.knowledge import ResearchStore
from agent.llm import MockProvider
from agent.util import PlanError, TAIPEI
from agent.orchestration.market_data import MarketSnapshot
from scripts.create_demo_fixture import build_fixture

workflow = importlib.import_module("agent.orchestration.daily")


def research_fixture(ticker: str, as_of: str) -> tuple[dict, dict]:
    source_id = "SRC_SYNTHETIC_CORPORATE"
    source = {"id": source_id, "url": "https://example.test/synthetic/corporate-test",
              "title": "SYNTHETIC：合成企業測試公告（非真實公司）", "publisher": "合成測試機構",
              "source_type": "corporate", "evidence_tier": "official", "legal_basis": "test_fixture",
              "published_at": "2026-10-14T15:00:00+08:00", "content_as_of": "2026-10-14T15:00:00+08:00",
              "fetched_at": "2026-10-14T18:00:00+08:00", "synthetic": True,
              "content": "SYNTHETIC：本段完全虛構，僅測試研究資料流。合成企業synthetic_process_index：歷史代理基準 10 ，本次觀測 20 ；"
                         "2026-10-19將驗證虛構產線。技術可能性不等於訂單，並保留量產與競爭疑慮。"}
    thesis = {"id": "THESIS_SYNTHETIC", "ticker": ticker,
              "claim": "SYNTHETIC：測試研究假說，合成製程改善值得驗證，不代表真實投資判斷。",
              "source_ids": [source_id], "counter_source_ids": [], "unknowns": ["量產改善尚待驗證"],
              "catalysts": [{"description": "合成產線測試驗證", "expected_at": "2026-10-19T10:00:00+08:00",
                             "status": "tentative", "source_ids": [source_id]}],
              "status": "early_validation", "evidence_strength": "high", "market_recognition": "emerging",
              "risks": [{"description": "合成測試：改善未必轉化為訂單", "source_ids": [], "status": "risk_hypothesis"}],
              "updated_at": as_of, "next_tasks": ["驗證虛構製程指標與量產結果"],
              "scenarios": [{"name": name, "description": f"SYNTHETIC {name}，沒有校準報酬預測",
                             "upside": None, "downside": None, "assumptions": ["僅供軟體測試"],
                             "drivers": [], "evidence_to_upgrade": ["新增可驗證資料"], "invalidation": ["製程改善失敗"]}
                            for name in ("bull", "base", "bear")],
              "fundamental_surprise": {"baseline_type": "historical_proxy", "metric": "synthetic_process_index",
                                       "baseline": 10, "observation": 20, "source_ids": [source_id],
                                       "interpretation": "合成測試代理基準，非市場共識或真實績效"},
              "adversarial_reviewed": False}
    response = {"theses": [thesis], "entities": [
        {"id": "C_SYNTHETIC", "type": "Company", "name": f"合成測試企業{ticker}"},
        {"id": "T_SYNTHETIC", "type": "Technology", "name": "合成測試製程"}],
        "edges": [{"source_id": "C_SYNTHETIC", "target_id": "T_SYNTHETIC", "relation": "POTENTIALLY_BENEFITS_FROM",
                   "source_ids": [source_id], "counter_source_ids": [], "status": "hypothesis", "confidence": "medium"}],
        "tasks": [], "discovery_chains": [{"technology": "合成測試製程",
            "steps": [{"claim": "合成技術改善可能影響製程，訂單尚未確認", "source_ids": [source_id], "status": "hypothesis"}],
            "candidate_tickers": [ticker]}], "limitations": ["SYNTHETIC：此流程僅驗證軟體，不是真實 GPT 研究。"]}
    return source, response


class DailyEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.first = build_fixture()
        self.fixture = self.root / "market.json"
        self.fixture.write_text(json.dumps(self.first, ensure_ascii=False), encoding="utf-8")

    def test_mock_research_daily_forward_settle_and_repeat_are_isolated(self):
        ticker = max(self.first["prepared"]["scores"], key=self.first["prepared"]["scores"].get)
        source, response = research_fixture(ticker, self.first["as_of"])
        sources_path = self.root / "sources.json"
        sources_path.write_text(json.dumps([source], ensure_ascii=False), encoding="utf-8")
        mock = MockProvider([response])
        with patch.object(workflow, "build_provider", return_value=mock), \
                patch("requests.get", side_effect=AssertionError("離線測試不得使用網路")), \
                patch("subprocess.Popen", side_effect=AssertionError("Mock 不得啟動 Codex")):
            summary = workflow.daily(mode="shadow", provider="mock", offline=True, fixture=self.fixture,
                                     sources_path=sources_path, state_dir=self.state)
            repeated = workflow.daily(mode="shadow", provider="mock", offline=True, fixture=self.fixture,
                                      sources_path=sources_path, state_dir=self.state)
        self.assertTrue(repeated["reused"])
        self.assertEqual(summary["run_id"], repeated["run_id"])
        self.assertEqual(set(summary["portfolios"]), {"Q0", "H1", "H2"})
        self.assertEqual(summary["research"]["provider"], "mock")
        self.assertFalse(summary["research"]["used_llm"])
        self.assertGreaterEqual(summary["research"]["rounds_completed"], 1)
        store = ResearchStore(self.state / "research_mock.sqlite3")
        self.addCleanup(store.close)
        self.assertTrue(store.get_thesis("THESIS_SYNTHETIC", self.first["as_of"]))
        self.assertTrue(store.list_sources(self.first["as_of"]))
        self.assertFalse((self.state / "research.sqlite3").exists())
        self.assertTrue(store.query_edges(as_of=self.first["as_of"]))
        settings = load_settings()
        tracker = ForwardTracker(self.state, settings["initial_capital"], settings["commission_rate"],
                                 settings["tax_rate"], summary["strategy_version"],
                                 universe=[row["ticker"] for row in self.first["universe"]])
        for variant, portfolio in summary["portfolios"].items():
            self.assertEqual(portfolio["status"], "FROZEN")
            self.assertEqual(tracker.ledger(variant).positions, {})
            self.assertEqual(float(tracker.ledger(variant).cash), settings["initial_capital"])
            path = Path(portfolio["path"])
            document = json.loads(path.read_text(encoding="utf-8"))
            context = json.loads(path.with_suffix(".context.json").read_text(encoding="utf-8"))
            self.assertEqual(schema_errors(document), [])
            self.assertEqual(semantic_errors(document, context)[0], [])
            self.assertTrue(document["orders"])
            frozen = tracker.frozen(variant, self.first["trade_date"])
            self.assertEqual(frozen["orders"], document["orders"])
            if variant != "Q0":
                self.assertIn("THESIS_SYNTHETIC", frozen["candidates"][[r["ticker"] for r in frozen["candidates"]].index(ticker)]["thesis_ids"])
                self.assertTrue(any(row["url"] == source["url"] for row in document["sources"]))
                evidence_sources = {row["source_id"] for row in document["sources"] if row["url"] == source["url"]}
                self.assertTrue(any(evidence_sources.intersection(row["source_ref"]) for row in document["observations"]))
            if variant == "H2":
                choice = next(row for row in frozen["candidates"] if row["ticker"] == ticker)
                self.assertLessEqual(choice["max_weight"], .02)
                decision = next(row for row in document["decisions"] if row["ticker"] == ticker)
                self.assertLessEqual(decision["target_weight"], choice["max_weight"])
                self.assertTrue(choice["thesis_ids"])
        self.assertEqual({p["status"] for p in tracker.evaluate()["portfolios"]["Q0"]["predictions"]}, {"PENDING"})
        second = build_fixture("2026-10-16")
        second_path = self.root / "market_next.json"
        second_path.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
        with patch("requests.get", side_effect=AssertionError("離線測試不得使用網路")), \
                patch("subprocess.Popen", side_effect=AssertionError("不應啟動 Codex")):
            next_summary = workflow.daily(mode="shadow", provider="mock", offline=True, fixture=second_path,
                                          state_dir=self.state, no_research=True)
        self.assertEqual(next_summary["strategy_version"], summary["strategy_version"])
        result = tracker.evaluate()
        for variant, metrics in result["portfolios"].items():
            self.assertEqual(metrics["settled_sessions"], 1)
            self.assertEqual(metrics["frozen_decisions"], 2)
            self.assertTrue(metrics["positions"])
            self.assertGreater(metrics["trading_cost"], 0)
            self.assertIsNotNone(metrics["benchmark_relative_return"])
            self.assertEqual(metrics["history"][0]["date"], "2026-10-15")
            self.assertTrue(any(p["status"] == "MATURED" for p in metrics["predictions"]))
            self.assertTrue(any(p["horizon_sessions"] == 20 and p["status"] == "PENDING" for p in metrics["predictions"]))
            self.assertEqual(tracker.pending_dates(variant), ["2026-10-16"])
        report = tracker.write_report()
        self.assertTrue(report.is_file())
        official = self.state / "competition"
        self.assertFalse(official.exists())

    def test_partial_failure_resumes_committed_freeze_without_new_research(self):
        ticker = max(self.first["prepared"]["scores"], key=self.first["prepared"]["scores"].get)
        source, response = research_fixture(ticker, self.first["as_of"])
        source_path = self.root / "sources.json"
        source_path.write_text(json.dumps([source], ensure_ascii=False), encoding="utf-8")
        provider = MockProvider([response])
        original_write = workflow.atomic_json
        failed = False

        def fail_after_freeze(path, payload, **kwargs):
            nonlocal failed
            if Path(path).name == "Q0_summary.json" and not failed:
                failed = True
                raise OSError("test checkpoint disk failure after Q0 freeze commit")
            return original_write(path, payload, **kwargs)

        with patch.object(workflow, "build_provider", return_value=provider), \
                patch.object(provider, "generate", wraps=provider.generate) as generate, \
                patch.object(workflow, "atomic_json", side_effect=fail_after_freeze), \
                patch("requests.get", side_effect=AssertionError("離線不得使用網路")), \
                patch("subprocess.Popen", side_effect=AssertionError("禁止啟動 Codex")):
            with self.assertRaisesRegex(OSError, "Q0 freeze"):
                workflow.daily(mode="shadow", provider="mock", offline=True, fixture=self.fixture,
                               sources_path=source_path, state_dir=self.state)
            model_calls = generate.call_count
        self.assertTrue(failed)
        self.assertGreater(model_calls, 0)
        artifact = next(self.state.glob("shadow/sha256_*/2026-10-15"))
        self.assertFalse((artifact / "summary.json").exists())
        self.assertFalse((artifact / "Q0_summary.json").exists())
        original_document = (artifact / "D-Plan_Q0_2026-10-15.json").read_bytes()
        original_research = (artifact / "research.json").read_bytes()
        original_snapshot = (artifact / "snapshot.json").read_bytes()
        version = json.loads(original_document)["agent_metadata"]["code_version"]
        settings = load_settings()
        tracker = ForwardTracker(self.state, settings["initial_capital"], settings["commission_rate"],
                                 settings["tax_rate"], version,
                                 universe=[row["ticker"] for row in self.first["universe"]])
        original_freeze = tracker.frozen("Q0", self.first["trade_date"])
        self.assertIsNotNone(original_freeze)
        self.assertEqual(tracker.pending_dates("H1"), [])
        original_freeze_method = ForwardTracker.freeze
        resumed_variants = []

        def freeze_once(instance, variant, *args, **kwargs):
            self.assertNotEqual(variant, "Q0", "Q0 已提交凍結，恢復不得重新呼叫 freeze")
            resumed_variants.append(variant)
            return original_freeze_method(instance, variant, *args, **kwargs)

        with patch.object(workflow, "perform_research", side_effect=AssertionError("已保存研究不得重跑")), \
                patch.object(workflow, "build_provider", side_effect=AssertionError("不得重新建立模型")), \
                patch.object(ForwardTracker, "freeze", new=freeze_once), \
                patch("requests.get", side_effect=AssertionError("離線不得使用網路")), \
                patch("subprocess.Popen", side_effect=AssertionError("禁止啟動 Codex")):
            result = workflow.daily(mode="shadow", provider="mock", offline=True, fixture=self.fixture,
                                    sources_path=source_path, state_dir=self.state)
        self.assertEqual(resumed_variants, ["H1", "H2"])
        self.assertEqual(tracker.frozen("Q0", self.first["trade_date"]), original_freeze)
        self.assertEqual((artifact / "D-Plan_Q0_2026-10-15.json").read_bytes(), original_document)
        self.assertEqual((artifact / "research.json").read_bytes(), original_research)
        self.assertEqual((artifact / "snapshot.json").read_bytes(), original_snapshot)
        self.assertTrue((artifact / "summary.json").is_file())
        self.assertEqual(set(result["portfolios"]), {"Q0", "H1", "H2"})
        for metrics in tracker.evaluate()["portfolios"].values():
            self.assertEqual(metrics["frozen_decisions"], 1)
            self.assertEqual(metrics["settled_sessions"], 0)
            self.assertEqual(metrics["trading_cost"], 0)
            self.assertEqual(metrics["cash"], settings["initial_capital"])
            self.assertEqual(metrics["positions"], {})

    def test_research_evidence_uses_referenced_version_and_taipei_timestamps(self):
        common = {"id": "SRC_VERSIONED", "extraction_status": "extracted", "title": "合成版本測試",
                  "url": "https://example.test/synthetic/versioned", "authority": "other",
                  "content_as_of": "2026-10-14T05:30:00+00:00", "fetched_at": "2026-10-14T10:00:00+00:00"}
        result = {"sources": [{**common, "version": 1, "content": "當時已知的原始內容"},
                              {**common, "version": 2, "content": "後續更新不得覆蓋歷史引用"}]}
        rows, mapping = workflow._research_evidence(result, {
            "1000": {"source_refs": [{"source_id": "SRC_VERSIONED", "version": 1}]}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "research_SRC_VERSIONED_v1")
        self.assertEqual(datetime.fromisoformat(rows[0]["content_as_of"]), datetime(2026, 10, 14, 13, 30, tzinfo=TAIPEI))
        self.assertEqual(datetime.fromisoformat(rows[0]["fetched_at"]), datetime(2026, 10, 14, 18, 0, tzinfo=TAIPEI))
        self.assertTrue(rows[0]["content_as_of"].endswith("+08:00"))
        self.assertTrue(rows[0]["fetched_at"].endswith("+08:00"))
        self.assertEqual(mapping["1000"][0]["excerpt"], "當時已知的原始內容")
        self.assertNotIn("後續更新", json.dumps(mapping, ensure_ascii=False))

    def test_live_snapshot_preserves_real_completion_time_after_data_cutoff(self):
        # Exercise the live timestamp branch with a local double, without a
        # network request, model process, or a claim of real-market validation.
        snapshot = MarketSnapshot(datetime.fromisoformat(self.first["trade_date"]).date(),
                                  datetime.fromisoformat(self.first["pricing_date"]).date(),
                                  self.first["as_of"], self.first["prepared"], self.first["histories"],
                                  self.first["universe"], synthetic=False)
        started = datetime(2026, 10, 15, 8, 0, tzinfo=TAIPEI)
        tick = 0

        def advancing_clock():
            nonlocal tick
            value = started + timedelta(seconds=tick * 5)
            tick += 1
            return value

        with patch.object(workflow, "get_snapshot", return_value=snapshot), \
                patch.object(workflow, "taipei_now", side_effect=advancing_clock), \
                patch.object(workflow, "perform_research", side_effect=AssertionError("明確跳過研究")), \
                patch("requests.get", side_effect=AssertionError("測試不得使用網路")), \
                patch("subprocess.Popen", side_effect=AssertionError("測試不得啟動模型")):
            result = workflow.daily(mode="shadow", provider="mock", no_research=True,
                                    trade_date=snapshot.trade_date, state_dir=self.state)
        self.assertFalse(result["synthetic"])
        self.assertFalse(result["research"]["used_llm"])
        settings = load_settings()
        tracker = ForwardTracker(self.state, settings["initial_capital"], settings["commission_rate"],
                                 settings["tax_rate"], result["strategy_version"],
                                 universe=[row["ticker"] for row in self.first["universe"]])
        for variant in ("Q0", "H1", "H2"):
            frozen = tracker.frozen(variant, self.first["trade_date"])
            self.assertIsNotNone(frozen)
            document = frozen["extra"]["document"]
            context = frozen["extra"]["context"]
            finished = datetime.fromisoformat(document["agent_metadata"]["run_completed_at"])
            cutoff = datetime.fromisoformat(frozen["as_of"])
            self.assertGreater(finished, cutoff)
            self.assertLess(finished, started + timedelta(minutes=2))
            self.assertEqual(datetime.fromisoformat(document["agent_metadata"]["run_started_at"]), started)
            self.assertEqual(datetime.fromisoformat(context["as_of"]), cutoff)
            self.assertEqual(schema_errors(document), [])
            self.assertEqual(semantic_errors(document, context)[0], [])
            self.assertEqual(tracker.ledger(variant).positions, {})
            self.assertEqual(tracker.pending_dates(variant), [self.first["trade_date"]])

    def test_live_shadow_after_deadline_cannot_freeze_a_decision(self):
        snapshot = MarketSnapshot(datetime.fromisoformat(self.first["trade_date"]).date(),
                                  datetime.fromisoformat(self.first["pricing_date"]).date(),
                                  self.first["as_of"], self.first["prepared"], self.first["histories"],
                                  self.first["universe"], synthetic=False)
        after_deadline = datetime(2026, 10, 15, 9, 1, tzinfo=TAIPEI)
        with patch.object(workflow, "get_snapshot", return_value=snapshot), \
                patch.object(workflow, "taipei_now", return_value=after_deadline), \
                patch.object(workflow, "perform_research", side_effect=AssertionError("明確跳過研究")), \
                patch("requests.get", side_effect=AssertionError("測試不得使用網路")), \
                patch("subprocess.Popen", side_effect=AssertionError("測試不得啟動模型")):
            with self.assertRaisesRegex(PlanError, "截止|08:55"):
                workflow.daily(mode="shadow", provider="mock", no_research=True, state_dir=self.state)
        self.assertFalse(any(self.state.glob("shadow/sha256_*/2026-10-15/summary.json")))
        for database in self.state.glob("shadow/*/forward.sqlite3"):
            import sqlite3
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM freezes").fetchone()[0], 0)

    def test_synthetic_data_is_refused_for_official_competition(self):
        with self.assertRaisesRegex(PlanError, "正式競賽禁止"):
            workflow.daily(mode="competition", provider="mock", offline=True, fixture=self.fixture,
                           state_dir=self.state)
        self.assertFalse(self.state.exists())

    def test_fixture_is_reproducible_point_in_time_and_separate_from_official_data(self):
        self.assertEqual(self.first, build_fixture())
        self.assertTrue(self.first["synthetic"])
        self.assertEqual(len(self.first["universe"]), 150)
        self.assertEqual(sum(row["market"] == "twse" for row in self.first["universe"]), 100)
        self.assertEqual(sum(row["market"] == "tpex" for row in self.first["universe"]), 50)
        second = build_fixture("2026-10-16")
        for ticker, history in self.first["histories"].items():
            self.assertEqual(len(history), 64)
            self.assertEqual(history[-1]["date"], "2026-10-14")
            next_bars = {row["date"]: row for row in second["histories"][ticker]}
            for row in history[1:]:
                self.assertEqual(row, next_bars[row["date"]])


if __name__ == "__main__":
    unittest.main()
