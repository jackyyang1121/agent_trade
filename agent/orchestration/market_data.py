from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from agent.calendar_tw import default_trade_date, previous_trading_day, is_trading_day
from agent.config import enabled_factors, load_prospectus
from agent.market import Http, load_holidays, prepare
from agent.util import PlanError, stamp


@dataclass
class MarketSnapshot:
    trade_date: date
    pricing_date: date
    as_of: str
    prepared: dict
    histories: dict
    universe: list[dict]
    synthetic: bool = False
    http_calls: int = 0

    def validate(self) -> None:
        cutoff = datetime.fromisoformat(self.as_of)
        if cutoff.tzinfo is None:
            raise PlanError("行情 as_of 必須帶時區")
        if self.pricing_date >= self.trade_date or cutoff.date() > self.trade_date:
            raise PlanError("定價日／交易日／資訊時點不一致")
        if not self.prepared.get("prices") or not self.histories:
            raise PlanError("必要定價資料或歷史行情缺失")
        ids = [row["ticker"] for row in self.universe]
        if len(ids) != len(set(ids)) or not ids:
            raise PlanError("股票池重複或為空")
        for ticker, price in self.prepared["prices"].items():
            if ticker not in ids or not math.isfinite(price) or price <= 0:
                raise PlanError(f"無效定價／股票池外：{ticker}")
            rows = self.histories.get(ticker, [])
            if not rows or rows[-1]["date"] != self.pricing_date.isoformat():
                raise PlanError(f"{ticker} 缺少定價日行情")
            if float(rows[-1]["close"]) != price:
                raise PlanError(f"{ticker} 歷史價格與定價不一致")
            for row in rows:
                if row["date"] > self.pricing_date.isoformat():
                    raise PlanError("行情包含未來資訊")
                if row.get("available_at") and datetime.fromisoformat(row["available_at"]) > cutoff:
                    raise PlanError("行情在決策後才可取得")
        for source in self.prepared.get("source_material", {}).values():
            for field in ("published_at", "content_as_of", "fetched_at"):
                if source.get(field):
                    moment = datetime.fromisoformat(source[field])
                    if moment.tzinfo is None or moment > cutoff:
                        raise PlanError(f"行情來源 {field} 晚於決策或缺時區")


def get_snapshot(settings: dict, strategy: dict, trade_date: date | None, now: datetime,
                 held: dict, fixture: Path | None = None, offline: bool = False) -> MarketSnapshot:
    if fixture:
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        if not raw.get("synthetic"):
            raise PlanError("--fixture 僅供明確標記 synthetic:true 的離線測試資料")
        snapshot = MarketSnapshot(date.fromisoformat(raw["trade_date"]), date.fromisoformat(raw["pricing_date"]),
                                  raw["as_of"], raw["prepared"], raw["histories"], raw["universe"], True)
        if trade_date and trade_date != snapshot.trade_date:
            raise PlanError("指定交易日與 fixture 不一致")
    else:
        if offline:
            raise PlanError("離線每日流程需要 --fixture PATH；不自動產生虛構行情")
        http = Http()
        closed = load_holidays(http)
        day = trade_date or default_trade_date(now, closed)
        if not is_trading_day(day, closed):
            raise PlanError(f"{day} 不是交易日")
        pricing = previous_trading_day(day, closed)
        if pricing > now.date() or (pricing == now.date() and (now.hour, now.minute) < (18, 30)):
            raise PlanError("定價日尚未完成，禁止使用未來收盤資料")
        factors = enabled_factors(strategy, load_prospectus())
        prepared = prepare(http, settings, factors, pricing, held, closed)
        # fetched_at 取本次實際完成時間，禁止回填歷史可得時間。
        cutoff = datetime.now(now.tzinfo)
        snapshot = MarketSnapshot(day, pricing, stamp(cutoff), prepared, prepared["histories"], prepared["universe"], False, http.calls)
    snapshot.validate()
    return snapshot
