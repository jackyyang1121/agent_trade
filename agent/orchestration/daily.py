from __future__ import annotations

import copy
import json
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
        return ResearchEngine(store, provider, runtime["research"]).run(universe, quant, as_of, sources, ticker=ticker)
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
    lines = ["# 每日自主投資研究紀錄", f"交易日：{summary['trade_date']}；定價日：{summary['pricing_date']}",
             f"Run ID：{summary['run_id']}；模式：{summary['mode']}；synthetic：{summary['synthetic']}",
             "", "## 今日研究與限制", json.dumps(research.get("metadata", {}), ensure_ascii=False), ""]
    lines += ["- " + str(item) for item in research.get("limitations", []) + summary.get("limitations", [])]
    lines += ["", "## 真正新增資訊／投資假說"]
    for thesis in research.get("theses", []):
        lines += [f"- {thesis.get('ticker')}：{thesis.get('claim')}（{thesis.get('status')}）",
                  f"  正證：{thesis.get('source_ids', [])}；反證：{thesis.get('counter_source_ids', [])}",
                  f"  預期差：{thesis.get('fundamental_surprise')}；催化劑：{thesis.get('catalysts')}",
                  f"  待查：{thesis.get('unknowns')}；下個任務：{thesis.get('next_tasks')}"]
    lines += ["", "## 原始資料"]
    for source in research.get("sources", []):
        lines.append(f"- [{source.get('title')}]({source.get('url')})：{source.get('novelty', 'UNVERIFIED')}；{source.get('delta', '')}")
    lines += ["", "## Quant → Research 候選"]
    ranked = sorted(quant.items(), key=lambda row: row[1].get("score", 0), reverse=True)[:15]
    lines += [f"- {ticker}：20日動能={row.get('momentum_20')}；相對強弱={row.get('relative_strength_20')}；量比={row.get('volume_surge')}" for ticker, row in ranked]
    for variant, portfolio in portfolios.items():
        lines += ["", f"## {variant} 決策", f"狀態：{portfolio['status']}；委託 {portfolio['orders']} 筆；{portfolio['path']}"]
        lines += [f"- {ticker}：{'；'.join(decision['reason']) if isinstance(decision['reason'], list) else decision['reason']}；權重上限 {decision['max_weight']:.2%}；{'候選' if decision['eligible'] else '未交易/觀察'}"
                  for ticker, decision in portfolio.get("candidates", {}).items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def daily(*, mode: str = "shadow", trade_date: date | None = None, runtime_path: Path | None = None,
          state_dir: Path | None = None, provider: str | None = None, model: str | None = None,
          offline: bool = False, fixture: Path | None = None, sources_path: Path | None = None,
          live: bool = False, no_research: bool = False, dry_run: bool = False) -> dict:
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
    if offline and runtime["llm"]["provider"] != "mock" and not no_research:
        raise PlanError("--offline 請選 --provider mock 或 --no-research；不會隱式冒充 GPT")
    if mode not in {"shadow", "competition", "personal"}:
        raise PlanError("未知執行模式")
    if mode == "competition" and (fixture or offline or runtime["llm"]["provider"] == "mock"):
        raise PlanError("正式競賽禁止 Mock／fixture／離線模擬資料")
    settings, strategy, prospectus = load_settings(), load_strategy(), load_prospectus()
    if runtime["competition"].get("official_universe_path"):
        universe_path = Path(runtime["competition"]["official_universe_path"])
        settings["official_universe_path"] = str(universe_path if universe_path.is_absolute() else ROOT / universe_path)
    state_dir = state_path(runtime, state_dir)
    if dry_run:
        import tempfile
        # dry-run 的所有 DB、來源匯入與帳本寫入都位於暫存目錄。
        with tempfile.TemporaryDirectory(prefix="agent-trade-dry-") as temporary:
            result = daily(mode=mode, trade_date=trade_date, runtime_path=runtime_path, state_dir=Path(temporary),
                           provider=provider, model=model, offline=offline, fixture=fixture, sources_path=sources_path,
                           live=live, no_research=no_research)
            result["dry_run"] = True
            result["artifacts_retained"] = False
            return result
    version = strategy_version({**runtime, "data_mode": "synthetic" if fixture else "live"})
    started_at = taipei_now()
    with execution_lock(state_dir):
        book = CompetitionBook(state_dir / "competition", settings["initial_capital"]) if mode == "competition" else None
        initial_held = book.get_ledger().positions if book else {}
        namespace = "personal" if mode == "personal" else mode
        from agent.calendar_tw import default_trade_date
        from agent.market import FALLBACK_CLOSED
        candidate_day = trade_date or (date.fromisoformat(json.loads(fixture.read_text(encoding="utf-8"))["trade_date"])
                                      if fixture else default_trade_date(started_at, FALLBACK_CLOSED))
        artifact_dir = state_dir / namespace / version.replace(":", "_") / candidate_day.isoformat()
        summary_path = artifact_dir / "summary.json"
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
                                 settings["tax_rate"], version, universe=[r["ticker"] for r in snapshot.universe]) if mode != "competition" else None
        if mode != "competition":
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
        atomic_json(frozen_snapshot_path, {"as_of": snapshot.as_of, "trade_date": day.isoformat(), "pricing_date": pricing.isoformat(),
                                          "prepared": prepared, "histories": snapshot.histories,
                                          "universe": snapshot.universe, "synthetic": snapshot.synthetic}, immutable=True)
        config = runtime["competition"]
        benchmarks = {}
        manifest = None
        definition = config.get("active_share_definition") or "top15_normalized"
        if config.get("benchmarks_path"):
            from .imports import read_benchmarks
            benchmark_path = Path(config["benchmarks_path"])
            benchmarks = read_benchmarks(benchmark_path if benchmark_path.is_absolute() else ROOT / benchmark_path)
            if config.get("benchmarks_manifest"):
                manifest_path = Path(config["benchmarks_manifest"])
                manifest = json.loads((manifest_path if manifest_path.is_absolute() else ROOT / manifest_path).read_text(encoding="utf-8"))
                definition = manifest["definition"]
        if mode == "competition":
            metadata = research.get("metadata", {})
            validate_competition_inputs(settings, runtime, snapshot.universe, benchmarks, book.get_ledger(), day,
                                        datetime.fromisoformat(cutoff), research_run=metadata, pricing_date=pricing)
        # 正式研究失敗時不可凍結成可重用的成功 checkpoint；登入或來源
        # 恢復後應重新研究。已通過正式守門的結果仍保留不可變恢復能力。
        atomic_json(research_path, research, immutable=True)
        portfolios = {}
        variants = ("H2",) if mode == "competition" else ("Q0", "H1", "H2")
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
            ledger = book.get_ledger() if mode == "competition" else tracker.ledger(variant)
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
            if not snapshot.synthetic:
                from datetime import time
                from agent.util import TAIPEI
                deadline = datetime.combine(day, time(8, 55), tzinfo=TAIPEI)
                if taipei_now() > deadline:
                    raise PlanError("計算完成時已超過收件時間窗截止（交易日 08:55），禁止凍結或驗證新交易書")
            name = settings["team_id"] if mode == "competition" else variant
            path = artifact_dir / f"D-Plan_{name}_{day.isoformat()}.json"
            atomic_json(path, document, immutable=True)
            atomic_json(path.with_suffix(".context.json"), context, immutable=True)
            if mode == "competition":
                validate_competition_inputs(settings, runtime, snapshot.universe, benchmarks, book.get_ledger(), day,
                                            taipei_now(), research_run=model_meta, pricing_date=pricing)
                run_id = f"{day.isoformat()}-{version.split(':')[1]}"
                book.record_plan(run_id, document, context)
                book.transition(run_id, "VALIDATED")
            else:
                tracker.freeze(variant, day.isoformat(), cutoff, document["orders"], prepared["prices"], choices,
                               {"code_version": version, "synthetic": snapshot.synthetic, "model": document["agent_metadata"]["model_version"]},
                               document["decisions"], signals if variant != "Q0" else [], quant,
                               prepared["industries"], snapshot.prepared.get("market", {}).get("taiex_close"),
                               document=document, context=context)
            portfolios[variant] = {"status": "VALIDATED" if mode == "competition" else "FROZEN",
                                   "orders": len(document["orders"]), "path": str(path), "warnings": warnings,
                                   "candidates": choices, "cash_pct": float(plan.cash_after / plan.nav_after)}
            atomic_json(saved_variant_path, portfolios[variant], immutable=True)
        summary = {"run_id": f"{day.isoformat()}-{version.split(':')[1]}", "mode": mode, "trade_date": day.isoformat(),
                   "pricing_date": pricing.isoformat(), "as_of": cutoff, "strategy_version": version,
                   "synthetic": snapshot.synthetic, "portfolios": portfolios, "research": research.get("metadata", {}),
                   "limitations": ["未自動提交競賽委託；FROZEN 不是成交", "沒有基準資料時 Shadow 不代表正式合規"] if mode != "competition" else ["請自行上傳並確認收件後才記錄 SUBMITTED"]}
        atomic_json(summary_path, summary, immutable=True)
        _report(artifact_dir / "研究報告.md", summary, research, quant, portfolios)
        return summary
