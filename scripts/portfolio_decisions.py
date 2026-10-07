"""Source-tied, deterministic private decisions. No trade API and no PIOS writes."""
from __future__ import annotations

import json
import math
import os
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_contract import event_eligibility, event_time
from instrument_registry import resolve_instrument
from investment_language import chinese_text, event_display, format_price, interval_label
from market_session import get_target_market_date
from models import (HoldingDecisionEvidence, MarketContext, PortfolioContext, PortfolioDecision,
                    PortfolioDecisionBrief, PortfolioEventFact)

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "portfolio_decision_policy.toml"
ACTIONS_ZH = {"ADD": "加碼", "HOLD": "持有", "REDUCE": "減碼", "EXIT": "退出", "WATCH": "觀察"}
CONFIDENCE_ZH = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}
ACTION_RANK = {"EXIT": 5, "REDUCE": 4, "ADD": 3, "WATCH": 2, "HOLD": 1}
NAMES_ZH = {"AMZN": "亞馬遜", "GOOG": "Alphabet C類", "IBKR": "盈透", "MU": "美光",
            "NVDA": "輝達", "TSLA": "特斯拉", "VST": "Vistra", "BTC": "比特幣", "ETH": "以太幣"}


def weight_text(value):
    return "小於0.01%" if 0 < value < .0001 else f"{value:.2%}"


def load_policy(path: Path = POLICY_PATH) -> dict:
    policy = tomllib.loads(path.read_text(encoding="utf-8"))
    if any(not 0 < value <= 1 for value in policy["max_weights"].values()):
        raise ValueError("allocation policy weights must be fractions in (0,1]")
    return policy


def load_decision_evidence() -> list[HoldingDecisionEvidence]:
    path = os.getenv("PORTFOLIO_DECISION_EVIDENCE_PATH", "").strip()
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("decision evidence must be an array")
    return [HoldingDecisionEvidence.model_validate(item) for item in payload]


def _kind(position, policy):
    if position.ticker in policy.get("stablecoins", {}):
        return "STABLECOIN"
    if position.asset_type in {"CRYPTO", "TOKEN", "STABLECOIN"}:
        return policy.get("crypto_kinds", {}).get(position.ticker, position.asset_type)
    role = policy.get("etfs", {}).get(position.ticker, {})
    return "THEMATIC_ETF" if position.asset_type == "ETF" and role.get("thematic") else position.asset_type


def _role(ticker, policy, as_of):
    role = policy.get("etfs", {}).get(ticker)
    if not role:
        return None
    checked = event_time(role.get("checked_at"))
    if not checked or not timedelta(0) <= as_of - checked <= timedelta(days=policy["role_max_age_days"]):
        return None
    return role


def _quote(position, context, as_of, policy):
    observation = context.quotes.get(position.instrument_id) or context.quotes.get(position.ticker)
    if not observation or observation.quality_status != "VALID" or observation.currency != position.currency:
        return None
    if observation.instrument_id != position.instrument_id or observation.canonical_symbol != position.ticker:
        return None
    if not math.isfinite(observation.price) or observation.price <= 0 or not math.isfinite(observation.change_pct):
        return None
    if observation.retrieved_at.astimezone(timezone.utc) > as_of:
        return None
    if observation.provider_timestamp and observation.provider_timestamp > as_of:
        return None
    market = observation.market or resolve_instrument(position.ticker).market
    if market in {"TW", "US"} and observation.market_date != get_target_market_date(context.report_type, market, now=as_of):
        return None
    if market == "GLOBAL" and observation.market_date not in {as_of.date().isoformat(), (as_of - timedelta(days=1)).date().isoformat()}:
        return None
    if observation.change_interval == "ROLLING_24H":
        stamp = observation.provider_timestamp
        if not stamp or not timedelta(0) <= as_of - stamp <= timedelta(minutes=policy["crypto_quote_max_age_minutes"]):
            return None
    return observation


def _valuation(portfolio, context, as_of, policy):
    quotes = {p.position_id: _quote(p, context, as_of, policy) for p in portfolio.positions}
    fx = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
    fx_ok = (fx and fx.instrument_id == "USDTWD" and fx.canonical_symbol == "USDTWD" and fx.currency == "TWD"
             and fx.quality_status == "VALID" and math.isfinite(fx.price) and fx.price > 0
             and timedelta(0) <= as_of - fx.retrieved_at <= timedelta(hours=1))
    native, converted = {}, {}
    for position in portfolio.positions:
        quote = quotes[position.position_id]
        native[position.position_id] = position.quantity * quote.price if quote else None
        rate = 1.0 if position.currency == "TWD" else fx.price if position.currency == "USD" and fx_ok else None
        converted[position.position_id] = native[position.position_id] * rate if native[position.position_id] is not None and rate is not None else None
    complete = bool(portfolio.positions) and all(value is not None and math.isfinite(value) for value in converted.values())
    total = sum(converted.values()) if complete else None
    return quotes, native, converted, total


def _triggers(kind, cap):
    allocation = f"占持倉估值超過 {cap:.0%} 的適用上限" if cap is not None else "核實配置上限後發現超標"
    if kind in {"ETF", "THEMATIC_ETF"}:
        return ("基金角色仍適用、合併重疊曝險後低於已確認目標，且可動用資金已核實。",
                allocation + "，或重疊曝險超過已確認目標時降低配置。",
                "發行人正式公告基金終止、追蹤目標變更，或確認不再符合原配置用途。")
    if kind in {"CRYPTO", "TOKEN"}:
        return ("原持有理由、協議／安全與流動性資料核實，估值或使用需求支持增加曝險且低於目標。",
                allocation + "，或核實安全、供給／解鎖與流動性風險惡化。",
                "核实不可修復的安全／協議問題、持有理由失效，或交易與退出渠道受阻。")
    if kind == "STABLECOIN":
        return ("有已確認的美元結算需求，儲備與兌回渠道核實且未超過發行人曝險上限。",
                allocation + "，或錨定偏離、儲備／兌回風險經核實升高。",
                "核實贖回停止或儲備嚴重不足，且原結算用途不再成立。")
    return ("核實原投資假設、最新獲利／營運展望與估值，且占比低於已確認目標；資金核實後才定量。",
            allocation + "，或來源證據顯示原假設轉弱／估值風險上升。",
            "核實原假設失效或結構性營運惡化；一般波動不構成退出條件。")


def build_decision_brief(context: MarketContext, portfolio: PortfolioContext, *,
                         events: list[PortfolioEventFact] | None = None,
                         evidence: list[HoldingDecisionEvidence] | None = None,
                         policy: dict | None = None, as_of: datetime | None = None) -> PortfolioDecisionBrief:
    policy = policy or load_policy()
    if len({p.position_id for p in portfolio.positions}) != len(portfolio.positions):
        raise ValueError("authoritative position identities must be unique")
    now = event_time(as_of or context.generated_at)
    events = events or []
    quotes, values, converted, total = _valuation(portfolio, context, now, policy)
    fresh_research = {e.instrument_id: e for e in evidence or [] if e.verified and e.source_ids
                      and timedelta(0) <= now - event_time(e.verified_at) <= timedelta(days=policy["research_max_age_days"])}
    role_by_id = {p.instrument_id: _role(p.ticker, policy, now) for p in portfolio.positions}
    cash_known = bool(portfolio.cash) and all(c.deployable is not None for c in portfolio.cash)
    cash_available = any(c.deployable and c.amount > 0 for c in portfolio.cash)
    cash_text = ("可動用現金尚未核實；本次不產生精確交易數量。" if not cash_known else
                 "可動用現金已核實；交易數量仍需通過配置與定量方法檢查。" if cash_available else
                 "目前沒有已核實的可動用現金，不以現金餘額推算可買股數。")
    decisions, checked_positions = [], set()
    for position in portfolio.positions:
        kind = _kind(position, policy)
        quote = quotes[position.position_id]
        weight = converted[position.position_id] / total if total else None
        cap = position.max_weight if position.allocation_verified and position.max_weight else portfolio.allocation_limits.get(kind)
        authoritative_cap = cap is not None
        if cap is None:
            cap = policy["max_weights"].get(kind, policy["max_weights"]["EQUITY"])
        if not 0 < cap <= 1:
            raise ValueError("invalid verified allocation limit")
        allocation_source = "PIOS 已驗證規則" if authoritative_cap else "可調整的預設檢視上限"
        applicable = [e for e in events if e.instrument_id == position.instrument_id or e.ticker == position.ticker]
        valid_checks = [e for e in applicable if event_eligibility(e, now) in {"RECENT_MATERIAL", "CURRENT_CHECK", "UPCOMING"}]
        fresh_events = [e for e in applicable if event_eligibility(e, now) == "RECENT_MATERIAL"]
        if valid_checks:
            checked_positions.add(position.position_id)
        event_signal = "MATERIAL" if fresh_events else "CHECKED" if valid_checks else "UNKNOWN"
        research = fresh_research.get(position.instrument_id)
        linked = next((e for e in fresh_events if research and research.event_source_url == e.source_url), None)
        if research and research.event_source_url and not linked:
            research = None  # A recently written note cannot refresh an old underlying event.
        thesis = research.thesis_status if research else "UNKNOWN"
        fundamental = research.fundamental_signal if research else "UNKNOWN"
        valuation = research.valuation_signal if research else "UNKNOWN"
        effect = research.event_effect if research and linked else "NONE"
        event_assessed = bool(research and linked and (effect != "NONE" or research.thesis_status in {"INTACT", "IMPROVING"}))
        unclassified_event = bool(fresh_events) and not event_assessed
        # An analyst's unsupported action flag cannot bypass event/source freshness.
        if thesis == "INVALIDATED" and not (linked and effect in {"THESIS_BREAK", "REDEMPTION_BLOCK"}):
            thesis = "UNKNOWN"
        role = role_by_id[position.instrument_id]
        peers = [p.ticker for p in portfolio.positions if p.position_id != position.position_id and role
                 and (other := role_by_id[p.instrument_id]) and other["family"] == role["family"]]
        basis_ok = position.basis_quality == "VERIFIED" and position.cost_basis is not None and math.isfinite(position.cost_basis) and (position.basis_currency or position.currency) == position.currency
        cost = position.cost_basis if basis_ok else None
        pnl = (quote.price / cost - 1) * 100 if quote and cost else None
        gaps = []
        if not quote:
            gaps.append(f"{position.ticker} 的行情／比較時窗尚未通過驗證")
        if weight is None:
            gaps.append("完整持倉報價或換算匯率未齊，無法驗證占比")
        if not basis_ok:
            gaps.append("成本基礎尚未完成驗證，因此本次不使用損益幅度作為加減碼依據")
        if not valid_checks:
            gaps.append("近期事件檢查未完成；未知不視為沒有事件")
        if unclassified_event:
            gaps.append("重大公告對原持有假設／配置用途的影響尚未核實")
        if not cash_known:
            gaps.append("可動用現金與精確定量方法未核實")
        if not position.allocation_verified:
            gaps.append("個人配置目標未核實；上限採明示風險檢視規則")
        if kind == "EQUITY" and (thesis == "UNKNOWN" or fundamental == "UNKNOWN"):
            gaps.append("原投資假設與最新營運／獲利資料未核實")
        if kind in {"ETF", "THEMATIC_ETF"}:
            gaps.append("未取得即時成分股穿透占比，不宣稱精確重疊率")
            if not role:
                gaps.append("基金投資角色與最新公開說明書尚待核對")
        if kind in {"CRYPTO", "TOKEN", "STABLECOIN"} and not (research and research.liquidity_verified):
            gaps.append("協議／發行人與實際流動性或兌回渠道未核實")
        if valuation == "UNKNOWN" and kind == "EQUITY":
            gaps.append("估值資料未驗證，不判斷便宜或昂貴")
        abnormal = bool(quote and abs(quote.change_pct) >= policy["abnormal_move_pct"])
        over = weight is not None and weight > cap
        target = position.target_weight if position.allocation_verified else None
        zero_target = target == 0 if target is not None else False
        above_target = weight is not None and target is not None and weight > target + policy["target_drift_tolerance"]
        depeg = bool(kind == "STABLECOIN" and quote and abs(quote.price - 1) * 100 >= policy["stablecoin_depeg_pct"])
        if kind in {"ETF", "THEMATIC_ETF"}:
            positive_case = bool(role and not peers)
        elif kind in {"CRYPTO", "TOKEN"}:
            positive_case = bool(research and thesis == "IMPROVING" and fundamental == "IMPROVING" and research.liquidity_verified)
        elif kind == "STABLECOIN":
            positive_case = bool(research and thesis in {"INTACT", "IMPROVING"} and research.liquidity_verified and not depeg)
        else:
            positive_case = bool(research and thesis in {"INTACT", "IMPROVING"} and fundamental in {"STABLE", "IMPROVING"} and valuation in {"FAIR", "ATTRACTIVE"})
        action, rule = "WATCH", "WAIT_FOR_EVIDENCE"
        if zero_target:
            action, rule = "EXIT", "VERIFIED_ZERO_ALLOCATION_TARGET"
        elif effect in {"THESIS_BREAK", "REDEMPTION_BLOCK"} and linked:
            action, rule = "EXIT", "VERIFIED_THESIS_BREAK"
        elif over:
            action, rule = "REDUCE", "EXCESS_CONCENTRATION"
        elif above_target:
            action, rule = "REDUCE", "VERIFIED_TARGET_DRIFT"
        elif effect in {"WEAKENING", "RESERVE_RISK"} or thesis == "WEAKENED" or valuation == "EXPENSIVE" or depeg:
            action, rule = "REDUCE", "VERIFIED_RISK_INCREASE"
        elif (quote and weight is not None and positive_case
              and valid_checks and not unclassified_event and position.allocation_verified and position.target_weight is not None
              and weight < min(position.target_weight, cap) and (not cash_known or cash_available)):
            action, rule = "ADD", "VERIFIED_POSITIVE_CASE_AND_TARGET_CAPACITY"
        elif quote and weight is not None and kind in {"ETF", "THEMATIC_ETF"} and role and not abnormal and not unclassified_event:
            action, rule = "HOLD", "VERIFIED_FUND_ROLE_WITHIN_LIMIT"
        elif quote and weight is not None and kind == "STABLECOIN" and not depeg and not unclassified_event:
            action, rule = "HOLD", "LIQUIDITY_ROLE_WITH_NO_OBSERVED_DEPEG"
        elif quote and weight is not None and thesis in {"INTACT", "IMPROVING"} and not abnormal and not unclassified_event:
            action, rule = "HOLD", "SOURCE_SUPPORTED_THESIS_WITHIN_LIMIT"
        confidence = "LOW"
        if action in {"REDUCE", "EXIT"} and (over or linked or above_target or zero_target):
            confidence = "MEDIUM"
        if action == "ADD" and kind != "EQUITY" and valid_checks:
            confidence = "MEDIUM"
        if quote and weight is not None and basis_ok and research and thesis != "UNKNOWN" and fundamental != "UNKNOWN" and valuation != "UNKNOWN" and valid_checks and not unclassified_event:
            confidence = "HIGH"
        if not quote:
            confidence = "LOW"
        reasons = []
        if quote:
            label = "日線比較" if kind in {"CRYPTO", "TOKEN", "STABLECOIN"} and quote.change_interval == "PREVIOUS_CLOSE" else interval_label(quote.change_interval)
            reasons.append(f"行情已驗證，{label}{quote.change_pct:+.2f}%" + ("；波動只形成核對訊號，不單獨決定買賣" if abnormal else ""))
        else:
            reasons.append("缺少可採用行情，暫時維持部位；行情修復前不新增曝險")
        if weight is not None:
            reasons.append(f"占持倉估值 {weight_text(weight)}，{'超過' if over else '未超過'} {cap:.0%} 的{allocation_source}")
            if target is not None:
                reasons[-1] += f"；已驗證配置目標 {target:.0%}"
        else:
            reasons.append("總值未完整驗證，暫不依未完成的占比判斷集中度")
        if zero_target:
            reasons.append("PIOS 已驗證配置目標為零；本次退出方向依配置用途，不假稱公司投資假設已失效")
        elif above_target:
            reasons.append(f"占比高於已驗證目標且超過明示 {policy['target_drift_tolerance'] * 100:g} 個百分點容忍帶，建議降低配置偏離")
        elif linked and effect != "NONE":
            display = event_display(linked, position.asset_type)
            reasons.append(chinese_text(research.thesis_basis_zh, f"已驗證事件需改變原配置判斷：{display['title']}"))
        elif over:
            reasons.append("建議降低單一部位集中度；方向基於配置風險，不代表營運或估值已轉差")
        elif role:
            reasons.append(role["role_zh"] + (f"；與 {' / '.join(sorted(peers))} 曝險範圍重疊，目前不建議同時加碼" if peers else ""))
        elif kind == "STABLECOIN":
            reasons.append("保留美元結算用途；報價接近錨定值不代表發行人與兌回風險已消失")
        elif research and thesis in {"INTACT", "IMPROVING"}:
            reasons.append(chinese_text(research.thesis_basis_zh, "來源支持原投資假設維持；目前没有已驗證的配置調整必要"))
        else:
            reasons.append("增加曝險所需的持有理由與風險證據未齊，先核對而不追價")
        risk = ("個股集中及獲利／估值與原假設不符的風險" if kind == "EQUITY" else
                "基金內部集中、追蹤偏離及重疊曝險風險" if kind in {"ETF", "THEMATIC_ETF"} else
                "發行人儲備、錨定與兌回渠道風險；不等同銀行現金保障" if kind == "STABLECOIN" else
                "協議安全、代幣供給與流動性風險")
        add, reduce, exit_ = _triggers(kind, cap)
        waiting = (f"等待 {position.ticker} 行情、完整持倉估值與必要匯率通過驗證" if not quote or weight is None else
                   f"等待核對已驗證公告「{event_display(fresh_events[0], kind)['title']}」是否改變原持有假設／配置用途" if unclassified_event else
                   f"等待核實此次 {quote.change_pct:+.2f}% 波動是否有{'基金規則／成分股調整' if kind in {'ETF','THEMATIC_ETF'} else '協議／安全／流動性事件' if kind in {'CRYPTO','TOKEN'} else '營運／公司公告'}支持" if abnormal else
                   f"等待 {position.ticker} 最新一季財報／營運展望與原持有假設完成核對；確認前維持部位、不追加" if kind == "EQUITY" else
                   "等待協議／安全、持有用途及可退出流動性完成核對" if kind in {"CRYPTO", "TOKEN"} else
                   "等待發行人最新儲備與兌回資格核對" if kind == "STABLECOIN" else
                   "等待基金角色、成分股重疊與配置目標完成核對")
        chosen_event = linked or (fresh_events[0] if fresh_events else None)
        sources = ([quote.quote_id] if quote else []) + (research.source_ids if research else []) + ([role["source_url"]] if role else [])
        if chosen_event:
            sources.append(chosen_event.source_url)
        decisions.append(PortfolioDecision(position_id=position.position_id, ticker=position.ticker,
            instrument_id=position.instrument_id, name=role["name_zh"] if role else NAMES_ZH.get(position.ticker, position.name),
            asset_type=kind, market=resolve_instrument(position.ticker).market, currency=position.currency,
            recommendation=action, confidence=confidence, quantity=position.quantity,
            quote_id=quote.quote_id if quote else None, quote_as_of=(quote.provider_timestamp or quote.retrieved_at) if quote else None,
            quote_market_date=quote.market_date if quote else None, quote_session=quote.session if quote else None,
            current_price=quote.price if quote else None, change_pct=quote.change_pct if quote else None,
            change_interval=quote.change_interval if quote else "UNKNOWN", cost_basis=cost,
            basis_quality="VERIFIED" if basis_ok else "UNRESOLVED", unrealized_pnl_pct=pnl,
            position_market_value=values[position.position_id], position_market_value_twd=converted[position.position_id],
            portfolio_market_value_twd=total, portfolio_weight=weight,
            weight_quality="VERIFIED_SNAPSHOT" if weight is not None else "UNAVAILABLE", max_weight=cap,
            target_weight=target, liquidity_verified=bool(research and research.liquidity_verified),
            allocation_source=allocation_source, price_signal="ABNORMAL" if abnormal else "QUIET" if quote else "UNKNOWN",
            valuation_signal=valuation, fundamental_signal=fundamental, event_signal=effect if effect != "NONE" else event_signal,
            event_impact_verified=event_assessed,
            concentration_signal="EXCESS" if over else "WITHIN_LIMIT" if weight is not None else "UNKNOWN",
            thesis_status=thesis, rule_id=rule, reasons=reasons[:3], risks=[risk], add_trigger=add,
            reduce_trigger=reduce, exit_trigger=exit_, watch_condition=waiting if action == "WATCH" else "",
            data_gaps=gaps, source_ids=list(dict.fromkeys(sources)), verified_event=chosen_event,
            event_checks=valid_checks,
            overlap_peers=sorted(peers), event_severity={"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}.get(chosen_event.severity, 0)
            if chosen_event and effect in {"WEAKENING", "THESIS_BREAK", "RESERVE_RISK", "REDEMPTION_BLOCK"} else 0))
    ranked = sorted(decisions, key=lambda d: (-ACTION_RANK[d.recommendation], -d.event_severity,
        -(d.portfolio_weight or 0), -(d.position_market_value_twd or 0),
        -{"HIGH": 3, "MEDIUM": 2, "LOW": 1}[d.confidence], -abs(d.change_pct or 0), d.ticker, d.position_id))
    counts = {action: sum(d.recommendation == action for d in decisions) for action in ACTIONS_ZH}
    return PortfolioDecisionBrief(run_id=context.run_id, as_of=now, portfolio_snapshot_id=portfolio.snapshot_id,
        policy_version=policy["version"], weight_basis=policy["weight_basis"], policy_description_zh=policy["description_zh"],
        expected_positions=len(portfolio.positions), quote_covered_positions=sum(q is not None for q in quotes.values()),
        event_checked_positions=len(checked_positions), decisions=decisions, summary_counts=counts,
        priority_position_ids=[d.position_id for d in ranked[:3]], cash_status_zh=cash_text,
        valuation_notes=["占比為全部持倉最新可得報價估值；各市場報價時間不同，非同步即時淨值；未含現金與負債。"])


def _holding_text(decision: PortfolioDecision) -> str:
    d = decision
    action = ACTIONS_ZH[d.recommendation]
    currency = {"USD": "美元", "TWD": "元（新台幣）"}.get(d.currency, d.currency)
    price = f"{format_price(d.current_price)} {currency}" if d.current_price is not None else "尚未通過驗證"
    label = "日線比較" if d.asset_type in {"CRYPTO", "TOKEN", "STABLECOIN"} and d.change_interval == "PREVIOUS_CLOSE" else interval_label(d.change_interval)
    lines = [f"{d.ticker} {d.name}｜{action}", f"現價（已驗證參考）：{price}" + (f"；{label} {d.change_pct:+.2f}%" if d.change_pct is not None else ""),
             f"報價日期：{d.quote_market_date or '未驗證'}；{'正式收盤參考' if d.asset_type in {'EQUITY','ETF','THEMATIC_ETF'} and d.quote_session in {'REGULAR','PREVIOUS_CLOSE','CLOSED_REFERENCE'} else '來源已驗證報價'}",
             f"我的成本：{format_price(d.cost_basis)} {currency}" if d.cost_basis is not None else "我的成本：尚未完成驗證",
             f"未實現損益：{d.unrealized_pnl_pct:+.2f}%" if d.unrealized_pnl_pct is not None else "未實現損益：未計算（成本未驗證）" if d.cost_basis is None else "未實現損益：未計算（行情未驗證）",
             f"部位占比：{weight_text(d.portfolio_weight)}（持倉估值）" if d.portfolio_weight is not None else "部位占比：尚未完成驗證",
             f"判斷：{action}；信心：{CONFIDENCE_ZH[d.confidence]}", "理由："]
    lines += [f"- {chinese_text(reason, '資料尚待來源核對') }" for reason in d.reasons]
    lines += ["主要風險：" + "；".join(chinese_text(risk, "資料與流動性風險尚待核對") for risk in d.risks)]
    if d.verified_event:
        display = event_display(d.verified_event, d.asset_type)
        lines += [f"已驗證事件：{display['date']}｜{display['title']}；來源：{display['source']}"]
    if d.watch_condition:
        lines.append("等待條件：" + chinese_text(d.watch_condition, "等待原持有理由及風險資料核實"))
    lines += ["加碼條件：" + chinese_text(d.add_trigger, "需先核實投資假設與配置目標"),
              "減碼條件：" + chinese_text(d.reduce_trigger, "核實曝險超標或風險升高"),
              "退出條件：" + chinese_text(d.exit_trigger, "核實原持有理由失效"),
              "資料缺口：" + "；".join(chinese_text(gap, "資料尚未核實") for gap in d.data_gaps)]
    return "\n".join(lines)


def decision_blocks(brief):
    lookup = {d.position_id: d for d in brief.decisions}
    summary = [f"💼 持股操作建議｜{brief.as_of.astimezone(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M}",
               "", "【今日操作總覽】"]
    summary += [f"{label}：{brief.summary_counts[action]} 檔" for action, label in ACTIONS_ZH.items()]
    summary += ["觀察＝暫時維持部位，先完成逐檔等待條件；不自動下單。"]
    summary += ["", "【優先處理】"]
    summary += [f"{i}. {lookup[key].ticker} — {ACTIONS_ZH[lookup[key].recommendation]}" for i, key in enumerate(brief.priority_position_ids, 1)]
    summary += ["", f"行情：{brief.quote_covered_positions}/{brief.expected_positions} 已驗證；近期事件檢查：{brief.event_checked_positions}/{brief.expected_positions}。",
                brief.cash_status_zh, *brief.valuation_notes, brief.policy_description_zh, "", "【逐檔操作建議】"]
    groups = [[], [], []]
    for decision in brief.decisions:
        index = 0 if decision.market == "TW" else 1 if decision.asset_type == "EQUITY" and decision.market == "US" else 2
        groups[index].append(_holding_text(decision))
    return "\n".join(summary), groups


def render_decision_brief(brief):
    summary, groups = decision_blocks(brief)
    sections = [summary]
    for label, group in zip(("🇹🇼 台股", "🇺🇸 美股", "ETF／加密資產／穩定幣"), groups):
        if group:
            sections += [label, "\n\n".join(group)]
    return "\n\n".join(sections)


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def decision_telegram_chunks(brief, max_units=4096):
    summary, groups = decision_blocks(brief)
    chunks = []
    for index, (label, group) in enumerate(zip(("🇹🇼 台股", "🇺🇸 美股", "ETF／加密資產／穩定幣"), groups)):
        if not group:
            continue
        current = (summary + "\n\n" if index == 0 else "💼 持股操作建議（續）\n\n") + label
        for block in group:
            if _units(block) > max_units - 80:
                raise ValueError("one holding block exceeds Telegram limit; refusing to truncate")
            if _units(current + "\n\n" + block) > max_units - 40:
                chunks.append(current)
                current = "💼 持股操作建議（續）\n\n" + label
            current += "\n\n" + block
        chunks.append(current)
    if not chunks:
        chunks = [summary]
    # If no Taiwan holding exists, the first message must still contain the summary.
    if groups[0] == [] and brief.decisions:
        chunks.insert(0, summary)
    return [f"（{index}/{len(chunks)}）\n{text}" for index, text in enumerate(chunks, 1)]


def validate_decision_brief(brief, context, portfolio):
    policy = load_policy()
    quotes, native, converted, total = _valuation(portfolio, context, brief.as_of, policy)
    if Counter(d.position_id for d in brief.decisions) != Counter(p.position_id for p in portfolio.positions):
        return False, "each active position must have exactly one decision"
    if brief.expected_positions != len(portfolio.positions) or len({d.position_id for d in brief.decisions}) != len(brief.decisions):
        return False, "decision identities or expected count invalid"
    if brief.quote_covered_positions != sum(q is not None for q in quotes.values()):
        return False, "quote coverage count mismatch"
    valid_check_count = sum(bool(d.event_checks) for d in brief.decisions)
    if brief.event_checked_positions != valid_check_count:
        return False, "event coverage count mismatch"
    if len(brief.priority_position_ids) > 3 or len(set(brief.priority_position_ids)) != len(brief.priority_position_ids) or not set(brief.priority_position_ids).issubset({d.position_id for d in brief.decisions}):
        return False, "invalid priority identities"
    if sum(brief.summary_counts.values()) != len(portfolio.positions):
        return False, "decision summary count mismatch"
    if set(brief.summary_counts) != set(ACTIONS_ZH):
        return False, "decision summary categories invalid"
    for action in ACTIONS_ZH:
        if brief.summary_counts[action] != sum(d.recommendation == action for d in brief.decisions):
            return False, "decision category count mismatch"
    for d in brief.decisions:
        p = next(p for p in portfolio.positions if p.position_id == d.position_id)
        if d.quantity != p.quantity or not d.reasons or not d.risks or not all((d.add_trigger, d.reduce_trigger, d.exit_trigger)):
            return False, "position identity or decision explanation incomplete"
        if d.max_weight is None or not 0 < d.max_weight <= 1:
            return False, "decision allocation bound invalid"
        expected_cap = p.max_weight if p.allocation_verified and p.max_weight else portfolio.allocation_limits.get(d.asset_type)
        if expected_cap is None:
            expected_cap = policy["max_weights"].get(d.asset_type, policy["max_weights"]["EQUITY"])
        if d.max_weight != expected_cap:
            return False, "allocation bound differs from applicable verified rule"
        quote = quotes[p.position_id]
        if d.current_price != (quote.price if quote else None) or d.quote_id != (quote.quote_id if quote else None):
            return False, "decision quote differs from verified observation"
        expected_weight = converted[p.position_id] / total if total else None
        if d.portfolio_weight != expected_weight or d.portfolio_market_value_twd != total or d.position_market_value != native[p.position_id]:
            return False, "valuation or concentration denominator mismatch"
        expected_basis = p.cost_basis if p.basis_quality == "VERIFIED" and p.cost_basis is not None and math.isfinite(p.cost_basis) and (p.basis_currency or p.currency) == p.currency else None
        if d.cost_basis != expected_basis:
            return False, "cost basis differs from authoritative verified basis"
        if d.basis_quality != ("VERIFIED" if expected_basis is not None else "UNRESOLVED"):
            return False, "basis-quality label differs from authoritative verification"
        expected_pnl = (quote.price / expected_basis - 1) * 100 if quote and expected_basis else None
        if d.unrealized_pnl_pct != expected_pnl:
            return False, "unrealized P/L provenance mismatch"
        if d.basis_quality == "UNRESOLVED" and (d.cost_basis is not None or d.unrealized_pnl_pct is not None):
            return False, "unverified basis produced P/L"
        if d.trade_quantity is not None:
            return False, "exact sizing is not implemented"
        if d.recommendation == "WATCH" and not d.watch_condition:
            return False, "WATCH requires an observable verification condition"
        if d.verified_event and event_eligibility(d.verified_event, brief.as_of) != "RECENT_MATERIAL":
            return False, "stale event affected decision"
        if any(event_eligibility(e, brief.as_of) not in {"RECENT_MATERIAL", "CURRENT_CHECK", "UPCOMING"} for e in d.event_checks):
            return False, "stale check affected event coverage"
        if d.target_weight != (p.target_weight if p.allocation_verified else None):
            return False, "allocation target differs from authoritative verification"
        if d.recommendation == "EXIT" and not ((p.allocation_verified and p.target_weight == 0 and d.rule_id == "VERIFIED_ZERO_ALLOCATION_TARGET")
                or (d.verified_event and d.event_signal in {"THESIS_BREAK", "REDEMPTION_BLOCK"})):
            return False, "EXIT lacks source-linked thesis break"
        if d.recommendation == "ADD":
            positive = (bool(_role(p.ticker, policy, brief.as_of)) and not d.overlap_peers if d.asset_type in {"ETF", "THEMATIC_ETF"}
                        else d.thesis_status == "IMPROVING" and d.fundamental_signal == "IMPROVING" and d.liquidity_verified if d.asset_type in {"CRYPTO", "TOKEN"}
                        else d.thesis_status in {"INTACT", "IMPROVING"} and d.liquidity_verified and d.current_price is not None
                             and abs(d.current_price - 1) * 100 < policy["stablecoin_depeg_pct"] if d.asset_type == "STABLECOIN"
                        else d.thesis_status in {"INTACT", "IMPROVING"} and d.fundamental_signal in {"STABLE", "IMPROVING"} and d.valuation_signal in {"FAIR", "ATTRACTIVE"})
            if not (p.allocation_verified and p.target_weight is not None and d.portfolio_weight is not None
                    and d.portfolio_weight < min(p.target_weight, d.max_weight) and positive and d.event_signal != "UNKNOWN"):
                return False, "ADD lacks asset-specific evidence or verified target"
        if d.rule_id == "EXCESS_CONCENTRATION" and not (d.portfolio_weight is not None and d.portfolio_weight > d.max_weight):
            return False, "concentration reduction not justified"
        if d.rule_id == "VERIFIED_TARGET_DRIFT" and not (p.allocation_verified and p.target_weight is not None and d.portfolio_weight is not None
                and d.portfolio_weight > p.target_weight + policy["target_drift_tolerance"]):
            return False, "target reduction not justified"
        if d.confidence == "HIGH" and not (d.current_price is not None and d.portfolio_weight is not None and d.basis_quality == "VERIFIED"
                and d.thesis_status != "UNKNOWN" and d.fundamental_signal != "UNKNOWN" and d.valuation_signal != "UNKNOWN" and d.event_signal != "UNKNOWN"):
            return False, "confidence exceeds available evidence"
        if d.confidence == "HIGH" and d.verified_event and not d.event_impact_verified:
            return False, "unassessed material event cannot imply high confidence"
    text = render_decision_brief(brief)
    if any(token in text for token in ("ACTION_REVIEW", "NO_MATERIAL_CHANGE", "EVENT_UNCHECKED", "DATA_BLOCKED", "ROLLING_24H", "PREVIOUS_CLOSE")):
        return False, "internal enum leaked"
    if any(_units(chunk) > 4096 for chunk in decision_telegram_chunks(brief)):
        return False, "Telegram chunk length exceeded"
    return True, "OK"
