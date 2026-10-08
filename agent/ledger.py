from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

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
            total += Decimal(shares) * Decimal(str(prices[ticker]))
        return money(total)


def load_ledger(initial_capital: float) -> Ledger:
    if not LEDGER_PATH.exists():
        return Ledger(cash=money(Decimal(str(initial_capital))), positions={})
    raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    positions = {str(ticker): int(shares) for ticker, shares in (raw.get("positions") or {}).items() if int(shares) > 0}
    return Ledger(
        cash=money(Decimal(str(raw["cash"]))),
        positions=positions,
        last_settled_date=raw.get("last_settled_date"),
        pending=raw.get("pending"),
        nav_history=list(raw.get("nav_history") or []),
        reconciled=bool(raw.get("reconciled")),
    )


def save_ledger(ledger: Ledger) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cash": f"{ledger.cash:.2f}",
        "positions": {ticker: int(shares) for ticker, shares in sorted(ledger.positions.items()) if int(shares) > 0},
        "last_settled_date": ledger.last_settled_date,
        "pending": ledger.pending,
        "nav_history": ledger.nav_history,
        "reconciled": ledger.reconciled,
    }
    LEDGER_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_official_book(ledger: Ledger, pricing: date) -> str | None:
    if not INBOX_BOOK.exists():
        return None
    raw = json.loads(INBOX_BOOK.read_text(encoding="utf-8"))
    as_of = parse_date(str(raw["as_of"]))
    if as_of != pricing:
        print(f"inbox/official_book.json 的 as_of 是 {as_of}，這次定價日是 {pricing}，先不採用")
        return None
    positions = {}
    for ticker, shares in (raw.get("positions") or {}).items():
        count = int(shares)
        if count < 0:
            raise PlanError(f"官方庫存 {ticker} 股數為負")
        if count % 1000 != 0:
            raise PlanError(f"官方庫存 {ticker} 為 {count} 股，不是 1000 的倍數")
        if count > 0:
            positions[str(ticker)] = count
    ledger.cash = money(Decimal(str(raw["cash"])))
    ledger.positions = positions
    ledger.last_settled_date = as_of.isoformat()
    ledger.reconciled = True
    if ledger.pending and str(ledger.pending.get("trade_date")) <= as_of.isoformat():
        ledger.pending = None
    return f"已用官方庫存覆寫帳本，基準日 {as_of}"
