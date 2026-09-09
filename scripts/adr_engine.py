#!/usr/bin/env python3
"""Deterministic ADR engine with strict temporal contracts and fail-closed validation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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
    issue_kinds = []

    def add_reason(reason: str, kind: str) -> None:
        reasons.append(reason)
        issue_kinds.append(kind)

    # 1. Validate TSM ADR identity and temporal contract
    if tsm_obs is None:
        add_reason("TSM ADR quote unavailable", "TEMPORAL_MISMATCH")
    else:
        if tsm_obs.canonical_symbol != "TSM":
            add_reason(
                f"TSM symbol mismatch (got {tsm_obs.canonical_symbol}, expected TSM)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if (tsm_obs.market or "").upper() != "US":
            add_reason(
                f"TSM market mismatch (got {tsm_obs.market}, expected US)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if tsm_obs.currency.upper() != "USD":
            add_reason(
                f"TSM currency mismatch (got {tsm_obs.currency}, expected USD)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if tsm_obs.market_date != target_us_date:
            add_reason(
                f"TSM ADR date mismatch (got {tsm_obs.market_date}, expected {target_us_date} US close)",
                "TEMPORAL_MISMATCH",
            )
        if tsm_obs.quality_status != "VALID":
            add_reason(f"TSM ADR quality is {tsm_obs.quality_status}", "TEMPORAL_MISMATCH")
        if tsm_obs.price <= 0:
            add_reason("TSM ADR price non-positive", "INPUT_IDENTITY_MISMATCH")

    # 2. Validate 2330 TW identity and temporal contract
    if tw_obs is None:
        add_reason("2330 quote unavailable", "TEMPORAL_MISMATCH")
    else:
        if tw_obs.canonical_symbol != "2330":
            add_reason(
                f"2330 symbol mismatch (got {tw_obs.canonical_symbol}, expected 2330)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if (tw_obs.market or "").upper() != "TW":
            add_reason(
                f"2330 market mismatch (got {tw_obs.market}, expected TW)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if tw_obs.currency.upper() != "TWD":
            add_reason(
                f"2330 currency mismatch (got {tw_obs.currency}, expected TWD)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if tw_obs.market_date != target_tw_date:
            add_reason(
                f"2330 date mismatch (got {tw_obs.market_date}, expected {target_tw_date} TW close)",
                "TEMPORAL_MISMATCH",
            )
        if tw_obs.quality_status != "VALID":
            add_reason(f"2330 quality is {tw_obs.quality_status}", "TEMPORAL_MISMATCH")
        if tw_obs.price <= 0:
            add_reason("2330 price non-positive", "INPUT_IDENTITY_MISMATCH")

    # 3. Validate USD/TWD FX identity and temporal compatibility
    as_of = report_as_of or datetime.now(tz=TPE)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        add_reason("report_as_of timestamp missing timezone info", "TEMPORAL_MISMATCH")
        as_of = as_of.replace(tzinfo=TPE)

    if fx_obs is None:
        add_reason("USD/TWD quote unavailable", "TEMPORAL_MISMATCH")
    else:
        if fx_obs.canonical_symbol != "USDTWD":
            add_reason(
                f"FX symbol mismatch (got {fx_obs.canonical_symbol}, expected USDTWD)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if fx_obs.currency.upper() != "TWD":
            add_reason(
                f"FX currency mismatch (got {fx_obs.currency}, expected TWD)",
                "INPUT_IDENTITY_MISMATCH",
            )
        if fx_obs.quality_status != "VALID":
            add_reason(f"USD/TWD quality is {fx_obs.quality_status}", "TEMPORAL_MISMATCH")
        if fx_obs.price <= 0:
            add_reason("USD/TWD price non-positive", "INPUT_IDENTITY_MISMATCH")

        # Explicit timezone-aware timestamp
        fx_time = fx_obs.provider_timestamp or fx_obs.observed_at or fx_obs.retrieved_at
        if fx_time is None:
            add_reason("USD/TWD timestamp missing", "TEMPORAL_MISMATCH")
        elif fx_time.tzinfo is None or fx_time.utcoffset() is None:
            add_reason("USD/TWD timestamp missing timezone info", "TEMPORAL_MISMATCH")
        else:
            # Future-dated check (allow up to 60s clock skew)
            if fx_time > as_of + timedelta(seconds=60):
                add_reason(
                    f"USD/TWD observation is future-dated ({fx_time.isoformat()} > {as_of.isoformat()})",
                    "TEMPORAL_MISMATCH",
                )

            # Freshness / maximum age check
            fx_date = fx_time.astimezone(as_of.tzinfo).date()
            as_of_date = as_of.date()
            age_days = (as_of_date - fx_date).days
            if age_days < 0:
                add_reason(
                    f"USD/TWD observation date is future-dated ({fx_date} > {as_of_date})",
                    "TEMPORAL_MISMATCH",
                )
            elif age_days > max_fx_age_days:
                add_reason(
                    f"USD/TWD observation is stale (age {age_days} days > max {max_fx_age_days} days)",
                    "TEMPORAL_MISMATCH",
                )

    if reasons:
        reason_code = (
            "INPUT_IDENTITY_MISMATCH"
            if "INPUT_IDENTITY_MISMATCH" in issue_kinds
            else "TEMPORAL_MISMATCH"
        )
        msg = (
            "**ADR 溢折價分析：**\n"
            f"DATA_BLOCKED — {reason_code}\n"
            + "\n".join(f"- 阻斷原因：{r}" for r in reasons)
        )
        return False, msg, {
            "status": "DATA_BLOCKED",
            "reason": reason_code,
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
    assert fx_time is not None
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
