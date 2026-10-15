from __future__ import annotations

import copy
import json
import uuid
from decimal import Decimal
from datetime import date, datetime
from pathlib import Path

from agent.config import enabled_factors, load_prospectus, load_settings, load_strategy
from agent.dplan import render_dplan
from agent.evaluation import ForwardTracker
from agent.knowledge import ResearchStore
from agent.llm import build_provider
from agent.portfolio import construct, judge_regime
from agent.signals import quant_signals, research_signal, fuse
from agent.util import ROOT, PlanError, stamp, taipei_now
from .configuration import load_runtime, strategy_version
from .io import atomic_json, execution_lock
from .market_data import get_snapshot


def state_path(runtime: dict, override: Path | None = None) -> Path:
    path = override or Path(runtime.get("state_dir", "data/runtime"))
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_sources(path: Path | None) -> list[dict]:
    if not path:
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise PlanError("研究來源檔必須為 JSON list，不能直接把任意網頁文字當成已驗證證據")
    return raw


def research_database(runtime: dict, state_dir: Path) -> Path:
    synthetic = runtime["llm"].get("provider") == "mock" or runtime["research"].get("synthetic_data", False)
    return state_dir / ("research_mock.sqlite3" if synthetic else "research.sqlite3")


def perform_research(runtime: dict, state_dir: Path, universe: list[str], quant: dict,
                     as_of: str, sources: list[dict], ticker: str | None = None) -> dict:
    from agent.research import ResearchEngine

    store = ResearchStore(research_database(runtime, state_dir))
    try:
        provider = build_provider(runtime["llm"])
        config = dict(runtime["research"])
        config["require_sources"] = bool(runtime["llm"].get("allow_live"))
        discovery_meta, discovery_limits = None, []
        if config.get("auto_discover") and runtime["llm"].get("allow_live"):
            from agent.research.discovery import discover
            import time
            started = time.monotonic()
            documents, discovery_meta, discovery_limits = discover(runtime["llm"], config, universe, quant, as_of, ticker)
            sources = list(sources) + documents
            config["max_model_calls"] = max(0, int(config.get("max_model_calls", 3)) - int(discovery_meta.get("attempts", 1)))
            config["max_seconds"] = max(0.01, float(config.get("max_seconds", 3600)) - (time.monotonic() - started))
            store.record_model_run({**discovery_meta, "id": "discovery-" + uuid.uuid4().hex,
                                    "as_of": as_of, "phase": "source_discovery"})
        result = ResearchEngine(store, provider, config).run(universe, quant, as_of, sources, ticker=ticker)
        if discovery_meta:
            result["metadata"]["discovery"] = discovery_meta
            result["metadata"]["model_calls"] += int(discovery_meta.get("attempts", 1))
            result["limitations"].extend(discovery_limits)
        return result
    finally:
        store.close()


def _research_evidence(result: dict, decisions: dict) -> tuple[list[dict], dict]:
    """引用 archive 內的原文，絕不將模型 claim 改寫成公司已確認事實。"""
    rows, mapping = [], {}
    sources = {(row["id"], row.get("version")): row for row in result.get("sources", [])}
    used = set()
    for ticker, decision in decisions.items():
        evidence = []
        refs = decision.get("source_refs", [])
        for ref in refs:
            sid, source_version = ref["source_id"], ref["version"]
            source = sources.get((sid, source_version))
            if not source or source.get("extraction_status") not in {"ok", "success", "extracted", "available", "summary"}:
                continue
            if not source.get("content"):
                continue
            key = f"research_{sid}_v{source_version}"
            if key not in used:
                rows.append({"key": key, "authority": source.get("authority", "other"),
                             "name": source["title"][:300], "url": source["url"],
                             "content_as_of": stamp(datetime.fromisoformat(source["content_as_of"])),
                             "fetched_at": stamp(datetime.fromisoformat(source["fetched_at"]))})
                used.add(key)
            evidence.append({"key": key, "excerpt": source["content"][:300]})
        if evidence:
            mapping[ticker] = evidence
    return rows, mapping


def _report(path: Path, summary: dict, research: dict, quant: dict, portfolios: dict) -> None:
    meta = research.get("metadata", {})
    model_label = meta.get('model') or ('CLI 未回報（未以請求型號冒充）' if meta.get('used_llm') else '未執行真實模型')
    lines = ["# 每日自主投資研究報告", "",
             f"交易日：{summary['trade_date']}；定價日：{summary['pricing_date']}；模式：{summary['mode']}", "",
             f"Run ID：{summary['run_id']}", "",
             f"模型來源：{meta.get('provider')}；實際型號：{model_label}；"
             f"完成新模型研究：{'是' if meta.get('new_research') else '否'}；真實研究輪數：{meta.get('rounds_completed', 0) if meta.get('used_llm') else 0}。",
             "", "## 本次資料條件與限制", ""]
    lines += ["- " + str(item) for item in research.get("limitations", []) + summary.get("limitations", [])]
    lines += ["", "## 真正新增資訊／投資假說"]
    for thesis in research.get("theses", []):
        surprise = thesis.get('fundamental_surprise') or {}
        lines += ["", f"### {thesis.get('ticker')}｜{thesis.get('status')}", "", thesis.get('claim', ''), "",
                  f"證據強度：{thesis.get('evidence_strength')}；研究時點：{thesis.get('updated_at')}；"
                  f"完成反方檢驗：{bool(thesis.get('adversarial_reviewed'))}。", "",
                  f"預期差：{surprise.get('interpretation', '尚無可驗證基準')}（基準：{surprise.get('baseline_type', 'unknown')}）。", "",
                  "支持來源：" + '、'.join(thesis.get('source_ids', [])) + "；反方來源：" + ('、'.join(thesis.get('counter_source_ids', [])) or '未取得，保留風險假說'), ""]
        lines += [f"- 催化劑：{c.get('description')}；{c.get('status')}；日期 {c.get('expected_at') or '未知'}" for c in thesis.get('catalysts', [])]
        lines += [f"- 反方／失效風險：{r.get('description')}（{r.get('status')}）" for r in thesis.get('risks', [])]
        lines += [f"- {s.get('name')} 情境：{s.get('description')}；假設：{'、'.join(s.get('assumptions', []))}" for s in thesis.get('scenarios', [])]
        lines += ["", "待查：" + '、'.join(thesis.get('unknowns', [])), "", "後續研究：" + '、'.join(thesis.get('next_tasks', []))]
    lines += ["", "## 原始資料"]
    for source in research.get("sources", []):
        lines.append(f"- **{source.get('id')} v{source.get('version')}** [{source.get('title')}]({source.get('url')})；"
                     f"發布：{source.get('published_at')}；內容時點：{source.get('content_as_of')}；抓取：{source.get('fetched_at')}；"
                     f"SHA-256：{source.get('content_hash')}；{source.get('novelty', 'UNVERIFIED')}")
    lines += ["", "## Quant → Research 候選"]
    ranked = sorted(quant.items(), key=lambda row: row[1].get("score", 0), reverse=True)[:15]
    def pct(value):
        return f'{value:.2%}' if value is not None else '資料不足'
    lines += [f"- {ticker}：20日動能={pct(row.get('momentum_20'))}；相對強弱={pct(row.get('relative_strength_20'))}；近期量能變化={pct(row.get('volume_surge'))}" for ticker, row in ranked]
    for variant, portfolio in portfolios.items():
        document = json.loads(Path(portfolio['path']).read_text(encoding='utf-8'))
        lines += ["", f"## {variant} 交易計畫", "", f"狀態：{portfolio['status']}；委託 {portfolio['orders']} 筆；預估現金比 {portfolio.get('cash_pct', 0):.2%}",
                  "", f"D-Plan：{portfolio['path']}", "", document['market_view']['logic'], "",
                  "| 股票 | 動作 | 目標權重 | 委託股數 | 研究支持 |", "| --- | --- | ---: | ---: | --- |"]
        orders = {o['ticker']: o for o in document['orders']}
        for decision in document['decisions']:
            ticker = decision['ticker']
            has_research = portfolio.get('candidates', {}).get(ticker, {}).get('thesis_ids', [])
            lines.append(f"| {ticker} | {decision['action']} | {decision['target_weight']:.2%} | {orders[ticker]['shares']:,} | "
                         + ('、'.join(has_research) if has_research else '量化依據；未取得個股研究支持') + ' |')
        lines += ["", "### 決策理由與反方", ""]
        for inference in document['inferences']:
            lines += [f"- {inference.get('conclusion', inference['inf_id'])}：{inference['logic']}"
                      + (f" 反方：{inference['counter_evidence']}" if inference.get('counter_evidence') else '')]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def daily(*, mode: str = "shadow", trade_date: date | None = None, runtime_path: Path | None = None,
          state_dir: Path | None = None, provider: str | None = None, model: str | None = None,
          offline: bool = False, fixture: Path | None = None, sources_path: Path | None = None,
          live: bool = False, no_research: bool = False, dry_run: bool = False, skip_benchmarks: bool = False) -> dict:
    from agent.checks import semantic_errors
    from agent.compliance import CompetitionBook, validate_competition_inputs

    runtime = load_runtime(runtime_path)
    if provider:
        runtime["llm"]["provider"] = provider
    if model:
        runtime["llm"]["model"] = model
    runtime["llm"]["allow_live"] = bool(live and not offline)
    runtime["research"]["allow_network"] = bool(live and not offline)
    runtime["research"]["live_as_of"] = bool(live and not offline)
    runtime["research"]["synthetic_data"] = bool(fixture)
    if mode not in {"shadow", "competition", "personal", "rehearsal"}:
        raise PlanError("未知執行模式")
    if mode == "rehearsal" and (not live or fixture or offline or no_research or runtime["llm"]["provider"] == "mock"):
        raise PlanError("演練需 --live 真實模型與行情；離線測試請使用 shadow --provider mock --offline --fixture")
    if offline and runtime["llm"]["provider"] != "mock" and not no_research:
        raise PlanError("--offline 請選 --provider mock 或 --no-research；不會隱式冒充 GPT")
    if not no_research and runtime["llm"]["provider"] != "mock" and not live:
        raise PlanError("真實模型研究需明確 --live；賽前請執行 python -m agent rehearse --live，不會自動退回量化報告")
    if mode == "competition" and (fixture or offline or runtime["llm"]["provider"] == "mock"):
        raise PlanError("正式競賽禁止 Mock／fixture／離線模擬資料")
    settings, strategy, prospectus = load_settings(), load_strategy(), load_prospectus()
    if runtime["competition"].get("official_universe_path"):
        universe_path = Path(runtime["competition"]["official_universe_path"])
        settings["official_universe_path"] = str(universe_path if universe_path.is_absolute() else ROOT / universe_path)
    from .outputs import output_roots, publish
    reports_dir, datapack_dir = output_roots(state_dir)
    state_dir = state_path(runtime, state_dir)
    if mode == "rehearsal":
        # 每次演練重新從全現金開始；研究庫可持續累積，沒有正式結算副作用。
        state_dir = state_dir / "rehearsal"
    if dry_run:
        import tempfile
        # dry-run 的所有 DB、來源匯入與帳本寫入都位於暫存目錄。
        with tempfile.TemporaryDirectory(prefix="agent-trade-dry-") as temporary:
            result = daily(mode=mode, trade_date=trade_date, runtime_path=runtime_path, state_dir=Path(temporary),
                           provider=provider, model=model, offline=offline, fixture=fixture, sources_path=sources_path,
                           live=live, no_research=no_research, skip_benchmarks=skip_benchmarks)
            result["dry_run"] = True
            result["artifacts_retained"] = False
            return result
    version = strategy_version({**runtime, "data_mode": "synthetic" if fixture else "live"})
    started_at = taipei_now()
    with execution_lock(state_dir):
        book = CompetitionBook(state_dir / "competition", settings["initial_capital"]) if mode == "competition" else None
        initial_held = book.get_ledger().positions if book else {}
        namespace = "runs" if mode == "rehearsal" else mode
        from agent.calendar_tw import default_trade_date
        from agent.market import FALLBACK_CLOSED
        if mode == "rehearsal" and trade_date is None:
            from agent.calendar_tw import next_trading_day
            trade_date = next_trading_day(started_at.date(), FALLBACK_CLOSED)
        candidate_day = trade_date or (date.fromisoformat(json.loads(fixture.read_text(encoding="utf-8"))["trade_date"])
                                      if fixture else default_trade_date(started_at, FALLBACK_CLOSED))
        artifact_dir = state_dir / namespace / version.replace(":", "_") / candidate_day.isoformat()
        if mode == "rehearsal":
            artifact_dir = artifact_dir / started_at.strftime("%H%M%S_%f")
        summary_path = artifact_dir / "summary.json"
        if (artifact_dir / "CLEANED.json").exists():
            raise PlanError("舊無研究報告已清除，帳本仍保留；請使用 rehearse --live 產生新的模型演練")
        if summary_path.exists():
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            return {**saved, "reused": True}
        frozen_snapshot_path = artifact_dir / "snapshot.json"
        if frozen_snapshot_path.exists():
            from .market_data import MarketSnapshot
            raw = json.loads(frozen_snapshot_path.read_text(encoding="utf-8"))
            pricing_raw = raw.get("pricing_date") or next(iter(raw["histories"].values()))[-1]["date"]
            snapshot = MarketSnapshot(candidate_day, date.fromisoformat(pricing_raw), raw["as_of"], raw["prepared"],
                                      raw["histories"], raw["universe"], raw["synthetic"])
            snapshot.validate()
        else:
            snapshot = get_snapshot(settings, strategy, trade_date, taipei_now(), initial_held, fixture, offline)
        day, pricing, cutoff = snapshot.trade_date, snapshot.pricing_date, snapshot.as_of
        if day != candidate_day:
            artifact_dir = state_dir / namespace / version.replace(":", "_") / day.isoformat()
            summary_path, frozen_snapshot_path = artifact_dir / "summary.json", artifact_dir / "snapshot.json"
            if summary_path.exists():
                return {**json.loads(summary_path.read_text(encoding="utf-8")), "reused": True}
        prepared = snapshot.prepared
        tracker_root = state_dir / "personal" if mode == "personal" else state_dir
        tracker = ForwardTracker(tracker_root, settings["initial_capital"], settings["commission_rate"],
                                 settings["tax_rate"], version, universe=[r["ticker"] for r in snapshot.universe]) if mode in {"shadow", "personal"} else None
        simulated = None
        if mode == "rehearsal":
            from agent.datapack import simulated_initial_pack, parse_datapack
            from agent.ledger import Ledger
            raw_pack = simulated_initial_pack(str(settings["team_id"]), day, taipei_now(), settings["initial_capital"])
            pack = parse_datapack(raw_pack, pricing, allow_simulated=True)
            simulated = Ledger(pack.cash, pack.positions)
            pack_path = datapack_dir / "simulated" / day.isoformat() / (artifact_dir.name + ".json")
            atomic_json(pack_path, raw_pack, immutable=True)
        if tracker:
            # 只結算已在先前交易日前凍結的預測，不使用本次決策當日未來行情。
            for variant in ("Q0", "H1", "H2"):
                for pending_day in tracker.pending_dates(variant):
                    if pending_day > pricing.isoformat():
                        continue
                    quotes = {ticker: next((row for row in history if row["date"] == pending_day), {})
                              for ticker, history in snapshot.histories.items()}
                    if not any(quotes.values()):
                        raise PlanError(f"缺少 {pending_day} 結算行情，保留 pending，不跳過未結交易")
                    benchmark = next((row["close"] for row in prepared.get("taiex_history", [])
                                      if row["date"] == pending_day), None)
                    if benchmark is None and pending_day == pricing.isoformat():
                        benchmark = prepared["market"].get("taiex_close")
                    tracker.settle(variant, pending_day, quotes, benchmark=benchmark)
        quant = quant_signals(snapshot.histories, cutoff, prepared["industries"])
        for ticker, row in quant.items():
            row["source_ids"] = ["tpex" if prepared["markets"].get(ticker) == "tpex" else "twse"]
            row["baseline_score"] = prepared.get("scores", {}).get(ticker)
            row["baseline_components"] = prepared.get("components", {}).get(ticker, {})
        research = {"theses": [], "sources": [], "metadata": {"provider": runtime["llm"]["provider"],
                    "used_llm": False, "new_research": False, "success": False}, "limitations": [], "tasks": []}
        research_path = artifact_dir / "research.json"
        if research_path.exists():
            research = json.loads(research_path.read_text(encoding="utf-8"))
            cutoff = research["decision_as_of"]
        elif not no_research:
            try:
                if tracker:
                    prior_outcomes = tracker.evaluate().get("portfolios", {})
                    runtime["research"]["forward_feedback"] = {
                        variant: {"nav": outcome.get("nav"), "mdd": outcome.get("mdd"),
                                  "missed_opportunities": outcome.get("missed_opportunities", [])[:10],
                                  "matured_predictions": [p for p in outcome.get("predictions", [])
                                                          if p.get("status") == "MATURED"][:10]}
                        for variant, outcome in prior_outcomes.items()}
                research = perform_research(runtime, state_dir, [r["ticker"] for r in snapshot.universe], quant,
                                            cutoff, load_sources(sources_path))
            except (ValueError, RuntimeError, OSError) as exc:
                research["limitations"].append(f"Research Failure：{type(exc).__name__}: {exc}")
        else:
            research["limitations"].append("本次明確跳過新 GPT 研究；僅使用已保存研究與量化訊號")
        if not snapshot.synthetic and not research_path.exists():
            cutoff = stamp(taipei_now())
        for row in quant.values():
            row["as_of"] = cutoff
        store = ResearchStore(research_database(runtime, state_dir))
        theses = research["theses"] if research_path.exists() else store.list_theses(cutoff)
        signals = []
        for thesis in theses:
            try:
                signals.append(research_signal(thesis, cutoff))
            except ValueError as exc:
                research["limitations"].append(f"排除無效研究訊號 {thesis.get('id')}：{exc}")
        research["theses"] = theses
        research["sources"] = store.resolve_thesis_sources(theses, as_of=cutoff)
        store.close()
        if mode == "competition" and (any(t.get("synthetic") for t in theses) or any(s.get("synthetic") for s in research["sources"])):
            raise PlanError("正式研究含模擬證據，禁止產生交易書")
        research["decision_as_of"] = cutoff
        if live and not no_research:
            metadata = research.get("metadata", {})
            usable = [s for s in research["sources"] if s.get("content") and not s.get("synthetic")
                      and s.get("extraction_status") != "unavailable"]
            if not (metadata.get("success") and metadata.get("new_research") and metadata.get("used_llm")
                    and not metadata.get("synthetic") and usable):
                atomic_json(artifact_dir / "research_failure.json", research)
                raise PlanError("真實研究失敗或沒有可核對的來源，未產生 D-Plan；"
                                + "；".join(research.get("limitations", [])[-5:])
                                + f"。診斷：{artifact_dir / 'research_failure.json'}")
        atomic_json(frozen_snapshot_path, {"as_of": snapshot.as_of, "trade_date": day.isoformat(), "pricing_date": pricing.isoformat(),
                                          "prepared": prepared, "histories": snapshot.histories,
                                          "universe": snapshot.universe, "synthetic": snapshot.synthetic}, immutable=True)
        config = runtime["competition"]
        benchmarks = {}
        manifest = None
        benchmark_update = None
        definition = config.get("active_share_definition") or "top15_normalized"
        saved_benchmarks = research.get("benchmark_inputs")
        if saved_benchmarks:
            benchmarks = saved_benchmarks["weights"]
            manifest = saved_benchmarks["manifest"]
            definition = saved_benchmarks["definition"]
            benchmark_update = saved_benchmarks["update"]
            config = saved_benchmarks["config"]
        if not saved_benchmarks and live and config.get("auto_refresh_benchmarks") and not skip_benchmarks and not no_research:
            from agent.research.benchmarks import refresh
            try:
                benchmark_update = refresh(runtime, datapack_dir / "benchmarks.csv", pricing)
                config = {**config, "benchmarks_path": benchmark_update["path"], "benchmarks_manifest": benchmark_update["manifest"]}
            except (ValueError, RuntimeError, OSError) as exc:
                if mode == "competition":
                    raise PlanError(f"Active Share 基準更新失敗：{exc}") from exc
                research["limitations"].append(f"Active Share 未完成最新核對：{exc}")
        if not saved_benchmarks and not skip_benchmarks and config.get("benchmarks_path"):
            from .imports import read_benchmarks
            benchmark_path = Path(config["benchmarks_path"])
            benchmark_path = benchmark_path if benchmark_path.is_absolute() else ROOT / benchmark_path
            if benchmark_path.is_file():
                benchmarks = read_benchmarks(benchmark_path)
            elif mode != "competition":
                research["limitations"].append("尚無可用的 ETF 持股基準，本次 Active Share 未驗證")
            if benchmarks and config.get("benchmarks_manifest"):
                manifest_path = Path(config["benchmarks_manifest"])
                manifest = json.loads((manifest_path if manifest_path.is_absolute() else ROOT / manifest_path).read_text(encoding="utf-8"))
                definition = manifest["definition"]
        if benchmark_update and not saved_benchmarks:
            cutoff = stamp(taipei_now())
            research["decision_as_of"] = cutoff
            for row in quant.values():
                row["as_of"] = cutoff
        runtime["competition"] = config
        research["benchmark_inputs"] = {"weights": benchmarks, "manifest": manifest, "definition": definition,
                                        "update": benchmark_update, "config": config}
        if mode == "competition":
            metadata = research.get("metadata", {})
            validate_competition_inputs(settings, runtime, snapshot.universe, benchmarks, book.get_ledger(), day,
                                        datetime.fromisoformat(cutoff), research_run=metadata, pricing_date=pricing)
        # 正式研究失敗時不可凍結成可重用的成功 checkpoint；登入或來源
        # 恢復後應重新研究。已通過正式守門的結果仍保留不可變恢復能力。
        atomic_json(research_path, research, immutable=True)
        portfolios = {}
        variants = ("H2",) if mode in {"competition", "rehearsal"} else ("Q0", "H1", "H2")
        regime, _, counter = judge_regime(prepared["market"], strategy["regime_rules"])
        for variant in variants:
            saved_variant_path = artifact_dir / f"{variant}_summary.json"
            if saved_variant_path.exists():
                portfolios[variant] = json.loads(saved_variant_path.read_text(encoding="utf-8"))
                continue
            official_checkpoint = book.get_plan(f"{day.isoformat()}-{version.split(':')[1]}") if book else None
            if official_checkpoint:
                document, context = official_checkpoint["document"], official_checkpoint["context"]
                if official_checkpoint["state"] == "GENERATED":
                    validate_competition_inputs(settings, runtime, snapshot.universe, benchmarks, book.get_ledger(), day,
                                                taipei_now(), research_run=research.get("metadata", {}), pricing_date=pricing)
                    book.transition(official_checkpoint["run_id"], "VALIDATED")
                path = artifact_dir / f"D-Plan_{document['team_id']}_{day.isoformat()}.json"
                atomic_json(path, document, immutable=True)
                atomic_json(path.with_suffix(".context.json"), context, immutable=True)
                portfolios[variant] = {"status": book.get_plan(official_checkpoint["run_id"])["state"],
                                       "orders": len(document["orders"]), "path": str(path),
                                       "warnings": ["重用正式不可變checkpoint；沒有重新提交"],
                                       "candidates": context.get("investment_decisions", {})}
                atomic_json(saved_variant_path, portfolios[variant], immutable=True)
                continue
            frozen = tracker.frozen(variant, day.isoformat()) if tracker else None
            if frozen:
                original = frozen.get("extra", {})
                document, context = original.get("document"), original.get("context")
                if not document or not context:
                    raise PlanError("已凍結委託缺少原始D-Plan快照；保留帳本，請檢查checkpoint")
                path = artifact_dir / f"D-Plan_{variant}_{day.isoformat()}.json"
                atomic_json(path, document, immutable=True)
                atomic_json(path.with_suffix(".context.json"), context, immutable=True)
                choices = {row["ticker"]: row for row in frozen["candidates"]}
                portfolios[variant] = {"status": "FROZEN", "orders": len(document["orders"]), "path": str(path),
                                       "warnings": ["重用不可變checkpoint"], "candidates": choices}
                atomic_json(saved_variant_path, portfolios[variant], immutable=True)
                continue
            ledger = book.get_ledger() if mode == "competition" else simulated if mode == "rehearsal" else tracker.ledger(variant)
            if ledger.official_data:
                official_nav = Decimal(str(ledger.official_data["account"]["nav_tradable"]))
                if abs(ledger.nav_on(prepared["prices"]) - official_nav) > Decimal("0.02"):
                    raise PlanError("必要定價行情與官方 DataPack 的 nav_tradable 不一致，禁止推測目標股數")
            choices = fuse(quant, signals, variant, ledger.positions, {**strategy["portfolio"], **runtime["signals"], "as_of": cutoff})
            metrics = copy.deepcopy(prepared["metrics"])
            for ticker, choice in choices.items():
                metrics.setdefault(ticker, {})["decision"] = choice
                metrics[ticker]["max_weight"] = choice["max_weight"]
                for key in ("volatility_20", "mean_correlation", "correlations", "tradable"):
                    metrics[ticker][key] = quant.get(ticker, {}).get(key)
            portfolio_config = {**strategy["portfolio"], **runtime["signals"]}
            portfolio_config["active_share_definition"] = definition
            if mode == "competition":
                portfolio_config.update(require_benchmarks=True, active_share_definition_confirmed=True,
                                        active_share_definition=manifest["definition"])
            plan = construct({t: c["score"] for t, c in choices.items()}, metrics, prepared["prices"], prepared["industries"],
                             ledger.positions, ledger.cash, portfolio_config, benchmarks, regime,
                             settings["commission_rate"], settings["tax_rate"])
            extra_sources, evidence = _research_evidence(research, choices) if variant != "Q0" else ([], {})
            model_meta = research.get("metadata", {})
            used = bool(model_meta.get("used_llm")) and variant != "Q0"
            model_name = model_meta.get("actual_model") or model_meta.get("model")
            document = render_dplan(prospectus, prepared["market"], prepared["names"], prepared["markets"],
                                    prepared["prices"], metrics, {t: c["score"] for t, c in choices.items()},
                                    prepared.get("components", {}), [f for f, _, _ in enabled_factors(strategy, prospectus)],
                                    ledger.positions, plan, regime, counter,
                                    list(prepared["source_material"].values()) + extra_sources,
                                    settings.get("team_id") or "TEAM_PREVIEW", day.isoformat(),
                                    "openai" if used else "other", str(model_name or "unknown")[:60] if used else "deterministic-quant-no-new-llm",
                                    version, datetime.fromisoformat(cutoff) if snapshot.synthetic else started_at,
                                    datetime.fromisoformat(cutoff) if snapshot.synthetic else taipei_now(),
                                    snapshot.http_calls, float(strategy["portfolio"]["rebalance_band"]),
                                    research_evidence=evidence, decision_context=choices)
            if mode == "rehearsal":
                document["_NOTE"] = "賽前演練：使用模擬初始全現金帳戶，禁止上傳正式競賽；行情與模型來源依實際執行紀錄。"
            context = {"held": ledger.positions, "prices": prepared["prices"], "pre_nav": plan.pre_nav,
                       "cash": str(ledger.cash), "commission": settings["commission_rate"], "tax": settings["tax_rate"],
                       "benchmarks": benchmarks, "active_share_min": strategy["portfolio"]["active_share_min"],
                       "universe": [r["ticker"] for r in snapshot.universe], "competition": mode == "competition",
                       "as_of": cutoff, "validation_time": document["agent_metadata"]["run_completed_at"],
                       "pricing_date": pricing.isoformat(), "active_share_definition": definition}
            context["investment_decisions"] = choices
            if mode == "competition":
                context.update(mode="competition", active_share_definition_confirmed=True,
                               active_share_definition=manifest["definition"], benchmark_etf_ids=manifest["etf_ids"])
            errors, warnings = semantic_errors(document, context)
            if errors:
                atomic_json(artifact_dir / f"{variant}_INVALID.json", {"errors": errors, "document": document})
                raise PlanError("D-Plan 未通過驗證：" + "；".join(errors[:15]))
            if not snapshot.synthetic and mode != "rehearsal":
                from datetime import time
                from agent.util import TAIPEI
                deadline = datetime.combine(day, time(8, 55), tzinfo=TAIPEI)
                if taipei_now() > deadline:
                    raise PlanError("計算完成時已超過收件時間窗截止（交易日 08:55），禁止凍結或驗證新交易書")
            name = settings["team_id"] if mode in {"competition", "rehearsal"} else variant
            path = artifact_dir / f"D-Plan_{name}_{day.isoformat()}.json"
            atomic_json(path, document, immutable=True)
            atomic_json(path.with_suffix(".context.json"), context, immutable=True)
            if mode == "competition":
                validate_competition_inputs(settings, runtime, snapshot.universe, benchmarks, book.get_ledger(), day,
                                            taipei_now(), research_run=model_meta, pricing_date=pricing)
                run_id = f"{day.isoformat()}-{version.split(':')[1]}"
                book.record_plan(run_id, document, context)
                book.transition(run_id, "VALIDATED")
            elif tracker:
                tracker.freeze(variant, day.isoformat(), cutoff, document["orders"], prepared["prices"], choices,
                               {"code_version": version, "synthetic": snapshot.synthetic, "model": document["agent_metadata"]["model_version"]},
                               document["decisions"], signals if variant != "Q0" else [], quant,
                               prepared["industries"], snapshot.prepared.get("market", {}).get("taiex_close"),
                               document=document, context=context)
            portfolios[variant] = {"status": "VALIDATED" if mode == "competition" else "REHEARSAL" if mode == "rehearsal" else "FROZEN",
                                   "orders": len(document["orders"]), "path": str(path), "warnings": warnings,
                                   "candidates": choices, "cash_pct": float(plan.cash_after / plan.nav_after)}
            atomic_json(saved_variant_path, portfolios[variant], immutable=True)
        summary = {"run_id": f"{day.isoformat()}-{version.split(':')[1]}", "mode": mode, "trade_date": day.isoformat(),
                   "pricing_date": pricing.isoformat(), "as_of": cutoff, "strategy_version": version,
                   "synthetic": snapshot.synthetic or mode == "rehearsal" or bool(research.get("metadata", {}).get("synthetic")),
                   "market_synthetic": snapshot.synthetic,
                   "account_simulated": mode == "rehearsal", "portfolios": portfolios, "research": research.get("metadata", {}),
                   "limitations": ["未自動提交競賽委託；FROZEN 不是成交", "沒有基準資料時 Shadow 不代表正式合規"] if mode != "competition" else ["請自行上傳並確認收件後才記錄 SUBMITTED"]}
        if mode == "rehearsal":
            summary["run_id"] += "-" + artifact_dir.name
            summary.update(datapack=str(pack_path), submit_ready=False)
            summary["limitations"] = ["演練：模擬十億元初始全現金，行情與研究使用真實公開資料；禁止上傳競賽",
                                       "不計入 Forward 績效；不套用正式收件時間窗；Active Share 口徑未確認時僅供參考"]
        summary["benchmarks_update"] = benchmark_update
        if skip_benchmarks:
            summary["limitations"].append("本次略過 ETF 持股更新，未驗證最新 Active Share")
        _report(artifact_dir / "研究報告.md", summary, research, quant, portfolios)
        publish(summary, artifact_dir, reports_dir)
        _report(Path(summary["report"]), summary, research, quant, portfolios)
        atomic_json(summary_path, summary, immutable=True)
        return summary
