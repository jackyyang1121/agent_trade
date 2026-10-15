from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.knowledge import ResearchStore
from agent.llm import CodexCLIProvider, LLMError, MockProvider, build_provider
from agent.research import RESEARCH_SCHEMA, ResearchEngine, ResearchError, SourceConnector


AS_OF = "2026-10-15T20:00:00+08:00"


def source(source_id="SOURCE_TEST", **changes):
    item = {
        "id": source_id, "url": "https://example.org/authorized-fixture",
        "publisher": "明確標示的測試機構（非真實財訊）", "title": "SYNTHETIC 示範研究文件",
        "source_type": "corporate", "published_at": "2026-10-15T10:00:00+08:00",
        "content_as_of": "2026-10-15T10:00:00+08:00", "fetched_at": "2026-10-15T11:00:00+08:00",
        "content": "SYNTHETIC：公司測試產線尚未完成驗證，沒有已確認的客戶訂單。",
        "extraction_status": "extracted", "evidence_tier": "official", "legal_basis": "test_fixture",
        "synthetic": True,
    }
    return {**item, **changes}


def response():
    return {"theses": [], "entities": [], "edges": [], "tasks": [], "discovery_chains": [], "limitations": []}


def thesis():
    return {
        "id": "THESIS_SYNTHETIC_2330", "ticker": "2330",
        "claim": "SYNTHETIC：若測試產線通過驗證，可能改善製程；不代表已取得訂單。",
        "source_ids": ["SOURCE_TEST"], "counter_source_ids": [], "unknowns": ["尚無訂單驗證"],
        "catalysts": [{"description": "可能的驗證進度", "expected_at": None, "status": "unknown", "source_ids": ["SOURCE_TEST"]}],
        "status": "early_validation", "evidence_strength": "medium", "market_recognition": "unknown",
        "risks": [{"description": "驗證可能延遲（待證偽的風險假說）", "source_ids": [], "status": "risk_hypothesis"}],
        "updated_at": AS_OF, "next_tasks": ["確認是否完成驗證"],
        "scenarios": [{"name": name, "description": "不預測精確報酬或成功機率", "upside": None, "downside": None,
                       "assumptions": ["仍需真實證據"], "drivers": ["驗證結果"],
                       "evidence_to_upgrade": ["正式公告"], "invalidation": ["驗證失敗"]}
                      for name in ("bull", "base", "bear")],
        "fundamental_surprise": {"baseline_type": "unknown", "metric": "", "baseline": None, "observation": None,
                                 "source_ids": [], "interpretation": "無可驗證市場共識或代理基準"},
        "adversarial_reviewed": False,
    }


class ResearchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ResearchStore(Path(self.directory.name) / "research.sqlite")
        self.addCleanup(self.store.close)

    def engine(self, responses=None, **config):
        return ResearchEngine(self.store, MockProvider(responses), {"max_rounds": 3, **config})

    def test_mock_never_launches_codex_or_network(self):
        with patch("subprocess.Popen", side_effect=AssertionError("recursive Codex")), \
             patch("requests.get", side_effect=AssertionError("network")):
            output = self.engine().run(["2330"], {}, AS_OF, [source()])
        self.assertFalse(output["metadata"]["used_llm"])
        self.assertTrue(output["metadata"]["synthetic"])
        self.assertEqual(output["metadata"]["model_calls"], 3)
        self.assertEqual(output["theses"], [])
        self.assertEqual(len(self.store.model_runs()), 3)

    def test_research_adversarial_history_and_evidence(self):
        first = response()
        first["theses"] = [thesis()]
        reviewed = copy.deepcopy(first)
        reviewed["theses"][0]["unknowns"].append("反方檢查：目前沒有驗證完成證據")
        output = self.engine([first, first, reviewed]).run(["2330", "2454"], {}, AS_OF, [source()])
        saved = output["theses"][0]
        self.assertTrue(saved["adversarial_reviewed"])
        self.assertTrue(saved["synthetic"])
        self.assertEqual(saved["source_ids"], ["SOURCE_TEST"])
        self.assertEqual(saved["fundamental_surprise"]["baseline_type"], "unknown")
        self.assertGreaterEqual(len(self.store.thesis_history(saved["id"])), 2)
        self.assertEqual(saved["catalyst_proximity"]["horizons"]["unknown"], ["可能的驗證進度"])

    def test_unknown_source_rejected_without_thesis_mutation(self):
        data = response()
        data["theses"] = [thesis()]
        data["theses"][0]["source_ids"] = ["INVENTED"]
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [source()])
        self.assertEqual(output["theses"], [])
        self.assertFalse(output["metadata"]["success"])
        self.assertEqual(output["metadata"]["provider"], "mock")
        self.assertTrue(output["metadata"]["synthetic"])
        self.assertIn("研究引用不存在", " ".join(output["limitations"]))
        self.assertEqual(self.store.model_runs()[0]["status"], "rejected")

    def test_store_rejected_replacement_never_reaches_model(self):
        self.store.put_source(SourceConnector().ingest(source(), AS_OF))
        forged = source(content="FORGED_REJECTED_BODY", fetched_at="2026-10-15T10:30:00+08:00")
        engine = self.engine(max_rounds=1)
        with patch.object(engine.provider, "generate", wraps=engine.provider.generate) as generate:
            output = engine.run(["2330"], {}, AS_OF, [forged])
        self.assertNotIn("FORGED_REJECTED_BODY", generate.call_args.args[0])
        self.assertIn("source_archive_rejected", " ".join(output["limitations"]))

    def test_nested_source_refs_must_be_pinned_at_root(self):
        data = response()
        data["theses"] = [thesis()]
        data["theses"][0]["risks"] = [{"description": "另一個來源的反證", "source_ids": ["COUNTER"], "status": "evidenced"}]
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [source(), source("COUNTER")])
        self.assertEqual(output["theses"], [])
        self.assertIn("固定版本", " ".join(output["limitations"]))

    def test_reusing_archived_new_label_is_not_new_information_again(self):
        data = response()
        data["theses"] = [thesis()]
        first = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [source()])
        self.assertEqual(first["theses"][0]["information_novelty"], "NEW")
        second = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [source()])
        self.assertEqual(second["theses"][0]["information_novelty"], "REPUBLICATION")

    def test_rumor_cannot_become_confirmed_thesis(self):
        data = response()
        data["theses"] = [thesis()]
        data["theses"][0]["status"] = "confirmed_acceleration"
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [source(evidence_tier="rumor")])
        self.assertEqual(output["theses"], [])
        self.assertIn("不能冒充確認事實", " ".join(output["limitations"]))

    def test_no_consensus_cannot_receive_invented_numbers(self):
        data = response()
        data["theses"] = [thesis()]
        data["theses"][0]["fundamental_surprise"]["observation"] = 8
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [source()])
        self.assertEqual(output["theses"], [])
        self.assertIn("不可捏造預期差", " ".join(output["limitations"]))

    def test_real_source_id_does_not_validate_invented_eps_numbers(self):
        data = response()
        data["theses"] = [thesis()]
        data["theses"][0]["fundamental_surprise"] = {
            "baseline_type": "company_guidance", "metric": "EPS", "baseline": 8, "observation": 87,
            "source_ids": ["SOURCE_TEST"], "interpretation": "公司指引代理，非市場共識",
        }
        src = source(content="SYNTHETIC：公司 EPS 原指引 8 元，本期修訂為 9 元。")
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [src])
        self.assertEqual(output["theses"], [])
        self.assertIn("數字未能", " ".join(output["limitations"]))
        data["theses"][0]["fundamental_surprise"]["observation"] = 9
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [src])
        self.assertEqual(len(output["theses"]), 1)

    def test_company_guidance_cannot_impersonate_consensus(self):
        data = response()
        data["theses"] = [thesis()]
        data["theses"][0]["fundamental_surprise"] = {
            "baseline_type": "consensus", "metric": "EPS", "baseline": 8, "observation": 9,
            "source_ids": ["SOURCE_TEST"], "interpretation": "錯誤的共識標籤",
        }
        src = source(content="SYNTHETIC：公司 EPS 原指引 8 元，本期修訂為 9 元。")
        output = self.engine([data], max_rounds=1).run(["2330"], {}, AS_OF, [src])
        self.assertEqual(output["theses"], [])
        self.assertIn("不能偽裝", " ".join(output["limitations"]))

    def test_surprise_grounding_supports_chinese_number_units(self):
        values = {"SOURCE_TEST": source(content="SYNTHETIC：公司原指引EPS8元，本期修訂EPS9元。")}
        ResearchEngine._ground_surprise({"baseline_type": "company_guidance", "metric": "EPS",
                                         "baseline": 8, "observation": 9, "source_ids": ["SOURCE_TEST"]}, values)
        with self.assertRaises(ResearchError):
            ResearchEngine._ground_surprise({"baseline_type": "company_guidance", "metric": "EPS",
                                             "baseline": 8, "observation": 87, "source_ids": ["SOURCE_TEST"]}, values)

    def test_forward_feedback_is_in_prompt_as_historical_experiment(self):
        feedback = {"H2": {"nav": 100, "mdd": 0.02, "missed_opportunities": [], "predictions": []}}
        engine = self.engine(max_rounds=1, forward_feedback=feedback)
        with patch.object(engine.provider, "generate", wraps=engine.provider.generate) as generate:
            engine.run(["2330"], {}, AS_OF, [])
        prompt = generate.call_args.args[0]
        self.assertIn('"forward_feedback":{"H2":', prompt)
        self.assertIn("不得據此改寫過去假說", prompt)

    def test_reverse_research_preserves_unknown_explanation(self):
        output = self.engine(max_rounds=0).run(
            ["2330", "2454"], {"2454": {"valid": True, "relative_strength_20": 0.1, "volume_surge": 0.8}}, AS_OF, []
        )
        self.assertEqual(output["tasks"][0]["ticker"], "2454")
        self.assertIn("尚無解釋", output["tasks"][0]["reason"])
        self.assertEqual(output["tasks"][0]["status"], "unavailable")
        self.assertEqual(output["theses"], [])

    def test_followup_search_fetch_and_multiround_are_executed(self):
        first = response()
        first["tasks"] = [{"kind": "search", "query": "非科技業製程瓶頸", "ticker": None, "url": None,
                           "reason": "從技術需求推導受惠公司", "source_ids": []}]
        engine = self.engine([first, response()], max_rounds=2)
        with patch.object(engine.search, "search", return_value=([source()], None)) as search:
            output = engine.run(["2330"], {}, AS_OF, [])
        search.assert_called_once()
        self.assertEqual(output["tasks"][0]["status"], "completed")
        self.assertEqual(output["metadata"]["sources_available"], 1)
        self.assertEqual(output["metadata"]["rounds_completed"], 2)

    def test_new_sources_are_not_fetched_after_final_round(self):
        data = response()
        data["tasks"] = [{"kind": "fetch", "query": "追蹤", "ticker": "2330", "url": "https://example.org/future",
                          "reason": "下一輪驗證", "source_ids": []}]
        engine = self.engine([data], max_rounds=1)
        with patch.object(engine.connector, "fetch", side_effect=AssertionError("post-decision fetch")):
            output = engine.run(["2330"], {}, AS_OF, [])
        self.assertEqual(output["tasks"][0]["status"], "pending")

    def test_research_cancellation_and_zero_call_budget(self):
        provider = MockProvider()
        cancelled = threading.Event()
        cancelled.set()
        output = ResearchEngine(self.store, provider).run(["2330"], {}, AS_OF, [], cancel_event=cancelled)
        self.assertEqual(provider.calls, 0)
        self.assertFalse(output["metadata"]["success"])
        output = self.engine(max_model_calls=0).run(["2330"], {}, AS_OF, [])
        self.assertEqual(output["metadata"]["model_calls"], 0)

    def test_prompt_budget_keeps_150_tickers_without_quadratic_correlations(self):
        tickers = [str(2000 + i) for i in range(150)]
        quant = {ticker: {"as_of": AS_OF, "score": 0.2, "momentum_20": 0.03,
                          "correlations": {other: 0.5 for other in tickers}} for ticker in tickers}
        compact = ResearchEngine._compact_quant(quant, AS_OF)
        self.assertEqual(set(compact), set(tickers))
        self.assertLess(len(json.dumps(compact)), 40000)
        self.assertNotIn("correlations", json.dumps(compact))
        output = self.engine(max_rounds=1).run(tickers, quant, AS_OF, [])
        self.assertEqual(output["metadata"]["rounds_completed"], 1)

    def test_quant_future_cutoff_rejected(self):
        with self.assertRaises(ResearchError):
            ResearchEngine._compact_quant({"2330": {"as_of": "2026-10-16T01:00:00+08:00"}}, AS_OF)

    def test_dates_and_outside_universe_rejected(self):
        with self.assertRaises(ValueError):
            self.engine().run(["2330"], {}, "2026-10-15", [])
        with self.assertRaises(ResearchError):
            self.engine().run(["2330"], {}, AS_OF, [], ticker="NOT_IN_POOL")


class SourcesTest(unittest.TestCase):
    def test_missing_dates_future_and_inaccessible_are_unavailable(self):
        connector = SourceConnector()
        for manifest in (source(published_at=None), source(content_as_of="2026-10-16T00:00:00+08:00"),
                         source(content=""), source(legal_basis="unknown"), source(suspected_nonpublic=True)):
            item = connector.ingest(manifest, AS_OF)
            self.assertEqual(item["extraction_status"], "unavailable")
            self.assertEqual(item["content"], "")

    def test_source_hash_integrity_is_preserved(self):
        item = SourceConnector().ingest(source(content_hash="untrusted-wrong-hash"), AS_OF)
        self.assertEqual(item["extraction_status"], "unavailable")
        self.assertIn("Hash", item["error"])

    def test_source_file_requires_explicit_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "licensed.md"
            path.write_text("使用者授權的技術文件，不代表已商業化。", encoding="utf-8")
            manifest = source(content="", path=str(path))
            denied = SourceConnector().ingest(manifest, "2099-01-01T00:00:00Z")
            self.assertEqual(denied["extraction_status"], "unavailable")
            allowed = SourceConnector({"source_roots": [directory]}).ingest(manifest, "2099-01-01T00:00:00Z")
            self.assertEqual(allowed["extraction_status"], "extracted")
            self.assertIn("技術文件", allowed["content"])

    def test_private_network_and_untrusted_file_paths_are_blocked(self):
        connector = SourceConnector({"allow_network": True, "allowed_domains": ["localhost"]})
        addresses = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=addresses), \
             patch("requests.get", side_effect=AssertionError("SSRF")):
            item = connector.ingest(source(url="https://localhost/credentials", content=""), AS_OF)
        self.assertEqual(item["extraction_status"], "unavailable")
        self.assertIn("禁止存取", item["error"])

    def test_supplied_content_still_rejects_non_https_source_url(self):
        item = SourceConnector().ingest(source(url="file:///etc/passwd"), AS_OF)
        self.assertEqual(item["extraction_status"], "unavailable")
        self.assertEqual(item["content"], "")

    def test_live_as_of_uses_observed_now_not_future(self):
        connector = SourceConnector({"live_as_of": True})
        item = connector.ingest(source(), "2026-10-01T00:00:00Z")
        self.assertEqual(item["extraction_status"], "extracted")
        item = connector.ingest(source(fetched_at="2099-01-01T00:00:00Z"), AS_OF)
        self.assertEqual(item["extraction_status"], "unavailable")


class ProviderSafetyTest(unittest.TestCase):
    def test_live_requires_explicit_opt_in_and_mock_default_metadata(self):
        with patch.object(CodexCLIProvider, "doctor", side_effect=AssertionError("must not invoke CLI")):
            with self.assertRaises(LLMError):
                CodexCLIProvider().generate("test", RESEARCH_SCHEMA)
        result = build_provider({"provider": "mock"}).generate("test", RESEARCH_SCHEMA)
        self.assertIsNone(result.metadata["model"])
        self.assertIsNone(result.metadata["usage"])
        with self.assertRaises(LLMError):
            build_provider({"provider": "unsupported"})

    def test_safe_command_no_shell_permissions_and_no_raw_source_argument(self):
        provider = CodexCLIProvider(model="user model; $(untrusted)")
        command = provider._command("/bin/codex", Path("/tmp/research-isolated"))
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--strict-config", command)
        self.assertNotIn("--full-auto", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[-1], "-")
        self.assertIn("user model; $(untrusted)", command)
        self.assertNotIn('model_reasoning_effort="xhigh"', command)
        high = CodexCLIProvider(reasoning_effort="xhigh")._command("/bin/codex", Path("/tmp/research-isolated"))
        self.assertIn('model_reasoning_effort="xhigh"', high)
        with self.assertRaises(LLMError):
            CodexCLIProvider(reasoning_effort="maximum")
        with patch.dict("os.environ", {"BROKER_SECRET": "not-for-child", "OPENAI_API_KEY": "not-for-child"}):
            environment = provider._environment()
        self.assertNotIn("BROKER_SECRET", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_capability_checks_fail_closed_without_inference(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(command, 0, "codex-cli 99.0.0", "")
            if command[1:] == ["login", "status"]:
                return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch("shutil.which", return_value="/bin/codex"), patch("subprocess.run", side_effect=run):
            status = CodexCLIProvider().doctor()
        self.assertTrue(status["authenticated"])
        self.assertFalse(status["safe"])
        self.assertFalse(status["reviewed_version"])
        self.assertEqual(len(commands), 4)
        self.assertNotIn(["/bin/codex", "exec"], commands)

    def test_real_metadata_only_and_forbidden_tool_events_rejected(self):
        events = '\n'.join([
            json.dumps({"type": "thread.started", "thread_id": "actual-test-thread"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 6}}),
        ])
        metadata = CodexCLIProvider._metadata(events)
        self.assertEqual(metadata["usage"]["input_tokens"], 17)
        self.assertIsNone(metadata["model"])
        with self.assertRaises(LLMError):
            CodexCLIProvider._metadata(json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}))

    def test_subprocess_timeout_covers_child_not_reading_stdin(self):
        # This starts a harmless Python sleeper, never a real model process.
        provider = CodexCLIProvider(timeout_seconds=0.15)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LLMError, "逾時"):
                provider._invoke([sys.executable, "-c", "import time; time.sleep(10)"], "x" * 500000,
                                 Path(directory), None)

    def test_fast_child_cannot_evade_output_budget(self):
        provider = CodexCLIProvider(max_output_bytes=1024)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LLMError, "超過預算"):
                provider._invoke([sys.executable, "-c", "print('x'*10000)"], "", Path(directory), None)


if __name__ == "__main__":
    unittest.main()
