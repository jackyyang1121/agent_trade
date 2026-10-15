"""Fail-closed competition prerequisites and the official decision lifecycle.

This module never submits orders or simulates competition fills. A saved plan is
only GENERATED; submission needs an external receipt, and only an imported
official snapshot can settle/reconcile the competition book.
"""
from __future__ import annotations

import hashlib
import csv
import json
import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from agent.config import valid_team_id
from agent.ledger import Ledger, official_snapshot
from agent.util import ROOT, TAIPEI, PlanError, stamp


def structural_errors(document: dict) -> list[str]:
    """Book-free CLI validation; this never claims trading compliance."""
    from agent.checks import structural_errors as validate_structure
    return validate_structure(document)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _path(value) -> Path:
    result = Path(value)
    return result if result.is_absolute() else ROOT / result


def _manifest(value, label: str, errors: list[str]) -> dict:
    try:
        result = value if isinstance(value, dict) else json.loads(_path(value).read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("manifest 必須是 JSON object")
    except (TypeError, ValueError, OSError) as exc:
        errors.append(f"缺少可讀取的 {label} manifest：{exc}")
        return {}
    if result.get("confirmed") is not True:
        errors.append(f"{label} 來源/完整性尚未由使用者確認")
    source = urlparse(str(result.get("source_url") or ""))
    if source.scheme != "https" or not source.hostname or source.hostname in {"example.com", "example.org"} or source.hostname.endswith((".invalid", ".test")):
        errors.append(f"{label} 缺少真實 HTTPS 來源紀錄")
    return result


def _verify_hash(path_value, manifest: dict, label: str, errors: list[str]) -> None:
    try:
        expected = str(manifest.get("sha256") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("manifest 缺少 sha256")
        actual = hashlib.sha256(_path(path_value).read_bytes()).hexdigest()
        if expected != actual:
            raise ValueError("檔案 SHA-256 與已確認 manifest 不一致")
    except (TypeError, OSError, ValueError) as exc:
        errors.append(f"{label} 版本驗證失敗：{exc}")


def validate_competition_inputs(
    settings: dict,
    runtime: dict,
    universe_rows: list[dict],
    benchmarks: dict,
    ledger: Ledger,
    trade_day: date,
    as_of: datetime,
    *,
    research_run: dict | None = None,
    pricing_date: date | None = None,
    closed: set[date] | None = None,
) -> None:
    """Reject missing/unverified prerequisites before producing a formal plan.

    Manifest confirmation records the user's official-data review; it does not
    infer the contest's unresolved Active Share definition or start date.
    """
    competition = runtime.get("competition") or {}
    errors: list[str] = []
    if not valid_team_id(str(settings.get("team_id") or "")):
        errors.append("缺少主辦方正式 team_id")
    for key, expected in (("initial_capital", 1_000_000_000), ("commission_rate", 0.001425), ("tax_rate", 0.003), ("lot", 1000)):
        try:
            value = float(settings[key])
            if not math.isfinite(value) or abs(value - expected) > max(1e-12, abs(expected) * 1e-12):
                raise ValueError("不符合競賽規定")
        except (KeyError, TypeError, ValueError):
            errors.append(f"正式競賽 {key} 必須為 {expected}")

    manifest = _manifest(competition.get("universe_manifest"), "官方固定股票池", errors)
    _verify_hash(competition.get("official_universe_path"), manifest, "官方固定股票池", errors)
    if str(manifest.get("as_of")) != str(settings.get("universe_as_of", "2026-07-31")):
        errors.append("官方股票池凍結日與設定不一致")
    tickers = [str(row.get("ticker") or "") for row in universe_rows]
    if len(tickers) != 150 or len(set(tickers)) != 150 or any(not re.fullmatch(r"[0-9]{4}", ticker) for ticker in tickers):
        errors.append("官方股票池必須是 150 個不重複四碼股票代號")
    markets = [row.get("market") for row in universe_rows]
    if markets.count("twse") != 100 or markets.count("tpex") != 50:
        errors.append("官方股票池必須包含上市 100 檔與上櫃 50 檔")
    # Hash verification alone is insufficient if a caller passed different rows.
    try:
        with _path(competition.get("official_universe_path")).open(encoding="utf-8-sig", newline="") as handle:
            official_rows = list(csv.DictReader(handle))
        actual_pairs = {(str(row.get("ticker")), row.get("market")) for row in universe_rows}
        expected_pairs = {(str(row.get("ticker")), row.get("market")) for row in official_rows}
        if actual_pairs != expected_pairs:
            errors.append("執行中的股票池與已確認官方檔案不一致")
    except (TypeError, OSError, ValueError):
        pass  # The file/hash prerequisite above already reports the missing input.

    bench_manifest = _manifest(competition.get("benchmarks_manifest"), "Active ETF 完整基準", errors)
    _verify_hash(competition.get("benchmarks_path"), bench_manifest, "Active ETF 完整基準", errors)
    declared_ids = bench_manifest.get("etf_ids")
    if not isinstance(declared_ids, list) or not declared_ids or any(not isinstance(item, str) or not item for item in declared_ids) or len(set(declared_ids)) != len(declared_ids) or set(declared_ids) != set(benchmarks):
        errors.append("Active Share 缺少已確認完整 ETF 名單或基準覆蓋不完整")
    definition = bench_manifest.get("definition")
    if definition not in {"top10_normalized", "top10_raw", "full_normalized"}:
        errors.append("Active Share 計算定義尚未明確確認")
    if not benchmarks:
        errors.append("Active Share 基準為空，禁止標記可提交")
    for etf_id, weights in benchmarks.items():
        try:
            if len(weights) != 10 or any(not re.fullmatch(r"[0-9]{4}", str(ticker)) or not math.isfinite(float(weight)) or float(weight) <= 0 for ticker, weight in weights.items()):
                raise ValueError("需要完整前十大正數權重")
            if sum(map(float, weights.values())) > 1.000001:
                raise ValueError("基準原始權重總和超過 100%")
        except (AttributeError, TypeError, ValueError) as exc:
            errors.append(f"Active ETF {etf_id} 資料不完整：{exc}")
    try:
        with _path(competition.get("benchmarks_path")).open(encoding="utf-8-sig", newline="") as handle:
            imported = {}
            for row in csv.DictReader(handle):
                etf_id, ticker = row["etf_id"], row["ticker"]
                if ticker in imported.setdefault(etf_id, {}):
                    raise ValueError(f"{etf_id}/{ticker} 重複")
                imported[etf_id][ticker] = float(row["weight"])
        if imported != benchmarks:
            errors.append("執行中的 Active ETF 基準與已確認檔案不一致")
    except (TypeError, OSError, KeyError, ValueError) as exc:
        errors.append(f"Active ETF 匯入檔格式無效或不可讀取：{exc}")
    try:
        bench_date = date.fromisoformat(str(bench_manifest.get("as_of")))
        if pricing_date is None or bench_date > pricing_date or (pricing_date - bench_date).days > int(competition.get("benchmark_max_age_days", 0)):
            errors.append("Active ETF 基準日期過期、超前或未提供必要定價日")
    except (TypeError, ValueError):
        errors.append("Active ETF 基準缺少有效 as_of")

    if competition.get("schedule_confirmed") is not True:
        errors.append("主辦方實際賽程尚未確認（文件有 10/26 與 10/27 差異）")
    try:
        first = date.fromisoformat(str(competition.get("first_trade_date")))
        last = date.fromisoformat(str(competition.get("last_trade_date")))
        if not first <= trade_day <= last:
            errors.append("交易日不在已確認競賽期間")
    except (TypeError, ValueError):
        errors.append("需要完整且有效的競賽首日及末日")
    if trade_day.weekday() >= 5 or trade_day in (closed or set()):
        errors.append("指定日期不是可交易日")
    if as_of.tzinfo is None:
        errors.append("正式決策時點必須包含時區")
    else:
        local = as_of.astimezone(TAIPEI)
        start = datetime.combine(trade_day - timedelta(days=1), time(19, 30), TAIPEI)
        end = datetime.combine(trade_day, time(8, 55), TAIPEI)
        if not start <= local <= end:
            errors.append("不在正式收件時間窗（前一日 19:30 至交易日 08:55）")

    if not ledger.reconciled or pricing_date is None or ledger.last_settled_date != pricing_date.isoformat():
        errors.append("缺少定價日已核對的主辦方現金/持股，禁止用模擬結算替代")
    try:
        official_snapshot({"as_of": pricing_date.isoformat(), "cash": str(ledger.cash), "positions": ledger.positions}, pricing_date)
        if set(ledger.positions) - set(tickers):
            errors.append("官方帳本含股票池以外持股，需先核對來源")
    except (AttributeError, PlanError) as exc:
        errors.append(f"官方帳本無效：{exc}")
    if ledger.pending:
        errors.append("正式帳本仍有未經官方核對的 pending 委託")

    research = research_run or {}
    provider = str(research.get("provider") or "").lower()
    if provider in {"", "mock", "fixture", "offline", "quant"} or research.get("synthetic") is True:
        errors.append("正式競賽必須使用可追溯的真實模型研究，禁止 Mock/Fixture")
    actual_model = str(research.get("actual_model") or research.get("model") or "").strip()
    if research.get("success") is not True or actual_model.lower() in {"", "none", "unknown", "unreported", "codex-default-unreported", "deterministic-quant-no-new-llm"}:
        errors.append("真實研究失敗或缺少實際模型執行紀錄")
    if research.get("new_research") is not True and competition.get("allow_degraded") is not True:
        errors.append("本次沒有新 GPT 研究且未明確允許保守降級")
    try:
        completed = datetime.fromisoformat(str(research.get("completed_at")))
        if completed.tzinfo is None or as_of.tzinfo is None or completed > as_of:
            errors.append("模型研究時間缺少時區或晚於決策時點")
    except (TypeError, ValueError):
        errors.append("缺少真實模型研究完成時間")
    if errors:
        raise PlanError("正式競賽尚未就緒：\n- " + "\n- ".join(errors))


class CompetitionBook:
    """Transactional, immutable plans and imported official book snapshots."""

    def __init__(self, state_root: Path, initial_capital: float = 1_000_000_000):
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.path = self.state_root / "competition.sqlite3"
        self.initial_capital = initial_capital
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    run_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL,
                    digest TEXT NOT NULL, document TEXT NOT NULL,
                    context TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                    state TEXT NOT NULL, recorded_at TEXT NOT NULL, detail TEXT);
                CREATE TABLE IF NOT EXISTS official_books (
                    as_of TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    document TEXT NOT NULL, imported_at TEXT NOT NULL);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(db, run_id: str, state: str, detail=None):
        db.execute("INSERT INTO events(run_id,state,recorded_at,detail) VALUES(?,?,?,?)", (run_id, state, stamp(datetime.now(TAIPEI)), _json(detail) if detail is not None else None))

    def get_ledger(self) -> Ledger:
        with self._connect() as db:
            row = db.execute("SELECT * FROM official_books ORDER BY as_of DESC LIMIT 1").fetchone()
        if row is None:
            from decimal import Decimal
            return Ledger(Decimal(str(self.initial_capital)), {})
        raw = json.loads(row["document"])
        pricing = date.fromisoformat(row["as_of"])
        cash, positions = official_snapshot(raw, pricing)
        return Ledger(cash, positions, last_settled_date=pricing.isoformat(), reconciled=True)

    def record_plan(self, run_id: str, doc: dict, context: dict | None = None) -> dict:
        from agent.checks import schema_errors
        errors = schema_errors(doc)
        if errors:
            raise PlanError("不能記錄無效 D-Plan：" + "; ".join(errors))
        encoded = _json(doc)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest FROM plans WHERE run_id=?", (run_id,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise PlanError("相同 Run ID 的已凍結決策不可覆寫")
            else:
                db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,NULL)", (run_id, doc["trade_date"], digest, encoded, _json(context or {}), "GENERATED"))
                self._event(db, run_id, "GENERATED", {"digest": digest})
        return self.get_plan(run_id)

    def get_plan(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM plans WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["document"] = json.loads(result["document"])
        result["context"] = json.loads(result["context"])
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        return result

    def transition(self, run_id: str, state: str, receipt=None) -> dict:
        from agent.checks import semantic_errors
        state = state.upper()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plans WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise PlanError(f"找不到正式 Run ID：{run_id}")
            previous = row["state"]
            if previous == state:
                if state == "SUBMITTED" and receipt is not None and _json(receipt) != row["receipt"]:
                    raise PlanError("已記錄的提交收據不可覆寫")
                return self.get_plan(run_id)
            if (previous, state) not in {("GENERATED", "VALIDATED"), ("VALIDATED", "SUBMITTED")}:
                raise PlanError(f"不允許狀態轉移 {previous} → {state}；結算/核對必須匯入官方帳本")
            if state == "VALIDATED":
                context = json.loads(row["context"])
                context["mode"] = "competition"
                official = db.execute("SELECT as_of,document FROM official_books ORDER BY as_of DESC LIMIT 1").fetchone()
                if official is None:
                    raise PlanError("缺少已匯入的官方帳本，不能標記 VALIDATED")
                snapshot = json.loads(official["document"])
                cash, positions = official_snapshot(snapshot, date.fromisoformat(official["as_of"]))
                from decimal import Decimal
                try:
                    matches = (context.get("held") == positions and Decimal(str(context.get("cash"))) == cash
                               and context.get("pricing_date") == official["as_of"])
                except (TypeError, ValueError, ArithmeticError):
                    matches = False
                if not matches:
                    raise PlanError("決策 context 與最新官方帳本的持股、現金或定價日不一致")
                errors, _warnings = semantic_errors(json.loads(row["document"]), context)
                if errors:
                    raise PlanError("正式 D-Plan 驗證失敗：" + "; ".join(errors))
            if state == "SUBMITTED":
                if not receipt or not isinstance(receipt, (dict, str)):
                    raise PlanError("SUBMITTED 需要主辦方實際提交成功的收據/識別碼")
                if db.execute("SELECT run_id FROM plans WHERE receipt=? AND run_id<>?", (_json(receipt), run_id)).fetchone():
                    raise PlanError("同一官方收據不能套用到不同決策紀錄")
                count = db.execute("SELECT COUNT(*) FROM events e JOIN plans p ON p.run_id=e.run_id WHERE e.state='SUBMITTED' AND p.trade_date=?", (row["trade_date"],)).fetchone()[0]
                if count >= 25:
                    raise PlanError("同日已達 25 次提交上限（不是委託筆數上限）")
            encoded_receipt = _json(receipt) if state == "SUBMITTED" else row["receipt"]
            db.execute("UPDATE plans SET state=?,receipt=? WHERE run_id=?", (state, encoded_receipt, run_id))
            self._event(db, run_id, state, receipt)
        return self.get_plan(run_id)

    def reconcile(self, rawbook: dict, pricing: date) -> Ledger:
        cash, positions = official_snapshot(rawbook, pricing)
        normalized = {**rawbook, "as_of": pricing.isoformat(), "cash": str(cash), "positions": positions}
        encoded = _json(normalized)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest FROM official_books WHERE as_of=?", (pricing.isoformat(),)).fetchone()
            if old and old["digest"] != digest:
                raise PlanError("同日官方快照已凍結；資料不一致需先獨立調查，禁止靜默覆寫")
            if not old:
                db.execute("INSERT INTO official_books VALUES(?,?,?,?)", (pricing.isoformat(), digest, encoded, stamp(datetime.now(TAIPEI))))
                submitted = db.execute("SELECT run_id FROM plans WHERE trade_date=? AND state='SUBMITTED'", (pricing.isoformat(),)).fetchall()
                # Multiple submissions can replace one another. Never claim all
                # revisions executed: associate an explicit run or the only one.
                official_run = rawbook.get("run_id")
                settled = [plan for plan in submitted if plan["run_id"] == official_run] if official_run else submitted if len(submitted) == 1 else []
                for plan in settled:
                    for state in ("SETTLED", "RECONCILED"):
                        self._event(db, plan["run_id"], state, {"official_book_digest": digest})
                    db.execute("UPDATE plans SET state='RECONCILED' WHERE run_id=?", (plan["run_id"],))
        return self.get_ledger()
