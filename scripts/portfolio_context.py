#!/usr/bin/env python3
"""Portfolio context provider boundary for future PIOS integration."""

from __future__ import annotations

from typing import Protocol

import portfolio_store
from instrument_registry import resolve_instrument
from models import CashContext, PortfolioContext, PositionContext


class PortfolioContextProvider(Protocol):
    def load(self) -> PortfolioContext: ...


class EncryptedPortfolioProvider:
    def load_raw(self) -> dict | None:
        return portfolio_store.load_portfolio()

    def load(self) -> PortfolioContext:
        raw = self.load_raw() or {}
        positions: list[PositionContext] = []
        for bucket in ("tw_positions", "us_positions"):
            for pos in raw.get(bucket, []):
                spec = resolve_instrument(str(pos.get("ticker", "")))
                quantity = float(pos.get("quantity") or pos.get("shares") or 0)
                if quantity <= 0:
                    continue
                positions.append(PositionContext(
                    instrument_id=spec.canonical_symbol,
                    ticker=spec.canonical_symbol,
                    name=str(pos.get("name") or spec.display_name),
                    account=pos.get("account"),
                    quantity=quantity,
                    cost_basis=pos.get("cost_basis"),
                    currency=pos.get("currency") or spec.currency,
                    asset_type=pos.get("asset_type") or spec.asset_type,
                    quote_id=pos.get("quote_id"),
                    note=str(pos.get("note") or ""),
                ))
        cash = []
        value = raw.get("available_cash")
        if isinstance(value, (int, float)) and value > 0:
            cash.append(CashContext(currency="TWD", amount=float(value), deployable=True))
        return PortfolioContext(positions=positions, cash=cash,
                                notes=str(raw.get("portfolio_notes") or ""))


class PIOSPortfolioProvider:
    def load(self) -> PortfolioContext:
        raise NotImplementedError("PIOS provider is a future read-only integration boundary")
