from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math

from agent.util import LOT, PlanError, fit_shares, money, target_shares


@dataclass
class Construction:
    share_targets: dict[str, int]
    weights: dict[str, float]
    orders: list[dict]
    cash_after: Decimal
    nav_after: Decimal
    pre_nav: float
    buy_notional: Decimal
    sell_notional: Decimal
    positions_after: dict[str, int]
    intent: str
    cash_low: float
    cash_high: float
    funding_sells: set[str] = field(default_factory=set)
    method: str = "heuristic_constrained_allocation_v2"
    diagnostics: dict = field(default_factory=dict)


def cap_of(ticker: str, portfolio: dict) -> float:
    regulatory = 0.25 if ticker == "2330" else 0.10
    configured = float(portfolio["cap_2330"] if ticker == "2330" else portfolio["cap_other"])
    decision = float(portfolio.get("_decision_caps", {}).get(ticker, configured))
    return max(0.0, min(regulatory, configured, decision))


# 2026-10-05 主辦公告改為比較前 15 大，低於 15% 才違規。
# top10_* 只保留既有公式測試；正式競賽只接受尚未二選一的 top15_*。
ACTIVE_SHARE_DEFINITIONS = {
    "top10_normalized": (10, True),
    "top10_raw": (10, False),
    "top15_normalized": (15, True),
    "top15_raw": (15, False),
    "full_normalized": (None, True),
}


def active_share_basket_size(definition: str) -> int | None:
    try:
        size, _normalized = ACTIVE_SHARE_DEFINITIONS[definition]
    except KeyError as exc:
        raise PlanError(f"未知 Active Share 定義 {definition}") from exc
    return size


def active_share(our_weights: dict[str, float], bench_weights: dict[str, float], definition: str = "top10_normalized") -> float:
    """Basket formula. Official top-15 normalization is still unconfirmed.

    The 2026-10-05 organizer notice compares the top 15 holdings and treats
    Active Share below 15% as a violation. It does not say whether those
    weights are renormalized. Callers must confirm top15_normalized or
    top15_raw before a formal submission.
    """
    if any(not math.isfinite(float(v)) or float(v) < 0 for v in list(our_weights.values()) + list(bench_weights.values())):
        raise PlanError("Active Share 權重必須為非負有限數值")
    size, normalized = ACTIVE_SHARE_DEFINITIONS.get(definition, (None, None))
    if definition not in ACTIVE_SHARE_DEFINITIONS:
        raise PlanError(f"未知 Active Share 定義 {definition}")
    if size is None:
        return _active_share_vectors(our_weights, bench_weights)
    our_top = dict(sorted(our_weights.items(), key=lambda item: (-item[1], item[0]))[:size])
    bench_top = dict(sorted(bench_weights.items(), key=lambda item: (-item[1], item[0]))[:size])
    if normalized:
        return _active_share_vectors(our_top, bench_top)
    return 0.5 * sum(abs(our_top.get(ticker, 0) - bench_top.get(ticker, 0)) for ticker in set(our_top) | set(bench_top))


def _renorm(weights: dict[str, float]) -> dict[str, float]:
    total = sum(value for value in weights.values() if value > 0)
    if total <= 0:
        return {}
    return {key: value / total for key, value in weights.items() if value > 0}


def _active_share_vectors(our: dict[str, float], bench: dict[str, float]) -> float:
    left = _renorm(our)
    right = _renorm(bench)
    keys = set(left) | set(right)
    return 0.5 * sum(abs(left.get(key, 0.0) - right.get(key, 0.0)) for key in keys)


def worst_active_share(weights: dict[str, float], benchmarks: dict[str, dict[str, float]], definition: str = "top10_normalized") -> tuple[float, str | None]:
    if not benchmarks:
        return 1.0, None
    worst = 1.0
    name = None
    for etf_id, bench in benchmarks.items():
        if not bench:
            raise PlanError(f"Active Share 基準 {etf_id} 沒有有效持股")
        score = active_share(weights, bench, definition)
        if score < worst:
            worst = score
            name = etf_id
    return worst, name


def repair_active_share(
    weights: dict[str, float],
    scores: dict[str, float],
    eligible: list[str],
    benchmarks: dict[str, dict[str, float]],
    buffer: float,
    cap_fn,
    definition: str = "top10_normalized",
) -> dict[str, float]:
    if not benchmarks:
        return weights
    current = dict(weights)
    for _ in range(12):
        worst, etf_id = worst_active_share(current, benchmarks, definition)
        if worst >= buffer or etf_id is None:
            break
        bench = benchmarks[etf_id]
        ranked = sorted(current, key=lambda ticker: current[ticker], reverse=True)[: active_share_basket_size(definition) or len(current)]
        overlap = max(ranked, key=lambda ticker: min(current[ticker], bench.get(ticker, 0.0)))
        replacement = next((ticker for ticker in eligible if ticker not in current and bench.get(ticker, 0.0) <= 0), None)
        if replacement is None or overlap == replacement:
            break
        moved = current.pop(overlap)
        current[replacement] = min(moved, cap_fn(replacement))
    return current


def _allocate(selected: list[str], scores: dict[str, float], invested: float, cap_fn, industries: dict[str, str], sector_cap: float) -> dict[str, float]:
    raw = {ticker: max(scores.get(ticker, 0.0), 0.0) for ticker in selected}
    if sum(raw.values()) <= 0:
        raw = {ticker: 1.0 for ticker in selected}
    total = sum(raw.values())
    weights = {ticker: value / total * invested for ticker, value in raw.items()}
    for _ in range(25):
        overflow = 0.0
        free: list[str] = []
        for ticker, value in list(weights.items()):
            limit = cap_fn(ticker)
            if value > limit:
                overflow += value - limit
                weights[ticker] = limit
            else:
                free.append(ticker)
        if overflow <= 1e-10:
            break
        room = sum(max(0.0, cap_fn(ticker) - weights[ticker]) for ticker in free)
        if room <= 1e-12:
            break
        for ticker in free:
            space = cap_fn(ticker) - weights[ticker]
            if space > 0:
                weights[ticker] += overflow * space / room
    for _ in range(15):
        buckets: dict[str, float] = {}
        for ticker, value in weights.items():
            key = industries.get(ticker, "NA")
            buckets[key] = buckets.get(key, 0.0) + value
        over = {key: value for key, value in buckets.items() if value > sector_cap + 1e-9}
        if not over:
            break
        freed = 0.0
        for ticker in list(weights):
            key = industries.get(ticker, "NA")
            if key in over and buckets[key] > 0:
                updated = weights[ticker] * sector_cap / buckets[key]
                freed += weights[ticker] - updated
                weights[ticker] = updated
        recipients = []
        for ticker in weights:
            if industries.get(ticker, "NA") in over:
                continue
            space = cap_fn(ticker) - weights[ticker]
            if space > 1e-12:
                recipients.append((ticker, space))
        if freed <= 1e-12 or not recipients:
            break
        room = sum(space for _ticker, space in recipients)
        for ticker, space in recipients:
            weights[ticker] += min(freed, room) * space / room
    return weights


def _correlation_pairs(selected: list[str], metrics: dict, portfolio: dict) -> list[tuple[str, str]]:
    """High absolute-correlation pair constraints, not transitive clusters.

    Broad market correlation can connect the entire eligible universe. A single
    connected-component cap would then conflict with the mandatory investment
    level. This transparent guardrail limits every observed high-correlation
    pair and reports that narrower scope explicitly; it does not claim to bound
    the weight of an entire market-connected component.
    """
    if not portfolio.get("max_correlated_weight"):
        return []
    threshold = float(portfolio.get("correlation_threshold", 0.85))
    pairs = []
    for i, left in enumerate(selected):
        for right in selected[i + 1:]:
            value = (metrics.get(left, {}).get("correlations") or {}).get(right)
            if value is None:
                value = (metrics.get(right, {}).get("correlations") or {}).get(left)
            if value is not None and math.isfinite(float(value)) and abs(float(value)) >= threshold:
                pairs.append((left, right))
    return pairs


def _limit_correlation(weights: dict, pairs: list[tuple[str, str]], limit: float) -> dict:
    current = dict(weights)
    # Only trim; later cash-band validation determines whether the constrained
    # portfolio is feasible. Never silently relax the configured guardrail.
    for _ in range(20):
        changed = False
        for left, right in pairs:
            total = current.get(left, 0) + current.get(right, 0)
            if total > limit + 1e-12:
                scale = limit / total
                current[left] *= scale
                current[right] *= scale
                changed = True
        if not changed:
            break
    return current


def judge_regime(market: dict, rules: dict) -> tuple[str, int, str | None]:
    score = 0
    opposed: list[str] = []
    taiex20 = market.get("taiex_ret_20")
    taiex5 = market.get("taiex_ret_5")
    sox = (market.get("us") or {}).get("SOX", {}).get("chg_pct")
    ndx = (market.get("us") or {}).get("NDX", {}).get("chg_pct")
    foreign = market.get("foreign_bn")

    def apply(value, up, down, label: str):
        nonlocal score
        if value is None:
            return
        if value >= up:
            score += 1
        elif value <= down:
            score -= 1

    apply(taiex20, rules["taiex_20_up"], rules["taiex_20_down"], "加權指數20日")
    apply(taiex5, rules["taiex_5_up"], rules["taiex_5_down"], "加權指數5日")
    apply(sox, rules["sox_up"], rules["sox_down"], "SOX")
    apply(ndx, rules["ndx_up"], rules["ndx_down"], "NDX")
    apply(foreign, rules["foreign_up_bn"], rules["foreign_down_bn"], "外資")
    if score >= int(rules["risk_on_score"]):
        regime = "risk_on"
    elif score <= int(rules["risk_off_score"]):
        regime = "risk_off"
    else:
        regime = "neutral"
    if regime != "risk_off" and sox is not None and sox <= rules["sox_down"]:
        opposed.append(f"SOX {sox:.2f}% 偏弱")
    if regime != "risk_off" and foreign is not None and foreign <= rules["foreign_down_bn"]:
        opposed.append(f"外資賣超 {abs(foreign):.1f} 億")
    if regime != "risk_on" and taiex20 is not None and taiex20 >= rules["taiex_20_up"]:
        opposed.append(f"加權指數近20日仍上漲 {taiex20 * 100:.2f}%")
    if regime != "risk_on" and sox is not None and sox >= rules["sox_up"]:
        opposed.append(f"SOX {sox:.2f}% 偏強")
    counter = "；".join(opposed) if opposed else None
    return regime, score, counter


def stance_of(regime: str, intent: str) -> str:
    if regime == "risk_on" and intent == "increase":
        return "aggressive"
    if regime == "risk_off" and intent == "reduce":
        return "defensive"
    return "neutral"


def _select_names(scores, metrics, prices, nav, held, portfolio, names_n, industries) -> tuple[list[str], list[str]]:
    # Hysteresis is measured on the score range, not presented as an expected
    # stock return. Fees/tax increase the hurdle; a small rank improvement alone
    # does not evict a holding. Defaults are deliberately labelled heuristic.
    spread = max(scores.values(), default=0.0) - min(scores.values(), default=0.0)
    hurdle = max(0.0, float(portfolio.get("min_score_improvement", 0.05)))
    hurdle += max(0.0, float(portfolio.get("_roundtrip_cost", 0.0)))
    retention = {t: scores[t] + (max(spread, 1e-6) * hurdle if held.get(t, 0) > 0 else 0.0) for t in scores}
    ranked = sorted(scores, key=lambda ticker: (-retention[ticker], ticker))
    eligible: list[str] = []
    for ticker in ranked:
        price = prices.get(ticker)
        if price is None or price <= 0:
            continue
        turnover = (metrics.get(ticker) or {}).get("avg_turnover")
        if held.get(ticker, 0) <= 0 and (turnover is None or turnover < float(portfolio["min_avg_turnover"])):
            continue
        if not math.isfinite(price) or LOT * price / nav > cap_of(ticker, portfolio):
            continue
        eligible.append(ticker)
    max_in_sector = max(3, int(names_n * float(portfolio["max_sector_weight"]) + 0.999))
    selected: list[str] = []
    counts: dict[str, int] = {}
    deferred: list[str] = []
    for ticker in eligible:
        if len(selected) >= names_n:
            break
        sector = industries.get(ticker, "NA")
        if counts.get(sector, 0) >= max_in_sector:
            deferred.append(ticker)
            continue
        selected.append(ticker)
        counts[sector] = counts.get(sector, 0) + 1
    for ticker in deferred:
        if len(selected) >= names_n:
            break
        selected.append(ticker)
    rank = {ticker: index for index, ticker in enumerate(eligible)}
    for ticker, shares in held.items():
        if shares <= 0 or ticker in selected or ticker not in prices or ticker not in eligible:
            continue
        if rank.get(ticker, 10**9) <= int(portfolio["keep_rank"]):
            if len(selected) >= names_n and selected:
                worst = min(selected, key=lambda item: (retention.get(item, -1e9), item))
                if retention.get(ticker, -1e9) >= retention.get(worst, -1e9):
                    selected.remove(worst)
                    selected.append(ticker)
            elif len(selected) < int(portfolio["max_names"]):
                selected.append(ticker)
    for ticker in eligible:
        if len(selected) >= int(portfolio["min_names"]):
            break
        if ticker not in selected:
            selected.append(ticker)
    return selected, eligible


def _share_plan(weights, held, prices, nav, portfolio, band: float) -> dict[str, int]:
    targets: dict[str, int] = {}
    for ticker, weight in weights.items():
        price = prices[ticker]
        ideal = target_shares(weight, nav, price, LOT)
        old = int(held.get(ticker, 0))
        # Reserve the maximum possible same-price cost erosion before applying
        # post-trade-NAV caps, including small scenario-based position limits.
        planning_limit = cap_of(ticker, portfolio) * (1 - portfolio.get("_roundtrip_cost", 0.0))
        cap_shares, _cap_weight = fit_shares(target_shares(planning_limit, nav, price, LOT), nav, price, planning_limit, LOT)
        ideal = min(ideal, cap_shares)
        if old > cap_shares:
            targets[ticker] = cap_shares
            continue
        if old > 0 and weight > 0:
            old_weight = old * price / nav
            if abs(weight - old_weight) < band:
                targets[ticker] = old
                continue
        targets[ticker] = ideal
    for ticker, old in held.items():
        if old > 0 and ticker not in targets:
            targets[ticker] = 0
    return targets


def _orders_from_targets(share_targets: dict[str, int], held: dict[str, int]) -> list[tuple[str, int, int, str]]:
    rows = []
    for ticker, target in share_targets.items():
        old = int(held.get(ticker, 0))
        if target == old:
            continue
        side = "BUY" if target > old else "SELL"
        rows.append((ticker, target, old, side))
    return rows


def _limit_orders(share_targets, held, prices, nav, portfolio) -> dict[str, int]:
    current = dict(share_targets)
    max_orders = int(portfolio["max_orders"])

    def notion(ticker: str, target: int, old: int) -> float:
        return abs(target - old) * prices[ticker]

    while True:
        orders = _orders_from_targets(current, held)
        if len(orders) <= max_orders:
            break
        orders.sort(key=lambda row: notion(row[0], row[1], row[2]))
        reverted = False
        for ticker, target, old, _side in orders:
            if ticker in portfolio.get("_force_exits", set()):
                continue
            if old > target:
                weight = old * prices[ticker] / nav
                if weight > cap_of(ticker, portfolio) + 1e-9:
                    continue
            if old > 0:
                current[ticker] = old
            else:
                current.pop(ticker, None)
            reverted = True
            break
        if not reverted:
            raise PlanError("委託超過 30 筆，而且剩下的都是必須做的減碼，組合無法合法縮減")

    while sum(1 for shares in current.values() if shares > 0) > int(portfolio["max_names"]):
        newcomers = [ticker for ticker, shares in current.items() if shares > 0 and held.get(ticker, 0) <= 0]
        if not newcomers:
            break
        victim = min(newcomers, key=lambda ticker: current[ticker] * prices[ticker])
        current.pop(victim, None)
    return current


def estimate(cash: Decimal, positions: dict[str, int], orders: list[dict], prices: dict[str, float], commission: Decimal, tax: Decimal, *, allow_negative_cash: bool = False):
    cash_now = Decimal(cash)
    if not cash_now.is_finite() or cash_now < 0 or commission < 0 or tax < 0:
        raise PlanError("現金與交易費率必須為非負有限數值")
    if any(int(shares) != shares or int(shares) < 0 for shares in positions.values()):
        raise PlanError("庫存不得為負或非整數")
    book = {ticker: int(shares) for ticker, shares in positions.items() if int(shares) > 0}
    buy_notional = Decimal("0")
    sell_notional = Decimal("0")
    traded: set[str] = set()
    for order in orders:
        ticker = order["ticker"]
        shares = int(order["shares"])
        if ticker in traded:
            raise PlanError(f"{ticker} 重複委託或當沖，不支援")
        traded.add(ticker)
        if shares != order["shares"] or shares <= 0 or shares % LOT or order["side"] not in {"BUY", "SELL"}:
            raise PlanError("委託必須為有效買賣方向與正整張股數")
        price = Decimal(str(prices[ticker]))
        if not price.is_finite() or price <= 0:
            raise PlanError(f"{ticker} 缺少有效定價")
        notional = price * shares
        fee = money(notional * commission)
        if order["side"] == "BUY":
            cash_now -= notional + fee
            buy_notional += notional
            book[ticker] = book.get(ticker, 0) + shares
        else:
            paid_tax = money(notional * tax)
            cash_now += notional - fee - paid_tax
            sell_notional += notional
            book[ticker] = book.get(ticker, 0) - shares
            if book[ticker] < 0:
                raise PlanError(f"{ticker} 賣出股數超過持有")
            if book[ticker] == 0:
                book.pop(ticker)
    nav = cash_now
    if cash_now < 0 and not allow_negative_cash:
        raise PlanError("交易成本計入後現金為負，禁止融資")
    for ticker, shares in book.items():
        nav += Decimal(shares) * Decimal(str(prices[ticker]))
    return money(cash_now), money(nav), book, buy_notional, sell_notional


def _intent(buy: Decimal, sell: Decimal, nav: float) -> str:
    net = buy - sell
    tol = Decimal(str(nav)) * Decimal("0.02")
    if net > tol:
        return "increase"
    if net < -tol:
        return "reduce"
    return "hold"


def _cash_band(cash_pct: float) -> tuple[float, float]:
    low = max(0.0, round(cash_pct - 0.015, 4))
    high = min(0.25, round(cash_pct + 0.015, 4))
    low = min(low, math_floor_4(cash_pct))
    high = max(high, math_ceil_4(cash_pct))
    low = max(0.0, min(low, 0.25))
    high = max(0.0, min(high, 0.25))
    if low > high:
        low, high = high, low
    return round(low, 4), round(high, 4)


def math_floor_4(value: float) -> float:
    return int(value * 10000) / 10000


def math_ceil_4(value: float) -> float:
    scaled = value * 10000
    base = int(scaled)
    if scaled - base > 1e-8:
        base += 1
    return base / 10000


def declared_weights(share_targets, held, prices, nav, portfolio) -> dict[str, float]:
    weights: dict[str, float] = {}
    for ticker, target in share_targets.items():
        old = int(held.get(ticker, 0))
        if target == old:
            continue
        _shares, weight = fit_shares(target, nav, prices[ticker], cap_of(ticker, portfolio), LOT)
        if _shares != target:
            share_targets[ticker] = _shares
            if _shares == old:
                continue
            weight = fit_shares(_shares, nav, prices[ticker], cap_of(ticker, portfolio), LOT)[1]
        weights[ticker] = weight
    return weights


def pretrade_nav(cash: Decimal, positions: dict[str, int], prices: dict[str, float]) -> Decimal:
    nav = Decimal(cash)
    if not nav.is_finite() or nav < 0:
        raise PlanError("現金無效或為負")
    for ticker, shares in positions.items():
        if int(shares) != shares or int(shares) < 0:
            raise PlanError(f"持股 {ticker} 不是非負整數")
        if int(shares) <= 0:
            continue
        if ticker not in prices:
            raise PlanError(f"持股 {ticker} 沒有定價日收盤價，無法下單")
        price = Decimal(str(prices[ticker]))
        if not price.is_finite() or price <= 0:
            raise PlanError(f"持股 {ticker} 沒有有效定價")
        nav += Decimal(int(shares)) * price
    return money(nav)


def construct(
    scores: dict[str, float],
    metrics: dict[str, dict],
    prices: dict[str, float],
    industries: dict[str, str],
    held: dict[str, int],
    cash: Decimal,
    portfolio: dict,
    benchmarks: dict[str, dict[str, float]],
    regime: str,
    commission: float,
    tax: float,
    decisions: dict[str, dict] | None = None,
) -> Construction:
    # Copy configuration: daily decisions must never leak caps into another
    # shadow variant, or mutate caller-owned configuration.
    portfolio = dict(portfolio)
    if "min_score_improvement" not in portfolio and "rebalance_score_margin" in portfolio:
        portfolio["min_score_improvement"] = portfolio["rebalance_score_margin"]
    for ticker, shares in held.items():
        if shares > 0 and metrics.get(ticker, {}).get("tradable") is False:
            raise PlanError(f"持股 {ticker} 停牌、無成交或行情過期，無法安全產生交易組合")
    definition = portfolio.get("active_share_definition", "top10_normalized")
    if portfolio.get("require_benchmarks") and not benchmarks:
        raise PlanError("正式模式缺少 Active Share 基準，禁止產生可提交組合")
    if portfolio.get("require_benchmarks") and not portfolio.get("active_share_definition_confirmed"):
        raise PlanError("Active Share 計算定義尚未確認")
    if benchmarks:
        worst_active_share({}, benchmarks, definition)
    decisions = decisions or {ticker: row["decision"] for ticker, row in metrics.items() if row.get("decision")}
    portfolio["_decision_caps"] = {ticker: float(row["max_weight"]) for ticker, row in decisions.items() if row.get("max_weight") is not None}
    portfolio["_force_exits"] = {ticker for ticker, row in decisions.items() if not row.get("eligible", False)}
    portfolio["_roundtrip_cost"] = 2 * max(0.0, commission) + max(0.0, tax)
    scores = {ticker: float(value) for ticker, value in scores.items()
              if math.isfinite(float(value)) and (not decisions.get(ticker) or decisions[ticker].get("eligible", False))}
    # A transparent risk haircut controls correlation and unusually high daily
    # volatility; independent raw signals remain unchanged in the archive.
    risk_scores = {}
    for ticker, score in scores.items():
        metric = metrics.get(ticker, {})
        vol = metric.get("volatility_20", metric.get("vol_20"))
        corr = metric.get("mean_correlation")
        vol_penalty = max(0.0, float(vol) / float(portfolio.get("volatility_reference", 0.03)) - 1) if vol is not None and math.isfinite(float(vol)) else 0.0
        corr_penalty = max(0.0, float(corr) - float(portfolio.get("correlation_reference", 0.70))) if corr is not None and math.isfinite(float(corr)) else 0.0
        risk_scores[ticker] = score / (1 + float(portfolio.get("risk_haircut", 0.25)) * (vol_penalty + corr_penalty)) if score > 0 else score
    scores = risk_scores
    nav_dec = pretrade_nav(cash, held, prices)
    nav = float(nav_dec)
    if nav <= 0:
        raise PlanError("淨值不是正數，請先檢查記帳或官方庫存")
    cash_target = float(portfolio["cash_by_regime"][regime])
    names_n = int(portfolio["target_names"])
    last_problem = "無法在限制內組成組合"
    commission_d = Decimal(str(commission))
    tax_d = Decimal(str(tax))
    for attempt in range(8):
        band = float(portfolio["rebalance_band"]) if attempt == 0 else 0.0
        selected, eligible = _select_names(scores, metrics, prices, nav, held, portfolio, names_n, industries)
        if len(selected) < int(portfolio["min_names"]):
            raise PlanError(f"流動性過濾後只剩 {len(selected)} 檔，少於 20 檔")
        invested = max(0.05, 1.0 - cash_target)
        weights = _allocate(
            selected,
            scores,
            invested,
            lambda ticker: cap_of(ticker, portfolio),
            industries,
            float(portfolio["max_sector_weight"]) * (1 - portfolio["_roundtrip_cost"]),
        )
        weights = repair_active_share(
            weights,
            scores,
            eligible,
            benchmarks,
            float(portfolio["active_share_buffer"]),
            lambda ticker: cap_of(ticker, portfolio),
            definition,
        )
        pairs = _correlation_pairs(list(weights), metrics, portfolio)
        if pairs:
            weights = _limit_correlation(weights, pairs, float(portfolio["max_correlated_weight"]) * (1 - portfolio["_roundtrip_cost"]))
        share_targets = _share_plan(weights, held, prices, nav, portfolio, band)
        share_targets = _limit_orders(share_targets, held, prices, nav, portfolio)
        declared = declared_weights(share_targets, held, prices, nav, portfolio)
        orders = []
        for ticker, weight in declared.items():
            target = target_shares(weight, nav, prices[ticker], LOT)
            old = int(held.get(ticker, 0))
            delta = target - old
            if delta == 0:
                continue
            orders.append(
                {
                    "ticker": ticker,
                    "side": "BUY" if delta > 0 else "SELL",
                    "shares": abs(delta),
                    "target_weight": weight,
                    "target_shares": target,
                }
            )
        cash_after, nav_after, positions_after, buy_notional, sell_notional = estimate(
            cash, held, orders, prices, commission_d, tax_d, allow_negative_cash=True
        )
        count = sum(1 for shares in positions_after.values() if shares > 0)
        cash_pct = float(cash_after / nav_after) if nav_after > 0 else -1
        overweight = []
        for ticker, shares in positions_after.items():
            weight = shares * prices[ticker] / float(nav_after)
            limit = cap_of(ticker, portfolio)
            if weight > limit + 1e-8:
                overweight.append(ticker)
        as_worst, as_name = worst_active_share(
            {ticker: shares * prices[ticker] / float(nav_after) for ticker, shares in positions_after.items()},
            benchmarks,
            definition,
        )
        sector_weights: dict[str, float] = {}
        for ticker, shares in positions_after.items():
            sector = industries.get(ticker, "NA")
            sector_weights[sector] = sector_weights.get(sector, 0) + shares * prices[ticker] / float(nav_after)
        sector_ok = all(weight <= float(portfolio["max_sector_weight"]) + 1e-8 for weight in sector_weights.values())
        actual_weights = {ticker: shares * prices[ticker] / float(nav_after) for ticker, shares in positions_after.items()}
        actual_pairs = _correlation_pairs(list(actual_weights), metrics, portfolio)
        correlation_ok = all(actual_weights[a] + actual_weights[b] <= float(portfolio["max_correlated_weight"]) + 1e-8
                             for a, b in actual_pairs)
        cash_ok = max(0.0, float(portfolio.get("cash_min", 0.02))) <= cash_pct <= min(0.25, float(portfolio.get("cash_max", 0.20))) and cash_pct < 0.25 and cash_after > 0
        count_ok = int(portfolio["min_names"]) <= count <= int(portfolio["max_names"])
        as_ok = (not benchmarks) or as_worst + 1e-9 >= float(portfolio["active_share_min"])
        if cash_ok and count_ok and not overweight and sector_ok and correlation_ok and as_ok and len(orders) <= int(portfolio["max_orders"]):
            intent = _intent(buy_notional, sell_notional, nav)
            low, high = _cash_band(cash_pct)
            funding: set[str] = set()
            sells = [order for order in orders if order["side"] == "SELL"]
            buys = [order for order in orders if order["side"] == "BUY"]
            if sells and buys:
                buy_only = [order for order in orders if order["side"] == "BUY"]
                cash_wo, nav_wo, _pos, _b, _s = estimate(cash, held, buy_only, prices, commission_d, tax_d, allow_negative_cash=True)
                pct_wo = float(cash_wo / nav_wo) if nav_wo > 0 else -1
                if pct_wo < low - 1e-9:
                    funding = {order["ticker"] for order in sells}
            return Construction(
                share_targets=share_targets,
                weights={order["ticker"]: order["target_weight"] for order in orders},
                orders=orders,
                cash_after=cash_after,
                nav_after=nav_after,
                pre_nav=nav,
                buy_notional=buy_notional,
                sell_notional=sell_notional,
                positions_after=positions_after,
                intent=intent,
                cash_low=low,
                cash_high=high,
                funding_sells=funding,
                diagnostics={"active_share_definition": definition, "active_share_definition_confirmed": bool(portfolio.get("active_share_definition_confirmed")),
                             "active_share_verified": bool(benchmarks), "sector_weights": sector_weights,
                             "roundtrip_cost": portfolio["_roundtrip_cost"], "hysteresis": portfolio.get("min_score_improvement", 0.05),
                             "correlation_constraint_scope": "pairwise_not_full_connected_component",
                             "correlated_pairs_checked": len(actual_pairs),
                             "defaults": "heuristic_not_calibrated_expected_returns"},
            )
        bits = []
        if not cash_ok:
            bits.append(f"現金比 {cash_pct:.2%}")
        if not count_ok:
            bits.append(f"持股 {count} 檔")
        if overweight:
            bits.append("權重超限 " + ",".join(overweight))
        if not sector_ok:
            bits.append("產業權重超限")
        if not correlation_ok:
            bits.append("高相關成對持股權重超限")
        if not as_ok:
            bits.append(f"Active Share {as_worst:.2%} 相對 {as_name}")
        last_problem = "、".join(bits) or last_problem
        if cash_pct > 0.20:
            cash_target = max(0.03, cash_target - 0.015)
            names_n = min(int(portfolio["max_names"]), names_n + 1)
        elif cash_pct < 0.02:
            cash_target = min(0.18, cash_target + 0.015)
        elif count > int(portfolio["max_names"]):
            names_n = max(int(portfolio["min_names"]), names_n - 1)
        elif count < int(portfolio["min_names"]):
            names_n = min(int(portfolio["max_names"]), names_n + 1)
        else:
            cash_target = min(0.15, cash_target + 0.01)
    raise PlanError(last_problem)
