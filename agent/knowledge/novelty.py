"""以既存來源判斷新增資訊；相似度只是去重提示，並非事實驗證。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


def canonical_url(url: str) -> str:
    """移除追蹤參數與片段，保留會影響原文內容的查詢參數。"""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("來源 URL 必須是沒有登入憑證的 HTTP(S) 網址")
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(sorted(query)), ""))


def content_hash(content: str) -> str:
    """使用標準化空白後的 SHA-256；不修改保存的原始文字。"""
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()


def parse_time(value: str, *, end_of_day: bool = False) -> datetime:
    """研究時間必須帶時區；查詢純日期解讀為臺北當日截止時間。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("時間戳不可缺少")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and end_of_day:
            parsed = datetime.fromisoformat(value + "T23:59:59.999999").replace(tzinfo=ZoneInfo("Asia/Taipei"))
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"無法解析 ISO 時間戳：{value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"研究時間戳必須明確指定時區：{value}")
    return parsed.astimezone(timezone.utc)


def timestamp(value: str | None = None) -> str:
    parsed = parse_time(value) if value is not None else datetime.now(timezone.utc)
    return parsed.isoformat(timespec="microseconds")


def novelty(source: dict, previous: list[dict], *, stale_days: int = 7) -> dict:
    """採可解釋規則分群，避免將轉載當成獨立佐證或把相似度當真實性。"""
    digest = content_hash(source["content"])
    url = canonical_url(source["url"])
    available = source["extraction_status"] not in {"unavailable", "error", "pending"}
    result = {"novelty": "NEW", "event_id": source.get("event_id") or source["id"],
              "duplicate_of": None, "delta": "首次保存此來源；尚未驗證投資含意。"}
    candidates = [old for old in previous if old["content_hash"] == digest and source["content"].strip()]
    exact = min(candidates, key=lambda old: (old.get("published_at") or old["fetched_at"], old["fetched_at"])) if candidates else None
    if exact:
        result.update(novelty="REPUBLICATION", event_id=exact["event_id"], duplicate_of=exact["id"],
                      delta="內容與已保存來源相同，沒有新增內容證據。")
        return result
    same_url = next((old for old in previous if canonical_url(old["url"]) == url), None)
    same_event = next((old for old in previous if source.get("event_id") and old["event_id"] == source["event_id"]), None)
    comparable = same_url or same_event
    if comparable:
        result.update(novelty="UPDATE", event_id=comparable["event_id"],
                      delta=_difference(comparable["content"], source["content"]))
    else:
        # 太短的片段容易巧合，不能據此認定同一事件。
        for old in previous:
            if min(len(source["content"]), len(old["content"])) < 80:
                continue
            ratio = SequenceMatcher(None, " ".join(old["content"].split()),
                                    " ".join(source["content"].split()), autojunk=False).ratio()
            if ratio >= 0.92:
                result.update(novelty="REPUBLICATION", event_id=old["event_id"], duplicate_of=old["id"],
                              delta="與既存內容高度相似，視為同事件轉載；未增加獨立證據。")
                return result
    if source.get("contradicts_source_ids"):
        result.update(novelty="CONTRADICTION", delta="來源明示反對既存證據；需人工或研究程序裁決，並非已證實反駁。")
    elif not available or source["evidence_tier"] in {"rumor", "inference"}:
        result.update(novelty="UNVERIFIED", delta="不可存取、未證實傳聞或研究推論，不能當作正式確認的事實。")
    elif (parse_time(source["fetched_at"]) - parse_time(source["published_at"])).days > stale_days:
        result.update(novelty="STALE", delta="發布時間早於時效門檻；首次取得不代表事件剛發生。")
    return result


def _difference(old: str, new: str) -> str:
    added = []
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for tag, _a, _b, start, end in matcher.get_opcodes():
        if tag in {"insert", "replace"} and new[start:end].strip():
            added.append(new[start:end].strip())
    text = "；".join(added)
    return "新增或替換片段（尚待驗證）：" + (text[:600] if text else "僅移除既有內容，未增加正面證據。")
