#!/usr/bin/env python3
"""Deterministic ADR engine with strict temporal contracts and fail-closed validation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from models import QuoteObservation

log = logging.getLogger("adr_engine")
TPE = timezone(timedelta(hours=8))


def calculate_tsm_adr_premium(
    tsm_obs: QuoteObservation | None,
    tw_obs: QuoteObservation | None,
    fx_obs: QuoteObservation | None,
    target_us_date: str,
    target_tw_date: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Calculate TSM ADR premium if and only if all 3 inputs satisfy temporal contract.
    
    1. TSM ADR: US regular close corresponding to target_us_date (quality VALID)
    2. 2330: TW official close corresponding to target_tw_date (quality VALID)
    3. USD/TWD: Compatible timestamp observation (quality VALID)
    
    Ratio: 1 TSM ADR = 5 common shares (2330.TW)
    
    Returns:
        (is_valid, rendered_markdown, payload_dict)
    """
    reasons = []

    # 1. Validate TSM ADR
    if tsm_obs is None:
        reasons.append("TSM ADR quote unavailable")
    else:
        if tsm_obs.quality_status != "VALID":
            reasons.append(f"TSM ADR quality is {tsm_obs.quality_status}")
        if tsm_obs.market_date != target_us_date:
            reasons.append(f"TSM ADR date mismatch (got {tsm_obs.market_date}, expected {target_us_date} US close)")
        if tsm_obs.price <= 0:
            reasons.append("TSM ADR price non-positive")

    # 2. Validate 2330 TW
    if tw_obs is None:
        reasons.append("2330 quote unavailable")
    else:
        if tw_obs.quality_status != "VALID":
            reasons.append(f"2330 quality is {tw_obs.quality_status}")
        if tw_obs.market_date != target_tw_date:
            reasons.append(f"2330 date mismatch (got {tw_obs.market_date}, expected {target_tw_date} TW close)")
        if tw_obs.price <= 0:
            reasons.append("2330 price non-positive")

    # 3. Validate USD/TWD FX
    if fx_obs is None:
        reasons.append("USD/TWD quote unavailable")
    else:
        if fx_obs.quality_status != "VALID":
            reasons.append(f"USD/TWD quality is {fx_obs.quality_status}")
        if fx_obs.price <= 0:
            reasons.append("USD/TWD price non-positive")

    if reasons:
        msg = (
            "**ADR 溢折價分析：**\n"
            "DATA_BLOCKED — TEMPORAL_MISMATCH\n"
            + "\n".join(f"- 阻斷原因：{r}" for r in reasons)
        )
        return False, msg, {
            "status": "DATA_BLOCKED",
            "reason": "TEMPORAL_MISMATCH",
            "details": reasons,
            "target_us_date": target_us_date,
            "target_tw_date": target_tw_date,
        }

    assert tsm_obs is not None and tw_obs is not None and fx_obs is not None

    # Exact TSM ADR ratio: 1 ADR = 5 common shares
    adr_ratio = 5.0
    tsm_price_usd = tsm_obs.price
    fx_rate = fx_obs.price
    tw_price = tw_obs.price

    tsm_twd_per_share = (tsm_price_usd * fx_rate) / adr_ratio
    premium_pct = ((tsm_twd_per_share - tw_price) / tw_price) * 100.0

    fx_time = fx_obs.observed_at or fx_obs.retrieved_at
    if fx_time.tzinfo is None:
        fx_time_str = f"{fx_obs.market_date}"
    else:
        fx_time_str = fx_time.astimezone(TPE).strftime("%Y-%m-%d %H:%M TPE")

    rendered = (
        "**ADR 溢折價分析：**\n"
        f"- TSM ADR:\n  {target_us_date} US close (${tsm_price_usd:.2f})\n"
        f"- 2330:\n  {target_tw_date} TW close ({tw_price:,.1f} TWD)\n"
        f"- USD/TWD:\n  {fx_time_str} ({fx_rate:.4f})\n"
        f"- 換算現貨價：{tsm_twd_per_share:.2f} TWD (ADR比例 1:5)\n"
        f"- 溢折價率：{premium_pct:+.2f}%"
    )

    payload = {
        "status": "VALID",
        "target_us_date": target_us_date,
        "target_tw_date": target_tw_date,
        "tsm_adr_usd": tsm_price_usd,
        "tw_2330_twd": tw_price,
        "usdtwd": fx_rate,
        "tsm_twd_per_share": round(tsm_twd_per_share, 2),
        "premium_pct": round(premium_pct, 2),
        "ratio": adr_ratio,
        "fx_timestamp": fx_time_str,
    }

    return True, rendered, payload
