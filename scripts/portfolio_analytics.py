#!/usr/bin/env python3
"""Portfolio Analytics Engine: multi-currency valuation, P/L, returns, and contribution."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from models import PortfolioContext, PositionContext, QuoteObservation
from instrument_registry import resolve_instrument

log = logging.getLogger("portfolio_analytics")


@dataclass
class PositionValuation:
    ticker: str
    name: str
    quantity: float
    currency: str
    market: str
    price: float
    prev_close: float
    current_valuation_native: float
    previous_valuation_native: float
    current_valuation_twd: float
    previous_valuation_twd: float
    daily_pl_twd: float
    daily_return_pct: float
    asset_type: str = ""
    weight_pct: float = 0.0
    contribution_bp: float = 0.0


@dataclass
class PortfolioAnalyticsResult:
    status: Literal["SUCCESS", "MISSING_PORTFOLIO", "MISSING_QUANTITY", "INCOMPLETE_QUOTES", "MISSING_FX"]
    reason: str = ""
    comparable_positions: int = 0
    expected_positions: int = 0
    total_valuation_twd: float = 0.0
    previous_valuation_twd: float = 0.0
    daily_pl_twd: float = 0.0
    daily_return_pct: float = 0.0
    positions: list[PositionValuation] = field(default_factory=list)
    top_positive_contributors: list[PositionValuation] = field(default_factory=list)
    top_negative_contributors: list[PositionValuation] = field(default_factory=list)
    tw_equities_twd: float = 0.0
    tw_equities_weight: float = 0.0
    tw_equities_pl_twd: float = 0.0
    tw_equities_return_pct: float = 0.0
    us_equities_twd: float = 0.0
    us_equities_weight: float = 0.0
    us_equities_pl_twd: float = 0.0
    us_equities_return_pct: float = 0.0
    crypto_twd: float = 0.0
    crypto_weight: float = 0.0
    crypto_pl_twd: float = 0.0
    crypto_return_pct: float = 0.0
    taiex_return_pct: float | None = None
    taiex_diff_pct: float | None = None
    comparison_notes: str = ""


def calculate_portfolio_analytics(
    portfolio_context: PortfolioContext | None,
    quotes: dict[str, QuoteObservation],
    taiex_change_pct: float | None = None,
    expected_dates: dict[str, str] | None = None,
) -> PortfolioAnalyticsResult:
    """Calculate multi-currency portfolio valuation, P/L, returns, and contribution."""
    if not portfolio_context or not portfolio_context.positions:
        return PortfolioAnalyticsResult(
            status="MISSING_PORTFOLIO",
            reason="未取得持股清單",
        )

    expected_count = len(portfolio_context.positions)
    # Check FX availability for USD/Crypto positions
    has_foreign_currency = any(p.currency != "TWD" for p in portfolio_context.positions)
    fx_quote = quotes.get("USDTWD") or quotes.get("TWD=X")
    if has_foreign_currency:
        if not fx_quote or fx_quote.quality_status != "VALID" or fx_quote.price <= 0:
            return PortfolioAnalyticsResult(
                status="MISSING_FX",
                expected_positions=expected_count,
                reason="未取得美元兌台幣即期匯率，暫無法計算跨幣別持股總值與報酬",
            )
        current_fx = fx_quote.price
        prev_fx = fx_quote.previous_regular_close if fx_quote.previous_regular_close > 0 else current_fx
    else:
        current_fx = 1.0
        prev_fx = 1.0

    # Validate each position
    position_valuations: list[PositionValuation] = []
    missing_positions = []

    for pos in portfolio_context.positions:
        if pos.quantity is None or pos.quantity <= 0:
            return PortfolioAnalyticsResult(
                status="MISSING_QUANTITY",
                expected_positions=expected_count,
                reason=f"持股 {pos.ticker} 缺乏有效股數/數量",
            )

        q = quotes.get(pos.ticker)
        if not q or q.quality_status != "VALID":
            missing_positions.append(pos.ticker)
            continue

        spec = resolve_instrument(pos.ticker)
        market = spec.market

        # Validate date contract if expected_dates given
        if expected_dates:
            expected_date = expected_dates.get(market)
            if expected_date and q.market_date != expected_date:
                missing_positions.append(pos.ticker)
                continue

        qty = float(pos.quantity)
        px = float(q.price)
        prev_px = float(q.previous_regular_close) if q.previous_regular_close > 0 else px

        curr_val_nat = qty * px
        prev_val_nat = qty * prev_px

        if pos.currency == "TWD":
            curr_val_twd = curr_val_nat
            prev_val_twd = prev_val_nat
        else:
            curr_val_twd = curr_val_nat * current_fx
            prev_val_twd = prev_val_nat * prev_fx

        daily_pl = curr_val_twd - prev_val_twd
        daily_ret = (daily_pl / prev_val_twd * 100) if prev_val_twd > 0 else 0.0

        position_valuations.append(PositionValuation(
            ticker=pos.ticker,
            name=pos.name,
            quantity=qty,
            currency=pos.currency,
            market=market,
            price=px,
            prev_close=prev_px,
            current_valuation_native=curr_val_nat,
            previous_valuation_native=prev_val_nat,
            current_valuation_twd=curr_val_twd,
            previous_valuation_twd=prev_val_twd,
            daily_pl_twd=daily_pl,
            daily_return_pct=daily_ret,
            asset_type=pos.asset_type,
        ))

    if missing_positions or len(position_valuations) < expected_count:
        return PortfolioAnalyticsResult(
            status="INCOMPLETE_QUOTES",
            expected_positions=expected_count,
            comparable_positions=len(position_valuations),
            reason=f"部分部位行情未取得（{len(position_valuations)}/{expected_count}），暫不計算整體持股報酬",
        )

    # Aggregates
    total_curr_twd = sum(p.current_valuation_twd for p in position_valuations)
    total_prev_twd = sum(p.previous_valuation_twd for p in position_valuations)
    total_pl_twd = total_curr_twd - total_prev_twd
    total_ret_pct = (total_pl_twd / total_prev_twd * 100) if total_prev_twd > 0 else 0.0

    # Calculate weights and contribution in basis points
    for p in position_valuations:
        p.weight_pct = (p.current_valuation_twd / total_curr_twd * 100) if total_curr_twd > 0 else 0.0
        p.contribution_bp = (p.daily_pl_twd / total_prev_twd * 10000) if total_prev_twd > 0 else 0.0

    # Top contributors
    sorted_by_contrib = sorted(position_valuations, key=lambda x: x.contribution_bp, reverse=True)
    pos_contrib = [p for p in sorted_by_contrib if p.contribution_bp > 0][:3]
    neg_contrib = [p for p in sorted_by_contrib if p.contribution_bp < 0][-3:]
    # Keep negative sorted from largest loss to smaller loss
    neg_contrib.sort(key=lambda x: x.contribution_bp)

    # Breakdown by market
    def _breakdown_items(items: list[PositionValuation]):
        curr = sum(p.current_valuation_twd for p in items)
        prev = sum(p.previous_valuation_twd for p in items)
        pl = curr - prev
        ret = (pl / prev * 100) if prev > 0 else 0.0
        wt = (curr / total_curr_twd * 100) if total_curr_twd > 0 else 0.0
        return curr, wt, pl, ret

    tw_curr, tw_wt, tw_pl, tw_ret = _breakdown_items([p for p in position_valuations if p.market == "TW"])
    us_curr, us_wt, us_pl, us_ret = _breakdown_items([p for p in position_valuations if p.market == "US"])
    crypto_curr, crypto_wt, crypto_pl, crypto_ret = _breakdown_items([
        p for p in position_valuations if p.asset_type == "CRYPTO" or p.market in ("CRYPTO", "GLOBAL")
    ])

    taiex_diff = None
    if taiex_change_pct is not None:
        taiex_diff = round(total_ret_pct - taiex_change_pct, 2)

    return PortfolioAnalyticsResult(
        status="SUCCESS",
        comparable_positions=expected_count,
        expected_positions=expected_count,
        total_valuation_twd=total_curr_twd,
        previous_valuation_twd=total_prev_twd,
        daily_pl_twd=total_pl_twd,
        daily_return_pct=total_ret_pct,
        positions=position_valuations,
        top_positive_contributors=pos_contrib,
        top_negative_contributors=neg_contrib,
        tw_equities_twd=tw_curr,
        tw_equities_weight=tw_wt,
        tw_equities_pl_twd=tw_pl,
        tw_equities_return_pct=tw_ret,
        us_equities_twd=us_curr,
        us_equities_weight=us_wt,
        us_equities_pl_twd=us_pl,
        us_equities_return_pct=us_ret,
        crypto_twd=crypto_curr,
        crypto_weight=crypto_wt,
        crypto_pl_twd=crypto_pl,
        crypto_return_pct=crypto_ret,
        taiex_return_pct=taiex_change_pct,
        taiex_diff_pct=taiex_diff,
        comparison_notes="（跨市場組合含美股與加密貨幣，時間區間與純台股大盤不同）",
    )


def format_portfolio_section(result: PortfolioAnalyticsResult, summary: str | None = None) -> list[str]:
    """Render investor-friendly portfolio section for Telegram briefing."""
    lines = ["## 【我的持股】"]
    if summary:
        lines.append(f"- {summary}")
    if result.status != "SUCCESS":
        lines.append(f"- 本次可比較部位：{result.comparable_positions}/{result.expected_positions}")
        lines.append(f"- {result.reason}。")
        return lines

    sign_pl = "+" if result.daily_pl_twd >= 0 else "-"
    lines.append(f"- 總資產：NT${result.total_valuation_twd:,.0f}")
    lines.append(f"- 本次可比較部位：{result.comparable_positions}/{result.expected_positions}")
    lines.append(f"- 本次組合變動：{sign_pl}NT${abs(result.daily_pl_twd):,.0f}（{result.daily_return_pct:+.2f}%）")
    lines.append("")

    if result.top_positive_contributors or result.top_negative_contributors:
        lines.append("主要貢獻：")
        for p in result.top_positive_contributors:
            lines.append(f"- ▲ {p.name} +NT${p.daily_pl_twd:,.0f} / +{p.contribution_bp:.1f} bp")
        for p in result.top_negative_contributors:
            lines.append(f"- ▼ {p.name} -NT${abs(p.daily_pl_twd):,.0f} / {p.contribution_bp:.1f} bp")
        lines.append("")

    lines.append(f"- 台股部位：NT${result.tw_equities_twd:,.0f}（{result.tw_equities_weight:.1f}%）單日 {result.tw_equities_return_pct:+.2f}%")
    lines.append(f"- 美股部位：NT${result.us_equities_twd:,.0f}（{result.us_equities_weight:.1f}%）單日 {result.us_equities_return_pct:+.2f}%")
    lines.append(f"- Crypto：NT${result.crypto_twd:,.0f}（{result.crypto_weight:.1f}%）單日 {result.crypto_return_pct:+.2f}%")
    lines.append("")

    if result.taiex_return_pct is not None and result.taiex_diff_pct is not None:
        diff_str = f"{result.taiex_diff_pct:+.2f} 個百分點"
        lines.append("相對台股大盤：")
        lines.append(f"- 組合 {result.daily_return_pct:+.2f}%")
        lines.append(f"- TAIEX {result.taiex_return_pct:+.2f}%")
        lines.append(f"- 差異 {diff_str}")
        if result.comparison_notes:
            lines.append(f"- {result.comparison_notes}")

    return lines
