from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import requests

from agent.calendar_tw import closed_dates, trading_days_between
from agent.config import BENCHMARK_PATH, UNIVERSE_OVERRIDE
from agent.factors import display_metrics, load_fundamentals, score_universe
from agent.util import ROOT, PlanError, cell_num, roc_to_date, stamp, taipei_now

CACHE = ROOT / "data" / "cache"
DAILY = CACHE / "daily"
ACTIVE = CACHE / "active"
REF = CACHE / "ref"
UNIVERSE_PATH = ROOT / "data" / "universe" / "universe.csv"
TAIEX_PATH = CACHE / "taiex.csv"

FALLBACK_CLOSED = {
    date(2026, 1, 1),
    date(2026, 2, 12),
    date(2026, 2, 13),
    date(2026, 2, 15),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 27),
    date(2026, 2, 28),
    date(2026, 4, 3),
    date(2026, 4, 4),
    date(2026, 4, 5),
    date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 10, 9),
    date(2026, 10, 10),
    date(2026, 10, 25),
    date(2026, 10, 26),
    date(2026, 12, 25),
}

US_SYMBOLS = {
    "SOX": "^SOX",
    "NDX": "^NDX",
    "SPX": "^GSPC",
    "TSM": "TSM",
    "UMC": "UMC",
}


class Http:
    def __init__(self) -> None:
        self.calls = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

    def get_json(self, url: str, params: dict | None = None):
        last: Exception | None = None
        for attempt in range(4):
            try:
                self.calls += 1
                response = self.session.get(url, params=params, timeout=45)
                if response.status_code >= 400:
                    last = PlanError(f"HTTP {response.status_code} {url}")
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return response.json()
            except Exception as exc:
                last = exc
                time.sleep(0.8 * (attempt + 1))
        raise PlanError(f"下載失敗 {url}：{last}")


def load_holidays(http: Http) -> set[date]:
    REF.mkdir(parents=True, exist_ok=True)
    path = REF / "holidays.json"
    rows = None
    if path.exists() and time.time() - path.stat().st_mtime < 24 * 3600:
        rows = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            rows = http.get_json("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule")
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        except PlanError as exc:
            print(f"休市表下載失敗，改用內建 2026 行事曆：{exc}")
            return set(FALLBACK_CLOSED)
    try:
        return closed_dates(rows)
    except Exception as exc:
        print(f"休市表解析失敗，改用內建 2026 行事曆：{exc}")
        return set(FALLBACK_CLOSED)


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _fresh(path: Path, hours: float) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < hours * 3600


def _cached_json(http: Http, url: str, path: Path, hours: float):
    if _fresh(path, hours):
        return json.loads(path.read_text(encoding="utf-8"))
    payload = http.get_json(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _banned(name: str) -> bool:
    return any(word in name for word in ("ETF", "受益", "指數", "權證", "牛熊", "特別股", "主動"))


def _parse_twse(payload: dict) -> tuple[list[dict], dict, list[dict]]:
    stat = str(payload.get("stat") or "")
    if stat.upper() != "OK":
        if "沒有" in stat or "抱歉" in stat:
            return [], {}, []
        raise PlanError(f"證交所回應：{stat}")
    quotes: list[dict] = []
    actives: list[dict] = []
    index = {"close": None, "chg": None, "semi": None}
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        data = table.get("data") or []
        if "指數" in fields and "收盤指數" in fields:
            pct = "漲跌百分比(%)"
            loc = {name: i for i, name in enumerate(fields)}
            for row in data:
                label = str(row[0]).strip()
                if label == "發行量加權股價指數":
                    index["close"] = cell_num(row[loc["收盤指數"]])
                    index["chg"] = cell_num(row[loc[pct]]) if pct in loc else None
                elif label == "半導體類指數":
                    index["semi"] = cell_num(row[loc[pct]]) if pct in loc else None
        if "證券代號" not in fields or "收盤價" not in fields:
            continue
        loc = {name: i for i, name in enumerate(fields)}
        for row in data:
            ticker = str(row[loc["證券代號"]]).strip()
            name = str(row[loc["證券名稱"]]).strip()
            if "主動" in name:
                actives.append({"ticker": ticker, "name": name, "market": "twse"})
            if len(ticker) != 4 or not ticker.isdigit() or ticker.startswith("00") or _banned(name):
                continue
            close = cell_num(row[loc["收盤價"]])
            volume = cell_num(row[loc["成交股數"]]) or 0.0
            value = cell_num(row[loc["成交金額"]]) or 0.0
            if close is None or close <= 0:
                continue
            pe = cell_num(row[loc["本益比"]]) if "本益比" in loc else None
            quotes.append(
                {
                    "ticker": ticker,
                    "market": "twse",
                    "name": name,
                    "close": close,
                    "volume": volume,
                    "value": value,
                    "vwap": (value / volume) if volume > 0 and value > 0 else close,
                    "pe": pe if pe and pe > 0 else "",
                }
            )
    return quotes, index, actives


def _parse_tpex(payload: dict) -> tuple[list[dict], list[dict]]:
    stat = str(payload.get("stat") or "")
    if stat and stat.upper() not in {"OK", "0"}:
        if "沒有" in stat or "抱歉" in stat:
            return [], []
        raise PlanError(f"櫃買中心回應：{stat}")
    tables = payload.get("tables") or []
    if not tables:
        return [], []
    fields = tables[0].get("fields") or []
    loc = {name: i for i, name in enumerate(fields)}
    quotes: list[dict] = []
    actives: list[dict] = []
    for row in tables[0].get("data") or []:
        ticker = str(row[loc["代號"]]).strip()
        name = str(row[loc["名稱"]]).strip()
        if "主動" in name:
            actives.append({"ticker": ticker, "name": name, "market": "tpex"})
        if len(ticker) != 4 or not ticker.isdigit() or ticker.startswith("00") or _banned(name):
            continue
        close = cell_num(row[loc["收盤"]])
        volume = cell_num(row[loc["成交股數"]]) or 0.0
        value = cell_num(row[loc["成交金額(元)"]]) or 0.0
        average = cell_num(row[loc["均價"]]) if "均價" in loc else None
        if close is None or close <= 0:
            continue
        quotes.append(
            {
                "ticker": ticker,
                "market": "tpex",
                "name": name,
                "close": close,
                "volume": volume,
                "value": value,
                "vwap": average if average and average > 0 else ((value / volume) if volume > 0 and value > 0 else close),
                "pe": "",
            }
        )
    return quotes, actives


def _write_daily(day: date, rows: list[dict], index: dict, actives: list[dict]) -> None:
    DAILY.mkdir(parents=True, exist_ok=True)
    ACTIVE.mkdir(parents=True, exist_ok=True)
    path = DAILY / f"{day.isoformat()}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "market", "name", "close", "volume", "value", "vwap", "pe"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with (ACTIVE / f"{day.isoformat()}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "market"])
        writer.writeheader()
        writer.writerows(actives)
    if index.get("close"):
        existing: dict[str, dict] = {}
        if TAIEX_PATH.exists():
            with TAIEX_PATH.open(encoding="utf-8", newline="") as handle:
                for item in csv.DictReader(handle):
                    existing[item["date"]] = item
        existing[day.isoformat()] = {
            "date": day.isoformat(),
            "close": index.get("close") or "",
            "chg": index.get("chg") if index.get("chg") is not None else "",
            "semi": index.get("semi") if index.get("semi") is not None else "",
        }
        with TAIEX_PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "close", "chg", "semi"])
            writer.writeheader()
            for key in sorted(existing):
                writer.writerow(existing[key])


def read_daily(day: date) -> list[dict]:
    path = DAILY / f"{day.isoformat()}.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        for item in csv.DictReader(handle):
            rows.append(
                {
                    "ticker": item["ticker"],
                    "market": item["market"],
                    "name": item["name"],
                    "close": float(item["close"]),
                    "volume": float(item["volume"] or 0),
                    "value": float(item["value"] or 0),
                    "vwap": float(item["vwap"] or item["close"]),
                    "pe": float(item["pe"]) if item.get("pe") else None,
                }
            )
    return rows


def ensure_day(http: Http, day: date, required: bool) -> list[dict]:
    path = DAILY / f"{day.isoformat()}.csv"
    if path.exists():
        return read_daily(day)
    twse_url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={_ymd(day)}&type=ALLBUT0999"
    tpex_url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={day.strftime('%Y/%m/%d')}&response=json"
    twse_payload = http.get_json(twse_url)
    time.sleep(0.05)
    tpex_payload = http.get_json(tpex_url)
    time.sleep(0.05)
    twse_rows, index, twse_active = _parse_twse(twse_payload)
    tpex_rows, tpex_active = _parse_tpex(tpex_payload)
    merged: dict[str, dict] = {}
    for row in twse_rows:
        merged[row["ticker"]] = row
    for row in tpex_rows:
        merged.setdefault(row["ticker"], row)
    rows = list(merged.values())
    if required and not rows:
        raise PlanError(f"{day} 沒有抓到收盤行情，請稍後重跑；這個交易日的資料可能還沒公布")
    _write_daily(day, rows, index, twse_active + tpex_active)
    return rows


def ensure_history(http: Http, start: date, end: date, closed: set[date]) -> None:
    days = trading_days_between(start, end, closed)
    print(f"價格歷史 {start} 到 {end}，共 {len(days)} 個交易日（已快取的會跳過）")
    for index, day in enumerate(days, start=1):
        ensure_day(http, day, required=(day == end))
        if index == 1 or index == len(days) or index % 10 == 0:
            print(f"  行情 {index}/{len(days)} {day}")


def _companies(http: Http) -> dict[str, dict]:
    listed = _cached_json(http, "https://openapi.twse.com.tw/v1/opendata/t187ap03_L", REF / "twse_company.json", 24 * 7)
    otc = _cached_json(http, "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", REF / "tpex_company.json", 24 * 7)
    out: dict[str, dict] = {}
    for row in listed:
        ticker = str(row.get("公司代號") or "").strip()
        shares = cell_num(row.get("已發行普通股數或TDR原股發行股數"))
        if len(ticker) == 4 and ticker.isdigit() and shares and shares > 0:
            out[ticker] = {
                "name": str(row.get("公司簡稱") or ticker).strip(),
                "industry": str(row.get("產業別") or "NA").strip() or "NA",
                "shares": int(shares),
                "market": "twse",
            }
    for row in otc:
        ticker = str(row.get("SecuritiesCompanyCode") or "").strip()
        shares = cell_num(row.get("IssueShares"))
        if len(ticker) == 4 and ticker.isdigit() and shares and shares > 0 and ticker not in out:
            out[ticker] = {
                "name": str(row.get("CompanyAbbreviation") or ticker).strip(),
                "industry": str(row.get("SecuritiesIndustryCode") or "NA").strip() or "NA",
                "shares": int(shares),
                "market": "tpex",
            }
    return out


def _read_universe(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for item in csv.DictReader(handle):
            ticker = (item.get("ticker") or "").strip()
            if len(ticker) != 4 or not ticker.isdigit():
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "name": (item.get("name") or ticker).strip(),
                    "market": (item.get("market") or "twse").strip().lower(),
                    "industry": (item.get("industry") or "NA").strip() or "NA",
                }
            )
    return rows


def load_universe(http: Http, as_of: date, listed_n: int, otc_n: int, closed: set[date], path_override: Path | None = None) -> list[dict]:
    if path_override:
        return _read_universe(path_override)
    if UNIVERSE_OVERRIDE.exists():
        rows = _read_universe(UNIVERSE_OVERRIDE)
        if rows:
            print(f"使用覆寫標的池 config/universe.csv，{len(rows)} 檔")
            return rows
    if UNIVERSE_PATH.exists():
        rows = _read_universe(UNIVERSE_PATH)
        if len(rows) >= listed_n:
            return rows
    raise PlanError("缺少固定股票池；請匯入主辦方名單，不自動重算市值排名。可用 import-universe PATH")


def build_research_universe(http: Http, as_of: date, listed_n: int, otc_n: int, closed: set[date]) -> list[dict]:
    """僅供顯式研究用途；產出不是已確認的競賽官方股票池。"""
    ensure_day(http, as_of, required=True)
    companies = _companies(http)
    book = {row["ticker"]: row for row in read_daily(as_of)}
    ranked = {"twse": [], "tpex": []}
    for ticker, info in companies.items():
        quote = book.get(ticker)
        if quote is None or quote["market"] != info["market"]:
            continue
        if _banned(quote["name"]):
            continue
        cap = quote["close"] * info["shares"]
        ranked[info["market"]].append((cap, ticker, quote["name"], info["industry"], info["market"]))
    chosen = []
    for market, limit in (("twse", listed_n), ("tpex", otc_n)):
        ranked[market].sort(key=lambda item: -item[0])
        for cap, ticker, name, industry, venue in ranked[market][:limit]:
            chosen.append({"ticker": ticker, "name": name, "market": venue, "industry": industry, "cap": int(cap)})
    if len(chosen) < listed_n + otc_n:
        raise PlanError(f"{as_of} 只能排出 {len(chosen)} 檔，少於上市 {listed_n} 加上櫃 {otc_n}")
    UNIVERSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with UNIVERSE_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "market", "industry", "cap"])
        writer.writeheader()
        writer.writerows(chosen)
    top = ", ".join(f"{row['ticker']}{row['name']}" for row in chosen[:3])
    print(f"已建立 {as_of} 市值標的池 {len(chosen)} 檔，上市前段：{top}")
    if not any(row["ticker"] == "2330" for row in chosen[:5]):
        print("警告：2330 不在上市前五名，請核對標的池是否和競賽名單一致")
    return chosen


def _valuation(http: Http) -> dict[str, dict]:
    payload = _cached_json(http, "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", REF / "bwibbu.json", 12)
    out = {}
    as_of = None
    for row in payload:
        ticker = str(row.get("Code") or "").strip()
        pe = cell_num(row.get("PEratio"))
        pb = cell_num(row.get("PBratio"))
        div = cell_num(row.get("DividendYield"))
        if as_of is None and row.get("Date"):
            try:
                as_of = roc_to_date(str(row["Date"]))
            except PlanError:
                as_of = None
        out[ticker] = {
            "pe": pe if pe and pe > 0 else None,
            "pb": pb if pb and pb > 0 else None,
            "dividend_yield": div if div is not None and div >= 0 else None,
        }
    return out, as_of


def _revenue(http: Http, url: str, path: Path) -> tuple[dict[str, float], date | None]:
    payload = _cached_json(http, url, path, 12)
    out: dict[str, float] = {}
    published = None
    yoy_key = "營業收入-去年同月增減(%)"
    for row in payload:
        ticker = str(row.get("公司代號") or "").strip()
        yoy = cell_num(row.get(yoy_key))
        if ticker and yoy is not None:
            out[ticker] = yoy
        if published is None and row.get("出表日期"):
            try:
                published = roc_to_date(str(row["出表日期"]))
            except PlanError:
                published = None
    return out, published


def _foreign(http: Http, day: date) -> dict:
    url = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate={_ymd(day)}&type=day"
    path = REF / f"bfi_{day.isoformat()}.json"
    payload = _cached_json(http, url, path, 18)
    result = {"foreign_bn": None, "trust_bn": None, "url": url}
    if str(payload.get("stat") or "").upper() != "OK":
        return result
    fields = payload.get("fields") or []
    loc = {name: i for i, name in enumerate(fields)}
    gap = loc.get("買賣差額", 3)
    for row in payload.get("data") or []:
        label = str(row[0])
        value = cell_num(row[gap])
        if value is None:
            continue
        if label.startswith("外資及陸資"):
            result["foreign_bn"] = value / 1e8
        elif label.startswith("投信"):
            result["trust_bn"] = value / 1e8
    return result


def _yahoo(http: Http, key: str, symbol: str) -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}?range=10d&interval=1d"
    try:
        payload = http.get_json(url)
    except PlanError as exc:
        print(f"美股 {key} 下載失敗：{exc}")
        return None
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    pairs = [(ts, close) for ts, close in zip(stamps, closes) if close]
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    if pairs and regular.get("start") and now < regular.get("end", 0) and pairs[-1][0] >= regular["start"]:
        pairs = pairs[:-1]
    if len(pairs) < 2 or pairs[-2][1] <= 0:
        return None
    chg = (pairs[-1][1] / pairs[-2][1] - 1) * 100
    as_of = datetime.fromtimestamp(pairs[-1][0], tz=taipei_now().tzinfo)
    return {"chg_pct": chg, "last": pairs[-1][1], "as_of": stamp(as_of), "url": url, "source_key": f"us_{key.lower()}"}


def _taiex_history() -> list[dict]:
    if not TAIEX_PATH.exists():
        return []
    rows = []
    with TAIEX_PATH.open(encoding="utf-8", newline="") as handle:
        for item in csv.DictReader(handle):
            close = cell_num(item.get("close"))
            if close:
                rows.append(
                    {
                        "date": item["date"],
                        "close": close,
                        "chg": cell_num(item.get("chg")),
                        "semi": cell_num(item.get("semi")),
                    }
                )
    return rows


def load_benchmarks() -> dict[str, dict[str, float]]:
    if not BENCHMARK_PATH.exists():
        return {}
    groups: dict[str, dict[str, float]] = {}
    with BENCHMARK_PATH.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            etf = (row.get("etf_id") or "").strip()
            ticker = (row.get("ticker") or "").strip()
            if not etf or not ticker:
                continue
            try:
                weight = float(row.get("weight") or "")
            except ValueError:
                continue
            if weight > 1:
                weight /= 100.0
            if weight <= 0:
                continue
            groups.setdefault(etf, {})[ticker] = weight
    return groups


def prepare(http: Http, settings: dict, strategy_factors: list[tuple[str, float, dict]], pricing: date, held: dict[str, int], closed: set[date]) -> dict:
    start = datetime.strptime(settings["history_start"], "%Y-%m-%d").date()
    as_of = datetime.strptime(settings["universe_as_of"], "%Y-%m-%d").date()
    if as_of not in closed and as_of.weekday() < 5 and as_of < start:
        start = as_of
    ensure_history(http, start, pricing, closed)
    universe = load_universe(http, as_of, int(settings["universe_listed"]), int(settings["universe_otc"]), closed,
                             Path(settings["official_universe_path"]) if settings.get("official_universe_path") else None)
    companies = _companies(http)
    tickers = [row["ticker"] for row in universe]
    for ticker in held:
        if ticker not in tickers:
            tickers.append(ticker)
    days = trading_days_between(start, pricing, closed)
    dates: list[date] = []
    closes = {ticker: [] for ticker in tickers}
    values = {ticker: [] for ticker in tickers}
    names = {row["ticker"]: row["name"] for row in universe}
    markets = {row["ticker"]: row["market"] for row in universe}
    industries = {row["ticker"]: row["industry"] for row in universe}
    history_rows = {ticker: [] for ticker in tickers}
    for day in days:
        book = {row["ticker"]: row for row in read_daily(day)}
        if not book:
            continue
        dates.append(day)
        for ticker in tickers:
            row = book.get(ticker)
            if row and row["close"] > 0:
                history_rows[ticker].append({"date": day.isoformat(), "close": row["close"], "volume": row["volume"],
                                             "turnover": row["value"], "value": row["value"], "vwap": row["vwap"]})
                closes[ticker].append(row["close"])
                values[ticker].append(row["value"] if row["value"] > 0 else None)
                names.setdefault(ticker, row["name"])
                markets.setdefault(ticker, row["market"])
            else:
                closes[ticker].append(None)
                values[ticker].append(None)
            info = companies.get(ticker)
            if info:
                industries.setdefault(ticker, info["industry"])
                names.setdefault(ticker, info["name"])
    if not dates or dates[-1] != pricing:
        raise PlanError(f"定價日 {pricing} 的行情不完整")
    valuation, valuation_date = _valuation(http)
    rev_listed, rev_listed_date = _revenue(http, "https://openapi.twse.com.tw/v1/opendata/t187ap05_L", REF / "rev_twse.json")
    rev_otc, rev_otc_date = _revenue(http, "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O", REF / "rev_tpex.json")
    revenue = dict(rev_otc)
    revenue.update(rev_listed)
    end = len(dates) - 1
    metrics = display_metrics(tickers, dates, closes, values, end, valuation, revenue)
    pricing_book = {row["ticker"]: row for row in read_daily(pricing)}
    for ticker in tickers:
        row = pricing_book.get(ticker)
        if ticker not in metrics and row:
            metrics[ticker] = {"close": row["close"], "rev_yoy": revenue.get(ticker)}
        if ticker not in metrics:
            continue
        if row:
            metrics[ticker]["close"] = row["close"]
            if metrics[ticker].get("pe") is None and row.get("pe"):
                metrics[ticker]["pe"] = row["pe"]
        if valuation.get(ticker):
            for key, value in valuation[ticker].items():
                if value is not None:
                    metrics[ticker][key] = value
        if revenue.get(ticker) is not None:
            metrics[ticker]["rev_yoy"] = revenue[ticker]
    fundamentals = load_fundamentals()
    scores, components = score_universe(tickers, dates, closes, values, metrics, strategy_factors, fundamentals)
    prices = {ticker: row["close"] for ticker, row in pricing_book.items() if ticker in tickers}
    for ticker in held:
        if ticker not in prices:
            raise PlanError(f"持股 {ticker} 在 {pricing} 沒有收盤價")
    taiex_rows = [row for row in _taiex_history() if row["date"] <= pricing.isoformat()]
    if not taiex_rows:
        raise PlanError("沒有加權指數資料")
    last = taiex_rows[-1]
    if last["date"] != pricing.isoformat():
        raise PlanError(f"加權指數只到 {last['date']}，還沒有定價日 {pricing}")
    def level_return(n: int) -> float | None:
        if len(taiex_rows) <= n:
            return None
        base = taiex_rows[-1 - n]["close"]
        if base <= 0:
            return None
        return last["close"] / base - 1

    foreign = _foreign(http, pricing)
    us = {}
    for key, symbol in US_SYMBOLS.items():
        item = _yahoo(http, key, symbol)
        if item:
            us[key] = item
    fetched = stamp(taipei_now())
    pricing_close = f"{pricing.isoformat()}T13:30:00+08:00"
    source_material = {
        "twse": {
            "authority": "twse",
            "name": f"證交所 {pricing} 大盤與上市收盤",
            "url": f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={_ymd(pricing)}&type=ALLBUT0999",
            "content_as_of": pricing_close,
            "fetched_at": fetched,
        },
        "tpex": {
            "authority": "tpex",
            "name": f"櫃買中心 {pricing} 上櫃收盤",
            "url": f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={pricing.strftime('%Y/%m/%d')}&response=json",
            "content_as_of": pricing_close,
            "fetched_at": fetched,
        },
        "valuation": {
            "authority": "twse",
            "name": "證交所個股本益比、殖利率、股價淨值比",
            "url": "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
            "content_as_of": f"{(valuation_date or pricing).isoformat()}T18:00:00+08:00",
            "fetched_at": fetched,
        },
        "rev_twse": {
            "authority": "mops",
            "name": "上市股票月營收（公開資訊觀測站）",
            "url": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
            "content_as_of": f"{(rev_listed_date or pricing).isoformat()}T18:00:00+08:00",
            "fetched_at": fetched,
        },
        "rev_tpex": {
            "authority": "mops",
            "name": "上櫃股票月營收（公開資訊觀測站）",
            "url": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
            "content_as_of": f"{(rev_otc_date or pricing).isoformat()}T18:00:00+08:00",
            "fetched_at": fetched,
        },
        "bfi": {
            "authority": "twse",
            "name": f"證交所 {pricing} 三大法人買賣金額",
            "url": foreign["url"],
            "content_as_of": f"{pricing.isoformat()}T18:00:00+08:00",
            "fetched_at": fetched,
        },
    }
    for key, item in us.items():
        source_material[item["source_key"]] = {
            "authority": "vendor",
            "name": f"Yahoo Finance {key} 日線",
            "url": item["url"],
            "content_as_of": item["as_of"],
            "fetched_at": fetched,
        }
    for key, source in source_material.items():
        source["key"] = key
    active_path = ACTIVE / f"{pricing.isoformat()}.csv"
    actives = []
    if active_path.exists():
        with active_path.open(encoding="utf-8", newline="") as handle:
            actives = list(csv.DictReader(handle))
    ref = CACHE / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    with (ref / "active_etfs.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "market"])
        writer.writeheader()
        writer.writerows(actives)
    return {
        "names": names,
        "markets": markets,
        "industries": industries,
        "prices": prices,
        "metrics": metrics,
        "scores": scores,
        "components": components,
        "market": {
            "taiex_close": last["close"],
            "taiex_chg_pct": last.get("chg"),
            "taiex_ret_5": level_return(5),
            "taiex_ret_20": level_return(20),
            "semi_chg_pct": last.get("semi"),
            "foreign_bn": foreign.get("foreign_bn"),
            "trust_bn": foreign.get("trust_bn"),
            "us": us,
        },
        "source_material": source_material,
        "active_etfs": actives,
        "universe_count": len(universe),
        "universe": universe,
        "histories": history_rows,
        "taiex_history": taiex_rows,
    }
