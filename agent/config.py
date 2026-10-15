from __future__ import annotations

import re
import json
from pathlib import Path

import yaml

from agent.util import ROOT, PlanError

SETTINGS_PATH = ROOT / "config" / "settings.yaml"
STRATEGY_PATH = ROOT / "config" / "strategy.yaml"
PROSPECTUS_PATH = ROOT / "ETF投資策略說明文檔.md"
SCHEMA_PATH = ROOT / "D-Plan.schema_v4.3.json"
if not SCHEMA_PATH.is_file():
    SCHEMA_PATH = ROOT / "(驗證繳交格式)D-Plan.schema_v4.2.json"
BENCHMARK_PATH = ROOT / "config" / "benchmarks.csv"
UNIVERSE_OVERRIDE = ROOT / "config" / "universe.csv"
FUNDAMENTALS_PATH = ROOT / "config" / "fundamentals.csv"
LEDGER_PATH = ROOT / "data" / "state" / "ledger.json"
NAV_DIR = ROOT / "DataPack" / "legacy"
DECISION_DIR = ROOT / "決策報告繳交紀錄"
PREVIEW_DIR = ROOT / "data" / "preview"
INBOX_BOOK = ROOT / "inbox" / "official_book.json"


def active_schema_version() -> str:
    """官方新版使用 4.x pattern，舊版使用 const；以內容驗證輸出版本。"""
    import jsonschema
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        rule = schema["properties"]["schema_version"]
        version = rule.get("const", "4.3")
        if "const" not in rule and "pattern" not in rule and "enum" not in rule:
            raise ValueError("Schema 未宣告支援版本")
        jsonschema.Draft202012Validator(rule).validate(version)
    except (OSError, ValueError, KeyError, TypeError, jsonschema.ValidationError) as exc:
        raise PlanError("無法確認 D-Plan 官方 Schema 支援的版本") from exc
    if not isinstance(version, str):
        raise PlanError("D-Plan Schema 版本必須是字串")
    return version


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise PlanError(f"找不到設定檔：{path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise PlanError(f"設定檔格式不對：{path}")
    return data


def load_settings() -> dict:
    return load_yaml(SETTINGS_PATH)


def load_strategy() -> dict:
    return load_yaml(STRATEGY_PATH)


def load_prospectus(path: Path = PROSPECTUS_PATH) -> dict:
    if not path.exists():
        raise PlanError(f"找不到公開說明書：{path}")
    text = path.read_text(encoding="utf-8")
    sections: list[tuple[str, list[str]]] = []
    current = "前言"
    bucket: list[str] = []
    for line in text.splitlines():
        if line.startswith("ETF "):
            sections.append((current, bucket))
            current = line.strip().rstrip("：:")
            bucket = []
        else:
            bucket.append(line)
    sections.append((current, bucket))
    blobs = {title: "\n".join(lines).strip() for title, lines in sections}

    def pick(*needles: str) -> str:
        for title, body in blobs.items():
            if any(needle in title for needle in needles):
                return body.strip()
        return ""

    name = pick("名稱")
    theme = pick("主題")
    philosophy = pick("理念")
    if not name or not theme or not philosophy:
        raise PlanError("公開說明書需包含 ETF 名稱、投資主題、投資理念三個段落")
    name = name.splitlines()[0].strip()
    return {"name": name, "theme": theme, "philosophy": philosophy, "text": text}


def enabled_factors(strategy: dict, prospectus: dict) -> list[tuple[str, float, dict]]:
    factors = strategy.get("factors") or {}
    blob = f"{prospectus.get('theme', '')}\n{prospectus.get('philosophy', '')}"
    selected: list[tuple[str, float, dict]] = []
    follow = bool(strategy.get("follow_prospectus", True))
    for fid, spec in factors.items():
        weight = float(spec.get("weight") or 0)
        if weight <= 0:
            continue
        keywords = spec.get("keywords") or []
        if follow and keywords and not any(word and word in blob for word in keywords):
            continue
        selected.append((fid, weight, spec.get("params") or {}))
    if not selected:
        selected = [
            (fid, float(spec.get("weight") or 0), spec.get("params") or {})
            for fid, spec in factors.items()
            if float(spec.get("weight") or 0) > 0 and fid != "dividend"
        ]
    total = sum(weight for _, weight, _ in selected)
    if total <= 0:
        raise PlanError("沒有可用的因子，請檢查 config/strategy.yaml")
    return [(fid, weight / total, params) for fid, weight, params in selected]


def valid_team_id(team_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", team_id or "")) and "REPLACE" not in team_id.upper()
