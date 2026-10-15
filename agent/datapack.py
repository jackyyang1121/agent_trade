"""官方 DataPack v3.0（v4.3 規格書）的唯讀轉接，不推測缺失的部位。"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from decimal import Decimal, InvalidOperation

from agent.util import PlanError


def _number(value, name: str, *, integer: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PlanError(f"DataPack {name} 必須是有限非負數") from exc
    if isinstance(value, (str, bool)) or not number.is_finite() or number < 0 or (integer and number != number.to_integral_value()):
        raise PlanError(f"DataPack {name} 必須是有限非負{'整數' if integer else '數字'}")
    return number


@dataclass(frozen=True)
class DataPack:
    trade_date: date
    as_of_date: date | None
    generated_at: datetime
    team_id: str
    cash: Decimal
    positions: dict[str, int]
    nav_tradable: Decimal
    aum: Decimal
    initial: bool
    source: dict


def parse_datapack(raw: dict, pricing_date: date, *, allow_simulated: bool = False) -> DataPack:
    required = {"schema_version", "doc_type", "team_id", "trade_date", "as_of_date", "generated_at",
                "missing", "prev_day_orders", "account", "holdings", "compliance"}
    if not isinstance(raw, dict) or required - raw.keys():
        raise PlanError("DataPack 缺少必要頂層欄位")
    if raw.get("_simulation") and not allow_simulated:
        raise PlanError("模擬 DataPack 僅限演練，不能匯入正式帳本")
    # 規格書的檔名版本和 JSON 資料版本不同，不能把 3.0 擅改為 4.3。
    if raw["doc_type"] != "DataPack" or raw["schema_version"] != "3.0":
        raise PlanError("不支援的 DataPack doc_type 或 schema_version（目前為 3.0）")
    try:
        trade_day = date.fromisoformat(raw["trade_date"])
        book_day = date.fromisoformat(raw["as_of_date"]) if raw["as_of_date"] is not None else None
        generated = datetime.fromisoformat(raw["generated_at"])
    except (TypeError, ValueError) as exc:
        raise PlanError("DataPack 日期或產生時間無效") from exc
    if generated.tzinfo is None or generated.utcoffset() != timedelta(hours=8):
        raise PlanError("DataPack generated_at 必須是台北時間 +08:00")
    if not isinstance(raw["team_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", raw["team_id"]):
        raise PlanError("DataPack 隊伍代號無效")
    if trade_day <= pricing_date or generated.date() > trade_day or (book_day and book_day != pricing_date):
        raise PlanError("DataPack 交易日、結算日與定價日不一致")
    if book_day and generated < datetime.combine(book_day, time(13, 30), tzinfo=generated.tzinfo):
        raise PlanError("DataPack 產生時間早於所稱的收盤結算資料")
    missing = raw["missing"]
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise PlanError("DataPack missing 必須是字串陣列")
    if "prev_settlement" in missing or raw["account"] is None or raw["holdings"] is None:
        raise PlanError("DataPack 前日尚未結算，部位未知；請重新下載，不可視為空手")
    if missing:
        raise PlanError("DataPack 資料尚未齊備，禁止產生正式交易：" + ",".join(missing))
    account, holdings, previous, compliance = raw["account"], raw["holdings"], raw["prev_day_orders"], raw["compliance"]
    if not isinstance(account, dict) or not isinstance(holdings, list) or not isinstance(previous, dict) or not isinstance(compliance, dict):
        raise PlanError("DataPack account、holdings、委託結果或合規資料型別錯誤")
    if {"cash", "cash_ratio_tradable", "nav_tradable", "aum", "receivable_cash", "holdings_count"} - account.keys():
        raise PlanError("DataPack account 缺少必要欄位")
    cash = _number(account["cash"], "cash")
    nav = _number(account["nav_tradable"], "nav_tradable")
    aum = _number(account["aum"], "aum")
    receivable_cash = _number(account["receivable_cash"], "receivable_cash")
    count = _number(account["holdings_count"], "holdings_count", integer=True)
    ratio = _number(account["cash_ratio_tradable"], "cash_ratio_tradable")
    if nav <= 0 or aum < nav or abs(ratio - cash / nav) > Decimal("0.000001"):
        raise PlanError("DataPack 可交易淨值、現金比或資產規模不一致")
    positions, seen = {}, set()
    market_value = Decimal(0)
    receivable_value = receivable_cash
    for holding in holdings:
        if not isinstance(holding, dict) or {"code", "shares", "receivable_shares", "close_price", "tradable_weight"} - holding.keys():
            raise PlanError("DataPack 持股列缺少必要欄位")
        code = holding["code"]
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4}", code) or code in seen:
            raise PlanError("DataPack 持股代號錯誤或重複")
        seen.add(code)
        shares = _number(holding["shares"], f"{code}.shares", integer=True)
        receivable = _number(holding["receivable_shares"], f"{code}.receivable_shares", integer=True)
        if shares == 0 and receivable == 0:
            raise PlanError("DataPack 不應包含持股與應收配股皆為零的列")
        price = _number(holding["close_price"], f"{code}.close_price") if holding["close_price"] is not None else None
        if price is None or price <= 0:
            raise PlanError(f"DataPack {code} 缺少必要收盤價；保留原帳本，不推測估值")
        if shares:
            positions[code] = int(shares)
            market_value += shares * price
            weight = _number(holding["tradable_weight"], f"{code}.tradable_weight")
            if abs(weight - shares * price / nav) > Decimal("0.000001"):
                raise PlanError(f"DataPack {code} 可交易權重與 nav_tradable 不一致")
        elif holding["tradable_weight"] is not None:
            raise PlanError("DataPack 僅有應收配股的持股列，tradable_weight 必須為 null")
        receivable_value += receivable * price
    if count != len(positions) or abs(nav - cash - market_value) > Decimal("0.02"):
        raise PlanError("DataPack 持股檔數或可交易淨值與持股估值不一致")
    if abs(aum - nav - receivable_value) > Decimal("0.02"):
        raise PlanError("DataPack aum 與可交易資產及應收權益不一致")
    outcome = previous.get("outcome")
    reasons, excluded = previous.get("void_reasons"), previous.get("excluded_codes")
    if outcome not in {"none", "unknown", "voided", "executed"} or not isinstance(reasons, list) or not isinstance(excluded, list):
        raise PlanError("DataPack 前日委託結果格式錯誤")
    if outcome == "unknown" or (outcome == "voided") != bool(reasons):
        raise PlanError("DataPack 前日委託尚未判定或作廢原因不一致")
    if any(not isinstance(item, str) for item in reasons + excluded):
        raise PlanError("DataPack 作廢原因及排除代號必須是字串")
    for field in ("violations", "notices"):
        _number(compliance.get(field), f"compliance.{field}", integer=True)
    if not isinstance(compliance.get("incidents"), list):
        raise PlanError("DataPack compliance.incidents 必須是陣列")
    for incident in compliance["incidents"]:
        if not isinstance(incident, dict) or {"date", "check_code", "status", "message"} - incident.keys():
            raise PlanError("DataPack 合規紀錄缺少必要欄位")
        try:
            date.fromisoformat(incident["date"])
        except (TypeError, ValueError) as exc:
            raise PlanError("DataPack 合規紀錄日期無效") from exc
        if incident["status"] not in {"warn", "fail"} or not isinstance(incident["check_code"], str) or not isinstance(incident["message"], str):
            raise PlanError("DataPack 合規紀錄狀態或說明格式無效")
    initial = book_day is None
    if initial and (holdings or receivable_cash or cash != nav or nav != aum or outcome != "none"):
        raise PlanError("DataPack 無結算日期只允許可確認的首次全現金狀態")
    return DataPack(trade_day, book_day, generated, raw["team_id"], cash, positions, nav, aum, initial, copy.deepcopy(raw))


def simulated_initial_pack(team_id: str, trade_day: date, generated: datetime, capital: int) -> dict:
    """只模擬帳戶初始條件，不模擬行情、新聞或模型執行。"""
    from agent.util import stamp
    return {"_simulation": True, "schema_version": "3.0", "doc_type": "DataPack", "team_id": team_id,
            "trade_date": trade_day.isoformat(), "as_of_date": None, "generated_at": stamp(generated),
            "missing": [], "prev_day_orders": {"outcome": "none", "void_reasons": [], "excluded_codes": []},
            "account": {"cash": capital, "nav_tradable": capital, "aum": capital,
                        "receivable_cash": 0, "cash_ratio_tradable": 1, "holdings_count": 0},
            "holdings": [], "compliance": {"violations": 0, "notices": 0, "incidents": []}}
