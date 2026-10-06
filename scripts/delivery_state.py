#!/usr/bin/env python3
"""Delivery state and lightweight idempotency helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from models import DeliveryState

ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "data" / "delivery_state.json"
RECEIPTS_DIR = ROOT / "reports" / ".delivery"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"delivered": {}, "states": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def already_delivered(key: str) -> bool:
    return receipt_path(key).exists() or key in load_state().get("delivered", {})


def receipt_path(key: str) -> Path:
    if not key or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_:-" for char in key.lower()):
        raise ValueError("invalid delivery key")
    return RECEIPTS_DIR / f"{key.replace(':', '_')}.json"


def mark_state(key: str, state: DeliveryState, *, telegram_result: dict | None = None) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    data = load_state()
    data.setdefault("states", {})[key] = {
        "state": state,
        "at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if telegram_result is not None:
        data["states"][key]["telegram_result"] = telegram_result
    if state == "DELIVERED":
        if not telegram_result or telegram_result.get("ok") is not True or telegram_result.get("simulated"):
            raise ValueError("DELIVERED requires confirmed non-simulated Telegram response")
        data.setdefault("delivered", {})[key] = data["states"][key]
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if state == "DELIVERED":
        path = receipt_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"key": key, "state": "DELIVERED", "at": data["states"][key]["at"],
                   "telegram_message_ids": telegram_result.get("message_ids", []),
                   "destination_fingerprints": telegram_result.get("destination_fingerprints", [])}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
