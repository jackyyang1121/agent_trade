from __future__ import annotations

from datetime import date, datetime, timedelta

from agent.util import roc_to_date


def closed_dates(rows: list[dict]) -> set[date]:
    """證交所行事曆裡夾有『開始交易』『最後交易』，那些日子是開市，不是休市。"""
    closed: set[date] = set()
    for row in rows:
        name = str(row.get("Name") or "")
        if "開始交易" in name or "最後交易" in name:
            continue
        closed.add(roc_to_date(str(row["Date"])))
    return closed


def is_trading_day(day: date, closed: set[date]) -> bool:
    return day.weekday() < 5 and day not in closed


def next_trading_day(day: date, closed: set[date]) -> date:
    cursor = day
    while not is_trading_day(cursor, closed):
        cursor += timedelta(days=1)
    return cursor


def previous_trading_day(day: date, closed: set[date]) -> date:
    cursor = day - timedelta(days=1)
    while not is_trading_day(cursor, closed):
        cursor -= timedelta(days=1)
    return cursor


def default_trade_date(now: datetime, closed: set[date]) -> date:
    """收件窗是前一交易日 19:30 到當日 08:55。08:55 前且今天開市，就做今天。"""
    today = now.date()
    if is_trading_day(today, closed) and (now.hour, now.minute) < (8, 55):
        return today
    return next_trading_day(today + timedelta(days=1), closed)


def trading_days_between(start: date, end: date, closed: set[date]) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor, closed):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days
