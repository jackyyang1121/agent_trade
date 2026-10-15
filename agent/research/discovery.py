"""Codex 只用原生搜尋找來源；Python 另抓正文，不把模型摘要當原文。"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from agent.llm import LLMError, build_provider
from .schema import obj, array, TEXT, NULL_TEXT
from .sources import SourceConnector, timestamp


DISCOVERY_SCHEMA = obj({
    "documents": array(obj({
        "url": TEXT, "title": TEXT, "publisher": TEXT,
        "published_at": NULL_TEXT, "content_as_of": NULL_TEXT,
        "source_type": {"type": "string", "enum": ["corporate", "technical", "financial", "public_discussion"]},
        "authority": {"type": "string", "enum": ["twse", "tpex", "mops", "fininst", "media", "other"]},
    })),
    "limitations": array(TEXT),
})


def discover(config: dict, research: dict, universe: list[str], quant: dict,
             as_of: str, ticker: str | None = None) -> tuple[list[dict], dict, list[str]]:
    if config.get("provider") != "codex_cli" or not config.get("allow_live"):
        raise LLMError("自動公開來源搜尋目前使用 Codex CLI，需 --provider codex_cli --live")
    limit = max(1, min(int(research.get("max_sources", 20)), 30))
    ranked = sorted(quant, key=lambda t: -(quant[t].get("score") or 0))[:12]
    prompt = (
        "執行公開投資研究來源探索。使用原生 web search 搜尋並開啟來源，僅輸出來源清單，尚不提出交易。"
        "涵蓋技術革新、供需瓶頸、跨產業受惠路徑、財務指引與反方證據；不要只搜尋個股新聞。"
        "只使用下列 allowed_domains，優先公司公告、法說、交易所、可信金融與技術新聞。"
        "提供原始文章網址，非搜尋結果或Google轉址。時間只能填頁面可確認且含時區的原始發布時間；"
        "找不到時間填 null，禁止填現在時間假裝發布日期。沒有可讀來源就回空清單並說明限制。"
        f"最多 {limit} 份文件。以近期資訊為主，保留重要舊證據並尋找相反觀點。\n"
        + json.dumps({"as_of": as_of, "universe": universe, "focus_ticker": ticker,
                      "quant_candidates": ranked, "allowed_domains": research.get("allowed_domains", []),
                      "research_questions": research.get("search_queries", [])}, ensure_ascii=False)
    )
    provider = build_provider({**config, "web_search": True})
    result = provider.generate(prompt, DISCOVERY_SCHEMA)
    if not result.metadata.get("web_search_calls"):
        raise LLMError("來源探索沒有實際 web search 紀錄，拒絕使用模型記憶中的網址")
    connector = SourceConnector(research)
    documents, seen = [], set()
    for row in result.data["documents"][:limit]:
        url = row["url"]
        SourceConnector._url_shape(url)
        host = urlparse(url).hostname.lower()
        if url in seen or not any(host == d or host.endswith("." + d) for d in connector.domains):
            continue
        seen.add(url)
        for field in ("published_at", "content_as_of"):
            if row.get(field):
                timestamp(row[field])
        # Corporate/official domains may contain third-party comments. Treat
        # discovered material conservatively until research corroborates it.
        documents.append({**row, "legal_basis": "public", "evidence_tier": "expert",
                          "discovery_method": "codex_native_web_search_then_python_fetch"})
    return documents, result.metadata, result.data["limitations"]
