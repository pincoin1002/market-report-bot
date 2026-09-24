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


def derive_taiex_intraday_character(taiex: TaiexMarketSummary, prev_close: float | None = None) -> list[str]:
    lines = []
    if taiex.high is not None and taiex.low is not None:
        pc = prev_close or (taiex.close - taiex.point_change)
        if pc > 0:
            high_gain = taiex.high - pc
            close_gain = taiex.close - pc
            if high_gain > 50 and close_gain > 0:
                faded = taiex.high - taiex.close
                retrace_pct = (faded / high_gain) * 100
                retrace_tenths = int(round(retrace_pct / 10))
                if retrace_pct >= 25:
                    lines.append(f"盤中高點漲幅達 {(high_gain / pc) * 100:+.2f}%，終場回吐約 {retrace_tenths} 成漲幅，收斂至 {taiex.change_pct:+.2f}%。")
                elif retrace_pct <= 10:
                    lines.append(f"終場以近全日最高點作收（距高點僅差 {faded:,.2f} 點），買盤貫徹至尾盤。")
            elif taiex.close < pc and (pc - taiex.low) > 50:
                low_drop = pc - taiex.low
                rebound = taiex.close - taiex.low
                rebound_pct = (rebound / low_drop) * 100
                if rebound_pct >= 25:
                    lines.append(f"盤中低點跌幅達 {((taiex.low - pc) / pc) * 100:+.2f}%，尾盤自低點拉升 {rebound:,.2f} 點，跌幅收至 {taiex.change_pct:+.2f}%。")
    return lines


def derive_state_changes(context: MarketContext) -> list[str]:
    changes = []
    inst = context.institutional_flows
    taiex = context.taiex_summary

    if inst and inst.foreign_buy_sell_ntd_billions is not None:
        curr = inst.foreign_buy_sell_ntd_billions
        prev = inst.foreign_buy_sell_prev_ntd_billions
        if prev is not None:
            if prev < 0 and curr > 0:
                changes.append(f"外資現貨由賣轉買：前日賣超 {abs(prev):.2f} 億 $\\to$ 今日買超 {curr:.2f} 億台幣。")
            elif prev > 0 and curr < 0:
                changes.append(f"外資現貨由買轉賣：前日買超 {prev:.2f} 億 $\\to$ 今日賣超 {abs(curr):.2f} 億台幣。")
            elif curr > 0 and curr > prev + 50:
                changes.append(f"外資買超擴大：由 {prev:.2f} 億增至 {curr:.2f} 億台幣。")
            elif curr < 0 and curr < prev - 50:
                changes.append(f"外資賣超擴大：由 {abs(prev):.2f} 億擴至 {abs(curr):.2f} 億台幣。")
        else:
            action = "買超" if curr >= 0 else "賣超"
            changes.append(f"外資現貨單日{action} {abs(curr):.2f} 億台幣。")

    if inst and inst.foreign_futures_net_oi is not None:
        oi = inst.foreign_futures_net_oi
        chg = inst.foreign_futures_oi_change
        pos_str = f"淨空單 {abs(oi):,} 口" if oi < 0 else f"淨多單 {oi:,} 口"
        if chg is not None and abs(chg) >= 500:
            chg_str = f"增加 {abs(chg):,} 口" if (oi < 0 and chg < 0) or (oi > 0 and chg > 0) else f"減少 {abs(chg):,} 口"
            changes.append(f"外資台指期{pos_str}（較前日{chg_str}）。")
        else:
            changes.append(f"外資台指期維持{pos_str}。")

    if taiex and taiex.turnover_ntd_billions is not None and inst and inst.turnover_prev_ntd_billions is not None:
        curr_t = taiex.turnover_ntd_billions
        prev_t = inst.turnover_prev_ntd_billions
        t_delta = round(curr_t - prev_t, 2)
        if abs(t_delta) >= 100:
            dir_str = "擴增" if t_delta > 0 else "萎縮"
            changes.append(f"成交量{dir_str}：由前日 {prev_t:,.2f} 億{dir_str} {abs(t_delta):,.2f} 億至 {curr_t:,.2f} 億台幣。")

    if taiex:
        close_p = taiex.close
        prev_p = close_p - taiex.point_change
        increment = 1000.0 if close_p >= 10_000 else 100.0
        round_level = round(close_p / increment) * increment
        if min(prev_p, close_p) < round_level <= max(prev_p, close_p):
            verb = "站回" if close_p > prev_p else "跌破"
            changes.append(f"指數跨越當日鄰近整數關：收在 {close_p:,.2f} 點，{verb} {int(round_level):,} 點。")

    if taiex and taiex.advancing is not None and taiex.declining is not None:
        if taiex.advancing > taiex.declining * 1.5:
            changes.append(f"市場結構轉強：上漲 {taiex.advancing} 家明顯多於下跌 {taiex.declining} 家，多方擴散良好。")
        elif taiex.declining > taiex.advancing * 1.5:
            changes.append(f"市場結構轉弱：下跌 {taiex.declining} 家顯著多於上漲 {taiex.advancing} 家，權值獨撐廣度欠佳。")

    usd_obs = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
    if usd_obs and usd_obs.quality_status == "VALID":
        if abs(usd_obs.change_pct) >= 0.1:
            dir_str = usd_twd_direction_label(usd_obs.change_pct)
            changes.append(f"新台幣對美元走勢：終場{dir_str}至 {usd_obs.price:.3f}（變動 {usd_obs.change_pct:+.2f}%）。")

    return changes


def derive_tomorrow_watch_signals(context: MarketContext) -> list[str]:
    signals = []
    taiex = context.taiex_summary
    inst = context.institutional_flows

    if taiex:
        increment = 1000.0 if taiex.close >= 10_000 else 100.0
        support = (taiex.close // increment) * increment
        signals.append(
            f"加權指數：現值 {taiex.close:,.2f} 點；明日觀察是否守住動態參考 {support:,.0f} 點，失守即代表收盤強度未延續。"
        )
        if taiex.high and taiex.high > taiex.close + 30:
            signals.append(f"盤中高點反壓：今日高點 {taiex.high:,.2f} 點留有上影線賣壓，明日需量能維持方具消化動能。")

    if inst and inst.foreign_futures_net_oi is not None and inst.foreign_futures_net_oi < -30000:
        signals.append(f"外資期貨空單防線：目前淨空單 {abs(inst.foreign_futures_net_oi):,} 口仍處高位，觀察是否出現實質減倉以確認避險情緒放緩。")

    usd_obs = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
    if usd_obs and usd_obs.quality_status == "VALID":
        direction = usd_twd_direction_label(usd_obs.change_pct)
        signals.append(
            f"USD/TWD：現值 {usd_obs.price:.3f}、單日新台幣{direction}；觀察下一交易日是否延續。"
        )

    for event in context.event_facts:
        summary = event.get("summary") or event.get("event")
        if summary:
            signals.append(f"重要事件：關注「{summary}」之後續市場反應與定價。")

    return signals


def usd_twd_direction_label(change_pct: float) -> str:
    """USD/TWD up means TWD depreciates; down means TWD appreciates."""
    if change_pct > 0:
        return "貶值"
    if change_pct < 0:
        return "升值"
    return "持平"


def derive_evidence_supported_drivers(context: MarketContext) -> list[str]:
    """Deterministic evidence-ranked Taiwan driver candidates."""
    if not context.report_type.startswith("tw_"):
        return []

    drivers: list[str] = []
    taiex = context.quotes.get("TAIEX")
    major_symbols = ["2330", "2317", "2454", "2308", "2303", "2382", "3711"]
    components = [
        (symbol, context.quotes[symbol])
        for symbol in major_symbols
        if symbol in context.quotes and context.quotes[symbol].quality_status == "VALID"
    ]
    losers = sorted(
        [(symbol, obs) for symbol, obs in components if obs.change_pct < -0.3],
        key=lambda item: item[1].change_pct,
    )
    winners = sorted(
        [(symbol, obs) for symbol, obs in components if obs.change_pct > 0.3],
        key=lambda item: item[1].change_pct,
        reverse=True,
    )

    if losers:
        detail = "、".join(
            f"{resolve_instrument(symbol).display_name} ({symbol}) {obs.change_pct:+.2f}%"
            for symbol, obs in losers[:3]
        )
        drivers.append(f"OBSERVED — 權值壓力：{detail}。")
    if winners:
        detail = "、".join(
            f"{resolve_instrument(symbol).display_name} ({symbol}) {obs.change_pct:+.2f}%"
            for symbol, obs in winners[:3]
        )
        drivers.append(f"OBSERVED — 相對支撐：{detail}。")

    if taiex and taiex.quality_status == "VALID" and components:
        same_direction = [
            obs for _, obs in components
            if (taiex.change_pct < 0 and obs.change_pct < 0)
            or (taiex.change_pct > 0 and obs.change_pct > 0)
        ]
        if len(same_direction) >= max(2, len(components) // 2):
            direction = "走弱" if taiex.change_pct < 0 else "走強"
            drivers.append(
                f"SUPPORTED_ASSOCIATION — 多數已驗證大型權值與加權指數 "
                f"{taiex.change_pct:+.2f}% 同向{direction}；這是共振證據，不宣稱單一因果。"
            )

    usd_obs = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
    if usd_obs and usd_obs.quality_status == "VALID":
        drivers.append(
            f"OBSERVED — 匯率：USD/TWD {usd_obs.price:.3f}（{usd_obs.change_pct:+.2f}%；"
            f"新台幣{usd_twd_direction_label(usd_obs.change_pct)}）。"
        )

    summary = context.taiex_summary
    if summary and summary.advancing is not None and summary.declining is not None:
        drivers.append(
            f"OBSERVED — 市場廣度：上漲 {summary.advancing} 家、"
            f"下跌 {summary.declining} 家。"
        )
    else:
        drivers.append("UNRESOLVED — 市場廣度：缺少可驗證的漲跌家數證據。")

    flows = context.institutional_flows
    if flows and flows.foreign_buy_sell_ntd_billions is not None:
        action = "買超" if flows.foreign_buy_sell_ntd_billions >= 0 else "賣超"
        drivers.append(
            f"OBSERVED — 外資現貨：{action} {abs(flows.foreign_buy_sell_ntd_billions):.2f} 億台幣。"
        )
    else:
        drivers.append("UNRESOLVED — 法人流向：缺少可驗證的當日外資現貨資料。")

    return drivers[:6]


def derive_session_deltas(context: MarketContext) -> list[str]:
    """Session-over-session Taiwan deltas with explicit unresolved evidence."""
    if context.report_type != "tw_close":
        return derive_state_changes(context)

    changes: list[str] = []
    taiex_obs = context.quotes.get("TAIEX")
    movers: list[tuple[float, str, float]] = []
    for symbol in ("2330", "2317", "2454", "2308", "2303", "2382", "3711"):
        obs = context.quotes.get(symbol)
        if obs and obs.quality_status == "VALID":
            movers.append((abs(obs.change_pct), symbol, obs.change_pct))
    movers.sort(reverse=True)

    if taiex_obs and taiex_obs.quality_status == "VALID":
        point_delta = taiex_obs.price - taiex_obs.previous_regular_close
        line = (
            f"指數：上一完成交易日 {taiex_obs.previous_regular_close:,.2f} → "
            f"{taiex_obs.price:,.2f} 點（{point_delta:+,.2f}；{taiex_obs.change_pct:+.2f}%）"
        )
        if movers:
            detail = "、".join(
                f"{resolve_instrument(symbol).display_name} {change:+.2f}%"
                for _, symbol, change in movers[:3]
            )
            line += f"；權值變化最大：{detail}"
        changes.append(line + "。")
    else:
        changes.append("指數：UNRESOLVED（缺少兩個相鄰完成交易日的有效 TAIEX 收盤）。")

    summary = context.taiex_summary
    flows = context.institutional_flows
    if summary and summary.turnover_ntd_billions is not None and flows and flows.turnover_prev_ntd_billions is not None:
        delta = summary.turnover_ntd_billions - flows.turnover_prev_ntd_billions
        breadth_text = (
            f"；今日上漲 {summary.advancing} 家、下跌 {summary.declining} 家，"
            "前一交易日 breadth 未入 canonical snapshot"
            if summary.advancing is not None and summary.declining is not None
            else "；breadth UNRESOLVED"
        )
        changes.append(
            f"量能/廣度：成交值 {flows.turnover_prev_ntd_billions:,.2f} → "
            f"{summary.turnover_ntd_billions:,.2f} 億台幣（{delta:+,.2f} 億）"
            f"{breadth_text}。"
        )
    else:
        changes.append("量能/廣度：UNRESOLVED（缺少前一完成交易日成交值或 breadth 證據）。")

    if flows and flows.foreign_buy_sell_ntd_billions is not None:
        current = flows.foreign_buy_sell_ntd_billions
        if flows.foreign_buy_sell_prev_ntd_billions is not None:
            prev = flows.foreign_buy_sell_prev_ntd_billions
            text = (
                f"法人：外資現貨 {prev:+.2f} → {current:+.2f} 億台幣"
                f"（變化 {current - prev:+.2f} 億）"
            )
        else:
            text = f"法人：外資今日 {current:+.2f} 億台幣；前一交易日外資流量 UNRESOLVED"
        if flows.investment_trust_buy_sell_ntd_billions is not None or flows.dealer_buy_sell_ntd_billions is not None:
            text += "；投信/自營商缺少前一交易日基準，delta UNRESOLVED"
        changes.append(text + "。")
    else:
        changes.append("法人：UNRESOLVED（缺少可驗證的 session-over-session 法人資料）。")

    usd_obs = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
    if usd_obs and usd_obs.quality_status == "VALID":
        changes.append(
            f"USD/TWD：{usd_obs.previous_regular_close:.3f} → {usd_obs.price:.3f}"
            f"（{usd_obs.change_pct:+.2f}%；新台幣{usd_twd_direction_label(usd_obs.change_pct)}）。"
        )
    else:
        changes.append("USD/TWD：UNRESOLVED（缺少相鄰完成交易日的有效匯率觀察）。")

    coverage = context.portfolio_quote_coverage
    if coverage and coverage.is_full:
        changes.append(
            "持股相對 TAIEX：UNRESOLVED（report context 尚無跨幣別 canonical valuation return series；"
            "不以未換匯或部分持股估算）。"
        )
    elif coverage is not None:
        changes.append(
            "持股相對 TAIEX：UNRESOLVED（持股行情覆蓋未達完整，依 fail-closed 不計算）。"
        )
    else:
        changes.append("持股相對 TAIEX：UNRESOLVED（缺少 canonical portfolio coverage）。")

    return changes

def portfolio_report_section(context: MarketContext) -> OptionalModule | None:
    """A privacy-safe portfolio section in the same report render pass."""
    coverage = context.portfolio_quote_coverage
    if coverage is None:
        return None
    if coverage.status == "FULL":
        return OptionalModule(
            name="Portfolio Status", state="AVAILABLE",
            summary=f"持股行情覆蓋完整（{coverage.covered_positions}/{coverage.expected_positions}）；明細僅送往私人 Action Brief。",
        )
    if coverage.status == "NOT_APPLICABLE":
        return OptionalModule(name="Portfolio Status", state="UNAVAILABLE", summary="未取得可分析的 canonical active positions；未產生持股結論。")
    return OptionalModule(
        name="Portfolio Status", state="PARTIAL",
        summary=f"持股行情覆蓋 {coverage.covered_positions}/{coverage.expected_positions}（{coverage.coverage_ratio:.0%}）；私人持股結論已 fail-closed。",
    )


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
    if context.report_type == "tw_close":
        material = context.material_changes or derive_session_deltas(context)
        watch = derive_tomorrow_watch_signals(context)
    else:
        material = context.material_changes or build_material_changes(context)
        watch = material[:5]

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
        drivers=derive_evidence_supported_drivers(context),
        rotation="",
        event_calendar=[],
        optional_modules=optional,
        watch_signals=watch,
        data_quality=[f"{k}: {v}" for k, v in context.data_quality.items()],
        price_references=refs,
        taiex_summary=context.taiex_summary,
        institutional_flows=context.institutional_flows,
        portfolio_section=portfolio_report_section(context),
    )
    draft.rendered_markdown = render_public_report(draft, context, narrative)
    return draft



def _extract_grounded_drivers(narrative: str | None, context: MarketContext | None = None) -> list[str]:
    """Reduce model prose to bounded content, never a nested report.

    Gemini is asked for a report by the legacy prompts. Until those prompts
    are fully retired, only the first substantive prose after 今日一句話 is
    accepted into the structured draft. Headings, tables, diagnostics, and
    ungrounded numeric claims are strictly rejected.
    """
    if not narrative or "新聞搜尋目前不可用" in narrative:
        return []
    rejected_phrases = (
        "DATA_BLOCKED", "DATE_MISMATCH", "數據阻斷", "資料阻斷",
        "Smart Money", "強烈預期", "利空出盡", "資金回流",
        "Fed 升息 1 碼", "升息 1 碼", "升息1碼",
        "波段新高", "歷史新高", "創下新高",
        "三大法人合計買超", "三大法人", "資金佔大盤成交比重",
    )
    allowed_numbers: set[float] = set()
    if context:
        for p in context.market_date.split("-"):
            if p.isdigit():
                allowed_numbers.add(float(p))
        for n in range(1, 10):
            allowed_numbers.add(float(n))
        for symbol, obs in context.quotes.items():
            if symbol.isdigit():
                allowed_numbers.add(float(symbol))
            allowed_numbers.add(round(float(obs.price), 2))
            allowed_numbers.add(float(int(obs.price)))
            allowed_numbers.add(round(float(obs.previous_regular_close), 2))
            allowed_numbers.add(float(int(obs.previous_regular_close)))
            allowed_numbers.add(round(float(obs.change_pct), 2))
            allowed_numbers.add(round(abs(float(obs.change_pct)), 2))
            delta = round(abs(obs.price - obs.previous_regular_close), 2)
            allowed_numbers.add(delta)
            allowed_numbers.add(float(int(delta)))
        if context.taiex_summary:
            ts = context.taiex_summary
            for val in [ts.open, ts.high, ts.low, ts.close, ts.point_change, ts.change_pct,
                        ts.turnover_ntd_billions, ts.advancing, ts.declining, ts.unchanged]:
                if val is not None:
                    allowed_numbers.add(round(float(val), 2))
                    allowed_numbers.add(round(abs(float(val)), 2))
                    allowed_numbers.add(float(int(abs(val))))
        if context.institutional_flows:
            fl = context.institutional_flows
            for val in [fl.foreign_buy_sell_ntd_billions, fl.investment_trust_buy_sell_ntd_billions,
                        fl.dealer_buy_sell_ntd_billions, fl.total_buy_sell_ntd_billions,
                        fl.foreign_futures_net_oi, fl.foreign_futures_oi_change,
                        fl.foreign_buy_sell_prev_ntd_billions, fl.turnover_prev_ntd_billions]:
                if val is not None:
                    allowed_numbers.add(round(float(val), 2))
                    allowed_numbers.add(round(abs(float(val)), 2))
                    allowed_numbers.add(float(int(abs(val))))

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

        # Grounding check: numbers must exist in context
        if allowed_numbers:
            check_plain = re.sub(r"\([0-9A-Za-z.]+\)", "", plain)
            found_nums = re.findall(r"(?<![A-Za-z0-9_])(\d+(?:,\d+)*(?:\.\d+)?)(?![A-Za-z0-9_])", check_plain)
            ungrounded = False
            for raw_num in found_nums:
                clean_num = float(raw_num.replace(",", ""))
                if clean_num not in allowed_numbers:
                    ungrounded = True
                    break
            if ungrounded:
                continue

        drivers.append(plain[:600])
        break

    return drivers


def render_public_report(draft: MarketReportDraft, context: MarketContext,
                         narrative: str | None = None) -> str:
    if context.report_type == "tw_close":
        return _render_tw_close_report(draft, context)

    lines = [
        f"# {draft.headline}",
        "",
    ]
    sec_num = 1

    # Section 1: Executive Market State
    lines.append(f"## {sec_num}. 市場核心概況 (Executive Market State)")
    sec_num += 1

    has_meaningful_ts = any(
        (context.quotes.get(ref.canonical_symbol) and context.quotes[ref.canonical_symbol].provider_timestamp is not None)
        for ref in draft.price_references
    )

    if has_meaningful_ts:
        lines.append("| 標的 | 最新報價 | 漲跌幅 | 行情時間 | 狀態 |")
        lines.append("|---|---:|---:|---|---|")
    else:
        lines.append("| 標的 | 最新報價 | 漲跌幅 | 狀態 |")
        lines.append("|---|---:|---:|---|")

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
        if has_meaningful_ts:
            if obs.provider_timestamp:
                as_of_str = obs.provider_timestamp.strftime("%Y-%m-%d %H:%M %Z")
            else:
                as_of_str = "—"
            lines.append(f"| {display} | {price_str} | {obs.change_pct:+.2f}% | {as_of_str} | {session_label} |")
        else:
            lines.append(f"| {display} | {price_str} | {obs.change_pct:+.2f}% | {session_label} |")
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

    if draft.portfolio_section:
        lines.append(f"## {sec_num}. 持股資料狀態")
        lines.append(f"- {draft.portfolio_section.summary}")
        lines.append("")

    return "\n".join(lines).strip()


def _render_tw_close_report(draft: MarketReportDraft, context: MarketContext) -> str:
    """Render institutional-grade Taiwan market daily close brief (6 sections)."""
    lines = [
        f"# {draft.headline}",
        "",
    ]
    sec_num = 1
    taiex = context.taiex_summary
    inst = context.institutional_flows

    # Section 1: 今日市場 (Today's Market)
    lines.append(f"## {sec_num}. 今日市場")
    sec_num += 1

    # Render all price references in table format to guarantee table formatting and symbol provenance
    lines.append("| 標的 | 最新報價 | 漲跌幅 | 狀態 |")
    lines.append("|---|---:|---:|---|")
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
        lines.append(f"| {display} | {price_str} | {obs.change_pct:+.2f}% | {session_label} |")
    lines.append("")

    # Today's market character & stats
    if taiex:
        char_lines = derive_taiex_intraday_character(taiex)
        for cl in char_lines:
            lines.append(f"- {cl}")
        stats_parts = []
        if taiex.open is not None:
            stats_parts.append(f"開盤 {taiex.open:,.2f}")
        if taiex.high is not None and taiex.low is not None:
            stats_parts.append(f"區間 {taiex.low:,.2f}–{taiex.high:,.2f}")
        if taiex.turnover_ntd_billions is not None:
            stats_parts.append(f"成交量 {taiex.turnover_ntd_billions:,.2f} 億台幣")
        if stats_parts:
            lines.append(f"- 市場量價：{'、'.join(stats_parts)}。")
        if taiex.advancing is not None and taiex.declining is not None:
            unchanged_str = f"、平盤 {taiex.unchanged}" if taiex.unchanged is not None else ""
            lines.append(f"- 大盤廣度：上漲 {taiex.advancing} 家、下跌 {taiex.declining} 家{unchanged_str}。")
        lines.append("")

    # Section 2: 法人與資金 (Institutional & Liquidity) - omit if no data
    if inst and any(v is not None for v in (
        inst.foreign_buy_sell_ntd_billions,
        inst.investment_trust_buy_sell_ntd_billions,
        inst.dealer_buy_sell_ntd_billions,
        inst.total_buy_sell_ntd_billions,
        inst.foreign_futures_net_oi,
    )):
        lines.append(f"## {sec_num}. 法人與資金")
        sec_num += 1

        flows_parts = []
        if inst.foreign_buy_sell_ntd_billions is not None:
            act = "買超" if inst.foreign_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"外資{act} {abs(inst.foreign_buy_sell_ntd_billions):.2f} 億")
        if inst.investment_trust_buy_sell_ntd_billions is not None:
            act = "買超" if inst.investment_trust_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"投信{act} {abs(inst.investment_trust_buy_sell_ntd_billions):.2f} 億")
        if inst.dealer_buy_sell_ntd_billions is not None:
            act = "買超" if inst.dealer_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"自營商{act} {abs(inst.dealer_buy_sell_ntd_billions):.2f} 億")
        if inst.total_buy_sell_ntd_billions is not None:
            act = "買超" if inst.total_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"三大法人合計{act} {abs(inst.total_buy_sell_ntd_billions):.2f} 億台幣")

        if flows_parts:
            lines.append(f"- 三大法人現貨：{'，'.join(flows_parts)}。")

        if inst.foreign_futures_net_oi is not None:
            oi = inst.foreign_futures_net_oi
            pos_str = f"淨空單 {abs(oi):,} 口" if oi < 0 else f"淨多單 {oi:,} 口"
            if inst.foreign_futures_oi_change is not None:
                chg = inst.foreign_futures_oi_change
                c_act = "增持" if chg > 0 else "減持"
                lines.append(f"- 台指期部位：外資留倉為{pos_str}（單日{c_act} {abs(chg):,} 口）。")
            else:
                lines.append(f"- 台指期部位：外資留倉為{pos_str}。")

        usd_obs = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
        if usd_obs and usd_obs.quality_status == "VALID":
            fx_dir = usd_twd_direction_label(usd_obs.change_pct)
            lines.append(f"- 匯率動態：USD/TWD 收在 {usd_obs.price:.3f}（變動 {usd_obs.change_pct:+.2f}%，新台幣{fx_dir}）。")
        lines.append("")

    # Section 3: 權值與族群 (Key Components & Sectors)
    lines.append(f"## {sec_num}. 權值與族群")
    sec_num += 1

    component_summaries = []
    weight_symbols = ["2330", "2317", "2454", "2308", "2303", "2382", "3711"]
    for sym in weight_symbols:
        obs = context.quotes.get(sym)
        if obs and obs.quality_status == "VALID":
            spec = resolve_instrument(sym)
            component_summaries.append(f"{spec.display_name} ({sym}) {obs.price:,.2f} ({obs.change_pct:+.2f}%)")

    if component_summaries:
        lines.append(f"- 權值表現：{'、'.join(component_summaries[:5])}。")

    if "2330" in context.quotes and context.quotes["2330"].quality_status == "VALID":
        q2330 = context.quotes["2330"]
        delta = round(q2330.price - q2330.previous_regular_close, 2)
        dir_t = "上漲" if delta > 0 else "下跌"
        lines.append(f"- 台積電 (2330) 單日{dir_t} {abs(delta):,.2f} 元（{q2330.change_pct:+.2f}%），對大盤具關鍵指引動能。")

    if draft.rotation and not any(ph in draft.rotation for ph in ("僅列 verified quote", "弱資料模組不硬填")):
        lines.append(f"- 族群輪動：{draft.rotation}")
    lines.append("")

    # Section 4: 今日關鍵驅動 (Top Market Drivers) - omit if no drivers
    if draft.drivers:
        lines.append(f"## {sec_num}. 今日關鍵驅動")
        sec_num += 1
        for item in draft.drivers:
            lines.append(f"- {item}")
        lines.append("")

    # Section 5: 相較昨日 (What Changed Since Yesterday) - omit if no changes
    if draft.material_changes:
        lines.append(f"## {sec_num}. 相較昨日")
        sec_num += 1
        for item in draft.material_changes:
            lines.append(f"- {item}")
        lines.append("")

    # Section 6: 明日觀察 (Tomorrow's Watch Signals) - omit if no watch signals
    clean_watch = [w for w in draft.watch_signals if "等待下一份" not in w]
    if clean_watch:
        lines.append(f"## {sec_num}. 明日觀察")
        sec_num += 1
        for item in clean_watch:
            lines.append(f"- {item}")
        lines.append("")

    # Section 7: 資料說明 (Data Notes)
    if context.degraded_mode:
        lines.append(f"## {sec_num}. 資料說明")
        sec_num += 1
        lines.append("- 部分外部即時新聞檢索受限，本報告行情數據均以交易所已驗證收盤價為準。")
        lines.append("")

    if draft.portfolio_section:
        lines.append(f"## {sec_num}. 持股資料狀態")
        lines.append(f"- {draft.portfolio_section.summary}")
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
