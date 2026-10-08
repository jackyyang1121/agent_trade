from __future__ import annotations

import json
import re
from decimal import Decimal

from jsonschema import Draft202012Validator, FormatChecker

from agent.config import SCHEMA_PATH
from agent.portfolio import active_share, estimate
from agent.util import PlanError, target_shares


def schema_errors(doc: dict) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = []
    for error in sorted(validator.iter_errors(doc), key=lambda item: list(item.path)):
        path = "/".join(str(part) for part in error.path) or "(root)"
        errors.append(f"{path}: {error.message}")
    return errors


def _ids(items: list[dict], key: str, prefix: str) -> list[str]:
    found = [item[key] for item in items]
    expected = [f"{prefix}{index}" for index in range(1, len(found) + 1)]
    if found != expected:
        return [f"{prefix} 編號應為連續的 {expected}，實際是 {found}"]
    return []


def semantic_errors(doc: dict, ctx: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(schema_errors(doc))
    errors.extend(_ids(doc["sources"], "source_id", "S"))
    errors.extend(_ids(doc["observations"], "obs_id", "O"))
    errors.extend(_ids(doc["inferences"], "inf_id", "I"))
    errors.extend(_ids(doc["decisions"], "decision_id", "D"))

    source_ids = {item["source_id"] for item in doc["sources"]}
    obs_ids = {item["obs_id"] for item in doc["observations"]}
    inf_ids = {item["inf_id"] for item in doc["inferences"]}
    decision_ids = {item["decision_id"] for item in doc["decisions"]}
    decisions = {item["decision_id"]: item for item in doc["decisions"]}

    for obs in doc["observations"]:
        for ref in obs["source_ref"]:
            if ref not in source_ids:
                errors.append(f"{obs['obs_id']} 引用不存在的來源 {ref}")
    for ref in doc["market_view"]["basis_refs"]:
        if ref not in obs_ids:
            errors.append(f"market_view 引用不存在的事實 {ref}")
    for inf in doc["inferences"]:
        for ref in inf["premise_refs"]:
            if ref not in obs_ids:
                errors.append(f"{inf['inf_id']} 引用不存在的事實 {ref}")
    for dec in doc["decisions"]:
        for ref in dec["inference_refs"]:
            if ref not in inf_ids:
                errors.append(f"{dec['decision_id']} 引用不存在的推論 {ref}")
        for ref in dec.get("funding_for") or []:
            target = decisions.get(ref)
            if target is None or target["action"] not in {"BUY", "ADD"}:
                errors.append(f"{dec['decision_id']} 的 funding_for 必須指向同日買進決策")
            if dec["action"] not in {"TRIM", "SELL_ALL"}:
                errors.append(f"{dec['decision_id']} 不是賣出，不能標記 funding_for")
    for item in doc["no_trade_decisions"]:
        for ref in item["reason_refs"]:
            if ref not in inf_ids:
                errors.append(f"{item['ticker']} 續抱引用不存在的推論 {ref}")
    for order in doc["orders"]:
        if order["decision_ref"] not in decision_ids:
            errors.append(f"{order['ticker']} 委託引用不存在的決策 {order['decision_ref']}")

    held = {str(k): int(v) for k, v in ctx["held"].items() if int(v) > 0}
    prices = ctx["prices"]
    nav = float(ctx["pre_nav"])
    decision_tickers = [item["ticker"] for item in doc["decisions"]]
    hold_tickers = [item["ticker"] for item in doc["no_trade_decisions"]]
    if len(decision_tickers) != len(set(decision_tickers)):
        errors.append("decisions 有重複代號")
    if len(hold_tickers) != len(set(hold_tickers)):
        errors.append("no_trade_decisions 有重複代號")
    overlap = set(decision_tickers) & set(hold_tickers)
    if overlap:
        errors.append("同一檔同時出現在交易與續抱：" + ",".join(sorted(overlap)))
    missing = [ticker for ticker in held if ticker not in set(decision_tickers) | set(hold_tickers)]
    if missing:
        errors.append("昨日持股沒有交代：" + ",".join(sorted(missing)))
    extra_hold = [ticker for ticker in hold_tickers if ticker not in held]
    if extra_hold:
        errors.append("續抱名單有未持有的代號：" + ",".join(extra_hold))

    orders_by_ticker = {}
    for order in doc["orders"]:
        if order["ticker"] in orders_by_ticker:
            errors.append(f"{order['ticker']} 有兩筆委託")
        orders_by_ticker[order["ticker"]] = order
        if order["shares"] > held.get(order["ticker"], 0) and order["side"] == "SELL":
            errors.append(f"{order['ticker']} 賣出超過持有")

    for dec in doc["decisions"]:
        ticker = dec["ticker"]
        price = prices.get(ticker)
        if price is None:
            errors.append(f"{ticker} 沒有價格，無法核對委託")
            continue
        expected = target_shares(dec["target_weight"], nav, price)
        old = held.get(ticker, 0)
        delta = expected - old
        order = orders_by_ticker.get(ticker)
        if delta == 0:
            errors.append(f"{ticker} 的目標權重換算後股數沒有變化，不該列為決策")
            continue
        if order is None:
            errors.append(f"{ticker} 有決策但沒有委託")
            continue
        side = "BUY" if delta > 0 else "SELL"
        if order["side"] != side or order["shares"] != abs(delta) or order["decision_ref"] != dec["decision_id"]:
            errors.append(f"{ticker} 委託與官方公式不一致，應為 {side} {abs(delta)}")
        if old == 0 and dec["action"] != "BUY":
            errors.append(f"{ticker} 應為 BUY")
        if old > 0 and expected == 0 and dec["action"] != "SELL_ALL":
            errors.append(f"{ticker} 應為 SELL_ALL")
        if old > 0 and expected > old and dec["action"] != "ADD":
            errors.append(f"{ticker} 應為 ADD")
        if old > 0 and 0 < expected < old and dec["action"] != "TRIM":
            errors.append(f"{ticker} 應為 TRIM")
        if ticker != "2330" and dec["target_weight"] > 0.10 + 1e-12:
            errors.append(f"{ticker} 目標權重超過 10%")
        if dec["target_weight"] > 0.25 + 1e-12:
            errors.append(f"{ticker} 目標權重超過 25%")

    if set(orders_by_ticker) != set(decision_tickers):
        errors.append("委託代號與決策代號不一致")

    cash = Decimal(str(ctx["cash"]))
    commission = Decimal(str(ctx["commission"]))
    tax = Decimal(str(ctx["tax"]))
    try:
        cash_after, nav_after, positions, buy, sell = estimate(cash, held, doc["orders"], prices, commission, tax)
    except (PlanError, KeyError) as exc:
        errors.append(str(exc))
        return errors, warnings

    count = sum(1 for shares in positions.values() if shares > 0)
    if not 20 <= count <= 30:
        errors.append(f"成交後持股 {count} 檔，需落在 20–30")
    if nav_after <= 0:
        errors.append("估算淨值不是正數")
    else:
        cash_pct = float(cash_after / nav_after)
        low, high = doc["market_view"]["posture"]["target_cash_pct_range"]
        if low > high:
            errors.append("現金區間下限高於上限")
        if cash_pct < low - 1e-6 or cash_pct > high + 1e-6:
            errors.append(f"估算現金比 {cash_pct:.4%} 不在宣告區間 [{low:.4%}, {high:.4%}]")
        if cash_after < 0 or cash_pct >= 0.25:
            errors.append(f"估算現金比 {cash_pct:.4%} 碰到 0% 或 25% 限制")
        for ticker, shares in positions.items():
            weight = shares * prices[ticker] / float(nav_after)
            limit = 0.25 if ticker == "2330" else 0.10
            if weight > limit + 1e-6:
                errors.append(f"{ticker} 成交後權重 {weight:.2%} 超過上限")

    net = buy - sell
    tol = Decimal(str(nav)) * Decimal("0.02")
    intent = doc["market_view"]["posture"]["net_exposure_intent"]
    if intent == "increase" and net <= 0:
        errors.append("宣告提高曝險，但委託不是淨買超")
    if intent == "reduce" and net >= 0:
        errors.append("宣告降低曝險，但委託不是淨賣超")
    if intent == "hold" and abs(net) > tol:
        errors.append("宣告持平，但淨買賣超過淨值 2%")

    funding_ids = []
    for dec in doc["decisions"]:
        if dec.get("funding_for"):
            funding_ids.append(dec["decision_id"])
    if funding_ids:
        kept = []
        for order in doc["orders"]:
            dec = decisions[order["decision_ref"]]
            if dec["decision_id"] in funding_ids:
                continue
            kept.append(order)
        cash_wo, nav_wo, _pos, _buy, _sell = estimate(cash, held, kept, prices, commission, tax)
        pct_wo = float(cash_wo / nav_wo) if nav_wo else -1
        low = doc["market_view"]["posture"]["target_cash_pct_range"][0]
        if pct_wo >= low - 1e-9:
            errors.append("標記了資金調度，但拿掉這些賣出後現金仍沒有跌破宣告下限")

    our = {}
    if nav_after > 0:
        for ticker, shares in positions.items():
            our[ticker] = shares * prices[ticker] / float(ctx["pre_nav"])
    benchmarks = ctx.get("benchmarks") or {}
    if not benchmarks:
        warnings.append("沒有主動 ETF 前十大持股，Active Share 這條競賽限制尚未檢查")
    else:
        for etf_id, bench in benchmarks.items():
            score = active_share(our, bench)
            if score < float(ctx.get("active_share_min", 0.20)) - 1e-9:
                errors.append(f"相對 {etf_id} 的 Active Share {score:.2%} 低於 20%")
    return errors, warnings
