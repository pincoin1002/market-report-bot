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


from instrument_registry import resolve_instrument


def select_report_symbols(context: MarketContext) -> list[str]:
    report_type = context.report_type
    if report_type.startswith("tw_"):
        priority = [
            "TAIEX", "2330", "2317", "2454", "2308", "2382", "2303", "3711",
            "2356", "3231", "2383", "4958", "2368", "3017", "3324", "2421", "2301",
            "TSM", "USDTWD", "SOX",
        ]
    else:
        priority = [
            "SPX", "NDX", "DJI", "SOX", "VIX", "TNX", "US2Y", "DXY",
            "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "TSM", "AVGO", "AMD",
            "BTC", "CL", "GC",
        ]
    selected = [s for s in priority if s in context.quotes and context.quotes[s].quality_status == "VALID"]
    primary_market = "TW" if report_type.startswith("tw_") else "US"
    for s, obs in context.quotes.items():
        if obs.quality_status == "VALID" and obs.market == primary_market and s not in selected:
            selected.append(s)
    if len(selected) < 12:
        for s, obs in context.quotes.items():
            if obs.quality_status == "VALID" and s not in selected:
                selected.append(s)
    return selected[:12]


def build_material_changes(context: MarketContext) -> list[str]:
    changes = []
    report_type = context.report_type
    if report_type.startswith("tw_"):
        symbols_to_check = [
            ("TAIEX", 0.5), ("USDTWD", 0.5), ("TSM", 1.0), ("SOX", 1.0),
            ("2330", 1.0), ("2317", 1.0), ("2454", 1.0), ("2308", 1.0),
            ("2382", 1.0), ("2303", 1.0), ("3711", 1.0), ("2383", 1.0),
            ("3017", 1.0), ("3324", 1.0),
        ]
    else:
        symbols_to_check = [
            ("SPX", 0.5), ("DJI", 0.5), ("TNX", 0.5), ("US2Y", 0.5),
            ("NDX", 1.0), ("SOX", 1.0), ("VIX", 1.0), ("DXY", 0.5),
            ("BTC", 1.5), ("CL", 1.5), ("GC", 1.0),
            ("NVDA", 1.0), ("AAPL", 1.0), ("MSFT", 1.0), ("GOOGL", 1.0),
            ("AMZN", 1.0), ("META", 1.0), ("TSLA", 1.5), ("TSM", 1.0),
        ]
    for symbol, threshold in symbols_to_check:
        obs = context.quotes.get(symbol) or context.macro_observations.get(symbol)
        if not obs or obs.quality_status != "VALID":
            continue
        if abs(obs.change_pct) >= threshold:
            spec = resolve_instrument(symbol)
            display = f"{spec.display_name} ({symbol})" if spec.display_name != symbol else symbol
            changes.append(f"{display} {obs.change_pct:+.2f}%")
    return changes[:6]


def build_public_draft(context: MarketContext, narrative: str | None = None) -> MarketReportDraft:
    symbols = select_report_symbols(context)
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
        OptionalModule(name="FedWatch", state="UNAVAILABLE"),
        OptionalModule(name="ETF flows", state="UNAVAILABLE"),
        OptionalModule(name="options positioning", state="UNAVAILABLE"),
    ]
    draft = MarketReportDraft(
        run_id=context.run_id,
        report_type=context.report_type,
        headline=f"{title} {context.market_date}｜{session_label}",
        market_state=[f"{r.canonical_symbol}: {r.value:g} ({r.session})" for r in refs[:8]],
        material_changes=material,
        drivers=_extract_grounded_drivers(narrative),
        rotation="",
        event_calendar=[],
        optional_modules=optional,
        watch_signals=material[:5],
        data_quality=[f"{k}: {v}" for k, v in context.data_quality.items()],
        price_references=refs,
    )
    draft.rendered_markdown = render_public_report(draft, context, narrative)
    return draft


def _extract_grounded_drivers(narrative: str | None) -> list[str]:
    """Reduce model prose to bounded content, never a nested report.

    Gemini is asked for a report by the legacy prompts. Until those prompts
    are fully retired, only the first substantive prose after 今日一句話 is
    accepted into the structured draft. Headings, tables and diagnostics are
    deliberately rejected.
    """
    if not narrative or "新聞搜尋目前不可用" in narrative:
        return []
    rejected_phrases = (
        "DATA_BLOCKED", "DATE_MISMATCH", "數據阻斷", "資料阻斷",
        "Smart Money", "強烈預期", "利空出盡", "資金回流",
        "Fed 升息 1 碼", "升息 1 碼", "升息1碼",
    )
    lines = [line.strip() for line in narrative.splitlines()]
    start = 0
    for idx, line in enumerate(lines):
        if "今日一句話" in line:
            start = idx + 1
            break
    drivers: list[str] = []
    for line in lines[start:]:
        plain = re.sub(r"^[#>*\-•\s]+", "", line).strip()
        plain = plain.replace("**", "").replace("__", "")
        if not plain or plain in ("---", "—"):
            continue
        if line.startswith("#") or re.match(r"^\*{0,2}\d+[.、]", plain):
            if drivers:
                break
            continue
        if line.startswith("|") or "DATA_BLOCKED" in plain or "DATE_MISMATCH" in plain:
            continue
        if "Market session:" in plain or len(plain) < 10:
            continue
        if any(phrase.lower() in plain.lower() for phrase in rejected_phrases):
            continue
        drivers.append(plain[:600])
        break
    return drivers


def render_public_report(draft: MarketReportDraft, context: MarketContext,
                         narrative: str | None = None) -> str:
    lines = [
        f"# {draft.headline}",
        "",
    ]
    sec_num = 1

    # Section 1: Executive Market State
    lines.append(f"## {sec_num}. 市場核心概況 (Executive Market State)")
    sec_num += 1
    lines.append("| 標的 | 最新報價 | 漲跌幅 | 行情時間 | 狀態 |")
    lines.append("|---|---:|---:|---|---|")
    for ref in draft.price_references:
        obs = context.quotes.get(ref.canonical_symbol)
        if not obs:
            continue
        spec = resolve_instrument(ref.canonical_symbol)
        display = f"{spec.display_name} ({ref.canonical_symbol})" if spec.display_name != ref.canonical_symbol else ref.canonical_symbol
        if spec.currency == "USD" or ref.value >= 1000:
            price_str = f"{ref.value:,.2f}"
        elif ref.value >= 100:
            price_str = f"{ref.value:,.1f}"
        else:
            price_str = f"{ref.value:g}"
        session_label = human_session_label(context.report_type, obs.session)
        as_of_dt = ref.as_of or obs.provider_timestamp or obs.observed_at or obs.retrieved_at
        as_of_str = as_of_dt.strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"| {display} | {price_str} | {obs.change_pct:+.2f}% | {as_of_str} | {session_label} |")
    lines.append("")

    # Section 2: What Changed Since Last Report
    if draft.material_changes:
        lines.append(f"## {sec_num}. 相較上一交易日變化 (What Changed Since Last Report)")
        sec_num += 1
        for item in draft.material_changes:
            lines.append(f"- {item}")
        lines.append("")

    # Section 3: Top Market Drivers
    if draft.drivers:
        lines.append(f"## {sec_num}. 今日走勢與市場驅動 (Top Market Drivers)")
        sec_num += 1
        for item in draft.drivers:
            lines.append(f"- {item}")
        lines.append("")

    # Section 4: Rotation & Sectors (omit placeholders!)
    if draft.rotation and not any(ph in draft.rotation for ph in ("僅列 verified quote", "弱資料模組不硬填")):
        lines.append(f"## {sec_num}. 產業與資金輪動 (Rotation & Sectors)")
        sec_num += 1
        lines.append(draft.rotation)
        lines.append("")

    # Section 5: High-Impact Event Calendar (omit placeholders!)
    clean_events = [e for e in draft.event_calendar if not any(ph in e for ph in ("僅在可靠搜尋", "本段不以模型記憶"))]
    if clean_events:
        lines.append(f"## {sec_num}. 重要事件與日程 (High-Impact Events)")
        sec_num += 1
        for item in clean_events:
            lines.append(f"- {item}")
        lines.append("")

    # Section 6: Watch Into Close / Next Report (omit placeholders!)
    clean_watch = [w for w in draft.watch_signals if "等待下一份" not in w]
    if clean_watch:
        lines.append(f"## {sec_num}. 後續觀察重點 (Watch Signals)")
        sec_num += 1
        for item in clean_watch:
            lines.append(f"- {item}")
        lines.append("")

    # Section 7: Data Notes (reader-facing note if degraded, NEVER ticker dumps)
    if context.degraded_mode:
        lines.append(f"## {sec_num}. 資料說明 (Data Notes)")
        sec_num += 1
        lines.append("- 部分外部即時新聞檢索受限，本報告行情數據均以交易所已驗證收盤價為準。")
        lines.append("")

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
