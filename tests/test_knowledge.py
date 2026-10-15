"""離線驗證版本記憶、去重與資訊可得時間；範例資料不代表投資事實。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.knowledge import ResearchStore


def source(key="s1", **changes):
    result = {"id": key, "url": "https://www.twse.com.tw/test/" + key,
              "publisher": "離線測試來源", "title": "製程測試公告", "source_type": "company_announcement",
              "published_at": "2026-10-01T08:00:00+08:00", "content_as_of": "2026-10-01T08:00:00+08:00",
              "fetched_at": "2026-10-01T09:00:00+08:00", "content": "測試企業公告設備驗證中，尚未取得量產訂單。",
              "extraction_status": "extracted", "evidence_tier": "official", "legal_basis": "test_fixture"}
    result.update(changes)
    return result


def thesis(**changes):
    result = {"id": "t1", "ticker": "2330", "claim": "測試企業若通過驗證可能改善收入，尚未確認。",
              "source_ids": ["s1"], "counter_source_ids": [], "unknowns": ["是否通過驗證"],
              "catalysts": [{"event": "待公告驗證結果", "expected_at": None}], "status": "hypothesis",
              "evidence_strength": "low", "market_recognition": "unknown", "risks": ["驗證失敗"],
              "updated_at": "2026-10-01T10:00:00+08:00", "next_tasks": ["尋找反方證據"], "scenarios": []}
    result.update(changes)
    return result


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "research.sqlite3"
        self.store = ResearchStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_source_survives_reopen_and_identical_retry(self):
        first = self.store.put_source(source())
        retried = self.store.put_source(source(fetched_at="2026-10-02T09:00:00+08:00"))
        self.assertEqual(first, retried)
        self.assertEqual(self.store.summary()["source_versions"], 1)
        self.store.close()
        self.store = ResearchStore(self.path)
        self.assertEqual(self.store.get_source("s1")["content"], source()["content"])

    def test_republication_does_not_become_independent_evidence(self):
        original = self.store.put_source(source())
        copy = self.store.put_source(source("s2", publisher="轉載來源", url="https://news.example.org/copy?utm_source=test#story"))
        self.assertEqual(copy["novelty"], "REPUBLICATION")
        self.assertEqual(copy["event_id"], original["event_id"])
        self.assertEqual(copy["duplicate_of"], "s1")
        self.assertNotIn("utm_", copy["url"])
        self.assertEqual(self.store.summary()["independent_events"], 1)

    def test_source_revision_and_historical_query(self):
        self.store.put_source(source())
        new = self.store.put_source(source(content="新增測試：公司公布驗證未通過，原先預期需下修。",
                                           fetched_at="2026-10-02T09:00:00+08:00"))
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["novelty"], "UPDATE")
        self.assertIn("新增", new["delta"])
        self.assertEqual(self.store.get_source("s1", "2026-10-01T10:00:00+08:00")["version"], 1)
        self.assertEqual(len(self.store.source_history("s1")), 2)

    def test_thesis_resolution_keeps_original_source_revision(self):
        original = self.store.put_source(source())
        self.store.upsert_thesis(thesis())
        old_thesis = self.store.get_thesis("t1")
        self.store.put_source(source(content="新的反方內容：驗證已失敗。", fetched_at="2026-10-02T09:00:00+08:00"))
        self.assertEqual(self.store.get_source("s1")["version"], 2)
        self.assertEqual(self.store.resolve_thesis_sources(old_thesis)[0]["content"], original["content"])
        self.assertIsNone(self.store.get_source_version("s1", 2, "2026-10-01"))
        self.store.upsert_thesis(thesis(updated_at="2026-10-02T10:00:00+08:00", status="disputed"))
        revisions = self.store.resolve_thesis_sources([old_thesis, self.store.get_thesis("t1")])
        self.assertEqual([item["version"] for item in revisions], [1, 2])
        # 呼叫方顯式固定版本時，狀態更新不偷偷把引用換成新原文。
        self.store.upsert_thesis(dict(old_thesis, updated_at="2026-10-03T10:00:00+08:00", status="disputed"))
        self.assertEqual(self.store.get_thesis("t1")["source_versions"], {"s1": 1})
        with self.assertRaisesRegex(ValueError, "缺少固定來源版本"):
            self.store.resolve_thesis_sources(thesis())
        with self.assertRaisesRegex(ValueError, "當時尚不可得"):
            self.store.resolve_thesis_sources(dict(old_thesis, source_versions={"s1": 2}))

    def test_late_discovery_is_excluded_from_past_research(self):
        self.store.put_source(source(fetched_at="2026-10-12T09:00:00+08:00"))
        self.assertEqual(self.store.list_sources("2026-10-05"), [])
        self.assertEqual(self.store.get_source("s1")["novelty"], "STALE")

    def test_date_only_cutoff_uses_taipei_day(self):
        self.store.put_source(source(fetched_at="2026-10-02T02:00:00+08:00"))
        self.assertEqual(self.store.list_sources("2026-10-01"), [])
        self.assertEqual(len(self.store.list_sources("2026-10-02")), 1)

    def test_missing_timestamps_unknown_legal_basis_and_bad_hash_fail(self):
        for change in ({"published_at": None}, {"fetched_at": None}, {"content_as_of": "2026-10-02T00:00:00+08:00"},
                       {"fetched_at": "2026-10-01T10:00:00"}, {"legal_basis": "unauthorized"}, {"content_hash": "wrong"},
                       {"url": "file:///etc/passwd"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.put_source(source(**change))
        self.assertEqual(self.store.list_sources(), [])

    def test_existing_source_id_cannot_be_rebound_to_unrelated_url(self):
        self.store.put_source(source())
        with self.assertRaisesRegex(ValueError, "不可改綁"):
            self.store.put_source(source(url="https://news.example.org/unrelated", content="新的不相關文件。",
                                         fetched_at="2026-10-02T09:00:00+08:00"))
        self.assertEqual(self.store.get_source("s1")["version"], 1)

    def test_unavailable_archive_can_preserve_unknown_publication(self):
        archived = self.store.put_source(source(content="", extraction_status="unavailable", published_at=None, content_as_of=None))
        self.assertEqual(archived["novelty"], "UNVERIFIED")
        with self.assertRaisesRegex(ValueError, "不可用來源"):
            self.store.upsert_thesis(thesis())

    def test_rejected_source_archives_only_unavailable_metadata(self):
        rejected = self.store.put_source(source(content="", extraction_status="unavailable", legal_basis="unknown",
                                                published_at=None, content_as_of=None))
        self.assertEqual(rejected["content"], "")
        self.assertEqual(rejected["novelty"], "UNVERIFIED")
        with self.assertRaises(ValueError):
            self.store.put_source(source(extraction_status="unavailable", legal_basis="unauthorized"))

    def test_rumor_cannot_be_upgraded_to_confirmed(self):
        self.store.put_source(source(evidence_tier="rumor"))
        self.assertEqual(self.store.upsert_thesis(thesis(status="hypothesis")), 1)
        with self.assertRaises(ValueError):
            self.store.upsert_thesis(thesis(status="confirmed"))

    def test_thesis_keeps_counter_evidence_versions_and_change_log(self):
        self.store.put_source(source())
        self.assertEqual(self.store.upsert_thesis(thesis()), 1)
        self.assertEqual(self.store.upsert_thesis(thesis()), 1)
        self.store.put_source(source("s2", content="測試反方來源指出驗證失敗。", contradicts_source_ids=["s1"],
                                     fetched_at="2026-10-02T09:00:00+08:00"))
        self.assertEqual(self.store.get_source("s2")["novelty"], "CONTRADICTION")
        self.assertEqual(self.store.upsert_thesis(thesis(status="disputed", counter_source_ids=["s2"],
                                                       updated_at="2026-10-02T10:00:00+08:00")), 2)
        history = self.store.thesis_history("t1")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["counter_source_ids"], [])
        self.assertEqual(history[1]["change_log"][-1]["to_status"], "disputed")
        self.assertEqual(self.store.get_thesis("t1", "2026-10-01T20:00:00+08:00")["status"], "hypothesis")
        self.assertEqual(history[1]["source_versions"], {"s1": 1, "s2": 1})

    def test_future_and_nonexistent_source_refs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.upsert_thesis(thesis())
        self.store.put_source(source(fetched_at="2026-10-02T09:00:00+08:00"))
        with self.assertRaisesRegex(ValueError, "尚不可得"):
            self.store.upsert_thesis(thesis())

    def test_graph_requires_real_entities_and_supported_evidence(self):
        self.store.put_source(source())
        self.store.upsert_entity({"id": "company:test", "type": "Company", "name": "測試公司"})
        self.store.upsert_entity({"id": "tech:test", "type": "Technology", "name": "測試製程"})
        edge = {"source_id": "company:test", "target_id": "tech:test", "relation": "POTENTIALLY_BENEFITS_FROM",
                "status": "hypothesis", "source_ids": ["s1"], "first_seen": "2026-10-01T10:00:00+08:00"}
        edge_id = self.store.upsert_edge(edge)
        self.assertEqual(self.store.upsert_edge(edge), edge_id)
        rows = self.store.query_edges(target_id="tech:test", as_of="2026-10-01T12:00:00+08:00")
        self.assertEqual(rows[0]["version"], 1)
        self.assertIn("非校準", rows[0]["confidence_interpretation"])
        self.assertEqual(self.store.query_edges(as_of="2026-09-30"), [])
        self.store.upsert_edge(dict(edge, status="disputed", updated_at="2026-10-02T10:00:00+08:00"))
        self.assertEqual(self.store.query_edges(status="hypothesis"), [])
        self.assertEqual(len(self.store.query_edges(status="disputed", since="2026-10-02T00:00:00+08:00")), 1)
        with self.assertRaises(ValueError):
            self.store.upsert_edge(dict(edge, status="verified", source_ids=[]))
        with self.assertRaises(ValueError):
            self.store.upsert_edge(dict(edge, target_id="missing"))

    def test_search_excludes_future_and_updated_away_text(self):
        self.store.put_source(source(content="測試製程瓶頸仍未解除。"))
        self.assertEqual(len(self.store.search("製程瓶頸", as_of="2026-10-01T12:00:00+08:00")), 1)
        self.assertEqual(self.store.search("製程瓶頸", as_of="2026-09-30"), [])
        self.store.put_source(source(content="設備驗證已結束。", fetched_at="2026-10-02T09:00:00+08:00"))
        self.assertEqual(self.store.search("製程瓶頸"), [])
        self.assertTrue(self.store.summary()["fts_enabled"])

    def test_task_dedup_and_immutable_model_run(self):
        key = self.store.enqueue_task({"ticker": "2330", "question": "驗證是否完成？"})
        self.assertEqual(key, self.store.enqueue_task({"ticker": "2330", "question": "驗證是否完成？"}))
        self.assertEqual(len(self.store.tasks("pending")), 1)
        self.store.enqueue_task({"id": key, "status": "completed"})
        self.store.enqueue_task({"id": key, "status": "pending"})
        self.assertEqual(self.store.tasks()[0]["status"], "completed")
        self.store.record_model_run({"id": "run1", "provider": "mock", "tokens": None})
        self.store.record_model_run({"id": "run1", "provider": "mock", "tokens": None})
        with self.assertRaises(ValueError):
            self.store.record_model_run({"id": "run1", "provider": "pretend_gpt"})


if __name__ == "__main__":
    unittest.main()
