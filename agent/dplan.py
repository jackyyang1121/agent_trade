from __future__ import annotations

import math
from datetime import datetime

from agent.portfolio import Construction, stance_of
from agent.config import active_schema_version
from agent.util import jnum, stamp


PHRASES = {
    "momentum": "價格動能",
    "volume_trend": "成交量變化",
    "quality": "品質與估值",
    "growth": "營收成長",
    "low_vol": "波動與下檔風險",
    "dividend": "股息收益",
}
REGIME_ZH = {"risk_on": "風險偏好", "neutral": "中性震盪", "risk_off": "風險趨避"}
STANCE_ZH = {"aggressive": "積極", "neutral": "中性", "defensive": "保守"}
INTENT_ZH = {"increase": "提高淨曝險", "hold": "淨曝險大致持平", "reduce": "降低淨曝險"}
ACTION_RANK = {"SELL_ALL": 0, "TRIM": 1, "ADD": 2, "BUY": 3}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _action(old: int, target: int) -> str:
    if old <= 0:
        return "BUY"
    if target <= 0:
        return "SELL_ALL"
    if target > old:
        return "ADD"
    return "TRIM"


def _fmt_pct(value: float | None, digits: int = 2) -> str | None:
    if value is None:
        return None
    return f"{value:.{digits}f}%"


def render_dplan(
    prospectus: dict,
    market: dict,
    names: dict[str, str],
    markets: dict[str, str],
    prices: dict[str, float],
    metrics: dict[str, dict],
    scores: dict[str, float],
    components: dict[str, dict],
    enabled: list[str],
    held: dict[str, int],
    plan: Construction,
    regime: str,
    counter: str | None,
    sources: list[dict],
    team_id: str,
    trade_date: str,
    model_provider: str,
    model_version: str,
    code_version: str,
    started: datetime,
    finished: datetime,
    http_calls: int,
    band: float,
    research_evidence: dict[str, list[dict]] | None = None,
    decision_context: dict[str, dict] | None = None,
) -> dict:
    research_evidence = research_evidence or {}
    decision_context = decision_context or {}
    stance = stance_of(regime, plan.intent)
    source_ids: dict[str, str] = {}
    source_rows = []
    for source in sources:
        sid = f"S{len(source_rows) + 1}"
        source_ids[source["key"]] = sid
        row = {
            "source_id": sid,
            "authority": source["authority"],
            "name": source["name"],
            "url": source["url"],
            "content_as_of": source["content_as_of"],
        }
        if source.get("fetched_at"):
            row["fetched_at"] = source["fetched_at"]
        source_rows.append(row)

    observations = []

    def add_obs(source_keys: list[str], statement: str, values: dict, topic: str) -> str:
        oid = f"O{len(observations) + 1}"
        clean = {}
        for key, value in values.items():
            if value is None:
                continue
            number = float(value)
            if not math.isfinite(number):
                continue
            clean[key] = jnum(number)
        if not clean:
            raise ValueError("事實缺少可寫入的數字")
        observations.append(
            {
                "obs_id": oid,
                "source_ref": [source_ids[key] for key in source_keys],
                "topic": topic,
                "statement": _clip(statement, 500),
                "values": clean,
            }
        )
        return oid

    taiex_bits = [f"發行量加權股價指數收 {market['taiex_close']:.2f}"]
    taiex_values = {"TAIEX": market["taiex_close"]}
    if market.get("taiex_chg_pct") is not None:
        taiex_bits.append(f"當日漲跌 {market['taiex_chg_pct']:.2f}%")
        taiex_values["TAIEX_chg_pct"] = market["taiex_chg_pct"]
    if market.get("taiex_ret_5") is not None:
        taiex_bits.append(f"近5日報酬 {market['taiex_ret_5'] * 100:.2f}%")
        taiex_values["TAIEX_ret_5d_pct"] = market["taiex_ret_5"] * 100
    if market.get("taiex_ret_20") is not None:
        taiex_bits.append(f"近20日報酬 {market['taiex_ret_20'] * 100:.2f}%")
        taiex_values["TAIEX_ret_20d_pct"] = market["taiex_ret_20"] * 100
    if market.get("semi_chg_pct") is not None:
        taiex_bits.append(f"半導體類指數當日 {market['semi_chg_pct']:.2f}%")
        taiex_values["semi_chg_pct"] = market["semi_chg_pct"]
    obs_taiex = add_obs(["twse"], "，".join(taiex_bits), taiex_values, "tw_market")

    basis = [obs_taiex]
    if market.get("foreign_bn") is not None:
        text = f"三大法人統計：外資買賣超 {market['foreign_bn']:.2f} 億元"
        values = {"foreign_net_twd_bn": market["foreign_bn"]}
        if market.get("trust_bn") is not None:
            text += f"，投信買賣超 {market['trust_bn']:.2f} 億元"
            values["trust_net_twd_bn"] = market["trust_bn"]
        basis.append(add_obs(["bfi"], text, values, "flows"))
    us = market.get("us") or {}
    if us:
        bits = []
        values = {}
        keys = []
        labels = [("SOX", "SOX"), ("NDX", "那斯達克100"), ("SPX", "標普500"), ("TSM", "TSM ADR"), ("UMC", "UMC ADR")]
        for key, label in labels:
            item = us.get(key)
            if not item:
                continue
            bits.append(f"{label} {item['chg_pct']:.2f}%")
            values[f"{key}_chg_pct"] = item["chg_pct"]
            keys.append(item["source_key"])
        if bits:
            basis.append(add_obs(keys, "隔夜美股收盤：" + "、".join(bits), values, "us_overnight"))

    covered = []
    decisions = []
    for ticker, weight in plan.weights.items():
        old = int(held.get(ticker, 0))
        target = next(order["target_shares"] for order in plan.orders if order["ticker"] == ticker)
        action = _action(old, target)
        decisions.append({"ticker": ticker, "action": action, "target_weight": weight, "target_shares": target, "old": old})
    decisions.sort(key=lambda item: (ACTION_RANK[item["action"]], item["ticker"]))
    buy_ids_preview = [f"D{index}" for index, item in enumerate(decisions, start=1) if item["action"] in {"BUY", "ADD"}]

    no_trade = []
    for ticker, shares in sorted(held.items()):
        if int(shares) <= 0 or any(item["ticker"] == ticker for item in decisions):
            continue
        no_trade.append(ticker)

    stock_obs: dict[str, str] = {}
    for ticker in [item["ticker"] for item in decisions] + no_trade:
        metric = dict(metrics.get(ticker) or {})
        metric.setdefault("close", prices[ticker])
        name = names.get(ticker, ticker)
        bits = [f"{name}（{ticker}）收盤 {metric['close']:.2f} 元"]
        values = {"close": metric["close"]}
        if metric.get("ret_60") is not None:
            bits.append(f"近60日報酬 {metric['ret_60'] * 100:.2f}%")
            values["ret_60d_pct"] = metric["ret_60"] * 100
        if metric.get("ret_20") is not None:
            bits.append(f"近20日報酬 {metric['ret_20'] * 100:.2f}%")
            values["ret_20d_pct"] = metric["ret_20"] * 100
        if metric.get("pe"):
            bits.append(f"本益比 {metric['pe']:.2f}")
            values["pe"] = metric["pe"]
        if metric.get("pb"):
            bits.append(f"股價淨值比 {metric['pb']:.2f}")
            values["pb"] = metric["pb"]
        if metric.get("dividend_yield") is not None:
            bits.append(f"殖利率 {metric['dividend_yield']:.2f}%")
            values["dividend_yield_pct"] = metric["dividend_yield"]
        if metric.get("rev_yoy") is not None:
            bits.append(f"最新月營收年增率 {metric['rev_yoy']:.2f}%")
            values["rev_yoy_pct"] = metric["rev_yoy"]
        if metric.get("avg_turnover") is not None:
            bits.append(f"近20日均成交額 {metric['avg_turnover'] / 1e8:.2f} 億元")
            values["avg_turnover_twd"] = metric["avg_turnover"]
        market_name = markets.get(ticker, "twse")
        keys = ["tpex" if market_name == "tpex" else "twse"]
        if metric.get("pe") or metric.get("pb") or metric.get("dividend_yield") is not None:
            if market_name == "twse":
                keys.append("valuation")
        if metric.get("rev_yoy") is not None:
            keys.append("rev_tpex" if market_name == "tpex" else "rev_twse")
        keys = [key for key in keys if key in source_ids]
        if not keys:
            keys = ["twse"]
        stock_obs[ticker] = add_obs(keys, "，".join(bits), values, "stock")

    phrase = "、".join(PHRASES[fid] for fid in enabled if fid in PHRASES)
    theme = _clip(prospectus["theme"], 80).rstrip("。.")
    logic_bits = [
        f"本決策對照公開說明書「{prospectus['name']}」，主題為{theme}",
        f"啟用因子為{phrase}",
        f"加權指數近20日報酬 {_fmt_pct((market.get('taiex_ret_20') or 0) * 100) if market.get('taiex_ret_20') is not None else '資料不足'}",
        f"判定市場為{REGIME_ZH[regime]}，操作姿態{STANCE_ZH[stance]}，並{INTENT_ZH[plan.intent]}",
        f"以昨日收盤價估算，委託後現金比約 {float(plan.cash_after / plan.nav_after):.2%}，落在宣告區間內",
    ]
    inferences = []

    def add_inf(premise: list[str], text: str, counter_text: str | None, conclusion: str) -> str:
        iid = f"I{len(inferences) + 1}"
        inferences.append(
            {
                "inf_id": iid,
                "premise_refs": premise[:10],
                "logic": _clip(text, 1000),
                "conclusion": _clip(conclusion, 300),
                "counter_evidence": _clip(counter_text, 500) if counter_text else None,
            }
        )
        return iid

    decision_rows = []
    inference_of: dict[str, str] = {}

    def evidence_observations(ticker: str) -> list[str]:
        refs = []
        for evidence in research_evidence.get(ticker, [])[:3]:
            key = evidence["key"]
            if key not in source_ids:
                continue
            refs.append(add_obs([key, "tpex" if markets.get(ticker) == "tpex" and "tpex" in source_ids else "twse"],
                                f"來源摘錄（非獲利保證）：{evidence['excerpt']}；{ticker} 定價收盤 {prices[ticker]:.2f} 元",
                                {"close": prices[ticker]}, "research"))
        return refs

    for index, item in enumerate(decisions, start=1):
        ticker = item["ticker"]
        name = names.get(ticker, ticker)
        score = scores.get(ticker, 0.0)
        comps = components.get(ticker) or {}
        comp_text = "、".join(f"{PHRASES.get(fid, fid)} {comps[fid]:.2f}" for fid in enabled if fid in comps)
        funding = ticker in plan.funding_sells and item["action"] in {"TRIM", "SELL_ALL"}
        text = (
            f"對照「{prospectus['name']}」的{phrase}。"
            f"{name}（{ticker}）綜合分數 {score:.2f}（{comp_text}），"
            f"由持股 {item['old']} 股調到目標權重 {item['target_weight']:.2%}。"
        )
        context = decision_context.get(ticker)
        if context:
            reason = context.get("reason", "量化規則決策")
            text = f"對照「{prospectus['name']}」。" + ("；".join(reason) if isinstance(reason, list) else str(reason))
            text += f"；確定性風控後目標權重 {item['target_weight']:.2%}。"
        if funding:
            text += "這筆賣出同時用來騰出現金：它在目前持股裡分數較低，讓位對組合的因子暴露影響較小。"
        if metrics.get(ticker, {}).get("rev_yoy") is None and "growth" in enabled:
            text += "此檔沒有可用的月營收年增率，成長因子以橫斷面平均處理。"
        local_counter = None
        ret20 = (metrics.get(ticker) or {}).get("ret_20")
        if item["action"] in {"BUY", "ADD"} and ret20 is not None and ret20 < 0:
            local_counter = f"近20日報酬 {ret20 * 100:.2f}% 為負，故控制權重、不滿倉單一訊號"
        elif item["action"] in {"TRIM", "SELL_ALL"} and score > 0:
            local_counter = "綜合分數仍非墊底，因此只依目標權重調整，不額外擴大賣超"
        premise = [stock_obs[ticker]]
        if len(basis) > 1:
            premise.append(basis[min(1, len(basis) - 1)])
        # Schema 的 values 必須含數字：引用真實定價，不捏造質性證據的0/1值。
        premise.extend(evidence_observations(ticker))
        risks = [risk.get("description", "") for signal in (context or {}).get("research", [])
                 for risk in signal.get("risks", []) if risk.get("description")]
        if risks:
            local_counter = "研究風險（未驗證者仍屬假說）：" + "；".join(risks)
        iid = add_inf(premise, text, local_counter, f"{item['action']} {ticker} 至權重 {item['target_weight']:.2%}")
        inference_of[ticker] = iid
        row = {
            "decision_id": f"D{index}",
            "ticker": ticker,
            "action": item["action"],
            "target_weight": item["target_weight"],
            "inference_refs": [iid],
            "risk_check": _clip(
                f"2330 上限 25%、其餘 10%、現金低於 25%、持股維持 20–30 檔；目標權重 {item['target_weight']:.2%}",
                300,
            ),
        }
        if funding and buy_ids_preview:
            row["funding_for"] = buy_ids_preview[:5]
        decision_rows.append(row)

    no_trade_rows = []
    for ticker in no_trade:
        name = names.get(ticker, ticker)
        score = scores.get(ticker, 0.0)
        old_weight = held[ticker] * prices[ticker] / plan.pre_nav
        text = (
            f"對照「{prospectus['name']}」的{phrase}。"
            f"{name}（{ticker}）綜合分數 {score:.2f}，現有權重 {old_weight:.2%} 與目標的差距未超過再平衡門檻 {band:.2%}，"
            f"沒有觸發加減碼，續抱原股數。"
        )
        if decision_context.get(ticker):
            reason = decision_context[ticker].get("reason", text)
            text = ("；".join(reason) if isinstance(reason, list) else str(reason)) + "；風控後維持原股數。"
        iid = add_inf([stock_obs[ticker], obs_taiex] + evidence_observations(ticker), text, None, f"{ticker} 續抱")
        no_trade_rows.append({"ticker": ticker, "reason_refs": [iid], "reason": _clip(f"偏離未逾 {band:.2%} 再平衡門檻", 300)})

    orders = []
    for row in decision_rows:
        order = next(item for item in plan.orders if item["ticker"] == row["ticker"])
        orders.append(
            {
                "ticker": row["ticker"],
                "side": order["side"],
                "shares": int(order["shares"]),
                "decision_ref": row["decision_id"],
                "derivation_detail": _clip(
                    f"floor({row['target_weight']:.6f}*{plan.pre_nav:.2f}/{prices[row['ticker']]:.4f}/1000)*1000"
                    f"={order['target_shares']}；持有 {held.get(row['ticker'], 0)} → {order['side']} {order['shares']}",
                    300,
                ),
            }
        )

    logic = "。".join(logic_bits)
    if counter:
        counter_text = counter
    else:
        counter_text = None
    document = {
        "schema_version": active_schema_version(),
        "doc_type": "D-Plan",
        "team_id": team_id,
        "trade_date": trade_date,
        "sources": source_rows,
        "observations": observations,
        "market_view": {
            "basis_refs": basis[:10],
            "logic": _clip(logic, 1000),
            "regime": regime,
            "stance": stance,
            "posture": {
                "net_exposure_intent": plan.intent,
                "target_cash_pct_range": [plan.cash_low, plan.cash_high],
            },
            "counter_evidence": counter_text,
        },
        "inferences": inferences,
        "decisions": decision_rows,
        "no_trade_decisions": no_trade_rows,
        "orders": orders,
        "agent_metadata": {
            "model_provider": model_provider,
            "model_version": model_version,
            "run_started_at": stamp(started),
            "run_completed_at": stamp(finished),
            "tool_use_count": int(http_calls),
            "code_version": code_version,
        },
    }
    if len(document["market_view"]["logic"]) < 20:
        raise ValueError("市場判斷文字過短")
    return document
