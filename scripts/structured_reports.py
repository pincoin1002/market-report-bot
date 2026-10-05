#!/usr/bin/env python3
"""Structured report and private brief builders, validators, and renderers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from market_session import TPE, get_previous_completed_session_date, get_target_market_date, human_session_label
from models import (
    MarketContext, MarketReportDraft, OptionalModule, PortfolioActionBrief,
    PortfolioActionItem, PortfolioContext, PortfolioEventFact, PriceReference, Trigger,
)
from portfolio_analytics import calculate_portfolio_analytics, format_portfolio_section
from trigger_engine import technical_trigger

ROUNDING_TOLERANCE = 0.005
QUOTE_KINDS = {"CURRENT_QUOTE", "PREMARKET_QUOTE", "REGULAR_QUOTE", "AFTER_HOURS_QUOTE", "PREVIOUS_CLOSE"}
RETURN_EPSILON = 0.005


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


@dataclass(frozen=True)
class EvidenceCandidate:
    """Deterministic report claim with its evidence class and materiality."""
    classification: str
    materiality: float
    evidence: tuple[str, ...]
    text: str

    @property
    def rendered(self) -> str:
        return f"[{self.classification}] {self.text}"


def _valid_quote(context: MarketContext, symbol: str):
    observation = context.quotes.get(symbol) or context.macro_observations.get(symbol)
    return observation if observation and observation.quality_status == "VALID" else None


def _weight_return_text(symbol: str, observation) -> str:
    spec = resolve_instrument(symbol)
    name = f"{spec.display_name} ({symbol})" if spec.display_name != symbol else symbol
    return f"{name} {observation.change_pct:+.2f}%"


def return_direction(change: float, epsilon: float = RETURN_EPSILON) -> str:
    """Natural-language direction that cannot describe a zero move as down."""
    if change > epsilon:
        return "上漲"
    if change < -epsilon:
        return "下跌"
    return "持平"


def _reader_evidence_line(line: str) -> str | None:
    """Translate internal evidence labels at the final Telegram boundary."""
    match = re.match(r"^\[(OBSERVED|SUPPORTED_ASSOCIATION|UNRESOLVED)\]\s*(.*)$", line)
    if not match:
        clean = line
    else:
        classification, text = match.groups()
        if classification != "UNRESOLVED":
            clean = text
        elif text.startswith("Portfolio"):
            return None
        else:
            subject = text.split("：", 1)[0]
            translations = {
                "法人買賣超、成交金額、市場廣度": "官方市場統計尚未通過驗證，暫不判讀。",
                "成交金額": "今日官方成交統計尚未通過驗證，暫不判讀。",
                "市場廣度": "今日官方廣度統計尚未通過驗證，暫不判讀。",
                "三大法人": "今日官方法人資料尚未通過驗證，暫不判讀。",
                "加權指數": "缺少已驗證加權指數收盤，暫不判讀。",
                "TAIEX": "缺少已驗證加權指數收盤，暫不判讀。",
                "權值單日變化": "缺少已驗證權值收盤，暫不判讀。",
                "USD/TWD": "缺少已驗證匯率收盤，暫不判讀。",
            }
            clean = f"{subject}：{translations.get(subject, '今日資料尚未通過驗證，暫不判讀。')}"

    # Strip forbidden engineering terms if present
    clean = clean.replace("session-over-session", "單日變化")
    clean = clean.replace("前一已完成 TWSE session", "前一交易日")
    clean = clean.replace("較前一 session", "較前一交易日")
    clean = clean.replace("；比較基準為前一正式收盤。", "。")
    clean = clean.replace("；皆以各自前一正式收盤為基準。", "。")
    clean = clean.replace("；此為關聯性觀察，未推定單一因果。", "。")
    clean = clean.replace("億台幣", "億元")
    return clean.strip()


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
                changes.append(f"外資現貨由賣轉買：前日賣超 {abs(prev):.2f} 億 $\\to$ 今日買超 {curr:.2f} 億元。")
            elif prev > 0 and curr < 0:
                changes.append(f"外資現貨由買轉賣：前日買超 {prev:.2f} 億 $\\to$ 今日賣超 {abs(curr):.2f} 億元。")
            elif curr > 0 and curr > prev + 50:
                changes.append(f"外資買超擴大：由 {prev:.2f} 億增至 {curr:.2f} 億元。")
            elif curr < 0 and curr < prev - 50:
                changes.append(f"外資賣超擴大：由 {abs(prev):.2f} 億擴至 {abs(curr):.2f} 億元。")
        else:
            action = "買超" if curr >= 0 else "賣超"
            changes.append(f"外資現貨單日{action} {abs(curr):.2f} 億元。")

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
            changes.append(f"成交量{dir_str}：由前日 {prev_t:,.2f} 億{dir_str} {abs(t_delta):,.2f} 億至 {curr_t:,.2f} 億元。")

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

    if taiex and taiex.high and taiex.high > taiex.close + 30:
        signals.append(
            f"加權指數：今日高點 {taiex.high:,.2f} 點至收盤回落 {taiex.high - taiex.close:,.2f} 點；"
            "明日觀察是否能收斂盤中賣壓。"
        )

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
    """Build ranked Taiwan-close driver claims from validated observations.

    A driver is never a model-inferred cause: every line is classified and
    carries an internal list of quote/statistic IDs.  The renderer receives
    only the deterministic, reader-facing text.
    """
    if context.report_type != "tw_close":
        return []

    candidates: list[EvidenceCandidate] = []
    taiex = _valid_quote(context, "TAIEX")
    if taiex:
        candidates.append(EvidenceCandidate(
            "OBSERVED", abs(taiex.change_pct), (taiex.quote_id,),
            f"市場結果：加權指數收在 {taiex.price:,.2f} 點（{taiex.change_pct:+.2f}%）。",
        ))

    weighted = []
    for symbol in ("2330", "2317", "2454", "2308", "2382", "2303", "3711"):
        if observation := _valid_quote(context, symbol):
            weighted.append((symbol, observation))
    laggards = sorted(
        [(symbol, observation) for symbol, observation in weighted if observation.change_pct <= -0.5],
        key=lambda item: item[1].change_pct,
    )
    leaders = sorted(
        [(symbol, observation) for symbol, observation in weighted if observation.change_pct >= 0.5],
        key=lambda item: item[1].change_pct,
        reverse=True,
    )
    if len(laggards) >= 2:
        candidates.append(EvidenceCandidate(
            "SUPPORTED_ASSOCIATION",
            sum(abs(observation.change_pct) for _, observation in laggards[:3]),
            tuple(observation.quote_id for _, observation in laggards[:3]),
            "權值壓力：" + "、".join(_weight_return_text(symbol, observation) for symbol, observation in laggards[:3])
            + "；多個大型權值同步走弱，與加權指數表現一致；此為關聯性觀察，未推定單一因果。",
        ))
    if len(leaders) >= 2:
        if taiex and taiex.change_pct < 0:
            association = "；少數大型權值逆勢上漲，部分抵銷權值跌勢；此為關聯性觀察，未推定單一因果。"
        elif taiex and taiex.change_pct > 0:
            association = "；多個大型權值同步上漲，與加權指數表現一致；此為關聯性觀察，未推定單一因果。"
        else:
            association = "；大型權值表現穩健；此為關聯性觀察，未推定單一因果。"
        candidates.append(EvidenceCandidate(
            "SUPPORTED_ASSOCIATION",
            sum(abs(observation.change_pct) for _, observation in leaders[:3]),
            tuple(observation.quote_id for _, observation in leaders[:3]),
            "相對支撐：" + "、".join(_weight_return_text(symbol, observation) for symbol, observation in leaders[:3])
            + association,
        ))

    if usd_twd := _valid_quote(context, "USDTWD"):
        candidates.append(EvidenceCandidate(
            "OBSERVED", abs(usd_twd.change_pct), (usd_twd.quote_id,),
            f"匯率：USD/TWD 收在 {usd_twd.price:.3f}（{usd_twd.change_pct:+.2f}%），新台幣{usd_twd_direction_label(usd_twd.change_pct)}。",
        ))

    taiex = context.taiex_summary
    inst = context.institutional_flows
    missing_stats = []
    if not inst or inst.foreign_buy_sell_ntd_billions is None:
        missing_stats.append("法人買賣超")
    if not taiex or taiex.turnover_ntd_billions is None:
        missing_stats.append("成交金額")
    if not taiex or taiex.advancing is None or taiex.declining is None:
        missing_stats.append("市場廣度")
    if missing_stats:
        candidates.append(EvidenceCandidate(
            "UNRESOLVED", 0.0, (),
            f"{'、'.join(missing_stats)}：UNRESOLVED（本次快照未提供已驗證交易所統計）。",
        ))

    rank = {"SUPPORTED_ASSOCIATION": 2, "OBSERVED": 1, "UNRESOLVED": 0}
    candidates.sort(key=lambda item: (-rank[item.classification], -item.materiality, item.text))
    return [candidate.rendered for candidate in candidates[:6]]


def derive_tw_session_deltas(context: MarketContext) -> list[str]:
    """Compare only the current and previous completed TWSE sessions."""
    if context.report_type != "tw_close":
        return []
    deltas: list[str] = []
    taiex = _valid_quote(context, "TAIEX")

    if taiex:
        point_delta = taiex.price - taiex.previous_regular_close
        deltas.append(
            f"[OBSERVED] TAIEX session-over-session：收在 {taiex.price:,.2f} 點，較前一已完成 TWSE session "
            f"{point_delta:+,.2f} 點（{taiex.change_pct:+.2f}%）；比較基準為前一正式收盤。"
        )
    else:
        deltas.append("[UNRESOLVED] TAIEX session-over-session：UNRESOLVED（缺少已驗證 current TWSE close）。")

    weighted = []
    for symbol in ("2330", "2317", "2454", "2308", "2382", "2303", "3711"):
        if observation := _valid_quote(context, symbol):
            weighted.append((symbol, observation))
    if weighted:
        weighted.sort(key=lambda item: abs(item[1].change_pct), reverse=True)
        deltas.append(
            "[OBSERVED] 權值單日變化："
            + "、".join(_weight_return_text(symbol, observation) for symbol, observation in weighted[:3])
            + "；皆以各自前一正式收盤為基準。"
        )
    else:
        deltas.append("[UNRESOLVED] 權值單日變化：UNRESOLVED（缺少已驗證權值正式收盤）。")

    if usd_twd := _valid_quote(context, "USDTWD"):
        deltas.append(
            f"[OBSERVED] USD/TWD session-over-session：收在 {usd_twd.price:.3f}，變動 {usd_twd.change_pct:+.2f}%；"
            f"新台幣{usd_twd_direction_label(usd_twd.change_pct)}。"
        )
    else:
        deltas.append("[UNRESOLVED] USD/TWD session-over-session：UNRESOLVED（缺少已驗證匯率收盤）。")

    taiex_summary = context.taiex_summary
    inst = context.institutional_flows
    if taiex_summary and taiex_summary.turnover_ntd_billions is not None and inst and inst.turnover_prev_ntd_billions is not None:
        turnover_delta = taiex_summary.turnover_ntd_billions - inst.turnover_prev_ntd_billions
        deltas.append(
            f"[OBSERVED] 成交金額：{taiex_summary.turnover_ntd_billions:,.2f} 億台幣，較前一 session {turnover_delta:+,.2f} 億。"
        )
    else:
        deltas.append("[UNRESOLVED] 成交金額：UNRESOLVED（缺少 current 與前一已完成 TWSE session 的已驗證統計）。")

    if (taiex_summary and taiex_summary.advancing is not None and taiex_summary.declining is not None
            and taiex_summary.advancing_prev is not None and taiex_summary.declining_prev is not None):
        net = taiex_summary.advancing - taiex_summary.declining
        previous_net = taiex_summary.advancing_prev - taiex_summary.declining_prev
        deltas.append(
            f"[OBSERVED] 市場廣度：上漲 {taiex_summary.advancing} 家、下跌 {taiex_summary.declining} 家；"
            f"淨廣度 {net:+d} 家，較前一 session {net - previous_net:+d} 家。"
        )
    else:
        deltas.append("[UNRESOLVED] 市場廣度：UNRESOLVED（缺少 current 與前一已完成 TWSE session 的已驗證統計）。")

    if (inst and inst.foreign_buy_sell_ntd_billions is not None and inst.foreign_buy_sell_prev_ntd_billions is not None
            and inst.investment_trust_buy_sell_ntd_billions is not None and inst.investment_trust_buy_sell_prev_ntd_billions is not None
            and inst.dealer_buy_sell_ntd_billions is not None and inst.dealer_buy_sell_prev_ntd_billions is not None):
        foreign_delta = inst.foreign_buy_sell_ntd_billions - inst.foreign_buy_sell_prev_ntd_billions
        trust_delta = inst.investment_trust_buy_sell_ntd_billions - inst.investment_trust_buy_sell_prev_ntd_billions
        dealer_delta = inst.dealer_buy_sell_ntd_billions - inst.dealer_buy_sell_prev_ntd_billions
        deltas.append(
            f"[OBSERVED] 三大法人：外資 {inst.foreign_buy_sell_ntd_billions:+.2f} 億（較前一 session {foreign_delta:+.2f} 億）、"
            f"投信 {inst.investment_trust_buy_sell_ntd_billions:+.2f} 億（{trust_delta:+.2f} 億）、"
            f"自營商 {inst.dealer_buy_sell_ntd_billions:+.2f} 億（{dealer_delta:+.2f} 億）。"
        )
    else:
        deltas.append("[UNRESOLVED] 三大法人：UNRESOLVED（缺少外資、投信、自營商 current 與前一 session 的完整已驗證統計）。")

    deltas.append("[UNRESOLVED] Portfolio coverage change：UNRESOLVED（沒有前一已完成 TWSE session 的 portfolio coverage artifact）。")
    deltas.append("[UNRESOLVED] Portfolio relative performance：UNRESOLVED（尚無滿足既有信心合約的跨幣別 canonical portfolio valuation evidence）。")
    return deltas


def portfolio_report_section(context: MarketContext) -> OptionalModule | None:
    """A privacy-safe portfolio section in the same report render pass."""
    coverage = context.portfolio_quote_coverage
    if coverage is None:
        return None
    if coverage.status == "FULL":
        return OptionalModule(
            name="Portfolio Status", state="AVAILABLE",
            summary=f"行情覆蓋：{coverage.covered_positions}/{coverage.expected_positions} FULL",
        )
    if coverage.status == "NOT_APPLICABLE":
        return OptionalModule(name="Portfolio Status", state="UNAVAILABLE", summary="未取得可分析的有效持股；未產生持股結論。")
    unresolved = [*coverage.stale, *coverage.missing, *coverage.unsupported]
    reasons = []
    for item in unresolved:
        reason = item.reason
        if "differs from expected" in reason or "session" in reason:
            r_short = "session mismatch"
        elif item.state == "UNSUPPORTED":
            r_short = "unsupported provider"
        else:
            r_short = "provider failure"
        reasons.append(f"{item.canonical_symbol}（{r_short}）")
    detail = f"\n- 未取得行情：{'、'.join(reasons)}" if reasons else ""
    return OptionalModule(
        name="Portfolio Status", state="PARTIAL",
        summary=f"行情覆蓋：{coverage.covered_positions}/{coverage.expected_positions} — 操作建議暫停{detail}",
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
    headline_date = context.market_date
    if context.report_type == "tw_open":
        # The report is for today's pre-open decision window even though the
        # Taiwan quotes inside it intentionally reference the prior completed
        # TWSE session. Keep those concepts distinct in the user-facing title.
        headline_date = context.generated_at.astimezone(TPE).strftime("%Y-%m-%d")
        session_label = "開盤前參考"
    if context.report_type == "tw_close":
        material = context.material_changes or derive_tw_session_deltas(context)
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
        headline=f"{title} {headline_date}｜{session_label}",
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
            if rendered := _reader_evidence_line(item):
                lines.append(f"- {rendered}")
        lines.append("")

    # Section 3: Top Market Drivers
    if draft.drivers:
        lines.append(f"## {sec_num}. 今日走勢與市場驅動 (Top Market Drivers)")
        sec_num += 1
        for item in draft.drivers:
            if rendered := _reader_evidence_line(item):
                lines.append(f"- {rendered}")
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
    """Render a compact, reader-facing Taiwan close Telegram brief."""
    lines = [
        f"# 📊 台股收盤｜{context.market_date}",
        "",
    ]
    taiex = context.taiex_summary
    inst = context.institutional_flows

    lines.append("## 【今天一句話】")
    if taiex:
        summary = f"加權指數{return_direction(taiex.point_change)} {abs(taiex.point_change):,.2f} 點，收在 {taiex.close:,.2f} 點（{taiex.change_pct:+.2f}%）"
        if taiex.turnover_ntd_billions is not None:
            summary += f"；成交金額 {taiex.turnover_ntd_billions:,.2f} 億元"
        if taiex.advancing is not None and taiex.declining is not None:
            summary += f"，上漲／下跌家數 {taiex.advancing}/{taiex.declining}"
        lines.append(f"- {summary}。")
    elif draft.drivers:
        lines.append(f"- {_reader_evidence_line(draft.drivers[0]) or draft.drivers[0]}")
    else:
        lines.append("- 官方收盤行情已完成驗證；未取得足以支持額外市場判讀的資料。")
    lines.append("")

    lines.append("## 【市場】")

    # Keep the compact table: every public numeric price remains tied to its quote observation.
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
            stats_parts.append(f"成交金額 {taiex.turnover_ntd_billions:,.2f} 億元")
        if stats_parts:
            lines.append(f"- 市場量價：{'、'.join(stats_parts)}。")
        if taiex.advancing is not None and taiex.declining is not None:
            unchanged_str = f"、平盤 {taiex.unchanged}" if taiex.unchanged is not None else ""
            lines.append(f"- 大盤廣度：上漲 {taiex.advancing} 家、下跌 {taiex.declining} 家{unchanged_str}。")
    usd_obs = context.quotes.get("USDTWD") or context.macro_observations.get("USDTWD")
    if usd_obs and usd_obs.quality_status == "VALID":
        fx_dir = usd_twd_direction_label(usd_obs.change_pct)
        lines.append(f"- USD/TWD：{usd_obs.price:.3f}（{usd_obs.change_pct:+.2f}%），新台幣{fx_dir}。")
    lines.append("")

    lines.append("## 【今天盤面重點】")
    component_summaries = []
    weight_symbols = ["2330", "2317", "2454", "2308", "2303", "2382", "3711"]
    for sym in weight_symbols:
        obs = context.quotes.get(sym)
        if obs and obs.quality_status == "VALID":
            spec = resolve_instrument(sym)
            component_summaries.append(f"{spec.display_name} ({sym}) {obs.change_pct:+.2f}%")
    if component_summaries:
        lines.append(f"- 權值股：{'、'.join(component_summaries[:5])}。")
    if "2330" in context.quotes and context.quotes["2330"].quality_status == "VALID":
        q2330 = context.quotes["2330"]
        delta = round(q2330.price - q2330.previous_regular_close, 2)
        direction = return_direction(delta)
        if direction == "持平":
            lines.append(f"- 台積電 (2330) 單日持平（{q2330.change_pct:+.2f}%）。")
        else:
            lines.append(f"- 台積電 (2330) 單日{direction} {abs(delta):,.2f} 元（{q2330.change_pct:+.2f}%）。")
    for item in draft.drivers:
        if rendered := _reader_evidence_line(item):
            # The market block already states the index result and FX level;
            # retain only distinct decision-relevant driver observations here.
            if rendered.startswith(("市場結果：", "匯率：")):
                continue
            lines.append(f"- {rendered}")

    if inst and any(v is not None for v in (
        inst.foreign_buy_sell_ntd_billions,
        inst.investment_trust_buy_sell_ntd_billions,
        inst.dealer_buy_sell_ntd_billions,
        inst.total_buy_sell_ntd_billions,
        inst.foreign_futures_net_oi,
    )):
        flows_parts = []
        if inst.foreign_buy_sell_ntd_billions is not None:
            act = "買超" if inst.foreign_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"外資{act} {abs(inst.foreign_buy_sell_ntd_billions):.2f} 億元")
        if inst.investment_trust_buy_sell_ntd_billions is not None:
            act = "買超" if inst.investment_trust_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"投信{act} {abs(inst.investment_trust_buy_sell_ntd_billions):.2f} 億元")
        if inst.dealer_buy_sell_ntd_billions is not None:
            act = "買超" if inst.dealer_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"自營商{act} {abs(inst.dealer_buy_sell_ntd_billions):.2f} 億元")
        if inst.total_buy_sell_ntd_billions is not None:
            act = "買超" if inst.total_buy_sell_ntd_billions >= 0 else "賣超"
            flows_parts.append(f"三大法人合計{act} {abs(inst.total_buy_sell_ntd_billions):.2f} 億元")

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

    if draft.rotation and not any(ph in draft.rotation for ph in ("僅列 verified quote", "弱資料模組不硬填")):
        lines.append(f"- 族群輪動：{draft.rotation}")
    lines.append("")

    if draft.material_changes:
        previous_date = (
            taiex.previous_session_date if taiex and taiex.previous_session_date
            else get_previous_completed_session_date("TW", context.market_date)
        )
        lines.append(f"## 【相較前一交易日 {previous_date}】")
        for item in draft.material_changes:
            if rendered := _reader_evidence_line(item):
                lines.append(f"- {rendered}")
        lines.append("")

    if draft.portfolio_section:
        # Quote coverage and portfolio performance are different contracts.
        # The current snapshot mixes TW completed-session closes, the latest
        # completed US session, and continuous crypto observations.  Until a
        # common cutoff-to-cutoff valuation series exists, aggregating each
        # instrument's own previous close into one portfolio return is not an
        # economically comparable measurement interval.
        lines.append("## 【我的持股】")
        lines.append(f"- {draft.portfolio_section.summary}")
        lines.append("- 跨市場部位目前沒有統一的起訖估值時間，因此暫不顯示整體損益、貢獻度或相對大盤績效。")
        lines.append("")

    clean_watch = [w for w in draft.watch_signals if "等待下一份" not in w]
    if clean_watch:
        lines.append("## 【明日觀察】")
        for item in clean_watch[:3]:
            lines.append(f"- {item}")
        lines.append("")

    if context.degraded_mode:
        lines.append("## 【資料說明】")
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


def build_action_brief(
    context: MarketContext,
    portfolio: PortfolioContext,
    verified_events: list[dict | PortfolioEventFact] | None = None,
    upcoming_events: list[dict | str] | None = None,
) -> PortfolioActionBrief:
    raw_events = verified_events if verified_events is not None else getattr(context, "event_facts", [])
    events_by_id: dict[str, list[dict | PortfolioEventFact]] = {}
    for evt in raw_events:
        if isinstance(evt, PortfolioEventFact):
            keys = {evt.ticker.upper(), evt.instrument_id.upper()}
        else:
            keys = {
                str(evt.get("ticker") or "").upper(),
                str(evt.get("instrument_id") or "").upper(),
            }
        for key in keys:
            if key:
                events_by_id.setdefault(key, []).append(evt)

    items: list[PortfolioActionItem] = []
    data_issues: list[str] = []

    for pos in portfolio.positions:
        pos_events = events_by_id.get(pos.instrument_id.upper(), []) or events_by_id.get(pos.ticker.upper(), [])
        obs = context.quotes.get(pos.instrument_id) or context.quotes.get(pos.ticker)
        if not obs or obs.quality_status != "VALID":
            # Price evidence and event evidence are independent. A quote outage
            # must not erase a verified material company/instrument event.
            material_events = [
                e for e in pos_events
                if (e.event_status if isinstance(e, PortfolioEventFact) else e.get("event_status")) == "EVENT_MATERIAL_FOUND"
            ]
            failed_events = [
                e for e in pos_events
                if (e.event_status if isinstance(e, PortfolioEventFact) else e.get("event_status")) == "EVENT_CHECK_FAILED"
            ]
            clean_events = [
                e for e in pos_events
                if (e.event_status if isinstance(e, PortfolioEventFact) else e.get("event_status")) == "EVENT_CHECKED_NO_MATERIAL_CHANGE"
            ]

            if material_events:
                evt_data = material_events[0]
                severity = evt_data.severity if isinstance(evt_data, PortfolioEventFact) else evt_data.get("severity", "LOW")
                status = "ACTION_REVIEW" if severity in ("HIGH", "CRITICAL") else "WATCH"
                event_status = "EVENT_MATERIAL_FOUND"
                summary = evt_data.summary if isinstance(evt_data, PortfolioEventFact) else (evt_data.get("summary") or "已驗證重大事件需要關注。")
                next_step = "行情資料目前未通過驗證；先依事件本身重新檢視投資邏輯，不做精確交易判斷。"
                verified_event = evt_data
                reason_codes = ["QUOTE_UNAVAILABLE_OR_INVALID", "MATERIAL_COMPANY_EVENT"]
            elif failed_events:
                status = "DATA_BLOCKED"
                event_status = "EVENT_CHECK_FAILED"
                summary = "持股行情未通過驗證，且公司事件資料本次檢查失敗。"
                next_step = "待行情與事件資料修復後重新檢視。"
                verified_event = failed_events[0]
                reason_codes = ["QUOTE_UNAVAILABLE_OR_INVALID", "EVENT_CHECK_FAILED"]
            elif clean_events:
                status = "DATA_BLOCKED"
                event_status = "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                summary = "公司事件已完成檢查且未發現重大事件；但持股行情未通過驗證。"
                next_step = "待行情資料修復後重新檢視。"
                verified_event = clean_events[0]
                reason_codes = ["QUOTE_UNAVAILABLE_OR_INVALID", "NO_MATERIAL_EVENT"]
            else:
                status = "DATA_BLOCKED"
                event_status = "EVENT_UNCHECKED"
                summary = "持股行情未通過驗證；公司事件面亦尚未完成驗證。"
                next_step = "待行情資料修復後重新檢視。"
                verified_event = None
                reason_codes = ["QUOTE_UNAVAILABLE_OR_INVALID", "EVENT_UNCHECKED"]

            item = PortfolioActionItem(
                instrument_id=pos.instrument_id,
                ticker=pos.ticker,
                status=status,
                price_status="DATA_BLOCKED",
                event_status=event_status,
                reason_codes=reason_codes,
                summary=summary,
                next_step=next_step,
                verified_event=verified_event,
            )
            data_issues.append(f"{pos.ticker}: quote unavailable or invalid")
            items.append(item)
            continue

        change_pct = obs.change_pct
        if abs(change_pct) >= 7.0:
            price_status = "PRICE_WATCH"
            price_reasons = ["LARGE_DAILY_MOVE"]
            price_summary = "單日波動達監控門檻，需追蹤是否伴隨基本面事件。"
            trigger = technical_trigger(
                pos.ticker,
                "previous regular close move",
                obs.previous_regular_close,
                obs.market_date,
                obs.quote_id,
            )
        else:
            price_status = "PRICE_NORMAL"
            price_reasons = ["PRICE_NORMAL"]
            price_summary = "今日價格未觸發監控門檻；公司事件面尚未完成驗證。"
            trigger = None

        pos_events = events_by_id.get(pos.instrument_id.upper(), []) or events_by_id.get(pos.ticker.upper(), [])
        evt_data: dict | PortfolioEventFact | None = None

        if pos_events:
            failed_evts = [
                e for e in pos_events
                if (e.event_status if isinstance(e, PortfolioEventFact) else e.get("event_status")) == "EVENT_CHECK_FAILED"
            ]

            def _is_material(e):
                st = e.event_status if isinstance(e, PortfolioEventFact) else e.get("event_status")
                if st == "EVENT_MATERIAL_FOUND":
                    return True
                if isinstance(e, dict) and (e.get("status") in ("ACTION_REVIEW", "WATCH") or e.get("material")):
                    return True
                return False

            def _is_action(e):
                if not _is_material(e):
                    return False
                if getattr(e, "severity", "") == "HIGH":
                    return True
                if isinstance(e, dict) and (e.get("status") == "ACTION_REVIEW" or e.get("severity") == "HIGH" or e.get("action_review")):
                    return True
                return False

            action_evts = [e for e in pos_events if _is_action(e)]
            watch_evts = [e for e in pos_events if _is_material(e)]
            clean_evts = [
                e for e in pos_events
                if (e.event_status if isinstance(e, PortfolioEventFact) else e.get("event_status")) == "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                or (isinstance(e, dict) and e.get("status") == "NO_MATERIAL_CHANGE")
            ]

            if action_evts:
                evt_data = action_evts[0]
                event_status = "EVENT_MATERIAL_FOUND"
                event_decision = "ACTION_REVIEW"
                code = (getattr(evt_data, "reason_code", None) or (evt_data.get("reason_code") if isinstance(evt_data, dict) else None)) or "MATERIAL_COMPANY_EVENT"
                event_reasons = [code]
                event_summary = evt_data.summary if isinstance(evt_data, PortfolioEventFact) else (evt_data.get("summary") or "公司出現重大已驗證事件，建議重新檢視。")
            elif watch_evts:
                evt_data = watch_evts[0]
                event_status = "EVENT_MATERIAL_FOUND"
                event_decision = "WATCH"
                code = (getattr(evt_data, "reason_code", None) or (evt_data.get("reason_code") if isinstance(evt_data, dict) else None)) or "COMPANY_EVENT_WATCH"
                event_reasons = [code]
                event_summary = evt_data.summary if isinstance(evt_data, PortfolioEventFact) else (evt_data.get("summary") or "公司出現已驗證事件，需持續關注後續影響。")
            elif clean_evts:
                evt_data = clean_evts[0]
                event_status = "EVENT_CHECKED_NO_MATERIAL_CHANGE"
                event_decision = "NO_MATERIAL_CHANGE"
                event_reasons = ["NO_MATERIAL_EVENT"]
                event_summary = evt_data.summary if isinstance(evt_data, PortfolioEventFact) else (evt_data.get("summary") or "行情與公司事件均已驗證，未發現重大異常。")
            elif failed_evts:
                evt_data = failed_evts[0]
                event_status = "EVENT_CHECK_FAILED"
                event_decision = None
                event_reasons = ["EVENT_CHECK_FAILED"]
                event_summary = "事件資料本次未完成驗證。"
                data_issues.append(f"{pos.ticker}：事件資料本次未完成驗證。")
            else:
                event_status = "EVENT_UNCHECKED"
                event_decision = None
                event_reasons = []
                event_summary = ""
        else:
            event_status = "EVENT_UNCHECKED"
            event_decision = None
            event_reasons = []
            event_summary = ""

        if event_decision == "ACTION_REVIEW":
            status = "ACTION_REVIEW"
            reasons = event_reasons
            summary = event_summary
            next_step = "重新檢視投資邏輯；目前可用現金與配置上限尚未完成驗證，不直接提供精確買賣股數。"
        elif price_status == "PRICE_WATCH":
            status = "WATCH"
            if event_decision == "WATCH":
                reasons = ["LARGE_DAILY_MOVE"] + event_reasons
                summary = f"{price_summary} 且伴隨已驗證事件：{event_summary}"
                next_step = "確認是否有財報、營運展望、產品或監管事件支持此次波動；目前不直接產生交易結論。"
            else:
                reasons = ["LARGE_DAILY_MOVE"]
                summary = price_summary
                next_step = "確認是否有財報、營運展望、產品或監管事件支持此次波動；目前事件面尚未完成驗證，不直接產生交易結論。"
        elif event_decision == "WATCH":
            status = "WATCH"
            reasons = event_reasons
            summary = event_summary
            next_step = "持續關注後續影響與市場定價。"
        else:
            status = "NO_MATERIAL_CHANGE"
            if event_status == "EVENT_CHECKED_NO_MATERIAL_CHANGE":
                reasons = ["PRICE_NORMAL", "NO_MATERIAL_EVENT"]
                summary = event_summary or "行情與公司事件均已驗證，未發現重大異常。"
            elif event_status == "EVENT_CHECK_FAILED":
                reasons = ["PRICE_NORMAL", "EVENT_CHECK_FAILED"]
                summary = "今日價格未觸發監控門檻；公司事件面檢查失敗，尚未完成驗證。"
            else:
                reasons = ["PRICE_NORMAL", "EVENT_UNCHECKED"]
                summary = "今日價格未觸發監控門檻；公司事件面尚未完成驗證。"
            next_step = ""

        item = PortfolioActionItem(
            instrument_id=pos.instrument_id,
            ticker=pos.ticker,
            status=status,
            quote_id=obs.quote_id,
            reference_price=obs.price,
            change_pct=change_pct,
            session=obs.session,
            as_of=obs.provider_timestamp or obs.retrieved_at,
            reason_codes=reasons,
            summary=summary,
            next_step=next_step,
            trigger=trigger,
            price_status=price_status,
            event_status=event_status,
            verified_event=evt_data,
        )
        items.append(item)

    if upcoming_events is not None:
        upcoming_events_list = list(upcoming_events)
    else:
        upcoming_events_list = []
        portfolio_tickers = {p.ticker.upper() for p in portfolio.positions} | {p.instrument_id.upper() for p in portfolio.positions}
        for evt in raw_events:
            sym = (evt.ticker if isinstance(evt, PortfolioEventFact) else (evt.get("instrument_id") or evt.get("ticker") or "")).upper()
            is_upcoming = getattr(evt, "is_upcoming", False) if isinstance(evt, PortfolioEventFact) else (
                evt.get("is_upcoming", False)
                or evt.get("event_type") in ("EARNINGS", "CALL", "CONFERENCE", "UPCOMING")
                or "upcoming" in evt.get("category", "").lower()
            )
            if is_upcoming and (not sym or sym in portfolio_tickers):
                if isinstance(evt, PortfolioEventFact):
                    parts = [evt.ticker, evt.title or evt.event_type or "事件", evt.event_date or "", evt.summary or evt.impact or ""]
                    upcoming_events_list.append("｜".join([p for p in parts if p]))
                elif isinstance(evt, dict):
                    t = evt.get("ticker") or sym
                    e = evt.get("event") or evt.get("title") or evt.get("summary")
                    d = evt.get("date") or evt.get("event_date") or evt.get("timing") or ""
                    imp = evt.get("why") or evt.get("impact") or ""
                    parts = [p for p in (t, e, d, imp) if p]
                    upcoming_events_list.append("｜".join(parts) if parts else str(evt))
                else:
                    upcoming_events_list.append(str(evt))

    total_positions = len(portfolio.positions)
    covered_positions = len([i for i in items if i.price_status != "DATA_BLOCKED"])

    event_total = len(portfolio.positions)
    event_checked = len([i for i in items if i.event_status in ("EVENT_CHECKED_NO_MATERIAL_CHANGE", "EVENT_MATERIAL_FOUND")])
    event_failed = len([i for i in items if i.event_status == "EVENT_CHECK_FAILED"])
    event_cov_ratio = round(event_checked / event_total, 4) if event_total > 0 else 0.0
    is_fully_verified = (event_checked == event_total and event_total > 0)

    return PortfolioActionBrief(
        run_id=context.run_id,
        as_of=context.generated_at,
        market_session=context.market_session,
        data_quality=[f"{k}: {v}" for k, v in context.data_quality.items()],
        action_queue=[i for i in items if i.status == "ACTION_REVIEW"],
        watchlist=[i for i in items if i.status == "WATCH"],
        no_material_change=[i for i in items if i.status == "NO_MATERIAL_CHANGE"],
        upcoming_events=upcoming_events_list,
        data_issues=data_issues,
        events_verified=is_fully_verified,
        covered_positions=covered_positions,
        total_positions=total_positions,
        event_checked_positions=event_checked,
        event_total_positions=event_total,
        event_check_failed_positions=event_failed,
        event_coverage_ratio=event_cov_ratio,
        event_facts=[e for e in raw_events if isinstance(e, PortfolioEventFact)],
    )


def render_action_brief(brief: PortfolioActionBrief) -> str:
    tpe = ZoneInfo("Asia/Taipei")
    as_of_dt = brief.as_of.astimezone(tpe) if brief.as_of.tzinfo else brief.as_of
    as_of_str = as_of_dt.strftime("%Y-%m-%d %H:%M")

    session_map = {
        "PREVIOUS_CLOSE": "前一交易日收盤資料",
        "REGULAR": "常規交易時段",
        "PRE_MARKET": "盤前交易時段",
        "POST_MARKET": "盤後交易時段",
        "AFTER_HOURS": "盤後交易時段",
        "CLOSED_REFERENCE": "前一交易日收盤資料",
    }
    session_label = session_map.get(str(brief.market_session), "市場資料")
    header = f"💼 持股監控｜{as_of_str}（{session_label}）"

    total = brief.total_positions if brief.total_positions is not None else (
        len(brief.action_queue) + len(brief.watchlist) + len(brief.no_material_change) + len(brief.data_issues)
    )
    covered = brief.covered_positions if brief.covered_positions is not None else (total - len(brief.data_issues))

    conclusion_lines = []
    # Line 1: keep quote coverage separate from event-driven monitoring.
    price_watch_count = sum(1 for i in brief.watchlist if i.price_status == "PRICE_WATCH")
    event_watch_count = sum(
        1 for i in brief.watchlist
        if i.event_status == "EVENT_MATERIAL_FOUND" and i.price_status != "PRICE_WATCH"
    )
    if brief.action_queue:
        conclusion_lines.append(
            f"{covered}/{total} 持股行情已驗證；共有 {len(brief.action_queue)} 檔持股建議重新檢視（詳見下方說明）。"
        )
    elif price_watch_count:
        conclusion_lines.append(
            f"{covered}/{total} 持股行情已驗證；共有 {price_watch_count} 檔持股觸發價格關注（詳見下方說明）。"
        )
    elif event_watch_count:
        conclusion_lines.append(
            f"{covered}/{total} 持股行情已驗證；價格面未觸發重大異常，另有 {event_watch_count} 檔公司／資產事件需要關注。"
        )
    elif brief.data_issues and any("quote unavailable" in d for d in brief.data_issues):
        conclusion_lines.append(f"{covered}/{total} 持股行情已驗證；部分持股行情或資料有缺漏（詳見下方說明）。")
    else:
        conclusion_lines.append(f"{covered}/{total} 持股行情已驗證，價格面沒有重大異常。")

    # Line 2: Event Monitoring Status (explicit coverage rules)
    ev_total = brief.event_total_positions or total
    ev_checked = brief.event_checked_positions
    if ev_total > 0 and ev_checked == ev_total:
        has_material_events = any(
            i.event_status == "EVENT_MATERIAL_FOUND"
            for i in brief.action_queue + brief.watchlist
        )
        if has_material_events:
            conclusion_lines.append(f"公司事件 {ev_checked}/{ev_total} 已完成檢查，已識別出重大事件（詳見下方）。")
        else:
            conclusion_lines.append(f"公司事件 {ev_checked}/{ev_total} 已完成檢查，今天沒有需要升級檢視的事件。")
    elif ev_checked > 0:
        remaining = ev_total - ev_checked
        conclusion_lines.append(f"公司事件目前完成 {ev_checked}/{ev_total}；其餘 {remaining} 檔不做事件結論。")
    else:
        conclusion_lines.append("公司事件面本次尚未完成驗證。")

    if brief.watchlist:
        watch_lines = _render_watchlist_items(brief.watchlist)
    else:
        watch_lines = ["無"]

    if brief.action_queue:
        action_lines = _render_action_items(brief.action_queue)
    else:
        action_lines = ["無"]

    if brief.upcoming_events:
        event_lines = []
        for evt in brief.upcoming_events:
            if isinstance(evt, dict):
                t = evt.get("ticker", "")
                e = evt.get("event") or evt.get("title") or ""
                d = evt.get("date") or evt.get("event_date") or evt.get("timing") or ""
                w = evt.get("why") or evt.get("impact") or ""
                source = evt.get("source") or evt.get("source_name") or ""
                parts = [p for p in (t, e, d, w) if p]
                line = "• " + "｜".join(parts)
                if source:
                    line += f"（來源：{source}）"
                event_lines.append(line)
            else:
                event_lines.append(f"• {evt}")
    else:
        event_lines = ["• 目前沒有已驗證、需要特別準備的事件。"]

    lines = [
        header,
        "",
        "【今天結論】",
        *conclusion_lines,
        "",
        "【需要關注】",
        *watch_lines,
        "",
        "【值得重新檢視】",
        *action_lines,
        "",
        "【近期事件】",
        *event_lines,
    ]

    if brief.data_issues:
        lines += [
            "",
            "【資料說明】",
            *[f"• {issue.replace('quote unavailable or invalid', '行情資料未取得或驗證未通過')}" for issue in brief.data_issues],
        ]

    return "\n".join(lines).strip()


def _render_watchlist_items(items: list[PortfolioActionItem]) -> list[str]:
    lines = []
    for item in items:
        parts = [item.ticker]
        if item.change_pct is not None:
            parts.append(f"單日 {item.change_pct:+.1f}%")
        if item.reference_price is not None:
            parts.append(f"參考價 {item.reference_price:g}")
        title = "｜".join(parts)
        lines.append(f"• {title}")
        lines.append(f"  原因：{item.summary}")
        next_step = item.next_step or "確認是否有財報、營運展望、產品或監管事件支持此次波動；目前不直接產生交易結論。"
        lines.append(f"  下一步：{next_step}")
    return lines


def _render_action_items(items: list[PortfolioActionItem]) -> list[str]:
    lines = []
    for item in items:
        event_title = ""
        if item.verified_event:
            if isinstance(item.verified_event, PortfolioEventFact):
                event_title = item.verified_event.title or item.verified_event.event_type or ""
            else:
                event_title = item.verified_event.get("event") or item.verified_event.get("title") or ""
        title = f"{item.ticker}｜{event_title}" if event_title else f"{item.ticker}｜觸發操作審查"
        lines.append(f"• {title}")
        if item.verified_event:
            if isinstance(item.verified_event, PortfolioEventFact):
                details = item.verified_event.summary or ""
                impact = item.verified_event.impact or ""
            else:
                details = item.verified_event.get("details") or item.verified_event.get("summary") or ""
                impact = item.verified_event.get("impact") or ""
            if details:
                lines.append(f"  已驗證事件：{details}")
            if impact:
                lines.append(f"  對原投資邏輯影響：{impact}")
        lines.append(f"  原因：{item.summary}")
        next_step = item.next_step or "重新檢視投資邏輯，而不是自動賣出。"
        lines.append(f"  建議：{next_step}")
        lines.append("  說明：目前只建議重新檢視，不提供精確買賣股數，因可用現金／配置上限尚未完成驗證。")
    return lines


def _render_items(items: list[PortfolioActionItem]) -> list[str]:
    return _render_watchlist_items(items)


def validate_action_brief(brief: PortfolioActionBrief, context: MarketContext,
                          portfolio: PortfolioContext) -> tuple[bool, str]:
    held = {p.instrument_id: p.quantity for p in portfolio.positions}
    for group in (brief.action_queue, brief.watchlist, brief.no_material_change):
        for item in group:
            if item.instrument_id not in held:
                return False, f"unknown held instrument {item.instrument_id}"
            obs = context.quotes.get(item.instrument_id) or context.quotes.get(item.ticker)
            if item.status == "DATA_BLOCKED":
                continue
            if item.price_status == "DATA_BLOCKED" and item.event_status == "EVENT_MATERIAL_FOUND":
                # A verified material event can surface independently of a
                # quote outage. It must not pretend to have quote evidence.
                if item.quote_id is not None or item.reference_price is not None:
                    return False, f"{item.instrument_id} quote-blocked event item carries quote evidence"
            else:
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
            if item.event_status == "EVENT_UNCHECKED":
                if any(phrase in item.summary for phrase in ("未偵測到足以升級的新資訊", "沒有重大事件", "無重大事件")):
                    return False, f"{item.instrument_id} ungrounded claim of no material events when event_status is EVENT_UNCHECKED"

    for evt in brief.upcoming_events:
        evt_str = json.dumps(evt, ensure_ascii=False) if isinstance(evt, dict) else str(evt)
        if "SIZE_NOT_COMPUTED" in evt_str:
            return False, "upcoming_events contains sizing state"
        if "search-grounded" in evt_str:
            return False, "upcoming_events contains internal jargon"

    rendered = render_action_brief(brief)

    forbidden_tokens = [
        "EVENT_CHECKED_NO_MATERIAL_CHANGE",
        "EVENT_MATERIAL_FOUND",
        "EVENT_CHECK_FAILED",
        "EVENT_UNCHECKED",
        "NO_MATERIAL_CHANGE",
        "SIZE_NOT_COMPUTED",
        "PREVIOUS_CLOSE",
        "DATA_BLOCKED",
        "ACTION_REVIEW",
        "NO_MATERIAL_EVENT",
        "LARGE_DAILY_MOVE",
        "QUOTE_UNAVAILABLE_OR_INVALID",
        "ACTION QUEUE",
        "WATCHLIST",
        "NO MATERIAL CHANGE",
    ]
    for token in forbidden_tokens:
        if token in rendered:
            return False, f"raw engineering token leaked into rendered text: {token}"

    if not brief.action_queue:
        if "不提供精確買賣股數" in rendered or "SIZE_NOT_COMPUTED" in rendered:
            return False, "sizing explanation present when action queue is empty"

    if re.search(r"(加碼|買進)\s*[0-9,.]+\s*(股|shares?)", rendered, flags=re.I):
        return False, "exact buy sizing is not allowed in daily bot"
    if re.search(r"(減碼|賣出)\s*[0-9,.]+\s*(股|shares?)", rendered, flags=re.I):
        return False, "exact sell sizing is not allowed in daily bot"

    # Strict event coverage check:
    # If event coverage is incomplete (< 100%), cannot claim full clearance
    ev_total = brief.event_total_positions
    ev_checked = brief.event_checked_positions
    if ev_total > 0 and ev_checked < ev_total:
        if "今天沒有需要升級檢視的事件" in rendered or "未發現需要升級的重大事件" in rendered or f"{ev_total}/{ev_total} 已完成檢查" in rendered:
            return False, f"incomplete event coverage ({ev_checked}/{ev_total}) claimed full portfolio clearance"

    return True, "OK"
