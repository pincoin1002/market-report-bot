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
    report_as_of: datetime | None = None,
    max_fx_age_days: int = 3,
) -> tuple[bool, str, dict[str, Any]]:
    """Calculate TSM ADR premium if and only if all 3 inputs satisfy temporal contract.
    
    Validation requirements:
    1. TSM input:
       - canonical symbol is TSM
       - market is US
       - expected currency is USD
       - expected completed US trading date matches target_us_date
       - quality == VALID
       - price > 0
       
    2. 2330 input:
       - canonical symbol is 2330
       - market is TW
       - expected currency is TWD
       - expected completed TW trading date matches target_tw_date
       - quality == VALID
       - price > 0
       
    3. FX input:
       - canonical symbol is USDTWD
       - semantics are USD/TWD
       - quality == VALID
       - price > 0
       - observation timestamp is explicit and timezone-aware
       - observation must be <= report_as_of (not future-dated)
       - observation age must not exceed max_fx_age_days relative to report_as_of
    
    Ratio: 1 TSM ADR = 5 common shares (2330.TW)
    
    Returns:
        (is_valid, rendered_markdown, payload_dict)
    """
    reasons = []

    # 1. Validate TSM ADR identity and temporal contract
    if tsm_obs is None:
        reasons.append("TSM ADR quote unavailable")
    else:
        if tsm_obs.canonical_symbol != "TSM":
            reasons.append(f"TSM symbol mismatch (got {tsm_obs.canonical_symbol}, expected TSM)")
        if (tsm_obs.market or "").upper() != "US":
            reasons.append(f"TSM market mismatch (got {tsm_obs.market}, expected US)")
        if tsm_obs.currency.upper() != "USD":
            reasons.append(f"TSM currency mismatch (got {tsm_obs.currency}, expected USD)")
        if tsm_obs.market_date != target_us_date:
            reasons.append(f"TSM ADR date mismatch (got {tsm_obs.market_date}, expected {target_us_date} US close)")
        if tsm_obs.quality_status != "VALID":
            reasons.append(f"TSM ADR quality is {tsm_obs.quality_status}")
        if tsm_obs.price <= 0:
            reasons.append("TSM ADR price non-positive")

    # 2. Validate 2330 TW identity and temporal contract
    if tw_obs is None:
        reasons.append("2330 quote unavailable")
    else:
        if tw_obs.canonical_symbol != "2330":
            reasons.append(f"2330 symbol mismatch (got {tw_obs.canonical_symbol}, expected 2330)")
        if (tw_obs.market or "").upper() != "TW":
            reasons.append(f"2330 market mismatch (got {tw_obs.market}, expected TW)")
        if tw_obs.currency.upper() != "TWD":
            reasons.append(f"2330 currency mismatch (got {tw_obs.currency}, expected TWD)")
        if tw_obs.market_date != target_tw_date:
            reasons.append(f"2330 date mismatch (got {tw_obs.market_date}, expected {target_tw_date} TW close)")
        if tw_obs.quality_status != "VALID":
            reasons.append(f"2330 quality is {tw_obs.quality_status}")
        if tw_obs.price <= 0:
            reasons.append("2330 price non-positive")

    # 3. Validate USD/TWD FX identity and temporal compatibility
    as_of = report_as_of or datetime.now(tz=TPE)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=TPE)

    if fx_obs is None:
        reasons.append("USD/TWD quote unavailable")
    else:
        if fx_obs.canonical_symbol != "USDTWD":
            reasons.append(f"FX symbol mismatch (got {fx_obs.canonical_symbol}, expected USDTWD)")
        if fx_obs.currency.upper() != "TWD":
            reasons.append(f"FX currency mismatch (got {fx_obs.currency}, expected TWD)")
        if fx_obs.quality_status != "VALID":
            reasons.append(f"USD/TWD quality is {fx_obs.quality_status}")
        if fx_obs.price <= 0:
            reasons.append("USD/TWD price non-positive")

        # Explicit timezone-aware timestamp
        fx_time = fx_obs.provider_timestamp or fx_obs.observed_at or fx_obs.retrieved_at
        if fx_time.tzinfo is None:
            reasons.append("USD/TWD timestamp missing timezone info")
        else:
            # Future-dated check (allow up to 60s clock skew)
            if fx_time > as_of + timedelta(seconds=60):
                reasons.append(f"USD/TWD observation is future-dated ({fx_time.isoformat()} > {as_of.isoformat()})")
            
            # Freshness / maximum age check
            fx_date = fx_time.astimezone(as_of.tzinfo).date()
            as_of_date = as_of.date()
            age_days = (as_of_date - fx_date).days
            if age_days < 0:
                reasons.append(f"USD/TWD observation date is future-dated ({fx_date} > {as_of_date})")
            elif age_days > max_fx_age_days:
                reasons.append(f"USD/TWD observation is stale (age {age_days} days > max {max_fx_age_days} days)")

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

    fx_time = fx_obs.provider_timestamp or fx_obs.observed_at or fx_obs.retrieved_at
    fx_time_aware = fx_time if fx_time.tzinfo is not None else fx_time.replace(tzinfo=TPE)
    fx_time_str = fx_time_aware.astimezone(TPE).strftime("%Y-%m-%d %H:%M TPE")

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
