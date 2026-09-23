#!/usr/bin/env python3
"""Read-only canonical portfolio boundary for the daily-report pipeline."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

import portfolio_store
from instrument_registry import resolve_instrument
from models import CashContext, LiabilityContext, PortfolioContext, PositionContext

log = logging.getLogger("portfolio_context")
BASE_DIR = Path(__file__).parent.parent


class PortfolioContextProvider(Protocol):
    def load(self) -> PortfolioContext: ...


def _position_context(pos: dict, ordinal: int, source: str) -> PositionContext | None:
    ticker = str(pos.get("ticker") or pos.get("symbol") or pos.get("instrument_id") or "").strip()
    if not ticker:
        return None
    spec = resolve_instrument(ticker)
    quantity = float(pos.get("quantity") or pos.get("shares") or 0)
    if quantity <= 0:
        return None
    account = pos.get("account")
    return PositionContext(
        position_id=str(pos.get("position_id") or pos.get("id") or f"{source}:{spec.canonical_symbol}:{account or 'default'}:{ordinal}"),
        # The report registry key is a listing-level canonical symbol.  The
        # PIOS instrument identity stays in position_id when it is not itself
        # a quoteable listing symbol.
        instrument_id=spec.canonical_symbol,
        ticker=spec.canonical_symbol,
        name=str(pos.get("name") or spec.display_name),
        account=str(account) if account is not None else None,
        quantity=quantity,
        cost_basis=pos.get("cost_basis"),
        currency=pos.get("currency") or spec.currency,
        asset_type=pos.get("asset_type") or spec.asset_type,
        quote_id=pos.get("quote_id"),
        note=str(pos.get("note") or ""),
    )


class EncryptedPortfolioProvider:
    def load_raw(self) -> dict | None:
        return portfolio_store.load_portfolio()

    def load(self) -> PortfolioContext:
        raw = self.load_raw()
        if raw is None:
            return PortfolioContext(
                source="LEGACY_ENCRYPTED_UNAVAILABLE",
                notes="encrypted portfolio unavailable (missing key or unreadable ciphertext)",
            )
        positions: list[PositionContext] = []
        ordinal = 0
        for bucket in ("tw_positions", "us_positions"):
            for pos in raw.get(bucket, []):
                ordinal += 1
                item = _position_context(pos, ordinal, "legacy_encrypted")
                if item:
                    positions.append(item)
        cash = []
        value = raw.get("available_cash")
        if isinstance(value, (int, float)) and value > 0:
            cash.append(CashContext(currency="TWD", amount=float(value), deployable=None))
        return PortfolioContext(
            snapshot_id="legacy-encrypted-portfolio",
            source="LEGACY_ENCRYPTED_PORTFOLIO",
            positions=positions,
            cash=cash,
            notes=str(raw.get("portfolio_notes") or ""),
        )


class PIOSPortfolioProvider:
    """Read a stable, versioned PIOS PortfolioSnapshot JSON export.

    PIOS retains AcceptedTransaction and reconciliation ownership.  Required
    export fields are ``snapshot_id``, ``as_of``, and ``active_positions``.
    ``PIOS_PORTFOLIO_SNAPSHOT_PATH`` can point to a private CI checkout.
    """

    def __init__(self, path: str | Path | None = None):
        configured = path or os.getenv("PIOS_PORTFOLIO_SNAPSHOT_PATH")
        self.path = Path(configured) if configured else BASE_DIR / "data" / "pios_portfolio_snapshot.json"
        self.explicit_path = bool(configured)
        self.snapshot_json = os.getenv("PIOS_PORTFOLIO_SNAPSHOT_JSON", "").strip()

    def available(self) -> bool:
        return bool(self.snapshot_json) or self.path.exists()

    def load(self) -> PortfolioContext:
        if self.snapshot_json:
            payload = json.loads(self.snapshot_json)
        elif not self.path.exists():
            raise FileNotFoundError(f"PIOS PortfolioSnapshot export not found: {self.path}")
        else:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        snapshot_id = str(payload.get("snapshot_id") or payload.get("id") or "")
        as_of = payload.get("as_of")
        active = payload.get("active_positions")
        if not snapshot_id or not as_of or not isinstance(active, list):
            raise ValueError("PIOS snapshot requires snapshot_id, as_of, and active_positions")
        positions = []
        for ordinal, pos in enumerate(active, start=1):
            if not isinstance(pos, dict):
                raise ValueError("PIOS active_positions must contain objects")
            item = _position_context(pos, ordinal, "pios")
            if item:
                positions.append(item)
        cash = []
        for item in payload.get("cash", []):
            if isinstance(item, dict) and isinstance(item.get("amount"), (int, float)):
                cash.append(CashContext(
                    currency=str(item.get("currency") or "TWD"), amount=float(item["amount"]),
                    deployable=item.get("deployable") if isinstance(item.get("deployable"), bool) else None,
                ))
        liabilities = []
        for item in payload.get("liabilities", []):
            if not isinstance(item, dict):
                raise ValueError("PIOS liabilities must contain objects")
            principal = item.get("outstanding_principal")
            if not isinstance(principal, (int, float)):
                raise ValueError("PIOS liability outstanding_principal must be numeric")
            liabilities.append(LiabilityContext(
                liability_id=str(item.get("liability_id") or item.get("id") or item.get("name") or "liability"),
                name=str(item.get("name") or "Liability"),
                currency=str(item.get("currency") or "TWD"),
                outstanding_principal=float(principal),
                monthly_payment=float(item["monthly_payment"]) if isinstance(item.get("monthly_payment"), (int, float)) else None,
            ))
        return PortfolioContext(
            snapshot_id=snapshot_id,
            as_of=datetime.fromisoformat(str(as_of).replace("Z", "+00:00")),
            source="PIOS_PORTFOLIO_SNAPSHOT",
            positions=positions,
            cash=cash,
            liabilities=liabilities,
            notes=str(payload.get("notes") or ""),
        )


def load_authoritative_portfolio() -> PortfolioContext:
    """Prefer PIOS; only use the existing encrypted transaction store as fallback."""
    pios = PIOSPortfolioProvider()
    if pios.available() or pios.explicit_path:
        try:
            return pios.load()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.error("PIOS snapshot unreadable; portfolio analysis unavailable", extra={"reason": str(exc)})
            return PortfolioContext(source="PIOS_UNAVAILABLE", notes=str(exc))
    return EncryptedPortfolioProvider().load()
