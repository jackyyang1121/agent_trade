from __future__ import annotations

import math
from statistics import mean, pstdev

from agent.signals.common import finite, timestamp


def _bars(rows: list[dict], cutoff) -> list[dict]:
    days: dict[str, dict] = {}
    for raw in rows:
        value = raw.get("date") or raw.get("as_of")
        if not value:
            continue
        try:
            observed = timestamp(value, session_close=True)
            available = timestamp(raw.get("available_at") or raw.get("fetched_at") or value, session_close=True)
        except (ValueError, TypeError):
            continue
        if max(observed, available) > cutoff:
            continue
        close = finite(raw.get("close"))
        if close is None or close <= 0:
            continue
        row = dict(raw, close=close, date=observed.date().isoformat())
        row["_observed"] = observed
        days[row["date"]] = row
    return sorted(days.values(), key=lambda item: item["_observed"])


def _return(bars: list[dict], n: int):
    return bars[-1]["close"] / bars[-n - 1]["close"] - 1 if len(bars) > n else None


def _returns(bars: list[dict]) -> dict[str, float]:
    return {after["date"]: after["close"] / before["close"] - 1 for before, after in zip(bars, bars[1:])}


def _corr(a: dict, b: dict):
    common = sorted(set(a) & set(b))[-60:]
    if len(common) < 10:
        return None
    x, y = [a[d] for d in common], [b[d] for d in common]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x) / len(x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y) / len(y))
    if sx < 1e-12 or sy < 1e-12:
        return None
    covariance = sum((v - mx) * (w - my) for v, w in zip(x, y)) / len(x)
    return max(-1.0, min(1.0, covariance / (sx * sy)))


def _ratio(rows: list[dict], field: str, short=5, long=20):
    values = [finite(row.get(field)) for row in rows[-long:]]
    if len(values) < long or any(value is None or value < 0 for value in values):
        return None
    base = mean(values)
    return mean(values[-short:]) / base - 1 if base > 0 else None


def quant_signals(histories: dict[str, list[dict]], as_of: str, industries: dict | None = None) -> dict[str, dict]:
    """Compute separate indicators using only records available by ``as_of``.

    Relative strength uses an equal-weight supplied universe proxy, explicitly
    not an exchange-index return. Industry proxies exclude the subject stock.
    The initial ranking is the 20-session momentum percentile, a heuristic
    default rather than a claim that a blend of horizons has been calibrated.
    Missing values remain null with coverage flags; NaN/Inf never escape.
    """
    cutoff = timestamp(as_of)
    industries = industries or {}
    histories = {str(t): _bars(rows, cutoff) for t, rows in histories.items()}
    returns = {t: _returns(rows) for t, rows in histories.items()}
    correlation_matrix: dict[str, dict] = {ticker: {} for ticker in histories}
    tickers = list(histories)
    for i, left in enumerate(tickers):
        for right in tickers[i + 1:]:
            value = _corr(returns[left], returns[right])
            if value is not None:
                correlation_matrix[left][right] = value
                correlation_matrix[right][left] = value
    result: dict[str, dict] = {}
    for ticker, rows in histories.items():
        values = list(returns[ticker].values())[-20:]
        down = [min(0.0, value) for value in values]
        signals = {f"momentum_{n}": _return(rows, n) for n in (5, 10, 20, 60)}
        peers = [t for t in histories if t != ticker and industries.get(t) is not None and industries.get(t) == industries.get(ticker)]
        # Use the same dates for each proxy, avoiding stock-specific horizon drift.
        dates20 = [row["date"] for row in rows[-21:]]

        def proxy_return(tickers):
            if len(dates20) < 21:
                return None
            products = []
            for other in tickers:
                prices = {row["date"]: row["close"] for row in histories[other]}
                if dates20[0] in prices and dates20[-1] in prices:
                    products.append(prices[dates20[-1]] / prices[dates20[0]] - 1)
            return mean(products) if products else None

        universe_ret = proxy_return([t for t in histories if t != ticker])
        industry_ret = proxy_return(peers)
        mom20 = signals["momentum_20"]
        signals.update({
            "relative_strength_20": mom20 - universe_ret if mom20 is not None and universe_ret is not None else None,
            "industry_relative_momentum_20": mom20 - industry_ret if mom20 is not None and industry_ret is not None else None,
            "industry_momentum_20": industry_ret,
            "volume_surge": _ratio(rows, "volume"),
            "turnover_change": _ratio(rows, "turnover"),
            "volatility_20": pstdev(values) if len(values) >= 10 else None,
            "downside_volatility_20": math.sqrt(mean(value * value for value in down)) if len(down) >= 10 else None,
            "momentum_acceleration": (signals["momentum_5"] - (signals["momentum_20"] - signals["momentum_5"]) / 3)
                if signals["momentum_5"] is not None and mom20 is not None else None,
        })
        turns = [finite(row.get("turnover")) for row in rows[-20:]]
        signals["avg_turnover"] = mean(turns) if len(turns) >= 10 and all(v is not None and v >= 0 for v in turns) else None
        intraday, overnight = [], []
        for i, row in enumerate(rows[-20:]):
            opened = finite(row.get("open"))
            if opened and opened > 0:
                intraday.append(row["close"] / opened - 1)
                # Use the actual preceding available bar, not a future session.
                absolute = len(rows) - len(rows[-20:]) + i
                if absolute > 0:
                    overnight.append(opened / rows[absolute - 1]["close"] - 1)
        signals["intraday_momentum"] = mean(intraday) if len(intraday) >= 10 else None
        signals["overnight_momentum"] = mean(overnight) if len(overnight) >= 10 else None
        correlations = correlation_matrix[ticker]
        signals["mean_correlation"] = mean(correlations.values()) if correlations else None
        clean = {key: finite(value) for key, value in signals.items()}
        last = rows[-1] if rows else {}
        last_volume, last_turnover = finite(last.get("volume")), finite(last.get("turnover"))
        stale = not rows or (cutoff.date() - rows[-1]["_observed"].date()).days > 7
        tradable = bool(rows and not stale and last_volume != 0 and last_turnover != 0)
        result[ticker] = {
            "ticker": ticker, "as_of": as_of, "latest_date": rows[-1]["date"] if rows else None,
            "signals": clean, **clean, "correlations": correlations,
            "source_ids": sorted({sid for row in rows for sid in row.get("source_ids", [])}),
            "missing": [key for key, value in clean.items() if value is None],
            "valid": len(rows) >= 21 and tradable, "tradable": tradable,
            "stale": stale, "observations": len(rows),
            "relative_strength_reference": "equal_weight_supplied_universe_proxy",
            "ranking_method": "heuristic_default_momentum_20_percentile",
        }
    ordered = sorted((row["momentum_20"], ticker) for ticker, row in result.items() if row["momentum_20"] is not None)
    ranks = {ticker: (index / max(1, len(ordered) - 1)) for index, (_, ticker) in enumerate(ordered)}
    # Equal values receive equal ranks, independently of ticker ordering.
    for ticker, row in result.items():
        ties = [ranks[t] for value, t in ordered if value == row["momentum_20"]]
        row["score"] = mean(ties) if ties else 0.0
    return result


def reverse_research_candidates(quant: dict[str, dict], theses: list[dict], limit: int = 10) -> list[dict]:
    """Queue unexplained strength; never infer an unpublished positive event."""
    covered = {str(t.get("ticker")) for t in theses if t.get("status") not in {"broken", "rejected", "THESIS_BROKEN"}}
    tasks = []
    for ticker, row in sorted(quant.items(), key=lambda item: (-item[1].get("score", 0), item[0])):
        if ticker in covered or not row.get("valid"):
            continue
        triggers = []
        for field, threshold in (("relative_strength_20", 0.03), ("volume_surge", 0.5), ("momentum_acceleration", 0.03)):
            value = row.get(field)
            if finite(value) is not None and value >= threshold:
                triggers.append(field)
        if triggers:
            tasks.append({"ticker": ticker, "type": "reverse_research", "as_of": row["as_of"],
                          "triggers": triggers, "source_ids": row.get("source_ids", []),
                          "question": "Investigate public industry, fundamental or event explanations; an explanation may remain unknown."})
    return tasks[:max(0, limit)]
