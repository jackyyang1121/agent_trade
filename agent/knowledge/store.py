"""SQLite 來源封存、版本化知識圖譜與投資假說記憶。

資料不會因更新而覆蓋。歷史查詢同時限制資訊發布、內容時間與實際取得時間，
不讓稍後取得的舊文章滲入已凍結的研究。信心是研究等級，並非校準後勝率。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .novelty import canonical_url, content_hash, novelty, parse_time, timestamp


SOURCE_FIELDS = {"id", "url", "publisher", "title", "source_type", "published_at", "content_as_of",
                 "fetched_at", "content", "extraction_status", "evidence_tier", "legal_basis"}
ENTITY_TYPES = {"Company", "Technology", "Product", "Component", "Process", "Manufacturing Bottleneck",
                "Equipment", "Material", "Customer", "Supplier", "Industry", "Event", "Source", "Research Thesis"}
RELATIONS = {"DEVELOPS", "SUPPLIES", "USES", "DEPENDS_ON", "COMPETES_WITH", "BENEFITS_FROM",
             "CONSTRAINED_BY", "SOLVES_BOTTLENECK", "EXPOSED_TO", "POTENTIALLY_BENEFITS_FROM"}
EDGE_STATES = {"verified", "hypothesis", "disputed", "refuted"}
TIERS = {"1": "official", "2": "corroborated", "3": "expert", "4": "rumor", "5": "inference"}
THESIS_FIELDS = {"id", "ticker", "claim", "source_ids", "counter_source_ids", "unknowns", "catalysts",
                 "status", "evidence_strength", "market_recognition", "risks", "updated_at", "next_tasks", "scenarios"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _list(value: Any, field: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必須是陣列")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必須是非空文字")
    return value.strip()


class ResearchStore:
    """單一 SQLite 檔案；competition / shadow 可配置不同的檔案路徑。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS source_archive(
          source_id TEXT NOT NULL, version INTEGER NOT NULL, url TEXT NOT NULL,
          content_hash TEXT NOT NULL, published_at TEXT, content_as_of TEXT,
          fetched_at TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(source_id,version));
        CREATE INDEX IF NOT EXISTS source_hash ON source_archive(content_hash);
        CREATE INDEX IF NOT EXISTS source_time ON source_archive(fetched_at);
        CREATE TABLE IF NOT EXISTS entities(
          id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entity_versions(
          entity_id TEXT NOT NULL, version INTEGER NOT NULL, known_at TEXT NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY(entity_id,version));
        CREATE TABLE IF NOT EXISTS edges(
          id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES entities(id),
          target_id TEXT NOT NULL REFERENCES entities(id), relation TEXT NOT NULL,
          UNIQUE(source_id,target_id,relation));
        CREATE TABLE IF NOT EXISTS edge_versions(
          edge_id INTEGER NOT NULL REFERENCES edges(id), version INTEGER NOT NULL,
          known_at TEXT NOT NULL, effective_from TEXT NOT NULL, effective_to TEXT,
          status TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(edge_id,version));
        CREATE INDEX IF NOT EXISTS edge_times ON edge_versions(known_at,effective_from);
        CREATE TABLE IF NOT EXISTS thesis_versions(
          thesis_id TEXT NOT NULL, version INTEGER NOT NULL, known_at TEXT NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY(thesis_id,version));
        CREATE TABLE IF NOT EXISTS evidence_links(
          object_type TEXT NOT NULL, object_id TEXT NOT NULL, version INTEGER NOT NULL,
          source_id TEXT NOT NULL, source_version INTEGER NOT NULL, polarity TEXT NOT NULL,
          PRIMARY KEY(object_type,object_id,version,source_id,polarity),
          FOREIGN KEY(source_id,source_version) REFERENCES source_archive(source_id,version));
        CREATE TABLE IF NOT EXISTS research_tasks(
          id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_runs(
          id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT OR IGNORE INTO metadata VALUES('schema_version','1');
        """)
        try:
            self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS research_fts USING fts5(kind UNINDEXED, object_id UNINDEXED, version UNINDEXED, body)")
            self.fts_enabled = True
        except sqlite3.OperationalError:
            # 某些 Python 發行版未編譯 FTS5，保留 LIKE 檢索而明確揭露狀態。
            self.fts_enabled = False
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ResearchStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _index(self, kind: str, key: str, version: int, body: str) -> None:
        if self.fts_enabled:
            self.connection.execute("INSERT INTO research_fts(kind,object_id,version,body) VALUES(?,?,?,?)", (kind, key, version, body))

    def put_source(self, source: dict) -> dict:
        """保存原文/合法摘要、驗證時間與授權欄位，無內容變更時保持冪等。"""
        missing = SOURCE_FIELDS - source.keys()
        if missing:
            raise ValueError("來源缺少欄位：" + ", ".join(sorted(missing)))
        item = dict(source)
        for field in ("id", "publisher", "title", "source_type", "legal_basis"):
            item[field] = _required_text(item[field], field)
        item["url"] = canonical_url(item["url"])
        if not isinstance(item["content"], str):
            raise ValueError("content 必須是文字")
        item["extraction_status"] = str(item["extraction_status"]).lower()
        if item["extraction_status"] not in {"extracted", "summary", "available", "success", "unavailable", "error", "pending"}:
            raise ValueError("不支援的 extraction_status")
        available = item["extraction_status"] not in {"unavailable", "error", "pending"}
        if available and not item["content"].strip():
            raise ValueError("可用來源必須保存原文或合法摘要")
        tier = str(item["evidence_tier"]).lower()
        item["evidence_tier"] = TIERS.get(tier, tier)
        if item["evidence_tier"] not in {"official", "corroborated", "expert", "rumor", "inference"}:
            raise ValueError("evidence_tier 必須區分正式確認、交叉佐證、專家、傳聞或推論")
        if item["legal_basis"].lower() in {"unknown", "illegal", "restricted", "unauthorized"}:
            if available or item["content"].strip():
                raise ValueError("來源沒有可用的合法保存依據；僅可保存不可用狀態與空內容")
        item["fetched_at"] = timestamp(_required_text(item["fetched_at"], "fetched_at"))
        for field in ("published_at", "content_as_of"):
            if item[field] is None and not available:
                continue
            item[field] = timestamp(item[field]) if item[field] is not None else self._missing_time(field)
            if item[field] > item["fetched_at"]:
                raise ValueError(f"{field} 晚於實際取得時間")
        digest = content_hash(item["content"])
        if item.get("content_hash") and item["content_hash"] != digest:
            raise ValueError("來源內容與宣告的 SHA-256 不一致")
        item["content_hash"] = digest
        prior = self.get_source(item["id"])
        if prior and prior["url"] != item["url"]:
            raise ValueError("同一 Source ID 不可改綁不同原始 URL")
        if prior and item["fetched_at"] < prior["fetched_at"]:
            raise ValueError("不能用較早的取得時間回填既有來源版本")
        ignored = {"version", "novelty", "event_id", "delta", "duplicate_of", "fetched_at"}
        if prior and {k: v for k, v in item.items() if k not in ignored} == {k: v for k, v in prior.items() if k not in ignored}:
            return prior
        previous = self.list_sources(as_of=item["fetched_at"])
        for ref in item.get("contradicts_source_ids", []):
            if not self.get_source(ref, as_of=item["fetched_at"]):
                raise ValueError(f"反對來源不存在或尚不可得：{ref}")
        item.update(novelty(item, previous))
        item["version"] = (prior["version"] + 1) if prior else 1
        with self.connection:
            self.connection.execute("INSERT INTO source_archive VALUES(?,?,?,?,?,?,?,?,?)", (
                item["id"], item["version"], item["url"], digest, item["published_at"], item["content_as_of"],
                item["fetched_at"], item["event_id"], _json(item)))
            self._index("source", item["id"], item["version"], item["title"] + "\n" + item["content"])
        return item

    @staticmethod
    def _missing_time(field: str):
        raise ValueError(f"可用來源缺少 {field}")

    def get_source(self, source_id: str, as_of: str | None = None) -> dict | None:
        query = "SELECT payload FROM source_archive WHERE source_id=?"
        params = [source_id]
        if as_of:
            cutoff = parse_time(as_of, end_of_day=True).isoformat(timespec="microseconds")
            query += " AND fetched_at<=? AND (published_at IS NULL OR published_at<=?) AND (content_as_of IS NULL OR content_as_of<=?)"
            params.extend((cutoff, cutoff, cutoff))
        row = self.connection.execute(query + " ORDER BY version DESC LIMIT 1", params).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_sources(self, as_of: str | None = None) -> list[dict]:
        params: tuple = ()
        where = ""
        if as_of:
            cutoff = parse_time(as_of, end_of_day=True).isoformat(timespec="microseconds")
            where = "WHERE fetched_at<=? AND (published_at IS NULL OR published_at<=?) AND (content_as_of IS NULL OR content_as_of<=?)"
            params = (cutoff, cutoff, cutoff)
        rows = self.connection.execute(f"SELECT payload FROM (SELECT payload,source_id,ROW_NUMBER() OVER(PARTITION BY source_id ORDER BY version DESC) AS rank FROM source_archive {where}) WHERE rank=1 ORDER BY source_id", params)
        return [json.loads(row["payload"]) for row in rows]

    def source_history(self, source_id: str) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.connection.execute(
            "SELECT payload FROM source_archive WHERE source_id=? ORDER BY version", (source_id,))]

    def get_source_version(self, source_id: str, version: int, as_of: str | None = None) -> dict | None:
        """取回決策實際引用的來源版本，絕不以後來的修訂內容替代。"""
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("來源版本必須為正整數")
        row = self.connection.execute("SELECT * FROM source_archive WHERE source_id=? AND version=?", (source_id, version)).fetchone()
        if row is None:
            return None
        if as_of:
            cutoff = parse_time(as_of, end_of_day=True).isoformat(timespec="microseconds")
            if any(row[field] and row[field] > cutoff for field in ("fetched_at", "published_at", "content_as_of")):
                return None
        return json.loads(row["payload"])

    def resolve_thesis_sources(self, theses: list[dict] | dict, as_of: str | None = None) -> list[dict]:
        """重建假說固定的正反證據；缺版本或斷鏈直接拒絕，不默默回退最新版。"""
        rows = [theses] if isinstance(theses, dict) else theses
        if not isinstance(rows, list):
            raise ValueError("theses 必須為假說 dict 或 list")
        resolved = {}
        for thesis in rows:
            versions = thesis.get("source_versions", {})
            if not isinstance(versions, dict):
                raise ValueError("Thesis source_versions 必須是版本對照表")
            cutoff = as_of or thesis.get("updated_at")
            if as_of and thesis.get("updated_at") and parse_time(thesis["updated_at"]) > parse_time(as_of, end_of_day=True):
                raise ValueError("假說晚於指定查詢時間")
            refs = set(thesis.get("source_ids", [])) | set(thesis.get("counter_source_ids", []))
            for source_id in sorted(refs):
                if source_id not in versions:
                    raise ValueError(f"Thesis 引用缺少固定來源版本：{source_id}")
                version = versions[source_id]
                source = self.get_source_version(source_id, version, cutoff)
                if source is None:
                    raise ValueError(f"Thesis 引用版本不存在或在當時尚不可得：{source_id}@{version}")
                resolved[(source_id, version)] = source
        return [resolved[key] for key in sorted(resolved)]

    def upsert_entity(self, entity: dict) -> str:
        item = dict(entity)
        key = _required_text(item.get("id"), "entity.id")
        name = _required_text(item.get("name"), "entity.name")
        raw_type = _required_text(item.get("type"), "entity.type")
        normalized = raw_type.replace("_", "").replace(" ", "").lower()
        kind = next((kind for kind in ENTITY_TYPES if kind.replace(" ", "").lower() == normalized), None)
        if not kind:
            raise ValueError(f"不支援的 Entity 類型：{raw_type}")
        item.update(id=key, name=name, type=kind)
        row = self.connection.execute("SELECT version,payload,known_at FROM entity_versions WHERE entity_id=? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
        if row and json.loads(row["payload"]) == item:
            return key
        version = row["version"] + 1 if row else 1
        known = timestamp(item.get("updated_at"))
        if row and known < row["known_at"]:
            raise ValueError("不能回填早於既有版本的 Entity")
        with self.connection:
            self.connection.execute("INSERT INTO entities VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET type=excluded.type,name=excluded.name,payload=excluded.payload", (key, kind, name, _json(item)))
            self.connection.execute("INSERT INTO entity_versions VALUES(?,?,?,?)", (key, version, known, _json(item)))
            self._index("entity", key, version, name + "\n" + _json(item))
        return key

    def list_entities(self, as_of: str | None = None) -> list[dict]:
        query = "SELECT * FROM entity_versions"
        params = ()
        if as_of:
            query += " WHERE known_at<=?"
            params = (parse_time(as_of, end_of_day=True).isoformat(timespec="microseconds"),)
        latest = {}
        for row in self.connection.execute(query + " ORDER BY version", params):
            latest[row["entity_id"]] = json.loads(row["payload"])
        return list(latest.values())

    def _validate_evidence(self, item: dict, as_of: str, *, verified: bool = False) -> dict[str, list[dict]]:
        result = {}
        pinned = item.get("source_versions")
        if pinned is not None and not isinstance(pinned, dict):
            raise ValueError("source_versions 必須是來源版本對照表")
        for field in ("source_ids", "counter_source_ids"):
            refs = _list(item.get(field, []), field)
            for ref in refs:
                _required_text(ref, field)
            if len(refs) != len(set(refs)):
                raise ValueError(f"{field} 不可重複引用同一來源")
            result[field] = []
            for ref in refs:
                if pinned is not None:
                    if ref not in pinned:
                        raise ValueError(f"引用來源缺少明確版本：{ref}")
                    source = self.get_source_version(ref, pinned[ref], as_of=as_of)
                else:
                    source = self.get_source(ref, as_of=as_of)
                if not source:
                    raise ValueError(f"引用來源不存在或在決策時間尚不可得：{ref}")
                if source["extraction_status"] in {"unavailable", "error", "pending"}:
                    raise ValueError(f"不可用來源不能作為研究證據：{ref}")
                result[field].append(source)
        if verified and (not result["source_ids"] or any(source["evidence_tier"] not in {"official", "corroborated"} for source in result["source_ids"])):
            raise ValueError("已驗證關係必須有正式或交叉佐證來源，推測不能升格為事實")
        return result

    def _link_evidence(self, kind: str, key: str, version: int, evidence: dict) -> None:
        for field, sources in evidence.items():
            for source in sources:
                self.connection.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?,?)", (
                    kind, key, version, source["id"], source["version"], "support" if field == "source_ids" else "counter"))

    def upsert_edge(self, edge: dict) -> int:
        item = dict(edge)
        for field in ("source_id", "target_id", "relation"):
            item[field] = _required_text(item.get(field), field)
        item["relation"] = item["relation"].upper()
        if item["relation"] not in RELATIONS:
            raise ValueError("不支援的知識關係")
        for key in (item["source_id"], item["target_id"]):
            if not self.connection.execute("SELECT 1 FROM entities WHERE id=?", (key,)).fetchone():
                raise ValueError(f"Edge 的 Entity 不存在：{key}")
        state = str(item.get("status", "hypothesis")).lower()
        if state not in EDGE_STATES:
            raise ValueError("不支援的 Edge 狀態")
        item["status"] = state
        item["source_ids"] = item.get("source_ids", [])
        item["counter_source_ids"] = item.get("counter_source_ids", [])
        known = timestamp(item.get("updated_at") or item.get("last_verified") or item.get("first_seen"))
        evidence = self._validate_evidence(item, known, verified=state == "verified")
        item["source_versions"] = {source["id"]: source["version"] for refs in evidence.values() for source in refs}
        item["first_seen"] = timestamp(item.get("first_seen") or known)
        item["last_verified"] = timestamp(item["last_verified"]) if item.get("last_verified") else (known if state == "verified" else None)
        if item["first_seen"] > known or (item["last_verified"] and item["last_verified"] > known):
            raise ValueError("發現或驗證時間不能晚於關係更新時間")
        item["effective_from"] = timestamp(item.get("effective_from") or known)
        item["effective_to"] = timestamp(item["effective_to"]) if item.get("effective_to") else None
        if item["effective_to"] and item["effective_to"] <= item["effective_from"]:
            raise ValueError("關係失效時間必須晚於生效時間")
        confidence = item.get("confidence", "low")
        if isinstance(confidence, bool) or (isinstance(confidence, (int, float)) and not 0 <= confidence <= 1):
            raise ValueError("confidence 數值需介於 0 與 1；不代表成功機率")
        if not isinstance(confidence, (int, float)) and (not isinstance(confidence, str) or confidence not in {"low", "medium", "high", "unknown"}):
            raise ValueError("confidence 必須為信心等級或 0–1 研究評分")
        item["confidence"] = confidence
        item["confidence_interpretation"] = "研究證據信心，非校準後成功機率"
        row = self.connection.execute("SELECT id FROM edges WHERE source_id=? AND target_id=? AND relation=?", (item["source_id"], item["target_id"], item["relation"])).fetchone()
        edge_id = row["id"] if row else None
        previous = None
        if edge_id:
            previous = self.connection.execute("SELECT * FROM edge_versions WHERE edge_id=? ORDER BY version DESC LIMIT 1", (edge_id,)).fetchone()
            prior = json.loads(previous["payload"])
            item["first_seen"] = prior["first_seen"]
            if "effective_from" not in edge:
                item["effective_from"] = prior["effective_from"]
            if "last_verified" not in edge and state != "verified":
                item["last_verified"] = prior["last_verified"]
            # 自動填入的時間不造成每日無意義新版本。
            if all(prior.get(k) == v for k, v in item.items() if k not in {"id", "version", "updated_at"}):
                return edge_id
            if known < previous["known_at"]:
                raise ValueError("不能回填早於既有版本的關係更新")
        version = previous["version"] + 1 if previous else 1
        with self.connection:
            if edge_id is None:
                cursor = self.connection.execute("INSERT INTO edges(source_id,target_id,relation) VALUES(?,?,?)", (item["source_id"], item["target_id"], item["relation"]))
                edge_id = cursor.lastrowid
            item.update(id=edge_id, version=version, updated_at=known)
            self.connection.execute("INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?)", (edge_id, version, known, item["effective_from"], item["effective_to"], state, _json(item)))
            self._link_evidence("edge", str(edge_id), version, evidence)
        return edge_id

    def query_edges(self, source_id: str | None = None, target_id: str | None = None, relation: str | None = None,
                    status: str | None = None, as_of: str | None = None, since: str | None = None) -> list[dict]:
        query = "SELECT e.*,v.version,v.known_at,v.effective_from,v.effective_to,v.status,v.payload FROM edges e JOIN edge_versions v ON v.edge_id=e.id"
        conditions, params = [], []
        for field, value in (("e.source_id", source_id), ("e.target_id", target_id), ("e.relation", relation.upper() if relation else None)):
            if value is not None:
                conditions.append(field + "=?")
                params.append(value)
        if as_of:
            cutoff = parse_time(as_of, end_of_day=True).isoformat(timespec="microseconds")
            conditions.append("v.known_at<=?")
            params.append(cutoff)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        latest = {}
        for row in self.connection.execute(query + " ORDER BY v.version", params):
            latest[row["id"]] = json.loads(row["payload"])
        result = list(latest.values())
        if as_of:
            result = [item for item in result if item["effective_from"] <= cutoff and (not item["effective_to"] or cutoff < item["effective_to"])]
        if since:
            start = parse_time(since).isoformat(timespec="microseconds")
            result = [item for item in result if item["updated_at"] >= start]
        if status:
            result = [item for item in result if item["status"] == status.lower()]
        return result

    def edge_history(self, edge_id: int) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.connection.execute(
            "SELECT payload FROM edge_versions WHERE edge_id=? ORDER BY version", (edge_id,))]

    def eligible_companies(self, tickers: list[str], technology_id: str | None = None,
                           as_of: str | None = None) -> list[dict]:
        """圖譜可含全球企業，交易候選明確與呼叫方提供的合格股票池取交集。"""
        universe = set(tickers)
        related = None
        if technology_id:
            related = {edge["source_id"] for edge in self.query_edges(target_id=technology_id, as_of=as_of)
                       if edge["relation"] in {"BENEFITS_FROM", "POTENTIALLY_BENEFITS_FROM"}
                       and edge["status"] in {"verified", "hypothesis"}}
        return [item for item in self.list_entities(as_of) if item["type"] == "Company"
                and item.get("ticker") in universe and (related is None or item["id"] in related)]

    def upsert_thesis(self, thesis: dict) -> int:
        missing = THESIS_FIELDS - thesis.keys()
        if missing:
            raise ValueError("Thesis 缺少欄位：" + ", ".join(sorted(missing)))
        item = dict(thesis)
        for field in ("id", "ticker", "claim", "status"):
            item[field] = _required_text(item[field], field)
        for field in ("unknowns", "catalysts", "risks", "next_tasks", "scenarios"):
            _list(item[field], field)
        item["updated_at"] = timestamp(item["updated_at"])
        evidence = self._validate_evidence(item, item["updated_at"], verified=item["status"].lower() in {"verified", "confirmed"})
        item["source_versions"] = {source["id"]: source["version"] for refs in evidence.values() for source in refs}
        if not item["source_ids"] and item["status"].lower() not in {"unverified", "hypothesis", "queued", "research_queue", "pending", "watchlist"}:
            raise ValueError("沒有來源的結論只能保留在未驗證研究佇列")
        if item["source_ids"] and all(source["evidence_tier"] in {"rumor", "inference"} for source in evidence["source_ids"]):
            if item["status"].lower() not in {"unverified", "hypothesis", "queued", "research_queue", "pending", "watchlist"}:
                raise ValueError("僅有傳聞或推論不能形成已確認投資論點")
        row = self.connection.execute("SELECT * FROM thesis_versions WHERE thesis_id=? ORDER BY version DESC LIMIT 1", (item["id"],)).fetchone()
        if row:
            prior = json.loads(row["payload"])
            if item.get("discovered_at") and timestamp(item["discovered_at"]) != prior["discovered_at"]:
                raise ValueError("Thesis 最初發現時間不可改寫")
            item["discovered_at"] = prior["discovered_at"]
            if {k: v for k, v in item.items() if k not in {"version", "change_log"}} == {k: v for k, v in prior.items() if k not in {"version", "change_log"}}:
                return row["version"]
            if item["updated_at"] < row["known_at"]:
                raise ValueError("不能以較早時間覆寫既有 Thesis")
            changed = [key for key in sorted(item) if key not in {"updated_at", "version", "change_log"} and item[key] != prior.get(key)]
            change_log = prior.get("change_log", []) + [{"at": item["updated_at"], "fields": changed,
                                                         "from_status": prior["status"], "to_status": item["status"]}]
        else:
            item.setdefault("discovered_at", item["updated_at"])
            item["discovered_at"] = timestamp(item["discovered_at"])
            if item["discovered_at"] > item["updated_at"]:
                raise ValueError("Thesis 發現日期不能晚於更新時間")
            change_log = [{"at": item["updated_at"], "fields": ["created"], "from_status": None, "to_status": item["status"]}]
        version = row["version"] + 1 if row else 1
        item.update(version=version, change_log=change_log)
        with self.connection:
            self.connection.execute("INSERT INTO thesis_versions VALUES(?,?,?,?)", (item["id"], version, item["updated_at"], _json(item)))
            self._link_evidence("thesis", item["id"], version, evidence)
            self._index("thesis", item["id"], version, item["claim"] + "\n" + _json(item))
        return version

    def list_theses(self, as_of: str | None = None) -> list[dict]:
        query = "SELECT * FROM thesis_versions"
        params = ()
        if as_of:
            query += " WHERE known_at<=?"
            params = (parse_time(as_of, end_of_day=True).isoformat(timespec="microseconds"),)
        latest = {}
        for row in self.connection.execute(query + " ORDER BY version", params):
            latest[row["thesis_id"]] = json.loads(row["payload"])
        return list(latest.values())

    def get_thesis(self, thesis_id: str, as_of: str | None = None) -> dict | None:
        return next((item for item in self.list_theses(as_of) if item["id"] == thesis_id), None)

    def thesis_history(self, thesis_id: str) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.connection.execute("SELECT payload FROM thesis_versions WHERE thesis_id=? ORDER BY version", (thesis_id,))]

    def enqueue_task(self, task: dict) -> str:
        item = dict(task)
        # 相同研究目的產生穩定 ID，重跑不會重複排程。
        item.setdefault("id", "task-" + content_hash(_json({k: v for k, v in item.items() if k not in {"created_at", "updated_at", "status"}}))[:20])
        key = _required_text(item["id"], "task.id")
        item.setdefault("status", "pending")
        prior = self.connection.execute("SELECT payload FROM research_tasks WHERE id=?", (key,)).fetchone()
        if prior:
            old = json.loads(prior["payload"])
            item.setdefault("created_at", old["created_at"])
            # 已完成任務不因每天重新發現而回到 pending。
            if old["status"] == "completed" and task.get("status", "pending") == "pending":
                return key
            item = {**old, **item}
        item["created_at"] = timestamp(item.get("created_at"))
        item["updated_at"] = timestamp(item.get("updated_at") or item["created_at"])
        if item["updated_at"] < item["created_at"]:
            raise ValueError("研究任務更新時間不能早於建立時間")
        with self.connection:
            self.connection.execute("INSERT INTO research_tasks VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,payload=excluded.payload", (key, item["status"], item["created_at"], item["updated_at"], _json(item)))
        return key

    def tasks(self, status: str | None = None) -> list[dict]:
        query = "SELECT payload FROM research_tasks"
        params = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        return [json.loads(row["payload"]) for row in self.connection.execute(query + " ORDER BY created_at,id", params)]

    def record_model_run(self, run: dict) -> None:
        item = dict(run)
        key = _required_text(item.get("id") or item.get("run_id"), "model_run.id")
        with self.connection:
            # 同一執行 ID 不允許改寫原始模型紀錄。
            existing = self.connection.execute("SELECT payload FROM model_runs WHERE id=?", (key,)).fetchone()
            if existing and json.loads(existing["payload"]) != item:
                raise ValueError("模型執行紀錄不可覆寫；請使用新的 Run ID")
            self.connection.execute("INSERT OR IGNORE INTO model_runs VALUES(?,?,?)", (key, timestamp(), _json(item)))

    def model_runs(self) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.connection.execute("SELECT payload FROM model_runs ORDER BY recorded_at,id")]

    def search(self, query: str, as_of: str | None = None, limit: int = 50) -> list[dict]:
        """FTS5 與中文子字串檢索並用；回傳的內容一律以指定時點重建。"""
        text = _required_text(query, "query")
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必須介於 1 與 1000")
        matched = set()
        if self.fts_enabled:
            # 將查詢作為字面詞組，避免使用者字串成為 FTS 查詢運算式。
            phrase = '"' + text.replace('"', '""') + '"'
            for row in self.connection.execute("SELECT kind,object_id FROM research_fts WHERE research_fts MATCH ? LIMIT ?", (phrase, limit * 10)):
                matched.add((row["kind"], row["object_id"]))
        result = []
        for kind, rows in (("source", self.list_sources(as_of)), ("entity", self.list_entities(as_of)), ("thesis", self.list_theses(as_of))):
            for item in rows:
                # 命中舊版但當時最新內容已刪除該文字時，不將舊資訊偽裝成目前內容。
                searchable = _json(item).casefold()
                if text.casefold() in searchable:
                    result.append({"kind": kind, "id": item["id"], "record": item, "method": "fts5" if (kind, item["id"]) in matched else "substring"})
        return result[:limit]

    def summary(self) -> dict:
        counts = {"sources": len(self.list_sources()), "entities": self.connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                  "edges": self.connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                  "theses": len(self.list_theses()), "tasks": len(self.tasks()), "model_runs": len(self.model_runs())}
        counts.update(source_versions=self.connection.execute("SELECT COUNT(*) FROM source_archive").fetchone()[0],
                      thesis_versions=self.connection.execute("SELECT COUNT(*) FROM thesis_versions").fetchone()[0],
                      independent_events=len({item["event_id"] for item in self.list_sources()}),
                      fts_enabled=self.fts_enabled, path=self.path, schema_version=1)
        return counts
