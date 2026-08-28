#!/usr/bin/env python3
"""Structured report and private brief builders, validators, and renderers."""

from __future__ import annotations

import re
from datetime import datetime

from market_session import human_session_label
from models import (
    MarketContext, MarketReportDraft, OptionalModule, PortfolioActionBrief,
    PortfolioActionItem, PortfolioContext, PriceReference, Trigger,
)
from trigger_engine import technical_trigger

ROUNDING_TOLERANCE = 0.005
QUOTE_KINDS = {"CURRENT_QUOTE", "PREMARKET_QUOTE", "REGULAR_QUOTE", "AFTER_HOURS_QUOTE", "PREVIOUS_CLOSE"}


def price_ref_kind(session: str) -> str:
    return {
        "PREMARKET": "PREMARKET_QUOTE",
        "REGULAR": "REGULAR_QUOTE",
        "AFTER_HOURS": "AFTER_HOURS_QUOTE",
        "PREVIOUS_CLOSE": "PREVIOUS_CLOSE",
        "CLOSED_REFERENCE": "PREVIOUS_CLOSE",
    }.get(session, "CURRENT_QUOTE")


def quote_price_reference(symbol: str, context: MarketContext) -> PriceReference:
    obs = context.quotes[symbol]
    return PriceReference(
        instrument_id=obs.instrument_id,
        canonical_symbol=obs.canonical_symbol,
        value=obs.price,
        kind=price_ref_kind(obs.session),
        quote_id=obs.quote_id,
        session=obs.session,
        as_of=obs.provider_timestamp or obs.retrieved_at,
    )


def build_material_changes(context: MarketContext) -> list[str]:
    changes = []
    for symbol in ("TNX", "US2Y", "DXY", "SOX", "VIX", "BTC", "TAIEX"):
        obs = context.quotes.get(symbol) or context.macro_observations.get(symbol)
        if not obs:
            continue
        threshold = 0.5 if symbol in ("TNX", "US2Y") else 1.0
        if abs(obs.change_pct) >= threshold:
            changes.append(f"{symbol} {obs.change_pct:+.2f}%")
    return changes[:5]


def build_public_draft(context: MarketContext, narrative: str | None = None) -> MarketReportDraft:
    symbols = list(context.quotes)[:12]
    refs = [quote_price_reference(symbol, context) for symbol in symbols]
    title = {
        "us_open": "美股開盤日報",
        "us_close": "美股收盤日報",
        "tw_open": "台股開盤戰報",
        "tw_close": "台股收盤日報",
    }[context.report_type]
    session_label = human_session_label(context.report_type, context.market_session)
    material = context.material_changes or build_material_changes(context)
    optional = [
        OptionalModule(name="FedWatch", state="PARTIAL", summary="僅在搜尋 grounding 可用且找到可靠來源時呈現"),
        OptionalModule(name="ETF flows", state="PARTIAL", summary="僅在可靠資料可取得時呈現"),
        OptionalModule(name="options positioning", state="PARTIAL", summary="弱資料不硬填"),
    ]
    draft = MarketReportDraft(
        run_id=context.run_id,
        report_type=context.report_type,
        headline=f"{title} {context.market_date}｜{session_label}",
        market_state=[f"{r.canonical_symbol}: {r.value:g} ({r.session})" for r in refs[:8]],
        material_changes=material,
        drivers=[],
        rotation="僅列 verified quote 與 search-grounded material events；弱資料模組不硬填。",
        event_calendar=[],
        optional_modules=optional,
        watch_signals=material[:5],
        data_quality=[f"{k}: {v}" for k, v in context.data_quality.items()],
        price_references=refs,
    )
    draft.rendered_markdown = render_public_report(draft, context, narrative)
    return draft


def render_public_report(draft: MarketReportDraft, context: MarketContext,
                         narrative: str | None = None) -> str:
    lines = [
        f"# {draft.headline}",
        "",
        "## 1. Executive Market State",
        "| Symbol | Quote | Session | As Of | Quality |",
        "|---|---:|---|---|---|",
    ]
    for ref in draft.price_references:
        obs = context.quotes[ref.canonical_symbol]
        as_of = (ref.as_of or obs.retrieved_at).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"| {ref.canonical_symbol} | {ref.value:g} | {ref.session} | {as_of} | {obs.quality_status} |")
    lines += ["", "## 2. What Changed Since Last Report"]
    lines += [f"- {item}" for item in (draft.material_changes or ["⚠️ 無達 materiality 門檻的 deterministic delta"])]
    lines += ["", "## 3. Top Market Drivers"]
    if narrative and "新聞搜尋目前不可用" not in narrative:
        lines.append(narrative[:1800])
    else:
        lines.append("⚠️ 新聞搜尋目前不可用或未通過 grounding；不產生即時新聞歸因。")
    lines += ["", "## 4. Rotation / Regime", draft.rotation]
    lines += ["", "## 5. High-Impact Event Calendar", "⚠️ 僅在可靠搜尋結果可得時呈現；本段不以模型記憶補完。"]
    lines += ["", "## 6. Watch Into Close / Next Report"]
    lines += [f"- {item}" for item in (draft.watch_signals or ["等待下一份 validated MarketContext"])]
    unavailable = [m for m in draft.optional_modules if m.state != "AVAILABLE"]
    if unavailable or draft.data_quality:
        lines += ["", "## 7. Data Limitations"]
        for module in unavailable:
            lines.append(f"- {module.name}: {module.state} — {module.summary}")
        for issue in draft.data_quality:
            lines.append(f"- {issue}")
    return "\n".join(lines).strip()


def validate_public_draft(draft: MarketReportDraft, context: MarketContext) -> tuple[bool, str]:
    for ref in draft.price_references:
        if ref.kind not in QUOTE_KINDS:
            continue
        obs = context.quotes.get(ref.canonical_symbol) or context.macro_observations.get(ref.canonical_symbol)
        if not obs:
            return False, f"{ref.canonical_symbol} missing from MarketContext"
        if ref.quote_id != obs.quote_id:
            return False, f"{ref.canonical_symbol} quote_id mismatch"
        if ref.session != obs.session:
            return False, f"{ref.canonical_symbol} session mismatch"
        if abs(ref.value - obs.price) > ROUNDING_TOLERANCE:
            return False, f"{ref.canonical_symbol} quote value mismatch"
        if obs.quality_status != "VALID":
            return False, f"{ref.canonical_symbol} quote quality {obs.quality_status}"
    return True, "OK"


def build_action_brief(context: MarketContext, portfolio: PortfolioContext) -> PortfolioActionBrief:
    items: list[PortfolioActionItem] = []
    data_issues = []
    for pos in portfolio.positions:
        obs = context.quotes.get(pos.instrument_id)
        if not obs or obs.quality_status != "VALID":
            item = PortfolioActionItem(
                instrument_id=pos.instrument_id,
                ticker=pos.ticker,
                status="DATA_BLOCKED",
                reason_codes=["QUOTE_UNAVAILABLE_OR_INVALID"],
                summary="持股行情未通過驗證，本次不產生數字監控結論。",
            )
            data_issues.append(f"{pos.ticker}: quote unavailable or invalid")
            items.append(item)
            continue
        status = "NO_MATERIAL_CHANGE"
        reasons = ["NO_MATERIAL_EVENT"]
        summary = "未偵測到足以升級為操作審查的新資訊。"
        trigger: Trigger | None = None
        if abs(obs.change_pct) >= 7:
            status = "WATCH"
            reasons = ["LARGE_DAILY_MOVE"]
            summary = "單日波動達監控門檻，需追蹤是否伴隨基本面事件。"
            trigger = technical_trigger(pos.ticker, "previous regular close move",
                                        obs.previous_regular_close, obs.market_date, obs.quote_id)
        item = PortfolioActionItem(
            instrument_id=pos.instrument_id,
            ticker=pos.ticker,
            status=status,
            quote_id=obs.quote_id,
            reference_price=obs.price,
            session=obs.session,
            as_of=obs.provider_timestamp or obs.retrieved_at,
            reason_codes=reasons,
            summary=summary,
            next_step="SIZE_NOT_COMPUTED",
            trigger=trigger,
        )
        items.append(item)
    return PortfolioActionBrief(
        run_id=context.run_id,
        as_of=context.generated_at,
        market_session=context.market_session,
        data_quality=[f"{k}: {v}" for k, v in context.data_quality.items()],
        action_queue=[i for i in items if i.status == "ACTION_REVIEW"],
        watchlist=[i for i in items if i.status == "WATCH"],
        no_material_change=[i for i in items if i.status == "NO_MATERIAL_CHANGE"],
        data_issues=data_issues,
    )


def render_action_brief(brief: PortfolioActionBrief) -> str:
    lines = [
        "💼 持股 Action Brief",
        f"As of: {brief.as_of.strftime('%Y-%m-%d %H:%M %Z')}",
        f"Market session: {brief.market_session}",
        f"Data quality: {'OK' if not brief.data_quality and not brief.data_issues else 'LIMITED'}",
        "",
        "1. ACTION QUEUE",
    ]
    lines += _render_items(brief.action_queue) or ["- 無"]
    lines += ["", "2. WATCHLIST"]
    lines += _render_items(brief.watchlist) or ["- 無"]
    lines += ["", "3. NO MATERIAL CHANGE"]
    lines += [", ".join(f"{i.ticker} — NO_MATERIAL_CHANGE" for i in brief.no_material_change) or "- 無"]
    lines += ["", "4. UPCOMING PORTFOLIO EVENTS", "- SIZE_NOT_COMPUTED；事件需 search-grounded 後才列入"]
    if brief.data_issues:
        lines += ["", "5. DATA QUALITY"]
        lines += [f"- {issue}" for issue in brief.data_issues]
    return "\n".join(lines).strip()


def _render_items(items: list[PortfolioActionItem]) -> list[str]:
    out = []
    for item in items:
        price = f"{item.reference_price:g} {item.session}" if item.reference_price else "DATA_BLOCKED"
        out.append(f"- {item.ticker} — {item.status} | Reference: {price} | {item.summary} | Next: {item.next_step}")
    return out


def validate_action_brief(brief: PortfolioActionBrief, context: MarketContext,
                          portfolio: PortfolioContext) -> tuple[bool, str]:
    held = {p.instrument_id: p.quantity for p in portfolio.positions}
    for group in (brief.action_queue, brief.watchlist, brief.no_material_change):
        for item in group:
            if item.instrument_id not in held:
                return False, f"unknown held instrument {item.instrument_id}"
            obs = context.quotes.get(item.instrument_id)
            if item.status == "DATA_BLOCKED":
                continue
            if not obs or obs.quality_status != "VALID":
                return False, f"{item.instrument_id} missing valid quote"
            if item.quote_id != obs.quote_id:
                return False, f"{item.instrument_id} quote_id mismatch"
            if item.reference_price != obs.price:
                return False, f"{item.instrument_id} reference_price mismatch"
            if item.trigger and item.trigger.generated_by != "TriggerEngineV1":
                return False, f"{item.instrument_id} unsupported trigger generator"
            if item.trigger and item.trigger.trigger_type == "TECHNICAL" and not item.trigger.source_ids:
                return False, f"{item.instrument_id} technical trigger lacks provenance"
    rendered = render_action_brief(brief)
    if re.search(r"(加碼|買進)\s*[0-9,.]+\s*(股|shares?)", rendered, flags=re.I):
        return False, "exact buy sizing is not allowed in daily bot"
    if re.search(r"(減碼|賣出)\s*[0-9,.]+\s*(股|shares?)", rendered, flags=re.I):
        return False, "exact sell sizing is not allowed in daily bot"
    return True, "OK"
