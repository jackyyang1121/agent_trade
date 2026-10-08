from __future__ import annotations

import math
from datetime import date

from agent.config import FUNDAMENTALS_PATH


def zscore(values: dict[str, float | None]) -> dict[str, float]:
    present = {key: value for key, value in values.items() if value is not None and math.isfinite(value)}
    if len(present) < 5:
        return {key: 0.0 for key in values}
    ordered = sorted(present.values())

    def quantile(p: float) -> float:
        pos = (len(ordered) - 1) * p
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return ordered[lo]
        return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)

    lo = quantile(0.05)
    hi = quantile(0.95)
    clipped = [min(hi, max(lo, value)) for value in present.values()]
    mean = sum(clipped) / len(clipped)
    var = sum((value - mean) ** 2 for value in clipped) / len(clipped)
    std = math.sqrt(var)
    out: dict[str, float] = {}
    for key, value in values.items():
        if key not in present or std == 0:
            out[key] = 0.0
        else:
            out[key] = (min(hi, max(lo, value)) - mean) / std
    return out


def _aligned(series: list[float | None], index: int) -> float | None:
    if index < 0 or index >= len(series):
        return None
    value = series[index]
    if value is None or value <= 0:
        return None
    return value


def _last_valid(series: list[float | None], end: int) -> int | None:
    for index in range(end, -1, -1):
        if _aligned(series, index) is not None:
            return index
    return None


def _return_between(series: list[float | None], end: int, lookback: int, skip: int) -> float | None:
    head = _last_valid(series, end)
    if head is None:
        return None
    newer = head - skip
    older = head - lookback
    p1 = _aligned(series, newer)
    p0 = _aligned(series, older)
    if p1 is None or p0 is None or p0 <= 0:
        return None
    return p1 / p0 - 1


def _window_values(series: list[float | None], end: int, window: int) -> list[float]:
    head = _last_valid(series, end)
    if head is None:
        return []
    values: list[float] = []
    index = head
    while index >= 0 and len(values) < window:
        value = _aligned(series, index)
        if value is not None:
            values.append(value)
        index -= 1
    return list(reversed(values))


def momentum(closes: dict[str, list[float | None]], end: int, params: dict) -> dict[str, float | None]:
    lookback = int(params.get("lookback", 60))
    skip = int(params.get("skip", 5))
    return {ticker: _return_between(series, end, lookback, skip) for ticker, series in closes.items()}


def volume_trend(values: dict[str, list[float | None]], end: int, params: dict) -> dict[str, float | None]:
    short = int(params.get("short", 20))
    long = int(params.get("long", 60))
    out: dict[str, float | None] = {}
    for ticker, series in values.items():
        window = _window_values(series, end, long)
        if len(window) < short:
            out[ticker] = None
            continue
        short_avg = sum(window[-short:]) / short
        long_avg = sum(window) / len(window)
        out[ticker] = None if long_avg <= 0 else short_avg / long_avg - 1
    return out


def low_vol(closes: dict[str, list[float | None]], end: int, params: dict) -> dict[str, float | None]:
    window = int(params.get("window", 20))
    out: dict[str, float | None] = {}
    for ticker, series in closes.items():
        prices = _window_values(series, end, window + 1)
        if len(prices) < window:
            out[ticker] = None
            continue
        rets = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices)) if prices[i - 1] > 0]
        if len(rets) < 8:
            out[ticker] = None
            continue
        mean = sum(rets) / len(rets)
        var = sum((item - mean) ** 2 for item in rets) / len(rets)
        out[ticker] = -math.sqrt(var)
    return out


def _optional_ratio(raw: float | None) -> float | None:
    if raw is None or not math.isfinite(raw):
        return None
    if abs(raw) > 1.5:
        return raw / 100.0
    return raw


def load_fundamentals(path=FUNDAMENTALS_PATH) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    import csv

    out: dict[str, dict[str, float]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip()
            if not ticker:
                continue
            item: dict[str, float] = {}
            roe = _optional_ratio(_to_float(row.get("roe")))
            cfo = _optional_ratio(_to_float(row.get("cfo_to_assets")))
            if roe is not None:
                item["roe"] = roe
            if cfo is not None:
                item["cfo_to_assets"] = cfo
            if item:
                out[ticker] = item
    return out


def _to_float(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def quality(tickers: list[str], metrics: dict[str, dict], fundamentals: dict[str, dict]) -> dict[str, float | None]:
    ey = {}
    pb = {}
    div = {}
    roe = {}
    cfo = {}
    for ticker in tickers:
        metric = metrics.get(ticker) or {}
        pe = metric.get("pe")
        ey[ticker] = (1.0 / pe) if pe and pe > 0 else None
        book = metric.get("pb")
        pb[ticker] = book if book and book > 0 else None
        div[ticker] = metric.get("dividend_yield")
        extra = fundamentals.get(ticker) or {}
        roe[ticker] = extra.get("roe")
        cfo[ticker] = extra.get("cfo_to_assets")
    parts = [zscore(ey), {k: -v for k, v in zscore(pb).items()}, zscore(div)]
    if sum(value is not None for value in roe.values()) >= 10:
        parts.append(zscore(roe))
    if sum(value is not None for value in cfo.values()) >= 10:
        parts.append(zscore(cfo))
    out: dict[str, float | None] = {}
    for ticker in tickers:
        vals = [part[ticker] for part in parts]
        out[ticker] = sum(vals) / len(vals)
    return out


def growth(tickers: list[str], metrics: dict[str, dict]) -> dict[str, float | None]:
    return {ticker: (metrics.get(ticker) or {}).get("rev_yoy") for ticker in tickers}


def dividend(tickers: list[str], metrics: dict[str, dict]) -> dict[str, float | None]:
    return {ticker: (metrics.get(ticker) or {}).get("dividend_yield") for ticker in tickers}


def display_metrics(
    tickers: list[str],
    dates: list[date],
    closes: dict[str, list[float | None]],
    values: dict[str, list[float | None]],
    end: int,
    valuation: dict[str, dict],
    revenue: dict[str, float],
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ticker in tickers:
        series = closes[ticker]
        head = _last_valid(series, end)
        close = _aligned(series, head) if head is not None else None
        if close is None:
            continue
        ret20 = _return_between(series, end, 20, 0)
        ret60 = _return_between(series, end, 60, 0)
        turn_window = _window_values(values[ticker], end, 20)
        avg_turn = sum(turn_window) / len(turn_window) if turn_window else None
        prices = _window_values(series, end, 21)
        vol = None
        if len(prices) >= 10:
            rets = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices)) if prices[i - 1] > 0]
            if len(rets) >= 8:
                mean = sum(rets) / len(rets)
                var = sum((item - mean) ** 2 for item in rets) / len(rets)
                vol = math.sqrt(var)
        val = valuation.get(ticker) or {}
        item = {
            "close": close,
            "ret_20": ret20,
            "ret_60": ret60,
            "avg_turnover": avg_turn,
            "vol_20": vol,
            "pe": val.get("pe"),
            "pb": val.get("pb"),
            "dividend_yield": val.get("dividend_yield"),
            "rev_yoy": revenue.get(ticker),
        }
        out[ticker] = item
    return out


def score_universe(
    tickers: list[str],
    dates: list[date],
    closes: dict[str, list[float | None]],
    values: dict[str, list[float | None]],
    metrics: dict[str, dict],
    enabled: list[tuple[str, float, dict]],
    fundamentals: dict[str, dict],
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    end = len(dates) - 1
    raw: dict[str, dict[str, float | None]] = {}
    for fid, _weight, params in enabled:
        if fid == "momentum":
            raw[fid] = momentum(closes, end, params)
        elif fid == "volume_trend":
            raw[fid] = volume_trend(values, end, params)
        elif fid == "low_vol":
            raw[fid] = low_vol(closes, end, params)
        elif fid == "quality":
            raw[fid] = quality(tickers, metrics, fundamentals)
        elif fid == "growth":
            raw[fid] = growth(tickers, metrics)
        elif fid == "dividend":
            raw[fid] = dividend(tickers, metrics)
        else:
            raise KeyError(f"未知因子 {fid}，請在 agent/factors.py 註冊")
    components: dict[str, dict[str, float]] = {ticker: {} for ticker in tickers}
    normalized: dict[str, dict[str, float]] = {}
    for fid, _weight, _params in enabled:
        normalized[fid] = zscore(raw[fid])
        for ticker in tickers:
            components[ticker][fid] = normalized[fid].get(ticker, 0.0)
    scores: dict[str, float] = {}
    for ticker in tickers:
        scores[ticker] = sum(weight * normalized[fid].get(ticker, 0.0) for fid, weight, _ in enabled)
    return scores, components
