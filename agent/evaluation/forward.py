from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
import os
from typing import Any

from agent.ledger import Ledger
from agent.util import LOT, TAIPEI, PlanError, money, parse_date

VARIANTS = ("Q0", "H1", "H2")
HORIZONS = (1, 5, 10, 20)


def _json(value: Any) -> str:
    def convert(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, set):
            return sorted(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=convert)


def _day(value) -> str:
    return parse_date(str(value)).isoformat()


def _timestamp(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PlanError("Forward Test 時間戳必須包含時區")
    return parsed.astimezone(TAIPEI)


def _positive(value) -> float | None:
    try:
        numeric = float(value)
    except (ValueError, TypeError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def _point_in_time(value: Any, cutoff: datetime) -> None:
    """Reject archived data that declares a publication/retrieval after the freeze."""
    time_fields = {"available_at", "published_at", "retrieved_at", "fetched_at", "content_as_of", "as_of", "observed_at", "pricing_day", "pricing_date", "data_date"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in time_fields and item:
                if len(str(item)) == 10:
                    if parse_date(str(item)) > cutoff.date():
                        raise PlanError(f"凍結快照包含未來資料：{key}")
                elif _timestamp(item) > cutoff:
                    raise PlanError(f"凍結快照包含未來資料：{key}")
            elif isinstance(item, (dict, list, tuple)):
                _point_in_time(item, cutoff)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _point_in_time(item, cutoff)


class ForwardTracker:
    """SQLite transactions make a freeze/fill/NAV update indivisible and repeat-safe.

    Generated orders are not fills. Only settle() on the planned next session may
    execute them, using that session's value/volume VWAP (or an explicit VWAP).
    All variants share rules, never a cash balance. Each strategy version has a
    separate database and curve; no official-ledger helper is called here.
    """

    def __init__(self, state_root, initial_capital=1_000_000_000, commission=0.001425,
                 tax=0.003, strategy_version="v1", trading_sessions=None, universe=None):
        self.initial_capital = money(Decimal(str(initial_capital)))
        self.commission = Decimal(str(commission))
        self.tax = Decimal(str(tax))
        if self.initial_capital <= 0 or not (0 <= self.commission < 1) or not (0 <= self.tax < 1):
            raise PlanError("Forward Test 資金與交易成本設定不合法")
        self.strategy_version = str(strategy_version)
        if not self.strategy_version:
            raise PlanError("策略版本不可留空")
        # Hash the version instead of accepting path traversal in a configured ID.
        version_dir = hashlib.sha256(self.strategy_version.encode()).hexdigest()[:16]
        self.path = Path(state_root) / "shadow" / version_dir / "forward.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions = sorted({_day(item) for item in trading_sessions}) if trading_sessions is not None else None
        self.universe = {str(item) for item in universe} if universe is not None else None
        config = {"initial_capital": str(self.initial_capital), "commission": str(self.commission),
                  "tax": str(self.tax), "strategy_version": self.strategy_version,
                  "trading_sessions": self.sessions, "universe": sorted(self.universe) if self.universe is not None else None}
        with self._transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ledgers (variant TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS freezes (variant TEXT, trade_date TEXT, payload TEXT NOT NULL,
                    content_hash TEXT NOT NULL, PRIMARY KEY(variant, trade_date));
                CREATE TABLE IF NOT EXISTS settlements (variant TEXT, date TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(variant, date));
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            existing = db.execute("SELECT payload FROM config WHERE id=1").fetchone()
            if existing and json.loads(existing[0]) != config:
                raise PlanError("同一策略版本的資金、成本、交易日或股票池不可改寫；請建立新版本")
            db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (_json(config),))
            for variant in VARIANTS:
                db.execute("INSERT OR IGNORE INTO ledgers VALUES (?, ?)", (variant, _json({
                    "cash": str(self.initial_capital), "positions": {}, "last_settled_date": None,
                    "last_prices": {}, "nav_history": [], "reconciled": False,
                })))

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _variant(variant):
        if variant not in VARIANTS:
            raise PlanError(f"未知 Shadow Portfolio：{variant}")
        return variant

    def ledger(self, variant) -> Ledger:
        variant = self._variant(variant)
        with self._transaction() as db:
            raw = json.loads(db.execute("SELECT payload FROM ledgers WHERE variant=?", (variant,)).fetchone()[0])
            pending = db.execute("SELECT payload FROM freezes WHERE variant=? AND trade_date>? ORDER BY trade_date LIMIT 1",
                                 (variant, raw["last_settled_date"] or "")).fetchone()
        return Ledger(cash=Decimal(raw["cash"]), positions=dict(raw["positions"]),
                      last_settled_date=raw["last_settled_date"], pending=json.loads(pending[0]) if pending else None,
                      nav_history=raw["nav_history"], reconciled=False)

    def frozen(self, variant, date) -> dict | None:
        variant = self._variant(variant)
        with self._transaction() as db:
            row = db.execute("SELECT payload FROM freezes WHERE variant=? AND trade_date=?",
                             (variant, _day(date))).fetchone()
        return json.loads(row[0]) if row else None

    def pending_dates(self, variant) -> list[str]:
        variant = self._variant(variant)
        with self._transaction() as db:
            rows = db.execute("SELECT f.trade_date FROM freezes f LEFT JOIN settlements s "
                              "ON f.variant=s.variant AND f.trade_date=s.date "
                              "WHERE f.variant=? AND s.date IS NULL ORDER BY f.trade_date", (variant,)).fetchall()
        return [row[0] for row in rows]

    def freeze(self, variant, trade_date, as_of, orders, prices, candidates=None, metadata=None,
               decisions=None, research_signals=None, quant_signals=None, industries=None,
               benchmark=None, **extra) -> dict:
        variant = self._variant(variant)
        trade_date = _day(trade_date)
        cutoff = _timestamp(as_of)
        deadline = datetime.combine(parse_date(trade_date), time(8, 55), tzinfo=TAIPEI)
        if cutoff > deadline:
            raise PlanError("決策必須在交易日 08:55 截止前凍結")
        if self.sessions is not None and trade_date not in self.sessions:
            raise PlanError("委託日期不在已確認的交易日曆")
        normal_orders = []
        seen = set()
        for item in orders:
            order = dict(item)
            ticker, side = str(order["ticker"]), str(order["side"]).upper()
            raw_shares = order["shares"]
            try:
                shares = int(raw_shares)
            except (TypeError, ValueError):
                raise PlanError("委託股數必須為正整張") from None
            if isinstance(raw_shares, bool) or str(raw_shares) != str(shares) or shares <= 0 or shares % LOT:
                raise PlanError("委託股數必須為正整張")
            if side not in {"BUY", "SELL"} or ticker in seen:
                raise PlanError("委託方向不合法或同股重複／當沖委託")
            if self.universe is not None and ticker not in self.universe:
                raise PlanError(f"{ticker} 不在實驗股票池")
            seen.add(ticker)
            order.update(ticker=ticker, side=side, shares=shares)
            normal_orders.append(order)
        clean_prices = {}
        for ticker, price in prices.items():
            if _positive(price) is not None:
                clean_prices[str(ticker)] = float(price)
        if any(item["ticker"] not in clean_prices for item in normal_orders):
            raise PlanError("凍結委託缺乏當時可得的定價資料")
        if isinstance(candidates, dict):
            candidate_rows = [{"ticker": str(key), **(dict(value) if isinstance(value, dict) else {"score": value})}
                              for key, value in candidates.items()]
        else:
            candidate_rows = [dict(item) if isinstance(item, dict) else {"ticker": str(item)} for item in candidates or []]
        for item in candidate_rows:
            item["ticker"] = str(item["ticker"])
            if self.universe is not None and item["ticker"] not in self.universe:
                raise PlanError("候選股票不在實驗股票池")
        snapshot = {"variant": variant, "trade_date": trade_date, "as_of": cutoff.isoformat(),
                    "status": "FROZEN", "orders": normal_orders, "prices": clean_prices,
                    "candidates": candidate_rows, "metadata": metadata or {}, "decisions": decisions or [],
                    "research_signals": research_signals or {}, "quant_signals": quant_signals or {},
                    "industries": industries or {}, "benchmark": benchmark,
                    "strategy_version": self.strategy_version, "extra": extra,
                    "run_id": hashlib.sha256(f"{self.strategy_version}:{variant}:{trade_date}".encode()).hexdigest()[:24]}
        _point_in_time(snapshot, cutoff)
        text = _json(snapshot)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._transaction() as db:
            existing = db.execute("SELECT payload,content_hash FROM freezes WHERE variant=? AND trade_date=?", (variant, trade_date)).fetchone()
            if existing:
                if existing[1] != digest:
                    raise PlanError("已凍結決策不可覆寫；原始快照保持不變")
                return json.loads(existing[0])
            ledger = json.loads(db.execute("SELECT payload FROM ledgers WHERE variant=?", (variant,)).fetchone()[0])
            if ledger["last_settled_date"] and trade_date <= ledger["last_settled_date"]:
                raise PlanError("不可在已結算日期補寫決策")
            if ledger["last_settled_date"]:
                settled_close = datetime.combine(parse_date(ledger["last_settled_date"]), time(13, 30), tzinfo=TAIPEI)
                if settled_close > cutoff:
                    raise PlanError("凍結時間早於已採用的帳本收盤資料")
            if any(ticker not in clean_prices for ticker in ledger["positions"]):
                raise PlanError("持股缺少凍結定價，無法確認帳本")
            for order in normal_orders:
                if order["side"] == "SELL" and order["shares"] > ledger["positions"].get(order["ticker"], 0):
                    raise PlanError("凍結委託不得超賣")
            snapshot["pretrade_ledger"] = {"cash": ledger["cash"], "positions": dict(ledger["positions"]),
                                           "last_settled_date": ledger["last_settled_date"]}
            snapshot["request_hash"] = digest
            db.execute("INSERT INTO freezes VALUES(?,?,?,?)", (variant, trade_date, _json(snapshot), digest))
        return snapshot

    def settle(self, variant, date, quotes, benchmark=None) -> dict:
        variant = self._variant(variant)
        session = _day(date)
        if self.sessions is not None and session not in self.sessions:
            raise PlanError("結算日期不在已確認的交易日曆")
        with self._transaction() as db:
            prior = db.execute("SELECT payload FROM settlements WHERE variant=? AND date=?", (variant, session)).fetchone()
            if prior:
                return json.loads(prior[0])
            raw = json.loads(db.execute("SELECT payload FROM ledgers WHERE variant=?", (variant,)).fetchone()[0])
            if raw["last_settled_date"] and session <= raw["last_settled_date"]:
                raise PlanError("不得以較早日期回寫已結算帳本")
            overdue = db.execute("SELECT trade_date FROM freezes WHERE variant=? AND trade_date<? AND trade_date>? LIMIT 1",
                                 (variant, session, raw["last_settled_date"] or "")).fetchone()
            if overdue:
                raise PlanError("較早交易日尚未結算，禁止跳過凍結決策")
            frozen_row = db.execute("SELECT payload FROM freezes WHERE variant=? AND trade_date=?", (variant, session)).fetchone()
            frozen = json.loads(frozen_row[0]) if frozen_row else None
            cash = Decimal(raw["cash"])
            positions = dict(raw["positions"])
            marks = {**raw["last_prices"], **(frozen["prices"] if frozen else {})}
            quote_rows = {str(key): dict(value) for key, value in quotes.items()}
            for ticker, quote in quote_rows.items():
                if quote.get("date") and _day(quote["date"]) != session:
                    raise PlanError("結算行情日期與交易日不一致")
                if _positive(quote.get("close")):
                    marks[ticker] = float(quote["close"])
            fills, costs = [], Decimal("0")
            buy, sell = Decimal("0"), Decimal("0")
            for order in sorted(frozen["orders"] if frozen else [], key=lambda item: item["side"] != "SELL"):
                ticker, shares, side = order["ticker"], order["shares"], order["side"]
                quote = quote_rows.get(ticker, {})
                volume, value = _positive(quote.get("volume")), _positive(quote.get("value"))
                vwap = value / volume if volume and value else _positive(quote.get("vwap"))
                fill = {**order, "status": "UNFILLED", "reason": "MISSING_EXECUTION_DATA", "date": session}
                if quote.get("suspended") or not volume:
                    fill["reason"] = "SUSPENDED_OR_NO_VOLUME"
                elif not vwap:
                    pass
                elif not _positive(quote.get("close")):
                    fill["reason"] = "MISSING_CLOSING_PRICE"
                elif shares > volume:
                    fill["reason"] = "INSUFFICIENT_MARKET_VOLUME"
                else:
                    notional = money(Decimal(str(vwap)) * shares)
                    fee = money(notional * self.commission)
                    paid_tax = money(notional * self.tax) if side == "SELL" else Decimal("0")
                    if side == "SELL" and positions.get(ticker, 0) < shares:
                        raise PlanError(f"{ticker} 模擬成交超賣，原子交易已取消")
                    if side == "BUY" and notional + fee > cash:
                        fill["reason"] = "INSUFFICIENT_CASH"
                    else:
                        if side == "SELL":
                            cash += notional - fee - paid_tax
                            sell += notional
                            positions[ticker] -= shares
                            if not positions[ticker]:
                                del positions[ticker]
                        else:
                            cash -= notional + fee
                            buy += notional
                            positions[ticker] = positions.get(ticker, 0) + shares
                        costs += fee + paid_tax
                        fill.update(status="FILLED", reason=None, price=vwap, notional=float(notional),
                                    commission=float(fee), tax=float(paid_tax), execution_method="SESSION_VWAP")
                fills.append(fill)
            missing = [ticker for ticker in positions if ticker not in marks]
            if missing:
                raise PlanError("無法定價持股，保留原帳本：" + ",".join(missing))
            stale = [ticker for ticker in positions if not _positive(quote_rows.get(ticker, {}).get("close"))]
            nav = money(cash + sum((Decimal(str(marks[ticker])) * shares for ticker, shares in positions.items()), Decimal("0")))
            previous_nav = Decimal(str(raw["nav_history"][-1]["nav"])) if raw["nav_history"] else self.initial_capital
            peak = max([float(self.initial_capital), float(nav)] + [item["nav"] for item in raw["nav_history"]])
            weights = {ticker: shares * marks[ticker] / float(nav) for ticker, shares in positions.items()} if nav > 0 else {}
            industry_map = dict(raw["nav_history"][-1].get("industries", {})) if raw["nav_history"] else {}
            if frozen:
                industry_map.update(frozen["industries"])
            industry_exposure = {}
            for ticker, weight in weights.items():
                sector = industry_map.get(ticker, "UNKNOWN")
                industry_exposure[sector] = industry_exposure.get(sector, 0.0) + weight
            benchmark_level = _positive(benchmark.get("close") if isinstance(benchmark, dict) else benchmark)
            previous_benchmark = raw["nav_history"][-1].get("benchmark") if raw["nav_history"] else None
            benchmark_base = next((item.get("benchmark_base") for item in raw["nav_history"] if item.get("benchmark_base")), None)
            if benchmark_base is None and frozen:
                benchmark_base = _positive(frozen.get("benchmark"))
            benchmark_base = benchmark_base or benchmark_level
            cumulative = float(nav / self.initial_capital - 1)
            daily_return = float(nav / previous_nav - 1) if previous_nav else None
            result = {"variant": variant, "strategy_version": self.strategy_version, "date": session,
                      "status": "SETTLED", "fills": fills, "positions": positions, "cash": float(money(cash)),
                      "nav": float(nav), "daily_return": daily_return, "cumulative_return": cumulative,
                      "drawdown": float(nav) / peak - 1, "trading_cost": float(costs),
                      "turnover": float((buy + sell) / previous_nav) if previous_nav else 0,
                      "buy_notional": float(buy), "sell_notional": float(sell),
                      "cash_weight": float(cash / nav) if nav else None,
                      "weights": weights, "industry_exposure": industry_exposure, "industries": industry_map,
                      "concentration_hhi": sum(weight ** 2 for weight in weights.values()),
                      "max_position_weight": max(weights.values(), default=0), "benchmark": benchmark_level,
                      "benchmark_base": benchmark_base,
                      "benchmark_relative_return": cumulative - (benchmark_level / benchmark_base - 1) if benchmark_level and benchmark_base else None,
                      "daily_relative_return": daily_return - (benchmark_level / previous_benchmark - 1) if benchmark_level and previous_benchmark else None,
                      "stale_marks": stale, "marking_status": "STALE_MARKS" if stale else "CURRENT",
                      "quotes": quote_rows, "decision_frozen": bool(frozen),
                      "run_id": frozen.get("run_id") if frozen else None}
            raw.update(cash=str(money(cash)), positions=positions, last_settled_date=session,
                       last_prices=marks, nav_history=[*raw["nav_history"], result])
            db.execute("UPDATE ledgers SET payload=? WHERE variant=?", (_json(raw), variant))
            db.execute("INSERT INTO settlements VALUES(?,?,?)", (variant, session, _json(result)))
            return result

    def evaluate(self) -> dict:
        results = {"strategy_version": self.strategy_version, "session_basis": "official_calendar" if self.sessions is not None else "observed_settlements", "portfolios": {}}
        with self._transaction() as db:
            for variant in VARIANTS:
                raw = json.loads(db.execute("SELECT payload FROM ledgers WHERE variant=?", (variant,)).fetchone()[0])
                freezes = [json.loads(row[0]) for row in db.execute("SELECT payload FROM freezes WHERE variant=? ORDER BY trade_date", (variant,))]
                history = raw["nav_history"]
                predictions, missed = self._predictions(freezes, history)
                latest = history[-1] if history else {}
                results["portfolios"][variant] = {
                    "nav": latest.get("nav", float(self.initial_capital)), "cash": float(Decimal(raw["cash"])),
                    "positions": raw["positions"], "cumulative_return": latest.get("cumulative_return", 0.0),
                    "mdd": min([0.0] + [item["drawdown"] for item in history]),
                    "turnover": sum(item["turnover"] for item in history),
                    "trading_cost": sum(item["trading_cost"] for item in history),
                    "benchmark_relative_return": latest.get("benchmark_relative_return"),
                    "industry_exposure": latest.get("industry_exposure", {}),
                    "concentration_hhi": latest.get("concentration_hhi", 0.0),
                    "history": history, "predictions": predictions, "missed_opportunities": missed,
                    "frozen_decisions": len(freezes), "settled_sessions": len(history),
                }
        return results

    def write_report(self, output_path=None) -> Path:
        """Save a reproducible human-readable report without changing decisions."""
        report = self.evaluate()
        target = Path(output_path) if output_path is not None else self.path.parent / "evaluation.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 前瞻實驗績效", "", f"策略版本：{self.strategy_version}", "",
                 f"交易日依據：{report['session_basis']}", "",
                 "所有交易均為獨立影子模擬，不代表正式競賽提交或結算。", "",
                 "換手率定義：當日買進與賣出成交金額合計／前一交易日 NAV；此處為累計雙邊換手率。", "",
                 "| 組合 | NAV | 累積報酬 | MDD | 累計換手率 | 交易成本 |", "|---|---:|---:|---:|---:|---:|"]
        for variant, metrics in report["portfolios"].items():
            lines.append(f"| {variant} | {metrics['nav']:,.2f} | {metrics['cumulative_return']:.2%} | "
                         f"{metrics['mdd']:.2%} | {metrics['turnover']:.2%} | {metrics['trading_cost']:,.2f} |")
        for variant, metrics in report["portfolios"].items():
            counts = {status: sum(row["status"] == status for row in metrics["predictions"])
                      for status in ("PENDING", "MATURED", "MISSING_DATA")}
            lines.extend(["", f"## {variant}", "", f"已凍結 {metrics['frozen_decisions']} 日；已結算 {metrics['settled_sessions']} 個交易日。",
                          f"預測狀態：{_json(counts)}", f"產業曝險：{_json(metrics['industry_exposure'])}",
                          f"集中度 HHI：{metrics['concentration_hhi']:.6f}",
                          f"相對基準報酬：{metrics['benchmark_relative_return']}",
                          "", "錯過機會歸因僅使用當時凍結快照；無足夠資訊時標記 UNDETERMINED。"])
            for missed in metrics["missed_opportunities"]:
                lines.append(f"- {missed['ticker']}：{missed['category']}；凍結時間 {missed['frozen_as_of']}；"
                             f"5 交易日相對報酬 {missed['benchmark_relative_return']:.2%}。")
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _predictions(self, freezes, history):
        by_date = {row["date"]: row for row in history}
        observed = sorted(by_date)
        sessions = self.sessions if self.sessions is not None else observed
        latest = observed[-1] if observed else ""
        predictions, missed = [], []
        for frozen in freezes:
            future_sessions = [item for item in sessions if item >= frozen["trade_date"]]
            bought = {item["ticker"] for item in frozen["orders"] if item["side"] == "BUY"}
            already_held = set(frozen.get("pretrade_ledger", {}).get("positions", {}))
            for candidate in frozen["candidates"]:
                ticker = candidate["ticker"]
                base = _positive(frozen["prices"].get(ticker))
                for horizon in HORIZONS:
                    target = future_sessions[horizon - 1] if len(future_sessions) >= horizon else None
                    status, outcome, relative = "PENDING", None, None
                    if target and target <= latest:
                        quote = by_date.get(target, {}).get("quotes", {}).get(ticker, {})
                        close = _positive(quote.get("close"))
                        status = "MATURED" if base and close else "MISSING_DATA"
                        if status == "MATURED":
                            outcome = close / base - 1
                            bench_base = _positive(frozen.get("benchmark"))
                            bench_end = by_date[target].get("benchmark")
                            if bench_base and bench_end:
                                relative = outcome - (bench_end / bench_base - 1)
                    row = {"variant": frozen["variant"], "ticker": ticker, "frozen_as_of": frozen["as_of"],
                           "trade_date": frozen["trade_date"], "horizon_sessions": horizon,
                           "target_date": target, "status": status, "return": outcome,
                           "benchmark_relative_return": relative, "was_ordered_buy": ticker in bought, "was_already_held": ticker in already_held,
                           "run_id": frozen.get("run_id")}
                    predictions.append(row)
                    if horizon == 5 and status == "MATURED" and ticker not in bought and ticker not in already_held and relative is not None and relative > 0.05:
                        missed.append({**row, **attribute_missed_opportunity(candidate),
                                       "evidence_basis": "FROZEN_SNAPSHOT_ONLY", "candidate_snapshot": candidate})
        return predictions, missed


def attribute_missed_opportunity(candidate: dict) -> dict:
    """Classify only recorded contemporaneous causes; never invent hindsight news.

    Coverage/discovery failures can be attributed only if the frozen full-universe
    candidate record explicitly recorded that gap. Unknown causes stay unknown.
    """
    reason = str(candidate.get("rejection_reason", candidate.get("reason", ""))).lower()
    recorded = candidate.get("attribution")
    categories = {"Discovery Failure", "Data Coverage Failure", "Research Failure", "Timing Failure", "Portfolio Constraint", "Unpredictable Event"}
    if recorded in categories:
        category = recorded
    elif candidate.get("coverage_status") == "missing" or "data" in reason:
        category = "Data Coverage Failure"
    elif candidate.get("discovery_status") == "missed":
        category = "Discovery Failure"
    elif candidate.get("research_rejected") is True:
        category = "Research Failure"
    elif any(word in reason for word in ("constraint", "cash", "cap", "risk_limit")):
        category = "Portfolio Constraint"
    elif candidate.get("quant_signal") and candidate.get("timing_rejected"):
        category = "Timing Failure"
    elif candidate.get("unpredictable_event") is True:
        category = "Unpredictable Event"
    else:
        category = "UNDETERMINED"
    return {"category": category, "reason": candidate.get("rejection_reason", candidate.get("reason")),
            "hindsight_sources_used": False}
