from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import date, datetime
from decimal import Decimal

from agent.calendar_tw import default_trade_date, is_trading_day, previous_trading_day
from agent.checks import semantic_errors
from agent.config import (
    DECISION_DIR,
    NAV_DIR,
    PREVIEW_DIR,
    enabled_factors,
    load_prospectus,
    load_settings,
    load_strategy,
    valid_team_id,
)
from agent.dplan import render_dplan
from agent.ledger import Ledger, apply_official_book, load_ledger, save_ledger
from agent.market import Http, ensure_day, load_benchmarks, load_holidays, prepare, read_daily
from agent.portfolio import construct, judge_regime
from agent.util import ROOT, PlanError, money, parse_date, stamp, taipei_now


def _stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def code_version() -> str:
    digest = hashlib.sha256()
    for relative in (
        "config/strategy.yaml",
        "ETF投資策略說明文檔.md",
        "agent/factors.py",
        "agent/portfolio.py",
        "agent/dplan.py",
    ):
        digest.update((ROOT / relative).read_bytes())
    return "v1.0.0-" + digest.hexdigest()[:12]


def _settle(ledger: Ledger, day: date, commission: float, tax: float, http: Http) -> dict:
    rows = {item["ticker"]: item for item in ensure_day(http, day, required=True)}
    cash = ledger.cash
    positions = dict(ledger.positions)
    commission_rate = Decimal(str(commission))
    tax_rate = Decimal(str(tax))
    fills = []
    fee_total = Decimal("0")
    tax_total = Decimal("0")
    for order in ledger.pending["orders"]:
        ticker = order["ticker"]
        shares = int(order["shares"])
        quote = rows.get(ticker)
        if quote is None or quote["volume"] <= 0:
            fills.append({"ticker": ticker, "side": order["side"], "shares": shares, "status": "unfilled"})
            continue
        price = Decimal(str(quote["vwap"]))
        notional = price * shares
        fee = money(notional * commission_rate)
        fee_total += fee
        if order["side"] == "BUY":
            cash -= notional + fee
            positions[ticker] = positions.get(ticker, 0) + shares
            fills.append({"ticker": ticker, "side": "BUY", "shares": shares, "vwap": float(price), "fee": float(fee), "status": "filled"})
        else:
            paid_tax = money(notional * tax_rate)
            tax_total += paid_tax
            cash += notional - fee - paid_tax
            positions[ticker] = positions.get(ticker, 0) - shares
            fills.append(
                {
                    "ticker": ticker,
                    "side": "SELL",
                    "shares": shares,
                    "vwap": float(price),
                    "fee": float(fee),
                    "tax": float(paid_tax),
                    "status": "filled",
                }
            )
        if positions.get(ticker, 0) < 0:
            raise PlanError(f"{ticker} 結算後股數為負，請改放官方庫存")
    if cash < 0:
        raise PlanError("結算後現金為負。請到競賽後台下載庫存，存成 inbox/official_book.json 後再執行")
    ledger.cash = money(cash)
    ledger.positions = {ticker: int(shares) for ticker, shares in positions.items() if int(shares) > 0}
    for ticker, shares in ledger.positions.items():
        if shares % 1000 != 0:
            raise PlanError(f"{ticker} 結算股數 {shares} 不是 1000 的倍數")
    ledger.last_settled_date = day.isoformat()
    ledger.pending = None
    ledger.reconciled = False
    return {"date": day.isoformat(), "fills": fills, "fees": float(fee_total), "tax": float(tax_total)}


def _write_book(as_of: date, trade_day: date, ledger: Ledger, prices: dict[str, float], names: dict[str, str], note: str) -> Path:
    nav = ledger.cash
    positions = []
    for ticker, shares in sorted(ledger.positions.items()):
        if shares <= 0:
            continue
        if ticker not in prices:
            raise PlanError(f"記帳時 {ticker} 沒有 {as_of} 收盤價")
        market_value = Decimal(shares) * Decimal(str(prices[ticker]))
        nav += market_value
        positions.append(
            {
                "ticker": ticker,
                "name": names.get(ticker, ticker),
                "shares": int(shares),
                "close": prices[ticker],
                "market_value": float(money(market_value)),
            }
        )
    nav = money(nav)
    for row in positions:
        row["weight"] = round(row["market_value"] / float(nav), 6) if nav else 0
    history = [item for item in ledger.nav_history if item["date"] != as_of.isoformat()]
    previous = history[-1]["nav"] if history else None
    payload = {
        "as_of": as_of.isoformat(),
        "for_trade_date": trade_day.isoformat(),
        "cash": float(ledger.cash),
        "nav": float(nav),
        "cash_pct": round(float(ledger.cash / nav), 6) if nav else None,
        "holdings_count": len(positions),
        "daily_pnl": None if previous is None else round(float(nav) - float(previous), 2),
        "note": note,
        "positions": positions,
    }
    NAV_DIR.mkdir(parents=True, exist_ok=True)
    path = NAV_DIR / f"記帳_{as_of.isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (NAV_DIR / f"記帳_{as_of.isoformat()}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "shares", "close", "market_value", "weight"])
        writer.writeheader()
        for row in positions:
            writer.writerow({key: row[key] for key in writer.fieldnames})
    ledger.nav_history = history + [{"date": as_of.isoformat(), "nav": float(nav), "cash": float(ledger.cash)}]
    return path


def _sources(material: dict, prepared: dict, tickers: list[str]) -> list[dict]:
    markets = prepared["markets"]
    metrics = prepared["metrics"]
    keys = ["twse"]
    if any(markets.get(ticker) == "tpex" for ticker in tickers):
        keys.append("tpex")
    if any(
        markets.get(ticker) != "tpex"
        and (
            (metrics.get(ticker) or {}).get("pe")
            or (metrics.get(ticker) or {}).get("pb")
            or (metrics.get(ticker) or {}).get("dividend_yield") is not None
        )
        for ticker in tickers
    ):
        keys.append("valuation")
    if any(markets.get(ticker) != "tpex" and (metrics.get(ticker) or {}).get("rev_yoy") is not None for ticker in tickers):
        keys.append("rev_twse")
    if any(markets.get(ticker) == "tpex" and (metrics.get(ticker) or {}).get("rev_yoy") is not None for ticker in tickers):
        keys.append("rev_tpex")
    if prepared["market"].get("foreign_bn") is not None:
        keys.append("bfi")
    for item in (prepared["market"].get("us") or {}).values():
        keys.append(item["source_key"])
    sources = []
    for key in keys:
        row = dict(material[key])
        row["key"] = key
        sources.append(row)
    return sources


def run(trade_day: date | None = None) -> Path:
    _stdio()
    started = taipei_now()
    settings = load_settings()
    strategy = load_strategy()
    prospectus = load_prospectus()
    factors = enabled_factors(strategy, prospectus)
    print("啟用因子：" + "、".join(f"{fid} {weight:.0%}" for fid, weight, _params in factors))
    http = Http()
    closed = load_holidays(http)
    trade_day = trade_day or default_trade_date(taipei_now(), closed)
    if not is_trading_day(trade_day, closed):
        raise PlanError(f"{trade_day} 不是交易日")
    pricing = previous_trading_day(trade_day, closed)
    print(f"交易日 {trade_day}，用 {pricing} 收盤價定價")
    contest = parse_date(str(settings["contest_first_trade_date"]))
    if trade_day < contest:
        print(f"注意：這份決策的交易日早於設定的初賽首個交易日 {contest}。確認賽程後再上傳。")
    ledger = load_ledger(float(settings["initial_capital"]))
    official = apply_official_book(ledger, pricing)
    if official:
        print(official)
    elif ledger.pending and str(ledger.pending.get("trade_date")) < trade_day.isoformat():
        settled_day = parse_date(str(ledger.pending["trade_date"]))
        print(f"結算 {settled_day} 的委託（成交價用當日均價）")
        report = _settle(ledger, settled_day, float(settings["commission_rate"]), float(settings["tax_rate"]), http)
        filled = sum(1 for item in report["fills"] if item["status"] == "filled")
        missed = sum(1 for item in report["fills"] if item["status"] == "unfilled")
        print(f"  成交 {filled} 筆，無成交 {missed} 筆，手續費 {report['fees']:.0f}，證交稅 {report['tax']:.0f}")
        save_ledger(ledger)
    benchmarks = load_benchmarks()
    prepared = prepare(http, settings, factors, pricing, ledger.positions, closed)
    note = "官方庫存" if ledger.reconciled else "本系統依均價結算"
    book_path = _write_book(pricing, trade_day, ledger, prepared["prices"], prepared["names"], note)
    print(f"記帳已寫入 {book_path}")
    regime, regime_score, counter = judge_regime(prepared["market"], strategy["regime_rules"])
    print(f"市場判定 {regime}（分數 {regime_score}）")
    plan = construct(
        scores=prepared["scores"],
        metrics=prepared["metrics"],
        prices=prepared["prices"],
        industries=prepared["industries"],
        held=ledger.positions,
        cash=ledger.cash,
        portfolio=strategy["portfolio"],
        benchmarks=benchmarks,
        regime=regime,
        commission=float(settings["commission_rate"]),
        tax=float(settings["tax_rate"]),
    )
    covered = list(ledger.positions) + [order["ticker"] for order in plan.orders]
    finished = taipei_now()
    team_id = str(settings.get("team_id") or "")
    document = render_dplan(
        prospectus=prospectus,
        market=prepared["market"],
        names=prepared["names"],
        markets=prepared["markets"],
        prices=prepared["prices"],
        metrics=prepared["metrics"],
        scores=prepared["scores"],
        components=prepared["components"],
        enabled=[fid for fid, _weight, _params in factors],
        held=ledger.positions,
        plan=plan,
        regime=regime,
        counter=counter,
        sources=_sources(prepared["source_material"], prepared, covered),
        team_id=team_id or "TEAM_PREVIEW",
        trade_date=trade_day.isoformat(),
        model_provider=str(settings["model_provider"]),
        model_version=str(settings["model_version"]),
        code_version=code_version(),
        started=started,
        finished=finished,
        http_calls=http.calls,
        band=float(strategy["portfolio"]["rebalance_band"]),
    )
    if not valid_team_id(team_id):
        document["team_id"] = "TEAM_PREVIEW"
    errors, warnings = semantic_errors(
        document,
        {
            "held": ledger.positions,
            "prices": prepared["prices"],
            "pre_nav": plan.pre_nav,
            "cash": ledger.cash,
            "commission": settings["commission_rate"],
            "tax": settings["tax_rate"],
            "benchmarks": benchmarks,
            "active_share_min": strategy["portfolio"]["active_share_min"],
        },
    )
    for warning in warnings:
        print("提醒：" + warning)
    if not benchmarks:
        print(f"主動 ETF 清單在 data/reference/active_etfs.csv（這次看到 {len(prepared['active_etfs'])} 檔）。")
        print("請把各檔前十大持股填進 config/benchmarks.csv，否則 Active Share 限制還沒被檢查。")
    if errors:
        preview = PREVIEW_DIR / f"D-Plan_INVALID_{trade_day.isoformat()}.json"
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        preview.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        raise PlanError("決策書沒有通過檢查，尚未覆寫正式檔：\n- " + "\n- ".join(errors[:30]))
    DECISION_DIR.mkdir(parents=True, exist_ok=True)
    if valid_team_id(team_id):
        path = DECISION_DIR / f"D-Plan_{team_id}_{trade_day.isoformat()}.json"
    else:
        path = DECISION_DIR / f"D-Plan_TEAM_PREVIEW_{trade_day.isoformat()}.json"
        print("還沒設定 team_id，檔名暫用 TEAM_PREVIEW。請先填 config/settings.yaml，否則不要上傳這份。")
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    contest_open = trade_day >= contest and valid_team_id(team_id)
    if contest_open:
        ledger.pending = {
            "trade_date": trade_day.isoformat(),
            "pricing_date": pricing.isoformat(),
            "orders": [
                {"ticker": order["ticker"], "side": order["side"], "shares": int(order["shares"])}
                for order in document["orders"]
            ],
        }
    else:
        ledger.pending = None
        print("這份還沒進入正式繳交，帳本不會預掛這些委託，避免之後被當成已成交。")
    save_ledger(ledger)
    cash_pct = float(plan.cash_after / plan.nav_after)
    print(
        f"委託 {len(document['orders'])} 筆，成交後約 {len(plan.positions_after)} 檔，"
        f"現金約 {cash_pct:.2%}，淨曝險 {plan.intent}。"
    )
    print(f"決策書：{path}")
    print(f"完成時間 {stamp(finished)}。請把決策書上傳到競賽系統，收件到交易日 08:55。")
    return path


def check_file(path: str) -> None:
    _stdio()
    document = json.loads(Path_read(path))
    errors = semantic_errors(document, _empty_context(document))[0]
    if errors:
        raise PlanError("\n".join(errors[:40]))
    print(f"{path} 結構檢查通過。這次沒有帶帳本，所以沒有重算股數。")


def Path_read(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def _empty_context(document: dict) -> dict:
    return {
        "held": {},
        "prices": {},
        "pre_nav": 1,
        "cash": 0,
        "commission": 0.001425,
        "tax": 0.003,
        "benchmarks": {},
        "active_share_min": 0.2,
    }
