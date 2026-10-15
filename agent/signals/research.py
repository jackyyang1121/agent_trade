from __future__ import annotations

from agent.signals.common import finite, grade, timestamp


def research_signal(thesis: dict, as_of: str) -> dict:
    """Expose evidence intermediates, not an LLM-implied success probability.

    This adapter requires referenced, available evidence. Source authenticity is
    established by the research archive before this adapter; an explicit failed
    verification blocks the signal. Quant-only decisions need no invented thesis.
    """
    cutoff = timestamp(as_of)
    dated = thesis.get("as_of") or thesis.get("updated_at")
    if not dated or timestamp(dated) > cutoff:
        raise ValueError("research signal is undated or contains future information")
    source_ids = sorted(set(thesis.get("source_ids", [])))
    surprise = thesis.get("fundamental_surprise") or {}
    if not isinstance(surprise, dict):
        surprise = {"description": str(surprise)}
    baseline_type = surprise.get("baseline_type", "unknown")
    verifiable_surprise = bool(surprise.get("source_ids") and baseline_type not in {"unknown", "none", ""}
                               and surprise.get("baseline") is not None and surprise.get("observation") is not None)
    catalysts = thesis.get("catalysts") or []
    proximity = "undated"
    catalyst_days = None
    for catalyst in catalysts:
        expected = catalyst.get("expected_at")
        if not expected or not catalyst.get("source_ids") or catalyst.get("status") in {"cancelled", "delayed", "unknown"}:
            continue
        try:
            date = timestamp(expected)
        except ValueError:
            continue
        # This is a calendar-day bucket, never mislabelled as exchange sessions.
        days = (date.date() - cutoff.date()).days
        if days >= 0 and (catalyst_days is None or days < catalyst_days):
            catalyst_days = days
    if catalyst_days is not None:
        proximity = "near" if catalyst_days <= 7 else "medium" if catalyst_days <= 28 else "long"
    evidence = grade(thesis.get("evidence_strength"))
    recognition_labels = {"unknown": 0.0, "unrecognized": 0.25, "emerging": 0.5,
                          "recognized": 0.75, "crowded": 1.0}
    recognition_raw = thesis.get("market_recognition")
    recognition = recognition_labels.get(str(recognition_raw).lower(), grade(recognition_raw, 0.5))
    novelty = grade(thesis.get("information_novelty", thesis.get("novelty", "medium")), 0.5)
    risk = grade(thesis.get("thesis_risk", thesis.get("downside_risk", "medium")), 0.5)
    status = str(thesis.get("status", "watchlist")).lower()
    broken = status in {"broken", "thesis_broken", "rejected", "invalidated", "expired"}
    eligible = bool(source_ids and thesis.get("sources_verified", True) and not broken)
    if broken:
        stage = "thesis_broken"
    elif recognition >= 0.9 or status in {"crowded", "fully_priced"}:
        stage = "crowded"
    elif evidence >= 0.75 and verifiable_surprise:
        stage = "confirmed_acceleration" if recognition >= 0.5 else "early_validation"
    elif evidence >= 0.5 and verifiable_surprise:
        stage = "early_validation"
    else:
        stage = "early_discovery"
    # An explicit discovery/watchlist state cannot be promoted merely because
    # an LLM used a strong evidence adjective elsewhere in the same record.
    if not broken and status in {"watchlist", "early_discovery"}:
        stage = "early_discovery"
    elif stage == "confirmed_acceleration" and status == "early_validation":
        stage = "early_validation"
    scenarios = []
    for item in thesis.get("scenarios", []):
        if not isinstance(item, dict):
            continue
        scenarios.append({"name": item.get("name", "unknown"), "description": item.get("description", ""),
                          "upside": finite(item.get("upside")), "downside": finite(item.get("downside")),
                          "assumptions": item.get("assumptions", [])})
    return {"ticker": str(thesis.get("ticker", "")), "thesis_id": thesis.get("id"), "as_of": as_of,
            "evidence_as_of": dated, "source_ids": source_ids,
            "source_versions": dict(thesis.get("source_versions", {})),
            "counter_source_ids": sorted(set(thesis.get("counter_source_ids", []))),
            "evidence_strength": evidence, "fundamental_surprise": surprise,
            "surprise_verifiable": verifiable_surprise, "surprise_baseline_type": baseline_type,
            "catalyst_proximity": proximity, "catalyst_calendar_days": catalyst_days,
            "information_novelty": novelty, "market_recognition": recognition,
            "thesis_risk": risk, "stage": stage, "eligible": eligible,
            "adversarial_reviewed": bool(thesis.get("adversarial_reviewed") or thesis.get("counter_source_ids")),
            "scenarios": scenarios, "risks": thesis.get("risks", []), "unknowns": thesis.get("unknowns", []),
            "scale_note": "uncalibrated_evidence_grades_not_probabilities"}
