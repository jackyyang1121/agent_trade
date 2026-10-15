from __future__ import annotations

from agent.signals.common import finite, timestamp


def _scenario_cap(research: dict, config: dict) -> tuple[float, list[str]]:
    base_cap = float(config.get("cap_other", 0.09))
    stage = research.get("stage", "early_discovery")
    caps = {"early_discovery": 0.0, "early_validation": float(config.get("exploratory_cap", 0.02)),
            "confirmed_acceleration": base_cap, "crowded": float(config.get("crowded_cap", 0.03)),
            "thesis_broken": 0.0}
    cap = min(base_cap, caps.get(stage, 0.0))
    notes = [f"evidence gate: {stage}"]
    bear = [abs(value) for scenario in research.get("scenarios", [])
            if (value := finite(scenario.get("downside"))) is not None and value != 0]
    if bear:
        # Loss budget divided by an unweighted worst-case loss range. No event
        # probabilities, expected returns or Kelly fractions are manufactured.
        cap = min(cap, float(config.get("scenario_loss_budget", 0.005)) / max(bear))
        notes.append("sized against worst supplied downside scenario, without probability assumptions")
    else:
        cap = min(cap, float(config.get("exploratory_cap", 0.02)))
        notes.append("missing downside range limits research exposure")
    if not research.get("adversarial_reviewed"):
        cap = min(cap, float(config.get("exploratory_cap", 0.02)))
        notes.append("adversarial review missing; exploratory cap")
    risk = finite(research.get("thesis_risk"), 0.5)
    if risk >= 0.75:
        cap *= 0.5
        notes.append("high thesis risk reduces the evidence cap")
    return max(0.0, cap), notes


def fuse(quant: dict[str, dict], research: list[dict], variant: str = "H1", held: dict | None = None,
         config: dict | None = None) -> dict[str, dict]:
    """Evidence-gated Q0/H1/H2 (and research-only R0) transparent ranking.

    Q0 is independent of research. Hybrids preserve standalone quant eligibility
    and can add early research opportunities; evidence earns conditional priority
    rather than a fixed weighted average. All initial thresholds are heuristic.
    Scores are rankings, never expected returns. Portfolio construction applies
    actual trading costs and common risk limits afterwards.
    """
    if variant not in {"Q0", "H1", "H2", "R0"}:
        raise ValueError(f"unknown signal variant: {variant}")
    held, config = held or {}, dict(config or {})
    if "exploratory_cap" not in config and "max_exploratory_weight" in config:
        config["exploratory_cap"] = config["max_exploratory_weight"]
    if variant == "Q0":
        research = []
    if variant == "R0":
        quant = {}
    if config.get("quant_ranking") == "legacy_baseline":
        # Preserve the prospectus-enabled baseline factor architecture while
        # comparing Q0/H1/H2 on the same ranking scale. Missing fundamentals are
        # already handled by the baseline; a wholly missing baseline score is
        # not imputed into a fresh quant purchase here.
        ordered = sorted((finite(row.get("baseline_score")), ticker) for ticker, row in quant.items()
                         if finite(row.get("baseline_score")) is not None)
        quant = {ticker: dict(row) for ticker, row in quant.items()}
        by_value = {}
        for i, (value, _) in enumerate(ordered):
            by_value.setdefault(value, []).append(i / max(1, len(ordered) - 1))
        for ticker, row in quant.items():
            raw = finite(row.get("baseline_score"))
            row["score"] = sum(by_value[raw]) / len(by_value[raw]) if raw in by_value else 0.0
            row["baseline_available"] = raw is not None
            row["ranking_method"] = "heuristic_default_legacy_baseline_percentile"
    cutoff_raw = config.get("as_of")
    cutoffs = [row.get("as_of") for row in quant.values()] + [row.get("as_of") for row in research]
    if cutoff_raw is None:
        present = [value for value in cutoffs if value]
        if not present:
            raise ValueError("fusion requires an explicit as-of cutoff")
        cutoff_raw = min(present, key=timestamp)
    cutoff = timestamp(cutoff_raw)
    for value in cutoffs:
        if not value or timestamp(value) > cutoff:
            raise ValueError("signal is undated or later than the decision cutoff")
    grouped = {}
    for signal in research:
        ticker = str(signal.get("ticker", ""))
        grouped.setdefault(ticker, []).append(signal)
    result = {}
    for ticker in sorted(set(quant) | set(grouped)):
        q = quant.get(ticker) or {}
        candidates = grouped.get(ticker, [])
        # Conflicting or broken thesis remains visible and blocks automatic
        # research promotion; this is not an LLM majority-vote mechanism.
        broken = any(r.get("stage") == "thesis_broken" for r in candidates)
        r = max(candidates, key=lambda item: (item.get("eligible", False), item.get("evidence_strength", 0)), default=None)
        base = finite(q.get("score"), 0.0)
        q_valid = bool(q.get("valid") and finite(q.get("momentum_20")) is not None
                       and q.get("baseline_available", True))
        quant_eligible = q_valid and base >= float(config.get("quant_min_rank", 0.0))
        cap = float(config.get("cap_2330", 0.22) if ticker == "2330" else config.get("cap_other", 0.09))
        score, eligible = base, quant_eligible
        reasons = ["quant-only prospectus-enabled legacy baseline percentile" if config.get("quant_ranking") == "legacy_baseline"
                   else "quant-only heuristic momentum rank"]
        r_eligible = bool(r and r.get("eligible") and r.get("stage") not in {"early_discovery", "thesis_broken"})
        if variant != "Q0":
            if broken:
                eligible, cap = False, 0.0
                reasons = ["invalidated thesis requires exit/review; automatic addition blocked"]
            elif r_eligible:
                eligible = quant_eligible or r_eligible
                reasons = [f"verified research candidate: {r['stage']}"]
                if r.get("surprise_verifiable"):
                    score += float(config.get("verified_surprise_priority", 0.20))
                    reasons.append("explicit fundamental baseline and new observation")
                if r.get("catalyst_proximity") == "near":
                    score += float(config.get("near_catalyst_priority", 0.10))
                    reasons.append("near-term dated catalyst; timing remains uncertain")
                if finite(q.get("relative_strength_20"), 0.0) > 0:
                    score += float(config.get("confirmation_priority", 0.10))
                    reasons.append("market relative strength confirms research direction")
                if not quant_eligible:
                    cap = min(cap, float(config.get("exploratory_cap", 0.02)))
                    reasons.append("research-first exploratory allocation without price breakout")
                if r.get("market_recognition", 0) >= 0.9:
                    score -= float(config.get("crowding_penalty", 0.15))
                    reasons.append("crowding raises the evidence hurdle")
                if variant == "H2":
                    sized, notes = _scenario_cap(r, config)
                    cap, reasons = min(cap, sized), reasons + notes
                elif r.get("stage") == "early_validation":
                    cap = min(cap, float(config.get("exploratory_cap", 0.02)))
            elif variant == "R0":
                eligible, cap = False, 0.0
                reasons = ["research-only portfolio requires verified non-discovery evidence"]
            elif r:
                reasons.append("research remains watchlist/unverified; no fundamental claim supports this quant decision")
        if variant == "R0" and r_eligible and not broken:
            score = float(r.get("evidence_strength", 0.0))
        if q and q.get("tradable") is False:
            eligible, cap = False, 0.0
            reasons.append("stale or zero-trading quote blocks automatic trading")
        if held.get(ticker, 0) > 0 and eligible:
            reasons.append("existing holding; turnover hurdle applied by portfolio constructor")
        source_ids = set(q.get("source_ids", []))
        source_refs = []
        if variant != "Q0":
            for item in candidates:
                source_ids.update(item.get("source_ids", []))
                source_ids.update(item.get("counter_source_ids", []))
                for sid, version in item.get("source_versions", {}).items():
                    source_refs.append({"source_id": sid, "version": version, "thesis_id": item.get("thesis_id")})
        result[ticker] = {"ticker": ticker, "as_of": cutoff_raw, "variant": variant,
                          "score": finite(score, 0.0), "eligible": bool(eligible and cap > 0),
                          "max_weight": max(0.0, cap), "reason": reasons, "source_ids": sorted(source_ids),
                          "source_refs": source_refs,
                          "thesis_ids": [item.get("thesis_id") for item in candidates if item.get("thesis_id")],
                          "quant": q, "research": candidates, "method": "heuristic_evidence_gated_rules_v1"}
    return result
