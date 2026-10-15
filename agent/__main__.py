from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from agent.util import ROOT, PlanError, parse_date, taipei_now, stamp


def _common(p):
    p.add_argument('--config', type=Path, help='runtime YAML 設定路徑')
    p.add_argument('--state-dir', type=Path, help='狀態及報告目錄（預設 data/runtime）')


def _research_args(p):
    p.add_argument('--provider', choices=['codex_cli', 'mock', 'openai_api'])
    p.add_argument('--model')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--sources', type=Path, help='具有來源 metadata 的 JSON list')
    p.add_argument('--live', action='store_true', help='明確允許真實模型及受控來源網路存取')


def parser():
    p = argparse.ArgumentParser(description='證據驅動雙引擎研究、前瞻實驗與競賽決策')
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('daily', 'run'):
        q = sub.add_parser(name, help='每日流程；run 為 daily shadow 相容入口')
        _common(q); _research_args(q)
        q.add_argument('--mode', choices=['shadow', 'competition', 'personal'], default='shadow')
        q.add_argument('--date', type=parse_date)
        q.add_argument('--fixture', type=Path)
        q.add_argument('--dry-run', action='store_true')
        q.add_argument('--no-research', action='store_true')
    q = sub.add_parser('research', help='有預算限制的產業／公司研究')
    _common(q); _research_args(q); q.add_argument('--ticker')
    q = sub.add_parser('knowledge', help='圖譜及研究資料庫查詢')
    _common(q)
    q.add_argument('--query'); q.add_argument('--technology'); q.add_argument('--ticker')
    q.add_argument('--synthetic', action='store_true', help='只查看隔離的Mock研究庫')
    for name in ('status', 'evaluate'):
        q = sub.add_parser(name); _common(q)
        if name == 'evaluate':
            q.add_argument('--version')
    q = sub.add_parser('check', help='Schema及引用鏈驗證；附context可重算交易合規')
    q.add_argument('path', type=Path); q.add_argument('--context', type=Path)
    for name in ('import-universe', 'import-benchmarks'):
        q = sub.add_parser(name); _common(q)
        q.add_argument('path', type=Path)
        q.add_argument('--source-url', required=True)
        q.add_argument('--as-of', required=True)
        q.add_argument('--confirm', action='store_true', help='僅在已核對官方完整資料後設定')
        q.add_argument('--definition', choices=['top10_normalized', 'top10_raw', 'top15_normalized', 'top15_raw', 'full_normalized'])
    q = sub.add_parser('import-book', help='匯入主辦實際結算庫存')
    _common(q); q.add_argument('path', type=Path)
    q.add_argument('--pricing-date', type=parse_date, required=True)
    q = sub.add_parser('mark-submitted', help='手動上傳且收到成功收件證據後記錄狀態')
    _common(q); q.add_argument('run_id'); q.add_argument('--receipt', required=True)
    return p


def execute(args):
    from agent.config import load_settings
    from agent.orchestration.configuration import load_runtime
    from agent.orchestration.daily import daily, load_sources, perform_research, state_path
    if args.cmd in {'daily', 'run'}:
        return daily(mode=args.mode, trade_date=args.date, runtime_path=args.config, state_dir=args.state_dir,
                     provider=args.provider, model=args.model, offline=args.offline, fixture=args.fixture,
                     sources_path=args.sources, live=args.live, no_research=args.no_research, dry_run=args.dry_run)
    if args.cmd == 'check':
        from agent.checks import schema_errors, semantic_errors
        from agent.compliance import structural_errors
        document = json.loads(args.path.read_text(encoding='utf-8'))
        errors = schema_errors(document)
        context_path = args.context or args.path.with_suffix('.context.json')
        warnings = []
        if not errors:
            errors = structural_errors(document)
        if not errors and context_path.exists():
            errors, warnings = semantic_errors(document, json.loads(context_path.read_text(encoding='utf-8')))
        if errors:
            raise PlanError('\n'.join(errors))
        return {'path': str(args.path), 'schema_and_references': 'passed', 'full_semantics': context_path.exists(),
                'warnings': warnings + ([] if context_path.exists() else ['無帳本context，未驗證股數/現金/Active Share，不代表正式可提交'])}
    runtime = load_runtime(args.config)
    state_dir = state_path(runtime, args.state_dir)
    if args.cmd in {'import-book', 'mark-submitted'}:
        from agent.compliance import CompetitionBook
        book = CompetitionBook(state_dir / 'competition', load_settings()['initial_capital'])
        if args.cmd == 'mark-submitted':
            return book.transition(args.run_id, 'SUBMITTED', receipt=args.receipt)
        raw = json.loads(args.path.read_text(encoding='utf-8'))
        ledger = book.reconcile(raw, args.pricing_date)
        return {'status': 'RECONCILED', 'as_of': ledger.last_settled_date, 'positions': len(ledger.positions)}
    if args.cmd in {'import-universe', 'import-benchmarks'}:
        from agent.orchestration.imports import import_data
        return import_data(args.cmd.removeprefix('import-'), args.path, state_dir, source_url=args.source_url,
                           as_of=args.as_of, confirmed=args.confirm, definition=args.definition)
    if args.cmd == 'evaluate':
        from agent.evaluation import ForwardTracker
        reports = []
        for path in sorted(state_dir.rglob('forward.sqlite3')) if state_dir.exists() else []:
            db = sqlite3.connect(path)
            try:
                cfg = json.loads(db.execute('SELECT payload FROM config WHERE id=1').fetchone()[0])
            finally:
                db.close()
            if args.version and cfg['strategy_version'] != args.version:
                continue
            tracker = ForwardTracker(path.parent.parent.parent, cfg['initial_capital'], cfg['commission'], cfg['tax'],
                                     cfg['strategy_version'], trading_sessions=cfg['trading_sessions'], universe=cfg['universe'])
            reports.append({'path': str(tracker.path), 'evaluation': tracker.evaluate(), 'report': str(tracker.write_report())})
        return {'versions': reports, 'note': '尚未到期預測維持PENDING；不同版本不混接績效曲線'}
    from agent.knowledge import ResearchStore
    store = ResearchStore(state_dir / ('research_mock.sqlite3' if args.cmd == 'knowledge' and args.synthetic else 'research.sqlite3'))
    if args.cmd == 'knowledge':
        query = args.query or args.technology or args.ticker
        return {'summary': store.summary(), 'matches': store.search(query) if query else []}
    if args.cmd == 'status':
        from agent.llm import CodexCLIProvider
        return {'provider': runtime['llm']['provider'], 'requested_model': runtime['llm'].get('model'),
                'codex': CodexCLIProvider(model=runtime['llm'].get('model')).doctor(), 'knowledge': store.summary(),
                'competition': runtime['competition'], 'team_id_configured': bool(load_settings().get('team_id')),
                'state_dir': str(state_dir), 'forward_databases': [str(p) for p in state_dir.rglob('forward.sqlite3')],
                'latest_runs': [str(p) for p in sorted(state_dir.rglob('summary.json'))[-5:]],
                'mock_knowledge': ResearchStore(state_dir / 'research_mock.sqlite3').summary(),
                'note': '登入狀態不代表已完成Live研究；本指令不呼叫模型推理'}
    if args.cmd == 'research':
        from agent.orchestration.imports import read_universe
        if args.provider:
            runtime['llm']['provider'] = args.provider
        if args.model:
            runtime['llm']['model'] = args.model
        runtime['llm']['allow_live'] = bool(args.live and not args.offline)
        runtime['research']['allow_network'] = bool(args.live and not args.offline)
        runtime['research']['live_as_of'] = bool(args.live and not args.offline)
        if args.offline and runtime['llm']['provider'] != 'mock':
            raise PlanError('離線研究需明確 --provider mock')
        universe = read_universe(ROOT / 'data' / 'universe' / 'universe.csv')
        return perform_research(runtime, state_dir, [r['ticker'] for r in universe], {}, stamp(taipei_now()),
                                load_sources(args.sources), ticker=args.ticker)
    raise PlanError('未知指令')


def main():
    import sys
    if len(sys.argv) == 1:
        sys.argv.extend(['daily', '--mode', 'shadow'])
    try:
        result = execute(parser().parse_args())
        if isinstance(result, dict) and 'portfolios' in result:
            result = {**result, 'portfolios': {key: {k: v for k, v in row.items() if k != 'candidates'}
                      for key, row in result['portfolios'].items()}}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))
    except (PlanError, ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f'執行失敗：{exc}', file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
