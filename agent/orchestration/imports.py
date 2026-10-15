from __future__ import annotations

import csv
import hashlib
import math
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from agent.util import PlanError
from .io import atomic_json


def read_universe(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    tickers = [r.get("ticker", "") for r in rows]
    if not rows or len(set(tickers)) != len(tickers) or any(len(t) != 4 or not t.isdigit() for t in tickers):
        raise PlanError("股票池 ticker 必須為唯一四位數代號")
    if any(r.get("market") not in {"twse", "tpex"} or not r.get("industry") for r in rows):
        raise PlanError("股票池需要 market(twse/tpex)、industry 欄位")
    return rows


def read_benchmarks(path: Path) -> dict[str, dict[str, float]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result = {}
    for row in rows:
        ticker, etf = row.get("ticker", ""), row.get("etf_id", "")
        try:
            weight = float(row["weight"])
        except (ValueError, KeyError) as exc:
            raise PlanError("ETF 持股 weight 需為原基金 NAV 權重小數，例如 0.08") from exc
        if not etf or len(ticker) != 4 or not ticker.isdigit() or not math.isfinite(weight) or not 0 < weight <= 1:
            raise PlanError("無效 ETF 持股列")
        if ticker in result.setdefault(etf, {}):
            raise PlanError("同一 ETF 有重複持股")
        result[etf][ticker] = weight
    if not result or any(len(book) < 10 or sum(book.values()) > 1.000001 for book in result.values()):
        raise PlanError("每檔 ETF 需至少十筆真實持股；原基金 NAV 權重加總不可超過1")
    return result


def import_data(kind: str, path: Path, state_dir: Path, *, source_url: str, as_of: str,
                confirmed: bool = False, definition: str | None = None) -> dict:
    if urlsplit(source_url).scheme != "https" or not urlsplit(source_url).hostname:
        raise PlanError("匯入必須記錄真實 HTTPS 官方來源／合法授權來源 URL")
    date.fromisoformat(as_of)
    parsed = read_universe(path) if kind == "universe" else read_benchmarks(path)
    if kind == "universe" and (len(parsed) != 150 or sum(r["market"] == "twse" for r in parsed) != 100 or sum(r["market"] == "tpex" for r in parsed) != 50):
        raise PlanError("官方股票池應為上市100檔＋上櫃50檔")
    if kind == "benchmarks" and definition not in {"top10_normalized", "top10_raw", "full_normalized"}:
        raise PlanError("請明確指定經主辦確認的 --definition；不得猜測 Active Share 公式")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    destination = state_dir / "imports" / f"{kind}_{as_of}_{digest[:12]}.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(path, destination)
    manifest = {"source_url": source_url, "confirmed": confirmed, "sha256": digest, "as_of": as_of,
                "path": str(destination.resolve())}
    if kind == "benchmarks":
        manifest.update(etf_ids=sorted(parsed), definition=definition)
    manifest_path = destination.with_suffix(".manifest.json")
    atomic_json(manifest_path, manifest, immutable=True)
    return {"path": str(destination.resolve()), "manifest": str(manifest_path.resolve()), "confirmed": confirmed}
