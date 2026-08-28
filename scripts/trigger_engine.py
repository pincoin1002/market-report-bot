#!/usr/bin/env python3
"""Small deterministic trigger evidence engine."""

from __future__ import annotations

from statistics import mean

from models import Trigger


def moving_average(values: list[float], window: int) -> float | None:
    clean = [float(v) for v in values if v and v > 0]
    if len(clean) < window:
        return None
    return round(mean(clean[-window:]), 4)


def technical_trigger(symbol: str, name: str, value: float,
                      calculation_date: str, source_id: str) -> Trigger:
    return Trigger(
        trigger_type="TECHNICAL",
        condition=f"{symbol} monitoring reference {name} = {value:g}",
        numeric_value=value,
        basis=f"deterministic {name} from verified price history as of {calculation_date}",
        source_ids=[source_id],
        generated_by="TriggerEngineV1",
        valid_until=calculation_date,
    )
