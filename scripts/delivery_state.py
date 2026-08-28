#!/usr/bin/env python3
"""Delivery state and lightweight idempotency helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from models import DeliveryState

ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "data" / "delivery_state.json"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"delivered": {}, "states": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def already_delivered(key: str) -> bool:
    return key in load_state().get("delivered", {})


def mark_state(key: str, state: DeliveryState) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    data = load_state()
    data.setdefault("states", {})[key] = {
        "state": state,
        "at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if state == "DELIVERED":
        data.setdefault("delivered", {})[key] = data["states"][key]
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
