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
    q = sub.add_parser('rehearse', help='賽前真實模型演練：今天視為首日，模擬十億元帳戶，報告禁止正式提交')
    _common(q); _research_args(q)
    q.add_argument('--date', type=parse_date)
    q.add_argument('--skip-benchmarks', action='store_true', help='快速看研究報告時略過 ETF 搜尋；會標示未驗證 Active Share')
    q = sub.add_parser('refresh-benchmarks', help='用 Codex 查前十檔 ETF 前十五大持股，核對原文後更新 benchmarks.csv')
    _common(q); _research_args(q)
    q.add_argument('--date', type=parse_date, help='持股資料不可晚於此定價日，預設前交易日')
    q = sub.add_parser('cleanup-shadow', help='清除舊 Shadow 中無新模型依據的報告，保留帳本')
    _common(q)
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
        q.add_argument('--source-url')
        q.add_argument('--source-note', help='已核對本機官方股票池 PDF 的來源記錄')
        q.add_argument('--as-of', required=True)
        q.add_argument('--confirm', action='store_true', help='僅在已核對官方完整資料後設定')
        q.add_argument('--definition', choices=['top10_normalized', 'top10_raw', 'top15_normalized', 'top15_raw', 'full_normalized'])
    q = sub.add_parser('import-book', help='匯入官方 DataPack 或既有現金／持股帳本')
    _common(q); q.add_argument('path', type=Path)
    q.add_argument('--pricing-date', type=parse_date, required=True)
    q = sub.add_parser('mark-submitted', help='手動上傳且收到成功收件證據後記錄狀態')
    _common(q); q.add_argument('run_id'); q.add_argument('--receipt', required=True)
    return p


def execute(args):
    from agent.config import load_settings
    from agent.orchestration.configuration import load_runtime
    from agent.orchestration.daily import daily, load_sources, perform_research, state_path
    if args.cmd == 'cleanup-shadow':
        from agent.orchestration.outputs import clean_unresearched_shadow
        runtime = load_runtime(args.config)
        roots = [state_path(runtime, args.state_dir) / 'shadow']
        if args.state_dir is None:
            roots += [ROOT / 'runtime' / 'shadow']
        return {'cleanup': [clean_unresearched_shadow(root) for root in dict.fromkeys(roots)]}
    if args.cmd in {'daily', 'run', 'rehearse'}:
        if args.cmd == 'rehearse':
            return daily(mode='rehearsal', trade_date=args.date, runtime_path=args.config, state_dir=args.state_dir,
                         provider=args.provider, model=args.model, live=args.live, offline=args.offline,
                         sources_path=args.sources, skip_benchmarks=args.skip_benchmarks)
        return daily(mode=args.mode, trade_date=args.date, runtime_path=args.config, state_dir=args.state_dir,
                     provider=args.provider, model=args.model, offline=args.offline, fixture=args.fixture,
                     sources_path=args.sources, live=args.live, no_research=args.no_research, dry_run=args.dry_run)
    if args.cmd == 'check':
        from agent.checks import schema_errors, semantic_errors
        from agent.compliance import structural_errors
        if args.context is not None and not args.context.is_file():
            raise PlanError(f'指定的驗證 context 不存在或不是檔案：{args.context}')
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
    if args.cmd == 'refresh-benchmarks':
        if not args.live or args.offline or args.provider == 'mock':
            raise PlanError('ETF 更新需要 --live，不能用 Mock 資料更新真實基準')
        from agent.calendar_tw import previous_trading_day
        from agent.market import FALLBACK_CLOSED
        from agent.research.benchmarks import refresh
        from agent.orchestration.outputs import output_roots
        if args.provider:
            runtime['llm']['provider'] = args.provider
        if args.model:
            runtime['llm']['model'] = args.model
        return refresh(runtime, output_roots(args.state_dir)[1] / 'benchmarks.csv',
                       args.date or previous_trading_day(taipei_now().date(), FALLBACK_CLOSED))
    if args.cmd in {'import-book', 'mark-submitted'}:
        from agent.compliance import CompetitionBook
        book = CompetitionBook(state_dir / 'competition', load_settings()['initial_capital'])
        if args.cmd == 'mark-submitted':
            return book.transition(args.run_id, 'SUBMITTED', receipt=args.receipt)
        raw = json.loads(args.path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise PlanError('官方帳本或 DataPack 必須是 JSON object')
        if raw.get('doc_type') == 'DataPack' and raw.get('team_id') != str(load_settings().get('team_id')):
            raise PlanError('DataPack 隊伍代號與目前設定不一致，原帳本不變')
        if raw.get('doc_type') == 'DataPack':
            from agent.datapack import parse_datapack
            if parse_datapack(raw, args.pricing_date).generated_at > taipei_now():
                raise PlanError('DataPack 產生時間在未來，原帳本不變')
        ledger = book.reconcile(raw, args.pricing_date)
        result = {'status': 'RECONCILED', 'as_of': ledger.last_settled_date, 'positions': len(ledger.positions)}
        if ledger.official_data:
            import hashlib
            from agent.orchestration.outputs import output_roots
            from agent.orchestration.io import atomic_json
            digest = hashlib.sha256(args.path.read_bytes()).hexdigest()[:12]
            source = ledger.official_data
            archive = output_roots(args.state_dir)[1] / 'official' / f"{source['team_id']}_{source['trade_date']}_{digest}.json"
            atomic_json(archive, source, immutable=True)
            result.update(trade_date=ledger.official_data['trade_date'], prev_day_orders=ledger.official_data['prev_day_orders'],
                          account=ledger.official_data['account'], compliance=ledger.official_data['compliance'], archive=str(archive))
        return result
    if args.cmd in {'import-universe', 'import-benchmarks'}:
        from agent.orchestration.imports import import_data
        return import_data(args.cmd.removeprefix('import-'), args.path, state_dir, source_url=args.source_url,
                           source_note=args.source_note, as_of=args.as_of, confirmed=args.confirm, definition=args.definition)
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
        from agent.config import SCHEMA_PATH, active_schema_version
        return {'provider': runtime['llm']['provider'], 'requested_model': runtime['llm'].get('model'),
                'dplan_schema': {'path': str(SCHEMA_PATH), 'active_version': active_schema_version(),
                                 'required_competition_version': runtime['competition'].get('required_dplan_schema_version', '4.3')},
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
        result = perform_research(runtime, state_dir, [r['ticker'] for r in universe], {}, stamp(taipei_now()),
                                  load_sources(args.sources), ticker=args.ticker)
        if args.live and not result['metadata'].get('success'):
            raise PlanError('研究未成功：' + '；'.join(result.get('limitations', [])))
        return result
    raise PlanError('未知指令')


def main():
    import sys
    if len(sys.argv) == 1:
        parser().print_help()
        return
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
