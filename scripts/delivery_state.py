#!/usr/bin/env python3
"""Delivery state and lightweight idempotency helpers."""

from __future__ import annotations

import json
import os
import base64
import urllib.error
import urllib.request
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
    path = receipt_path(key)
    if path.exists():
        if not valid_receipt(json.loads(path.read_text()), key):
            raise RuntimeError("DELIVERY_RECEIPT_UNCERTAIN")
        return True
    existing = load_state().get("delivered", {}).get(key)
    if existing:
        result = existing.get("telegram_result") or {}
        if not result.get("ok") or not valid_message_ids(result.get("message_ids")):
            raise RuntimeError("DELIVERY_RECEIPT_UNCERTAIN")
        return True
    # A second serialized run may have checked out the first run's pre-send SHA.
    # Query latest main, not generated report presence or trigger checkout SHA.
    if os.getenv("DELIVERY_RECEIPT_REMOTE_CHECK") == "true":
        return remote_receipt(key) is not None
    return False


def valid_message_ids(ids):
    return isinstance(ids, list) and bool(ids) and all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids)


def valid_receipt(receipt, key):
    return receipt.get("key") == key and receipt.get("state") == "DELIVERED" and valid_message_ids(receipt.get("telegram_message_ids"))


def remote_receipt(key):
    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    if not token or repo != "pincoin1002/market-report-bot":
        raise RuntimeError("REMOTE_DELIVERY_RECEIPT_AUTH_UNAVAILABLE")
    url = f"https://api.github.com/repos/{repo}/contents/reports/.delivery/{receipt_path(key).name}?ref=main"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json", "User-Agent": "market-report-receipt-check"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            metadata = json.loads(response.read())
        receipt = json.loads(base64.b64decode(metadata["content"], validate=False))
        if not valid_receipt(receipt, key):
            raise RuntimeError("DELIVERY_RECEIPT_UNCERTAIN")
        return receipt
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError("REMOTE_DELIVERY_RECEIPT_UNAVAILABLE") from None
    except (urllib.error.URLError, ValueError, KeyError):
        raise RuntimeError("REMOTE_DELIVERY_RECEIPT_UNAVAILABLE") from None


def put_remote_receipt(key, receipt, *, sha=None):
    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    if not token or repo != "pincoin1002/market-report-bot":
        raise RuntimeError("REMOTE_DELIVERY_RECEIPT_AUTH_UNAVAILABLE")
    body = {"branch": "main", "message": f"chore: record {receipt['state'].lower()} report receipt [skip ci]",
        "content": base64.b64encode(json.dumps(receipt, sort_keys=True).encode()).decode()}
    if sha:
        body["sha"] = sha
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/reports/.delivery/{receipt_path(key).name}",
        data=json.dumps(body).encode(), method="PUT", headers={"Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())["content"]["sha"]
    except (urllib.error.URLError, ValueError, KeyError):
        # No retry of a possibly accepted claim; no resend after an unknown send.
        raise RuntimeError("REMOTE_DELIVERY_CLAIM_OR_RECEIPT_UNCONFIRMED") from None


def receipt_path(key: str) -> Path:
    if not key or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_:-" for char in key.lower()):
        raise ValueError("invalid delivery key")
    return RECEIPTS_DIR / f"{key.replace(':', '_')}.json"


def mark_state(key: str, state: DeliveryState, *, telegram_result: dict | None = None) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    data = load_state()
    remote = os.getenv("DELIVERY_RECEIPT_REMOTE_CHECK") == "true"
    if state == "DELIVERING" and remote:
        claim = {"key": key, "state": "DELIVERING", "telegram_message_ids": [],
            "owner_run_id": os.getenv("GITHUB_RUN_ID"), "owner_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "at": datetime.now(timezone.utc).isoformat()}
        data.setdefault("claims", {})[key] = put_remote_receipt(key, claim)
    data.setdefault("states", {})[key] = {
        "state": state,
        "at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if telegram_result is not None:
        data["states"][key]["telegram_result"] = telegram_result
    if state == "DELIVERED":
        if (not telegram_result or telegram_result.get("ok") is not True or telegram_result.get("simulated")
                or not valid_message_ids(telegram_result.get("message_ids"))):
            raise ValueError("DELIVERED requires confirmed non-simulated Telegram response")
        data.setdefault("delivered", {})[key] = data["states"][key]
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if state == "DELIVERED":
        path = receipt_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"key": key, "state": "DELIVERED", "at": data["states"][key]["at"],
                   "telegram_api_ok": True,
                   "telegram_message_ids": telegram_result.get("message_ids", []),
                   "confirmations": telegram_result.get("confirmations", []),
                   "destination_fingerprints": telegram_result.get("destination_fingerprints", [])}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        if remote:
            sha = data.get("claims", {}).get(key)
            if not sha:
                raise RuntimeError("REMOTE_DELIVERY_CLAIM_MISSING")
            put_remote_receipt(key, receipt, sha=sha)
