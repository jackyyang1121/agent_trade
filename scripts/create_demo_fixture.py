"""產生確定性、明確標示的合成行情；不包含真實公司或投資研究。

bash: python scripts/create_demo_fixture.py --output data/runtime/demo_market.json
僅供 --offline --provider mock --fixture 驗證，不可用於正式競賽。
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, pstdev


def _sessions(end: date, count: int = 64) -> list[date]:
    result, cursor = [], end
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(result))


def _previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def build_fixture(trade_date: date | str = "2026-10-15", count: int = 150) -> dict:
    """Fixed formula, no RNG. All companies and source URLs are test-only.

    The dates follow a weekday test calendar, not the Taiwan exchange calendar.
    Prices are anchored to an absolute date so adjacent fixtures share bars;
    independently phased cycles prevent unrealistically perfect correlations.
    """
    trade = date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date
    if trade.weekday() >= 5:
        raise ValueError("合成 fixture 的交易日必須是平日；這不是官方交易日曆")
    pricing = _previous_weekday(trade)
    days = _sessions(pricing)
    as_of = trade.isoformat() + "T08:00:00+08:00"
    close_stamp = pricing.isoformat() + "T13:30:00+08:00"
    fetched_stamp = pricing.isoformat() + "T18:30:00+08:00"
    universe, histories = [], {}
    prices, names, markets, industries, metrics, scores, components = {}, {}, {}, {}, {}, {}, {}
    for index in range(count):
        ticker = str(1000 + index)
        market = "twse" if index < 100 else "tpex"
        name, industry = f"合成測試{index + 1:03d}", f"合成產業{index % 6 + 1}"
        universe.append({"ticker": ticker, "name": name, "market": market, "industry": industry})
        rows = []
        for day in days:
            step = (day - date(2026, 1, 1)).days
            drift = 0.00025 + (index % 17) * 0.000035
            cycle = 0.016 * math.sin(step * (0.17 + (index % 11) * 0.027) + index * 1.7)
            cycle += 0.009 * math.sin(step * (0.43 + (index % 7) * 0.031) + index * 0.61)
            close = round((45 + index * 0.37) * math.exp(drift * step + cycle), 4)
            volume = 10_000_000 + index * 10_000 + int(300_000 * math.sin(step * 0.4 + index))
            vwap = round(close * (1 + 0.0005 * math.sin(step + index)), 6)
            value = round(volume * vwap, 2)
            rows.append({"date": day.isoformat(), "close": close,
                         "open": round(close * (1 - .001 * math.cos(step + index)), 4),
                         "volume": volume, "value": value, "vwap": vwap, "turnover": value,
                         "available_at": day.isoformat() + "T18:30:00+08:00",
                         "source_ids": [market], "synthetic": True})
        histories[ticker] = rows
        closes = [row["close"] for row in rows]
        returns = [after / before - 1 for before, after in zip(closes[-21:-1], closes[-20:])]
        price = closes[-1]
        prices[ticker], names[ticker], markets[ticker], industries[ticker] = price, name, market, industry
        metrics[ticker] = {"close": price, "ret_20": price / closes[-21] - 1,
                           "ret_60": price / closes[-61] - 1,
                           "avg_turnover": mean(row["value"] for row in rows[-20:]),
                           "vol_20": pstdev(returns), "pe": 12 + index % 18,
                           "pb": 1.0 + (index % 12) * .12,
                           "dividend_yield": 1.0 + (index % 9) * .3,
                           "rev_yoy": 8 + index % 25, "synthetic": True}
        scores[ticker] = 0.2 + (index % 37) / 37
        components[ticker] = {"momentum": scores[ticker], "volume_trend": .1,
                              "quality": .2 + (index % 11) / 20,
                              "growth": .3 + (index % 13) / 20, "low_vol": -.1}
    source_material = {}
    for key, authority in (("twse", "twse"), ("tpex", "tpex"), ("valuation", "twse"),
                           ("rev_twse", "mops"), ("rev_tpex", "mops"), ("bfi", "twse")):
        source_material[key] = {"key": key, "authority": authority,
                                "name": f"SYNTHETIC 合成測試 {key}，非官方資料",
                                "url": f"https://example.test/synthetic/{key}",
                                "content_as_of": close_stamp, "fetched_at": fetched_stamp,
                                "synthetic": True}
    taiex_history = [{"date": day.isoformat(),
                      "close": round(20000 + (day - date(2026, 1, 1)).days * 2 + 20 * math.sin((day - date(2026, 1, 1)).days * .2), 4),
                      "available_at": day.isoformat() + "T18:30:00+08:00", "synthetic": True}
                     for day in days]
    market = {"taiex_close": taiex_history[-1]["close"], "taiex_chg_pct": .2,
              "taiex_ret_5": .006, "taiex_ret_20": .02,
              "semi_chg_pct": .4, "foreign_bn": 1.0, "trust_bn": .5, "us": {}}
    return {"synthetic": True, "description": "SYNTHETIC 離線合成測試行情；所有公司與價格均為虛構，禁止競賽使用。",
            "calendar_basis": "weekdays_only_test_calendar_not_official_exchange_calendar",
            "trade_date": trade.isoformat(), "pricing_date": pricing.isoformat(), "as_of": as_of,
            "universe": universe, "histories": histories,
            "prepared": {"prices": prices, "names": names, "markets": markets,
                         "industries": industries, "metrics": metrics, "scores": scores,
                         "components": components, "market": market, "source_material": source_material,
                         "taiex_history": taiex_history}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/runtime/demo_market.json"))
    parser.add_argument("--date", default="2026-10-15", help="合成交易日，YYYY-MM-DD")
    args = parser.parse_args()
    fixture = build_fixture(args.date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"已產生 SYNTHETIC 測試資料：{args.output}；不可用於正式競賽。")


if __name__ == "__main__":
    main()
