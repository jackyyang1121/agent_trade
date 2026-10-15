from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

from agent.config import SCHEMA_PATH
from agent.portfolio import active_share, active_share_basket_size, estimate
from agent.util import PlanError, target_shares


def schema_errors(doc: dict) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = []
    for error in sorted(validator.iter_errors(doc), key=lambda item: tuple(map(str, item.path))):
        path = "/".join(str(part) for part in error.path) or "(root)"
        errors.append(f"{path}: {error.message}")
    def finite(value, path="(root)"):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{path}: JSON 數值必須有限")
        elif isinstance(value, dict):
            for key, child in value.items():
                finite(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                finite(child, f"{path}/{index}")
    finite(doc)
    return errors


def _ids(items: list[dict], key: str, prefix: str) -> list[str]:
    found = [item[key] for item in items]
    expected = [f"{prefix}{index}" for index in range(1, len(found) + 1)]
    if found != expected:
        return [f"{prefix} 編號應為連續的 {expected}，實際是 {found}"]
    return []


def structural_errors(doc: dict, *, as_of=None, competition: bool = False, data_as_of=None) -> list[str]:
    """Validate schema, timestamps and evidence links without a portfolio book."""
    errors: list[str] = []
    errors.extend(schema_errors(doc))
    # The schema guards every indexed field below. Never inspect malformed layers.
    if errors:
        return errors
    metadata = doc["agent_metadata"]
    times = {}
    for key in ("run_started_at", "run_completed_at"):
        try:
            times[key] = datetime.fromisoformat(metadata[key])
        except ValueError:
            errors.append(f"agent_metadata/{key}: 無效日期時間")
    try:
        date.fromisoformat(doc["trade_date"])
    except ValueError:
        errors.append("trade_date: 無效日期")
    started, finished = times.get("run_started_at"), times.get("run_completed_at")
    if started and finished and started > finished:
        errors.append("研究起始時間晚於完成時間")
    received = as_of
    if received is not None:
        try:
            received = datetime.fromisoformat(received) if isinstance(received, str) else received
            if received.tzinfo is None:
                raise ValueError("timezone required")
            if finished and finished > received:
                errors.append("研究完成時間晚於驗證/收件時間")
        except (AttributeError, ValueError, TypeError):
            errors.append("驗證時點必須包含有效時區")
            received = None
    cutoff = min(finished, received) if finished and received else finished or received
    if data_as_of is not None:
        try:
            data_cutoff = datetime.fromisoformat(data_as_of) if isinstance(data_as_of, str) else data_as_of
            if data_cutoff.tzinfo is None:
                raise ValueError("timezone required")
            cutoff = min(cutoff, data_cutoff) if cutoff else data_cutoff
        except (AttributeError, ValueError, TypeError):
            errors.append("資料截止時點必須包含有效時區")
    for source in doc["sources"]:
        address = urlparse(source["url"])
        if address.scheme not in {"http", "https"} or not address.hostname:
            errors.append(f"{source['source_id']} 來源必須是 HTTP(S) 公開網址")
        if competition and address.hostname and (address.hostname in {"localhost", "127.0.0.1", "example.com", "example.org"} or address.hostname.endswith((".invalid", ".test", ".localhost"))):
            errors.append(f"{source['source_id']} 正式來源不能使用範例或本機網址")
        parsed = {}
        for key in ("content_as_of", "published_at", "fetched_at"):
            if key not in source:
                continue
            try:
                parsed[key] = datetime.fromisoformat(source[key])
                if cutoff and parsed[key] > cutoff:
                    errors.append(f"{source['source_id']}/{key} 使用決策完成後才可得的資料")
            except ValueError:
                errors.append(f"{source['source_id']}/{key} 無效日期時間")
        if parsed.get("published_at") and parsed.get("fetched_at") and parsed["published_at"] > parsed["fetched_at"]:
            errors.append(f"{source['source_id']} 發布時間晚於抓取時間")
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
    return errors


def semantic_errors(doc: dict, ctx: dict | None = None) -> tuple[list[str], list[str]]:
    errors = schema_errors(doc)
    warnings: list[str] = []
    if errors:
        return errors, warnings
    ctx = ctx or {}
    competition = ctx.get("mode") == "competition" or ctx.get("competition") is True
    validation_time = ctx.get("validation_time") or ctx.get("received_at") or ctx.get("as_of")
    errors = structural_errors(doc, as_of=validation_time, competition=competition,
                               data_as_of=ctx.get("as_of") if ctx.get("validation_time") or ctx.get("received_at") else None)
    decision_ids = {item["decision_id"] for item in doc["decisions"]}
    decisions = {item["decision_id"]: item for item in doc["decisions"]}

    missing_context = {"held", "prices", "pre_nav", "cash", "commission", "tax"} - set(ctx)
    if missing_context:
        errors.append("缺少必要的帳本/價格驗證 context：" + ",".join(sorted(missing_context)))
        return errors, warnings
    try:
        held = {}
        for ticker, value in ctx["held"].items():
            shares = Decimal(str(value))
            if isinstance(value, bool) or not shares.is_finite() or shares < 0 or shares != shares.to_integral_value() or shares % 1000:
                raise ValueError(f"{ticker} 持股必須是非負千股整數")
            if shares:
                held[str(ticker)] = int(shares)
        prices = dict(ctx["prices"])
        if not isinstance(prices, dict):
            raise ValueError("prices 必須是股票價格物件")
        nav = float(ctx["pre_nav"])
        for key in ("cash", "commission", "tax"):
            value = Decimal(str(ctx[key]))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{key} 必須是有限非負數")
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        errors.append(f"帳本/價格驗證 context 無效：{exc}")
        return errors, warnings
    required_tickers = set(held) | {item["ticker"] for item in doc["orders"]} | {item["ticker"] for item in doc["decisions"]}
    invalid_prices = set()
    for ticker in required_tickers:
        try:
            price = float(prices[ticker])
            if not math.isfinite(price) or price <= 0:
                raise ValueError("not positive and finite")
            prices[ticker] = price
        except (TypeError, ValueError, KeyError):
            invalid_prices.add(ticker)
            errors.append(f"{ticker} 缺少有限正數的必要定價資料（停牌/無成交時禁止推測價格）")
    if not math.isfinite(nav) or nav <= 0:
        errors.append("前日淨值必須是有限正數")
    universe = ctx.get("universe") or ctx.get("universe_rows")
    if universe:
        allowed = {str(item["ticker"]) if isinstance(item, dict) else str(item) for item in universe}
        unknown = required_tickers - allowed
        if unknown:
            errors.append("股票不在固定合格股票池：" + ",".join(sorted(unknown)))
    elif competition:
        errors.append("正式競賽缺少固定合格股票池")
    if invalid_prices or not math.isfinite(nav) or nav <= 0:
        return errors, warnings
    observed_nav = Decimal(str(ctx["cash"])) + sum((Decimal(shares) * Decimal(str(prices[ticker])) for ticker, shares in held.items()), Decimal(0))
    if abs(observed_nav - Decimal(str(nav))) > Decimal("0.02"):
        errors.append("前日淨值與已確認現金及持股估值不一致")
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
            dec = decisions.get(order["decision_ref"])
            if dec is None:
                continue
            if dec["decision_id"] in funding_ids:
                continue
            kept.append(order)
        cash_wo, nav_wo, _pos, _buy, _sell = estimate(cash, held, kept, prices, commission, tax, allow_negative_cash=True)
        pct_wo = float(cash_wo / nav_wo) if nav_wo else -1
        low = doc["market_view"]["posture"]["target_cash_pct_range"][0]
        if pct_wo >= low - 1e-9:
            errors.append("標記了資金調度，但拿掉這些賣出後現金仍沒有跌破宣告下限")

    our = {}
    if nav_after > 0:
        for ticker, shares in positions.items():
            our[ticker] = shares * prices[ticker] / float(nav_after)
    benchmarks = ctx.get("benchmarks") or {}
    definition = str(ctx.get("active_share_definition") or "top15_normalized")
    if competition and definition not in {"top15_normalized", "top15_raw"}:
        errors.append("正式競賽 Active Share 必須使用前 15 大持股的已確認定義")
    try:
        basket = active_share_basket_size(definition) or 15
    except PlanError:
        basket = 15
    if not benchmarks:
        message = f"沒有主動 ETF 前{basket}大持股，Active Share 這條競賽限制尚未檢查"
        (errors if competition else warnings).append(message)
    else:
        if competition and not ctx.get("active_share_definition_confirmed"):
            errors.append("Active Share 官方計算定義尚未確認")
        expected = set(ctx.get("benchmark_etf_ids") or [])
        if expected and expected != set(benchmarks):
            errors.append("Active Share 基準名單與已確認完整清單不一致")
        try:
            minimum = float(ctx.get("active_share_min", 0.15))
            if isinstance(ctx.get("active_share_min"), bool) or not math.isfinite(minimum) or not 0 <= minimum <= 1:
                raise ValueError("門檻必須是 0 到 1 的有限比率")
            if competition and minimum < 0.15:
                errors.append("正式競賽 Active Share 門檻不得低於 15%")
        except (TypeError, ValueError):
            errors.append("Active Share 門檻必須是 0 到 1 的有限比率")
            return errors, warnings
        for etf_id, bench in benchmarks.items():
            try:
                valid_weights = isinstance(bench, dict) and len(bench) == basket and all(math.isfinite(float(value)) and float(value) > 0 for value in bench.values())
            except (ValueError, TypeError):
                valid_weights = False
            if not valid_weights:
                errors.append(f"{etf_id} 基準缺少完整前{basket}大正數權重")
                continue
            try:
                score = active_share(our, bench, definition)
            except (ValueError, PlanError) as exc:
                errors.append(f"{etf_id} Active Share 定義無效：{exc}")
                continue
            if score < minimum - 1e-9:
                errors.append(f"相對 {etf_id} 的 Active Share {score:.2%} 低於 {minimum:.0%}")
    return errors, warnings
