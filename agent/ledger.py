from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from agent.config import INBOX_BOOK, LEDGER_PATH
from agent.util import PlanError, money, parse_date


@dataclass
class Ledger:
    cash: Decimal
    positions: dict[str, int]
    last_settled_date: str | None = None
    pending: dict | None = None
    nav_history: list[dict] = field(default_factory=list)
    reconciled: bool = False

    def nav_on(self, prices: dict[str, float]) -> Decimal:
        total = self.cash
        for ticker, shares in self.positions.items():
            if shares <= 0:
                continue
            if ticker not in prices:
                raise PlanError(f"記帳時 {ticker} 沒有收盤價")
            try:
                price = Decimal(str(prices[ticker]))
            except Exception as exc:
                raise PlanError(f"記帳時 {ticker} 價格無效") from exc
            if not price.is_finite() or price <= 0:
                raise PlanError(f"記帳時 {ticker} 價格必須為有限正數")
            total += Decimal(shares) * price
        return money(total)


def _book_values(raw: dict) -> tuple[Decimal, dict[str, int]]:
    """Validate the whole snapshot before changing any live ledger state."""
    if not isinstance(raw, dict) or "cash" not in raw or "positions" not in raw:
        raise PlanError("帳本必須同時包含 cash 與 positions")
    try:
        cash = Decimal(str(raw["cash"]))
    except Exception as exc:
        raise PlanError("帳本現金格式無效") from exc
    if not cash.is_finite() or cash < 0:
        raise PlanError("帳本現金必須是有限非負數")
    if not isinstance(raw["positions"], dict):
        raise PlanError("帳本 positions 必須是股票代號與股數的物件")
    positions = {}
    for ticker, shares in raw["positions"].items():
        ticker = str(ticker)
        if not re.fullmatch(r"[0-9]{4}", ticker):
            raise PlanError(f"帳本股票代號無效：{ticker}")
        try:
            numeric = Decimal(str(shares))
        except Exception as exc:
            raise PlanError(f"帳本 {ticker} 股數無效") from exc
        if isinstance(shares, bool) or not numeric.is_finite() or numeric < 0 or numeric != numeric.to_integral_value():
            raise PlanError(f"帳本 {ticker} 股數必須是非負整數")
        count = int(numeric)
        if count % 1000:
            raise PlanError(f"帳本 {ticker} 為 {count} 股，不是 1000 的倍數")
        if count:
            positions[ticker] = count
    return money(cash), positions


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"無法讀取帳本 {path}: {exc}") from exc


def load_ledger(initial_capital: float, path: Path | None = None) -> Ledger:
    path = Path(path) if path is not None else LEDGER_PATH
    if not path.exists():
        cash, _positions = _book_values({"cash": initial_capital, "positions": {}})
        return Ledger(cash=cash, positions={})
    raw = _read_json(path)
    cash, positions = _book_values(raw)
    return Ledger(
        cash=cash,
        positions=positions,
        last_settled_date=raw.get("last_settled_date"),
        pending=raw.get("pending"),
        nav_history=list(raw.get("nav_history") or []),
        reconciled=raw.get("reconciled") is True,
    )


def save_ledger(ledger: Ledger, path: Path | None = None) -> None:
    path = Path(path) if path is not None else LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cash, positions = _book_values({"cash": str(ledger.cash), "positions": ledger.positions})
    payload = {
        "cash": f"{cash:.2f}",
        "positions": dict(sorted(positions.items())),
        "last_settled_date": ledger.last_settled_date,
        "pending": ledger.pending,
        "nav_history": ledger.nav_history,
        "reconciled": ledger.reconciled,
    }
    _book_values(payload)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def official_snapshot(raw: dict, pricing: date) -> tuple[Decimal, dict[str, int]]:
    try:
        as_of = parse_date(str(raw["as_of"]))
    except (TypeError, KeyError, ValueError) as exc:
        raise PlanError("官方帳本缺少有效 as_of 日期") from exc
    if as_of != pricing:
        raise PlanError(f"官方帳本日期 {as_of} 與必要定價日 {pricing} 不一致")
    return _book_values(raw)


def apply_official_book(ledger: Ledger, pricing: date, path: Path | None = None) -> str | None:
    path = Path(path) if path is not None else INBOX_BOOK
    if not path.exists():
        return None
    raw = _read_json(path)
    cash, positions = official_snapshot(raw, pricing)
    ledger.cash = cash
    ledger.positions = positions
    ledger.last_settled_date = pricing.isoformat()
    ledger.reconciled = True
    if ledger.pending and str(ledger.pending.get("trade_date")) <= pricing.isoformat():
        ledger.pending = None
    return f"已用官方庫存核對帳本，基準日 {pricing}"
