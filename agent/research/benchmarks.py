"""模型尋找前十檔 ETF 持股；原文日期、代號與權重另由 Python 核對。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import date
from pathlib import Path

from agent.llm import build_provider
from agent.util import ROOT, PlanError, taipei_now
from agent.orchestration.io import atomic_json, execution_lock
from .schema import obj, array, TEXT
from .sources import SourceConnector


HOLDINGS_SCHEMA = obj({
    "etfs": array(obj({
        "etf_id": TEXT, "source_url": TEXT, "as_of": TEXT,
        "holdings": array(obj({"ticker": TEXT, "weight_pct": {"type": "number"}})),
    })),
    "limitations": array(TEXT),
})


def _ground(rows: list[dict], text: str, as_of: str) -> None:
    year, month, day = map(int, as_of.split("-"))
    dates = [rf"{y}\s*[-/.年]\s*0?{month}\s*[-/.月]\s*0?{day}(?:日)?" for y in (year, year - 1911)]
    if not any(re.search(pattern, text) for pattern in dates) and f"{year}{month:02}{day:02}" not in text:
        raise PlanError("ETF 原文未能核對持股資料日期")
    row_pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + '|'.join(re.escape(r['ticker']) for r in rows) + r")(?![A-Za-z0-9])")
    row_starts = [m.start() for m in row_pattern.finditer(text)]
    for row in rows:
        ticker = row["ticker"]
        weight = row["weight_pct"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,39}", ticker)
                or isinstance(weight, bool) or not math.isfinite(weight) or not 0 < weight <= 100):
            raise PlanError("ETF 代號或權重無效")
        # 不跨到下一檔持股，避免把鄰列的正確權重誤配給本列。
        windows = [text[m.end():min(next((p for p in row_starts if p > m.start()), len(text)), m.end() + 300)]
                   for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])", text)]
        if not any(any(abs(float(token.replace(',', '')) - weight) < 0.000001
                       for token in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", window)) for window in windows):
            raise PlanError(f"ETF {ticker} 的百分比權重未能在原文附近核對")


def refresh(runtime: dict, destination: Path, pricing: date, *, etf_limit: int | None = None) -> dict:
    with execution_lock(destination.parent / 'benchmark_updates'):
        return _refresh(runtime, destination, pricing, etf_limit=etf_limit)


def _refresh(runtime: dict, destination: Path, pricing: date, *, etf_limit: int | None = None) -> dict:
    config = runtime["competition"]
    reference = Path(config["official_etf_list_path"])
    reference = reference if reference.is_absolute() else ROOT / reference
    with reference.open(encoding="utf-8-sig", newline="") as handle:
        etfs = list(csv.DictReader(handle))
    limit = etf_limit if etf_limit is not None else config.get("benchmark_etf_limit", 10)
    if limit is not None:
        if isinstance(limit, bool) or int(limit) <= 0:
            raise PlanError("ETF 比較數量必須是正整數")
        etfs = etfs[:int(limit)]
    if not etfs:
        raise PlanError("ETF 參考名單為空")
    connector = SourceConnector({**runtime["research"], "allow_network": True})
    if runtime['llm'].get('provider') != 'codex_cli':
        raise PlanError('ETF 自動搜尋目前需要 Codex CLI provider')
    provider = build_provider({**runtime["llm"], "allow_live": True, "web_search": True})
    result = provider.generate(
        "以 web search 查詢以下每檔 ETF 最新可得的前十五大持股及原基金 NAV 權重。"
        "優先投信官方持股頁或官方下載文件，必須實際開啟，禁止用基金名稱猜股票或用記憶補資料。"
        "輸出百分比 weight_pct（例如 8.5 表示 8.5%），依權重遞減，只保留十五檔且不自行正規化。"
        "海外股票可填原市場代號，勿冒用台股代號。as_of 為原始持股資料日期 YYYY-MM-DD，"
        "source_url 為持股原頁而非搜尋轉址。只使用 allowed_domains，無法查證的 ETF 不輸出並說明限制。\n"
        + json.dumps({"etfs": etfs, "no_later_than": pricing.isoformat(),
                      "allowed_domains": runtime["research"].get("allowed_domains", [])}, ensure_ascii=False), HOLDINGS_SCHEMA)
    if not result.metadata.get("web_search_calls"):
        raise PlanError("ETF 更新沒有實際網路搜尋紀錄")
    expected = {e["ticker"] for e in etfs}
    parsed, snapshots, failures = {}, [], list(result.data["limitations"])
    for etf in result.data["etfs"]:
        code = etf["etf_id"]
        try:
            if code not in expected or code in parsed:
                raise PlanError("ETF 不在選定清單或重複")
            day = date.fromisoformat(etf["as_of"])
            if day > pricing or (pricing - day).days > int(config.get("benchmark_max_age_days", 7)):
                raise PlanError("ETF 持股日期超前或過期")
            rows = etf["holdings"]
            if (len(rows) != 15 or len({r["ticker"] for r in rows}) != 15
                    or sum(r["weight_pct"] for r in rows) > 100.000001
                    or [r["weight_pct"] for r in rows] != sorted((r["weight_pct"] for r in rows), reverse=True)):
                raise PlanError("ETF 必須有十五筆不重複、按原始權重遞減的持股")
            text, fetched = connector.document(etf["source_url"])
            reference_name = next(e.get('name') for e in etfs if e['ticker'] == code)
            if code not in text and (not reference_name or reference_name not in re.sub(r'\s+', '', text)):
                raise PlanError('ETF 原文未能核對基金代號或名稱')
            _ground(rows, text, etf["as_of"])
            digest = hashlib.sha256(text.encode()).hexdigest()
            parsed[code] = [{"etf_id": code, "ticker": row["ticker"], "weight": row["weight_pct"] / 100,
                             "source_url": etf["source_url"], "as_of": etf["as_of"], "fetched_at": fetched,
                             "content_sha256": digest} for row in rows]
            snapshots.append({"etf_id": code, "url": etf["source_url"], "as_of": etf["as_of"],
                              "fetched_at": fetched, "sha256": digest, "content": text})
        except (ValueError, OSError, RuntimeError) as exc:
            failures.append(f"{code}：{exc}")
    missing = sorted(expected - set(parsed))
    evidence_path = destination.parent / "benchmark_updates" / (taipei_now().strftime("%Y%m%dT%H%M%S_%f") + ".json")
    audit = {"as_of": pricing.isoformat(), "scope": [e["ticker"] for e in etfs], "missing": missing,
             "metadata": result.metadata, "sources": snapshots, "limitations": failures}
    atomic_json(evidence_path, audit, immutable=True)
    if missing:
        raise PlanError("ETF 更新不完整，保留既有 benchmarks.csv；缺少 " + ",".join(missing) + f"；診斷：{evidence_path}")
    # Replace only after every ETF passes; failed refreshes never destroy the
    # last good baseline or pretend that stale values were updated today.
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    fields = ["etf_id", "ticker", "weight", "source_url", "as_of", "fetched_at", "content_sha256"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(row for code in [e["ticker"] for e in etfs] for row in parsed[code])
    import os
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".benchmarks-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(output.getvalue())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    manifest = {"path": str(destination.resolve()), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "source_url": snapshots[0]["url"], "sources": [{k: v for k, v in s.items() if k != "content"} for s in snapshots],
                "as_of": min(s["as_of"] for s in snapshots), "etf_ids": [e["ticker"] for e in etfs],
                "definition": config.get("active_share_definition") or "top15_normalized",
                "confirmed": config.get("active_share_definition") in {"top15_normalized", "top15_raw"},
                "confirmation_method": "model_extraction_with_python_numeric_grounding", "evidence": str(evidence_path)}
    atomic_json(destination.with_suffix(".manifest.json"), manifest)
    return {"path": str(destination), "manifest": str(destination.with_suffix(".manifest.json")),
            "etfs": len(parsed), "holdings": 15 * len(parsed), "as_of": manifest["as_of"], "evidence": str(evidence_path)}
