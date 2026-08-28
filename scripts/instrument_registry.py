#!/usr/bin/env python3
"""Deterministic instrument registry.

The registry is the source of truth for ticker identity. LLMs may explain
market context, but they must not decide whether a portfolio ticker is real or
which provider symbol represents it.
"""

from __future__ import annotations

from models import InstrumentSpec


CORE_MARKET_SYMBOLS = [
    "SPX", "NDX", "DJI", "RUT", "SOX", "VIX", "TNX", "US2Y", "DXY", "BTC",
    "TAIEX", "GC", "CL", "TSM", "NVDA", "AAPL", "MSFT", "META", "GOOGL",
    "AMZN", "TSLA", "AVGO", "AMD", "MRVL", "MU", "ARM", "ASML", "SMCI",
    "DELL", "HPE", "ANET", "VRT", "COHR", "CEG", "VST", "ETN", "GEV",
    "PWR", "OKLO", "SMR", "APLD", "IREN", "FLNC", "SPY", "QQQ", "SOXX",
    "SMH", "XLK", "ARKK", "USDTWD",
    "2330", "2317", "2454", "2308", "2382", "2327", "2303", "3711",
    "2356", "3231", "2383", "4958", "2368", "3017", "3324", "2421",
    "2301",
]


def _us(symbol: str, name: str, asset_type: str = "EQUITY",
        provider: str | None = None, entity: str | None = None) -> InstrumentSpec:
    return InstrumentSpec(
        canonical_symbol=symbol,
        display_name=name,
        asset_type=asset_type,
        exchange="NASDAQ/NYSE",
        currency="USD",
        market="US",
        provider_symbols={"yfinance": provider or symbol, "yahoo_chart": provider or symbol},
        aliases=[],
        price_precision=2,
        lot_size=0.00001 if asset_type == "ETF" else 0.00001,
        session_support=["PREMARKET", "REGULAR", "AFTER_HOURS", "PREVIOUS_CLOSE"],
        economic_entity=entity or name,
    )


def _tw(symbol: str, name: str, provider: str | None = None,
        exchange: str = "TWSE", asset_type: str = "EQUITY") -> InstrumentSpec:
    provider_symbol = provider or f"{symbol}.TW"
    return InstrumentSpec(
        canonical_symbol=symbol,
        display_name=name,
        asset_type=asset_type,
        exchange=exchange,
        currency="TWD",
        market="TW",
        provider_symbols={"yfinance": provider_symbol, "yahoo_chart": provider_symbol},
        aliases=[provider_symbol],
        price_precision=2,
        lot_size=1,
        session_support=["REGULAR", "PREVIOUS_CLOSE"],
        economic_entity=name,
    )


def _macro(symbol: str, name: str, provider: str, currency: str = "") -> InstrumentSpec:
    return InstrumentSpec(
        canonical_symbol=symbol,
        display_name=name,
        asset_type="MACRO",
        exchange="",
        currency=currency,
        market="GLOBAL",
        provider_symbols={"yfinance": provider, "yahoo_chart": provider},
        aliases=[provider],
        price_precision=2,
        lot_size=1,
        session_support=["REGULAR", "PREVIOUS_CLOSE"],
        economic_entity=name,
    )


REGISTRY: dict[str, InstrumentSpec] = {
    "GOOG": _us("GOOG", "Alphabet Class C", entity="Alphabet"),
    "GOOGL": _us("GOOGL", "Alphabet Class A", entity="Alphabet"),
    "DRAM": _us("DRAM", "Roundhill Memory ETF", asset_type="ETF", entity="Roundhill Memory ETF"),
    "VOO": _us("VOO", "Vanguard S&P 500 ETF", asset_type="ETF"),
    "VTI": _us("VTI", "Vanguard Total Stock Market ETF", asset_type="ETF"),
    "QQQ": _us("QQQ", "Invesco QQQ Trust", asset_type="ETF"),
    "AMZN": _us("AMZN", "Amazon"),
    "MU": _us("MU", "Micron"),
    "NVDA": _us("NVDA", "NVIDIA"),
    "TSLA": _us("TSLA", "Tesla"),
    "AAPL": _us("AAPL", "Apple"),
    "MSFT": _us("MSFT", "Microsoft"),
    "META": _us("META", "Meta"),
    "TSM": _us("TSM", "Taiwan Semiconductor ADR"),
    "AVGO": _us("AVGO", "Broadcom"),
    "AMD": _us("AMD", "AMD"),
    "MRVL": _us("MRVL", "Marvell"),
    "ARM": _us("ARM", "ARM"),
    "ASML": _us("ASML", "ASML"),
    "SMCI": _us("SMCI", "Supermicro"),
    "DELL": _us("DELL", "Dell"),
    "HPE": _us("HPE", "HPE"),
    "ANET": _us("ANET", "Arista"),
    "VRT": _us("VRT", "Vertiv"),
    "COHR": _us("COHR", "Coherent"),
    "CEG": _us("CEG", "Constellation Energy"),
    "VST": _us("VST", "Vistra"),
    "ETN": _us("ETN", "Eaton"),
    "GEV": _us("GEV", "GE Vernova"),
    "PWR": _us("PWR", "Quanta Services"),
    "OKLO": _us("OKLO", "Oklo"),
    "SMR": _us("SMR", "NuScale Power"),
    "APLD": _us("APLD", "Applied Digital"),
    "IREN": _us("IREN", "Iris Energy"),
    "FLNC": _us("FLNC", "Fluence Energy"),
    "SPY": _us("SPY", "SPDR S&P 500 ETF", asset_type="ETF"),
    "SOXX": _us("SOXX", "iShares Semiconductor ETF", asset_type="ETF"),
    "SMH": _us("SMH", "VanEck Semiconductor ETF", asset_type="ETF"),
    "XLK": _us("XLK", "Technology Select Sector SPDR Fund", asset_type="ETF"),
    "ARKK": _us("ARKK", "ARK Innovation ETF", asset_type="ETF"),
    "0050": _tw("0050", "元大台灣50", asset_type="ETF"),
    "006208": _tw("006208", "富邦台50", asset_type="ETF"),
    "1519": _tw("1519", "華城"),
    "2327": _tw("2327", "國巨"),
    "2330": _tw("2330", "台積電"),
    "2383": _tw("2383", "台光電"),
    "2317": _tw("2317", "鴻海"),
    "2454": _tw("2454", "聯發科"),
    "2308": _tw("2308", "台達電"),
    "2382": _tw("2382", "廣達"),
    "2303": _tw("2303", "聯電"),
    "3711": _tw("3711", "日月光投控"),
    "2356": _tw("2356", "英業達"),
    "3231": _tw("3231", "緯創"),
    "4958": _tw("4958", "臻鼎-KY"),
    "2368": _tw("2368", "金像電"),
    "3017": _tw("3017", "奇鋐"),
    "3324": _tw("3324", "雙鴻", provider="3324.TWO", exchange="TPEX"),
    "2421": _tw("2421", "建準"),
    "2301": _tw("2301", "光寶科"),
    "SPX": _macro("SPX", "S&P 500", "^GSPC"),
    "NDX": _macro("NDX", "NASDAQ 100", "^NDX"),
    "DJI": _macro("DJI", "Dow Jones", "^DJI"),
    "RUT": _macro("RUT", "Russell 2000", "^RUT"),
    "SOX": _macro("SOX", "費城半導體 SOX", "^SOX"),
    "VIX": _macro("VIX", "VIX", "^VIX"),
    "TNX": _macro("TNX", "US10Y Yield", "^TNX", "percent"),
    "US2Y": _macro("US2Y", "US2Y Yield", "2YY=F", "percent"),
    "DXY": _macro("DXY", "美元指數", "DX-Y.NYB"),
    "BTC": _macro("BTC", "Bitcoin", "BTC-USD", "USD"),
    "TAIEX": _macro("TAIEX", "加權指數", "^TWII"),
    "GC": _macro("GC", "黃金 GC", "GC=F", "USD"),
    "CL": _macro("CL", "原油 WTI", "CL=F", "USD"),
    "USDTWD": _macro("USDTWD", "USD/TWD", "TWD=X", "TWD"),
}


def resolve_instrument(ticker: str) -> InstrumentSpec:
    symbol = ticker.upper().replace(".TW", "").replace(".TWO", "")
    if symbol in REGISTRY:
        return REGISTRY[symbol]
    if symbol.isdigit():
        return _tw(symbol, symbol)
    return _us(symbol, symbol)


def portfolio_symbols(portfolio: dict | None) -> list[str]:
    if not portfolio:
        return []
    symbols: list[str] = []
    for bucket in ("tw_positions", "us_positions"):
        for pos in portfolio.get(bucket, []):
            ticker = str(pos.get("ticker", "")).strip().upper()
            if ticker:
                symbols.append(resolve_instrument(ticker).canonical_symbol)
    return sorted(set(symbols))


def quote_symbol(spec: InstrumentSpec) -> str:
    return spec.provider_symbols["yfinance"]


def build_universe(portfolio: dict | None) -> dict[str, InstrumentSpec]:
    symbols = set(CORE_MARKET_SYMBOLS) | set(portfolio_symbols(portfolio))
    universe = {sym: resolve_instrument(sym) for sym in symbols}
    for sym in portfolio_symbols(portfolio):
        universe[sym].is_portfolio_critical = True
    return dict(sorted(universe.items()))
