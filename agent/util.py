from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TAIPEI = ZoneInfo("Asia/Taipei")
LOT = 1000


class PlanError(RuntimeError):
    pass


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    else:
        dt = dt.astimezone(TAIPEI)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def roc_to_date(text: str) -> date:
    digits = re.sub(r"\D", "", str(text))
    if len(digits) < 6:
        raise PlanError(f"無法辨識日期：{text}")
    day = int(digits[-2:])
    month = int(digits[-4:-2])
    year = int(digits[:-4]) + 1911
    return date(year, month, day)


def cell_num(value) -> float | None:
    if value is None:
        return None
    text = re.sub(r"<[^>]*>", "", str(value))
    text = text.replace(",", "").replace(" ", "").replace("＋", "+").replace("－", "-")
    if text in {"", "--", "---", "X", "null", "None", "N/A", "nan"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def jnum(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def target_shares(weight: float, nav: float, price: float, lot: int = LOT) -> int:
    """官方公式：floor(權重 × 前日淨值 ÷ 前日收盤價 ÷ 1000) × 1000。"""
    if weight <= 0 or nav <= 0 or price <= 0:
        return 0
    raw = (Decimal(str(weight)) * Decimal(str(nav))) / Decimal(str(price)) / Decimal(lot)
    lots = int(raw.to_integral_value(rounding=ROUND_FLOOR))
    return lots * lot


def shares_to_weight(shares: int, nav: float, price: float, cap: float, lot: int = LOT) -> float | None:
    """找一個不超過 cap、且官方公式會落在指定股數的目標權重。"""
    if shares <= 0:
        return 0.0
    if nav <= 0 or price <= 0:
        return None
    lo = shares * price / nav
    hi = (shares + lot) * price / nav
    if lo > cap + 1e-12:
        return None
    upper = min(hi - 1e-12, cap)
    if upper < lo:
        return None
    guess = math.floor(((lo + upper) / 2) * 1_000_000) / 1_000_000
    if guess < lo:
        guess = math.ceil(lo * 1_000_000) / 1_000_000
    guess = min(guess, math.floor(cap * 1_000_000) / 1_000_000)
    step = 0.000001
    x = math.ceil(lo * 1_000_000) / 1_000_000
    guard = 0
    while x <= upper + 1e-12 and guard < 5000:
        if target_shares(round(x, 6), nav, price, lot) == shares:
            return round(x, 6)
        x = round(x + step, 6)
        guard += 1
    return None


def fit_shares(shares: int, nav: float, price: float, cap: float, lot: int = LOT) -> tuple[int, float]:
    sh = int(shares)
    while sh > 0:
        weight = shares_to_weight(sh, nav, price, cap, lot)
        if weight is not None and target_shares(weight, nav, price, lot) == sh:
            return sh, weight
        sh -= lot
    return 0, 0.0


def iso_add_days(day: date, n: int) -> date:
    return day + timedelta(days=n)
