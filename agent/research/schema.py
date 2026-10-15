"""模型輸出契約：證據與未確認假設分開，不接收直接交易指令。"""
from __future__ import annotations


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def array(items: dict) -> dict:
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
TEXTS = array(TEXT)
NULL_TEXT = {"type": ["string", "null"]}
NUMBER = {"type": ["number", "null"]}
REFS = array(TEXT)
LEVEL = {"type": "string", "enum": ["unknown", "low", "medium", "high"]}

CATALYST = obj({
    "description": TEXT, "expected_at": NULL_TEXT,
    "status": {"type": "string", "enum": ["confirmed", "tentative", "unknown", "delayed", "cancelled"]},
    "source_ids": REFS,
})
SCENARIO = obj({
    "name": {"type": "string", "enum": ["bull", "base", "bear"]},
    "description": TEXT, "upside": NUMBER, "downside": NUMBER,
    "assumptions": TEXTS, "drivers": TEXTS,
    "evidence_to_upgrade": TEXTS, "invalidation": TEXTS,
})
RISK = obj({
    "description": TEXT, "source_ids": REFS,
    "status": {"type": "string", "enum": ["evidenced", "risk_hypothesis"]},
})
SURPRISE = obj({
    "baseline_type": {"type": "string", "enum": [
        "consensus", "company_guidance", "historical_proxy", "other_proxy", "unknown",
    ]},
    "metric": TEXT, "baseline": NUMBER, "observation": NUMBER,
    "source_ids": REFS, "interpretation": TEXT,
})
THESIS = obj({
    "id": TEXT, "ticker": TEXT, "claim": TEXT,
    "source_ids": REFS, "counter_source_ids": REFS,
    "unknowns": TEXTS, "catalysts": array(CATALYST),
    "status": {"type": "string", "enum": [
        "watchlist", "early_discovery", "early_validation", "confirmed_acceleration", "crowded", "thesis_broken",
    ]},
    "evidence_strength": LEVEL,
    "market_recognition": {"type": "string", "enum": ["unknown", "unrecognized", "emerging", "recognized", "crowded"]},
    "risks": array(RISK), "updated_at": TEXT, "next_tasks": TEXTS,
    "scenarios": array(SCENARIO), "fundamental_surprise": SURPRISE,
    "adversarial_reviewed": {"type": "boolean"},
})
TASK = obj({
    "kind": {"type": "string", "enum": ["search", "fetch", "verify", "reverse_research"]},
    "query": TEXT, "ticker": NULL_TEXT, "url": NULL_TEXT,
    "reason": TEXT, "source_ids": REFS,
})
ENTITY = obj({
    "id": TEXT,
    "type": {"type": "string", "enum": [
        "Company", "Technology", "Product", "Component", "Process", "Manufacturing Bottleneck",
        "Equipment", "Material", "Customer", "Supplier", "Industry", "Event", "Source", "Research Thesis",
    ]},
    "name": TEXT,
})
EDGE = obj({
    "source_id": TEXT, "target_id": TEXT,
    "relation": {"type": "string", "enum": [
        "DEVELOPS", "SUPPLIES", "USES", "DEPENDS_ON", "COMPETES_WITH", "BENEFITS_FROM",
        "CONSTRAINED_BY", "SOLVES_BOTTLENECK", "EXPOSED_TO", "POTENTIALLY_BENEFITS_FROM",
    ]},
    "source_ids": REFS, "counter_source_ids": REFS,
    "status": {"type": "string", "enum": ["verified", "hypothesis", "disputed", "refuted"]},
    "confidence": LEVEL,
})
DISCOVERY = obj({
    "technology": TEXT,
    "steps": array(obj({"claim": TEXT, "source_ids": REFS,
                         "status": {"type": "string", "enum": ["evidenced", "hypothesis"]}})),
    "candidate_tickers": TEXTS,
})
RESEARCH_SCHEMA = obj({
    "theses": array(THESIS), "entities": array(ENTITY), "edges": array(EDGE),
    "tasks": array(TASK), "discovery_chains": array(DISCOVERY), "limitations": TEXTS,
})
