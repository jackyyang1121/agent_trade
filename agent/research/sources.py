"""公開或已授權來源的有限擷取；禁止把搜尋摘要冒充已讀原文。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests

from agent.knowledge.novelty import content_hash


class SourceError(ValueError):
    pass


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SourceError("時間必須是含時區的 ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SourceError("時間必須含時區")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.published_at = None
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta" and (attributes.get("property") or attributes.get("name")) in {
            "article:published_time", "datePublished", "date", "pubdate",
        }:
            value = attributes.get("content")
            if value:
                try:
                    timestamp(value)
                    self.published_at = value
                except SourceError:
                    pass

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data.strip())
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


class SourceConnector:
    LEGAL_BASES = {"public", "user_authorized", "licensed", "test_fixture"}
    SOURCE_TYPES = {"official_market", "corporate", "technical", "financial", "public_discussion"}
    EVIDENCE_TIERS = {"official", "corroborated", "expert", "rumor", "inference"}

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self.allow_network = bool(self.config.get("allow_network", False))
        self.timeout = max(0.1, float(self.config.get("source_timeout_seconds", 15)))
        self.max_bytes = max(1024, int(self.config.get("max_source_bytes", 1_000_000)))
        self.max_chars = max(100, int(self.config.get("max_source_chars", 16000)))
        self.roots = [Path(value).expanduser().resolve() for value in self.config.get("source_roots", [])]
        self.domains = {str(value).lower() for value in self.config.get("allowed_domains", [])}

    def _url(self, url: str) -> None:
        self._url_shape(url)
        parsed = urlparse(url)
        host = parsed.hostname.lower()
        if not self.domains:
            raise SourceError("網路擷取需要明確配置受信任的 allowed_domains；不接受任意下載網域")
        if not any(host == item or host.endswith("." + item) for item in self.domains):
            raise SourceError("來源網域不在設定的 allowed_domains")
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise SourceError("來源 DNS 無法解析") from exc
        if not addresses:
            raise SourceError("來源 DNS 無有效公開位址")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
            if not ip.is_global:
                raise SourceError("禁止存取私有、loopback、link-local 或保留網路位址")

    @staticmethod
    def _url_shape(url: str) -> None:
        if not isinstance(url, str):
            raise SourceError("來源 URL 必須是文字")
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise SourceError("來源只允許無帳密的 HTTPS URL；本地文件亦需提供原始公開或授權來源 URL")
            if parsed.port not in (None, 443):
                raise SourceError("來源 URL 不允許自訂連接埠")
        except ValueError as exc:
            raise SourceError("來源 URL 格式不合法") from exc

    def fetch(self, url: str) -> tuple[bytes, str, str]:
        if not self.allow_network:
            raise SourceError("network_disabled：此執行未授權網路研究")
        current = url
        deadline = time.monotonic() + self.timeout
        # Every redirect is checked separately; no cookies/login/paywall bypass.
        for _ in range(4):
            self._url(current)
            with requests.get(
                current, timeout=self.timeout, stream=True, allow_redirects=False,
                headers={"User-Agent": "agent_trade research/1.0 (public research; no login)"},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    current = urljoin(current, response.headers.get("Location", ""))
                    continue
                if response.status_code in (401, 403, 429):
                    raise SourceError(f"unavailable：HTTP {response.status_code}；不繞過登入、授權或速率限制")
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(16384):
                    if time.monotonic() > deadline:
                        raise SourceError("來源抓取超過時間預算")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SourceError("來源內容超過資料量預算")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("Content-Type", ""), current
        raise SourceError("來源重新導向次數超過限制")

    def _file(self, value: str) -> tuple[bytes, str]:
        path = Path(value).expanduser().resolve()
        if not self.roots or not any(path.is_relative_to(root) for root in self.roots):
            raise SourceError("來源檔案不在明確授權的 source_roots")
        if path.suffix.lower() not in {".txt", ".md", ".html", ".htm", ".csv", ".json"}:
            raise SourceError("不支援此檔案格式；PDF 請提供有授權的文字擷取版本")
        if not path.is_file() or path.stat().st_size > self.max_bytes:
            raise SourceError("來源檔案不存在或超過資料量預算")
        return path.read_bytes(), "text/html" if path.suffix.lower() in {".html", ".htm"} else "text/plain"

    def ingest(self, manifest: dict, as_of: str) -> dict:
        """回傳可封存的 source。無法取得/無日期也保留 unavailable 紀錄。"""
        if not isinstance(manifest, dict):
            raise SourceError("來源 manifest 必須是 JSON object")
        cutoff = timestamp(as_of)
        source = dict(manifest)
        source.setdefault("url", "")
        source.setdefault("id", "SRC_" + hashlib.sha256(
            str(source.get("url") or source.get("path") or json.dumps(source, sort_keys=True)).encode()
        ).hexdigest()[:20])
        source.setdefault("title", "未提供標題")
        source.setdefault("publisher", "未提供發布機構")
        source.setdefault("source_type", "public_discussion")
        source.setdefault("evidence_tier", "inference")
        source.setdefault("legal_basis", "unknown")
        source.setdefault("published_at", None)
        source.setdefault("content_as_of", None)
        source.setdefault("content", "")
        source["synthetic"] = bool(source.get("synthetic") or source["legal_basis"] == "test_fixture")
        supplied_content = bool(source.get("content"))
        source["fetched_at"] = source.get("fetched_at") if supplied_content else utc_now()
        source["fetched_at"] = source["fetched_at"] or utc_now()
        try:
            self._url_shape(source["url"])
            if not isinstance(source["content"], str):
                raise SourceError("來源 content 必須是文字")
            if source["legal_basis"] not in self.LEGAL_BASES:
                raise SourceError("來源缺少合法公開或使用者授權的取得依據")
            if source["source_type"] not in self.SOURCE_TYPES:
                raise SourceError("未知來源類型")
            if source["evidence_tier"] not in self.EVIDENCE_TIERS:
                raise SourceError("未知證據等級")
            if source.get("suspected_nonpublic"):
                raise SourceError("疑似未公開資訊不納入研究或交易證據")
            if source.get("extraction_status") in {"unavailable", "error"}:
                raise SourceError(str(source.get("error") or "來源先前標記為無法取得"))
            if supplied_content:
                if len(source["content"].encode("utf-8")) > self.max_bytes:
                    raise SourceError("輸入來源內容超過預算")
                body = source["content"]
            else:
                if source.get("path"):
                    raw, content_type = self._file(source["path"])
                else:
                    raw, content_type, final_url = self.fetch(source["url"])
                    source["resolved_url"] = final_url
                if "pdf" in content_type.lower() or "octet-stream" in content_type.lower():
                    raise SourceError("此來源需額外擷取器；請匯入有授權且包含日期的文字版本")
                source["fetched_at"] = utc_now()
                body = raw.decode("utf-8", errors="replace")
                if "html" in content_type.lower() or re.search(r"<html\b", body[:500], re.I):
                    parser = _DocumentParser()
                    parser.feed(body)
                    body = "\n".join(parser.parts)
                    if source["title"] == "未提供標題" and parser.title:
                        source["title"] = " ".join(parser.title)
                    source["published_at"] = source["published_at"] or parser.published_at
            if not body.strip():
                raise SourceError("來源擷取為空")
            if self.config.get("live_as_of"):
                # A live run's evidence cutoff is observed after fetching,
                # never manufactured in the future. Historical runs stay strict.
                cutoff = timestamp(utc_now())
            if source["published_at"] and not source["content_as_of"]:
                source["content_as_of"] = source["published_at"]
                source["content_time_basis"] = "original_publication_proxy"
            for field in ("published_at", "content_as_of", "fetched_at"):
                if source[field] is None:
                    raise SourceError(f"缺少可確認的 {field}，不可假造原始發布或內容時間")
                observed = timestamp(source[field])
                if observed > cutoff:
                    raise SourceError(f"lookahead：{field} 晚於研究 as_of")
            if timestamp(source["fetched_at"]) < max(timestamp(source["published_at"]), timestamp(source["content_as_of"])):
                raise SourceError("抓取時間早於內容時間")
            source["raw_content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
            source["content"] = body[:self.max_chars]
            digest = content_hash(source["content"])
            if manifest.get("content_hash") and manifest["content_hash"] != digest:
                raise SourceError("來源宣告的 Content Hash 與保存內容不一致")
            source["content_hash"] = digest
            source["truncated"] = len(body) > self.max_chars
            source["archive_mode"] = "bounded_text_extract"
            source["extraction_status"] = "extracted"
            source["error"] = None
        except (SourceError, requests.RequestException, OSError, UnicodeError, TypeError) as exc:
            # Never archive an unknown or disallowed document body accidentally.
            source["content"] = ""
            source["content_hash"] = hashlib.sha256(b"").hexdigest()
            source["extraction_status"] = "unavailable"
            source["error"] = str(exc) if isinstance(exc, SourceError) else type(exc).__name__
            if not supplied_content:
                source["fetched_at"] = utc_now()
            try:
                timestamp(source["fetched_at"])
            except SourceError:
                source["reported_fetched_at"] = source["fetched_at"]
                source["fetched_at"] = utc_now()
            for field in ("published_at", "content_as_of"):
                if source[field] is not None:
                    try:
                        if timestamp(source[field]) > timestamp(source["fetched_at"]):
                            raise SourceError("來源宣告的時序不成立")
                    except SourceError:
                        source["reported_" + field] = source[field]
                        source[field] = None
        return source


class SearchConnector:
    """可配置的合法搜尋 JSON/RSS 端點；結果只是待擷取的候選 URL。

    不預設任何需要登入/付費的搜尋服務。template 需含 {query}，JSON
    接受 {results:[{url,title,publisher,published_at,...}]} 或同型 list。
    XML RSS 的 link/title 亦可。搜尋摘要不會充當來源正文。
    """

    def __init__(self, connector: SourceConnector, template: str | None = None):
        self.connector = connector
        self.template = template

    def search(self, query: str, limit: int = 3) -> tuple[list[dict], str | None]:
        if not self.template or "{query}" not in self.template:
            return [], "search_unavailable：未設定含 {query} 的授權搜尋端點"
        try:
            raw, _, _ = self.connector.fetch(self.template.replace("{query}", quote(query, safe="")))
            text = raw.decode("utf-8", errors="replace")
            if text.lstrip().startswith(("{", "[")):
                payload = json.loads(text)
                rows = payload.get("results", []) if isinstance(payload, dict) else payload
            else:
                root = ET.fromstring(text)
                rows = [{"url": item.findtext("link"), "title": item.findtext("title")}
                        for item in root.findall(".//item")]
            results = []
            if not isinstance(rows, list):
                return [], "search_unavailable：搜尋結果必須是 JSON list"
            for row in rows[:max(0, min(limit, 10))]:
                if not isinstance(row, dict):
                    continue
                url = row.get("url") or row.get("link")
                if not url or urlparse(url).scheme != "https":
                    continue
                # Keep search-result publication time only if the endpoint
                # explicitly supplies the original document's timestamp.
                results.append({
                    "url": url, "title": row.get("title") or "搜尋待驗證來源",
                    "publisher": row.get("publisher") or urlparse(url).hostname,
                    "source_type": row.get("source_type", "public_discussion"),
                    "evidence_tier": row.get("evidence_tier", "inference"),
                    "legal_basis": row.get("legal_basis", "public"),
                    "published_at": row.get("published_at"),
                    "content_as_of": row.get("content_as_of"),
                    "discovered_by_query": query,
                })
            return results, None if results else "search_unavailable：搜尋端點沒有可擷取的公開來源"
        except (SourceError, requests.RequestException, ValueError, ET.ParseError, AttributeError) as exc:
            return [], f"search_unavailable：{type(exc).__name__}"
