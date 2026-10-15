"""來源、模型與行情使用明確的服務替身，不在測試啟動真實 Codex。"""
from __future__ import annotations

import copy
import importlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent.config import active_schema_version
from agent.datapack import parse_datapack, simulated_initial_pack
from agent.llm import CodexCLIProvider, LLMResult
from agent.orchestration.configuration import load_runtime
from agent.orchestration.outputs import clean_unresearched_shadow
from agent.research.benchmarks import refresh, _ground
from agent.research.discovery import discover
from agent.research.sources import SourceConnector
from agent.util import PlanError, TAIPEI

workflow = importlib.import_module('agent.orchestration.daily')
formal = importlib.import_module('tests.test_daily_competition')


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.services = formal.DailyCompetitionContractTest()
        self.services.setUp()
        self.addCleanup(self.services.doCleanups)
        self.root = self.services.root
        self.state = self.services.state

    def services_after_deadline(self):
        now = datetime(2026, 10, 28, 15, 0, tzinfo=TAIPEI)
        tick = 0
        def clock():
            nonlocal tick
            tick += 1
            return now + timedelta(seconds=tick)
        return self.services.injected_services(clock)

    def test_v43_pattern_schema_and_real_research_rehearsal_after_deadline(self):
        self.assertEqual(active_schema_version(), '4.3')
        research = self.services_after_deadline()
        summary = workflow.daily(mode='rehearsal', trade_date=self.services.day, state_dir=self.state,
                                 provider='codex_cli', live=True, skip_benchmarks=True)
        self.assertEqual(research.call_count, 1)
        self.assertTrue(summary['account_simulated'])
        self.assertFalse(summary['market_synthetic'])
        self.assertFalse(summary['submit_ready'])
        self.assertTrue(summary['research']['new_research'])
        self.assertEqual(list((self.state / 'rehearsal').rglob('forward.sqlite3')), [])
        self.assertEqual(list((self.state / 'rehearsal').rglob('competition.sqlite3')), [])
        pack = json.loads(Path(summary['datapack']).read_text(encoding='utf-8'))
        self.assertEqual(pack['as_of_date'], None)
        self.assertEqual(pack['account']['cash'], 1000000000)
        with self.assertRaisesRegex(PlanError, '模擬 DataPack'):
            parse_datapack(pack, self.services.pricing)
        report = Path(summary['report'])
        self.assertIn('演練', str(report))
        self.assertIn('UNIT TEST DOUBLE ONLY', report.read_text(encoding='utf-8'))
        path = Path(summary['portfolios']['H2']['path'])
        document = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(path.name, f"D-Plan_{document['team_id']}_{document['trade_date']}.json")
        self.assertIn('禁止上傳', document['_NOTE'])
        self.assertEqual(document['schema_version'], '4.3')
        self.assertEqual(summary['portfolios']['H2']['status'], 'REHEARSAL')
        self.assertTrue(any('研究假說' in i['logic'] and 'UNIT TEST DOUBLE ONLY' in i['logic'] for i in document['inferences']))
        self.assertIn('SOURCE_UNIT_TEST_DOUBLE', report.read_text(encoding='utf-8'))
        repeated = workflow.daily(mode='rehearsal', trade_date=self.services.day, state_dir=self.state,
                                  provider='codex_cli', live=True, skip_benchmarks=True)
        self.assertNotEqual(repeated['run_id'], summary['run_id'])
        self.assertEqual(research.call_count, 2)

    def test_failed_live_research_never_falls_back_to_quant_report(self):
        research = self.services_after_deadline()
        research.side_effect = lambda *a, **kw: {'theses': [], 'sources': [], 'tasks': [],
                                               'metadata': {'success': False, 'used_llm': False},
                                               'limitations': ['test unavailable source']}
        for mode in ('rehearsal', 'shadow'):
            with self.assertRaisesRegex(PlanError, '真實研究失敗.*test unavailable source'):
                workflow.daily(mode=mode, trade_date=self.services.day, state_dir=self.state,
                               provider='codex_cli', live=True, skip_benchmarks=True)
        self.assertEqual(list(self.state.rglob('D-Plan_*.json')), [])
        self.assertTrue(list(self.state.rglob('research_failure.json')))
        self.assertEqual(list((self.state / 'reports').rglob('*.json')), [])

    def test_rehearsal_does_not_accept_synthetic_market_or_mock(self):
        for arguments in ({'provider': 'mock', 'live': True}, {'provider': 'codex_cli'},
                          {'provider': 'codex_cli', 'live': True, 'offline': True}):
            with self.assertRaisesRegex(PlanError, '演練需'):
                workflow.daily(mode='rehearsal', state_dir=self.root / 'unused', **arguments)
        self.assertFalse((self.root / 'unused').exists())


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_search_tool_is_explicitly_limited_to_discovery(self):
        commands = CodexCLIProvider(web_search=True)._command('/bin/codex', self.root)
        self.assertIn('web_search="live"', commands)
        self.assertIn('shell_tool', commands)
        self.assertIn('read-only', commands)
        event = json.dumps({'type': 'item.completed', 'item': {'type': 'web_search', 'action': {'url': 'https://www.twse.com.tw/'}}})
        with self.assertRaises(Exception):
            CodexCLIProvider._metadata(event)
        self.assertEqual(CodexCLIProvider._metadata(event, allow_web_search=True)['web_search_calls'], 1)
        with self.assertRaises(Exception):
            CodexCLIProvider._metadata(json.dumps({'item': {'type': 'command_execution'}}), allow_web_search=True)

    def test_model_urls_without_search_events_are_rejected(self):
        runtime = load_runtime()
        runtime['llm']['allow_live'] = True
        with patch('agent.research.discovery.build_provider') as provider:
            provider.return_value.generate.return_value = LLMResult({'documents': [], 'limitations': []}, {'web_search_calls': 0})
            with self.assertRaisesRegex(RuntimeError, '實際 web search'):
                discover(runtime['llm'], runtime['research'], ['2330'], {}, '2026-10-28T07:00:00+08:00')

    def test_fetched_page_date_overrides_model_claim_and_missing_date_is_rejected(self):
        manifest = {'url': 'https://www.twse.com.tw/news', 'title': 'test', 'legal_basis': 'public',
                    'source_type': 'corporate', 'evidence_tier': 'expert', 'published_at': '2026-10-20T00:00:00+08:00',
                    'discovery_method': 'codex_native_web_search_then_python_fetch'}
        connector = SourceConnector()
        body = b'<html><meta property="article:published_time" content="2026-10-27T10:00:00+08:00"><p>Verified test content 123</p></html>'
        with patch.object(connector, 'fetch', return_value=(body, 'text/html', manifest['url'])), \
             patch('agent.research.sources.utc_now', return_value='2026-10-28T07:00:00+08:00'):
            source = connector.ingest(manifest, '2099-01-01T00:00:00+08:00')
        self.assertEqual(source['published_at'], '2026-10-27T10:00:00+08:00')
        with patch.object(connector, 'fetch', return_value=(b'<html><p>No date</p></html>', 'text/html', manifest['url'])), \
             patch('agent.research.sources.utc_now', return_value='2026-10-28T07:00:00+08:00'):
            source = connector.ingest(manifest, '2099-01-01T00:00:00+08:00')
        self.assertEqual(source['extraction_status'], 'unavailable')
        self.assertEqual(source['content'], '')

    def test_benchmark_refresh_requires_all_ten_and_never_overwrites_on_failure(self):
        runtime = load_runtime()
        reference = self.root / 'etfs.csv'
        reference.write_text('ticker,name,market\n' + ''.join(f'ETF{i},Test ETF {i},twse\n' for i in range(11)), encoding='utf-8')
        runtime['competition'].update(official_etf_list_path=str(reference), benchmark_etf_limit=10)
        rows = [{'ticker': str(2000+i), 'weight_pct': 5 - i * .1} for i in range(15)]
        text = ' '.join(f'ETF{i}' for i in range(10)) + '\n2026/10/27\n' + '\n'.join(f"{r['ticker']} {r['weight_pct']:.2f}%" for r in rows)
        response = {'etfs': [{'etf_id': f'ETF{i}', 'source_url': 'https://www.twse.com.tw/test',
                              'as_of': '2026-10-27', 'holdings': rows} for i in range(10)], 'limitations': []}
        path = self.root / 'DataPack' / 'benchmarks.csv'
        with patch('agent.research.benchmarks.build_provider') as provider, \
             patch.object(SourceConnector, 'document', return_value=(text, '2026-10-28T07:00:00+08:00')):
            provider.return_value.generate.return_value = LLMResult(response, {'web_search_calls': 10})
            result = refresh(runtime, path, date(2026, 10, 27))
            self.assertEqual(result['etfs'], 10)
            self.assertEqual(result['holdings'], 150)
            self.assertIn('source_url,as_of,fetched_at', path.read_text(encoding='utf-8'))
            original = path.read_bytes()
            incomplete = copy.deepcopy(response)
            incomplete['etfs'].pop()
            provider.return_value.generate.return_value = LLMResult(incomplete, {'web_search_calls': 9})
            with self.assertRaisesRegex(PlanError, '保留既有.*ETF9'):
                refresh(runtime, path, date(2026, 10, 27))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list((path.parent / 'benchmark_updates').glob('*.json'))), 2)

    def test_fabricated_weight_or_date_is_not_grounded(self):
        with self.assertRaisesRegex(PlanError, '權重'):
            _ground([{'ticker': '2330', 'weight_pct': 20.0}], '2026/10/27 2330 18.0%', '2026-10-27')
        with self.assertRaisesRegex(PlanError, '日期'):
            _ground([{'ticker': '2330', 'weight_pct': 20.0}], '2026/10/26 2330 20.0%', '2026-10-27')
        with self.assertRaisesRegex(PlanError, '權重'):
            _ground([{'ticker': '2330', 'weight_pct': 20.0}, {'ticker': '2300', 'weight_pct': 18.0}],
                    '2026/10/27 2330 18.0%\n2300 20.0%', '2026-10-27')

    def test_cleanup_removes_only_identified_unresearched_outputs(self):
        root = self.root / 'shadow'
        for name, researched in [('old', False), ('live', True)]:
            folder = root / name
            folder.mkdir(parents=True)
            (folder / 'summary.json').write_text(json.dumps({'mode': 'shadow', 'research': {'used_llm': researched}}))
            (folder / 'D-Plan_Q0.json').write_text('{}')
            (folder / 'forward.sqlite3').write_bytes(b'preserve ledger')
            (folder / 'snapshot.json').write_text('{}')
        result = clean_unresearched_shadow(root)
        self.assertEqual(len(result['removed']), 1)
        self.assertFalse((root / 'old' / 'D-Plan_Q0.json').exists())
        self.assertTrue((root / 'old' / 'CLEANED.json').exists())
        self.assertTrue((root / 'live' / 'D-Plan_Q0.json').exists())
        self.assertEqual((root / 'old' / 'forward.sqlite3').read_bytes(), b'preserve ledger')
        self.assertTrue((root / 'old' / 'snapshot.json').exists())
        with self.assertRaisesRegex(PlanError, 'shadow'):
            clean_unresearched_shadow(self.root)

    def test_simulated_datapack_cannot_enter_official_book(self):
        from agent.compliance import CompetitionBook
        pack = simulated_initial_pack('TEAM_TEST', date(2026, 10, 28), datetime(2026, 10, 28, 7, tzinfo=TAIPEI), 1000000000)
        book = CompetitionBook(self.root / 'competition')
        with self.assertRaisesRegex(PlanError, '模擬 DataPack'):
            book.reconcile(pack, date(2026, 10, 27))
        self.assertFalse(book.get_ledger().reconciled)


if __name__ == '__main__':
    unittest.main()
