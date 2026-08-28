#!/usr/bin/env python3
"""Data-transfer objects shared by fetch / generate / validate / review scripts.

pydantic v2. These models ARE the schema of data/*.json — change them here only.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReportType = Literal["tw_open", "tw_close", "us_open", "us_close"]
Session = Literal["PREMARKET", "REGULAR", "AFTER_HOURS", "PREVIOUS_CLOSE", "CLOSED_REFERENCE"]
QualityStatus = Literal["VALID", "STALE", "CONFLICTING", "SUSPECT", "UNAVAILABLE"]
QuoteType = Literal["TRADE", "MID", "INDICATIVE", "OFFICIAL_CLOSE", "REFERENCE"]
PriceReferenceKind = Literal[
    "CURRENT_QUOTE", "PREMARKET_QUOTE", "REGULAR_QUOTE", "AFTER_HOURS_QUOTE",
    "PREVIOUS_CLOSE", "ACTION_TRIGGER", "TECHNICAL_LEVEL", "VALUATION",
]
PortfolioMonitoringStatus = Literal[
    "NO_MATERIAL_CHANGE", "WATCH", "ACTION_REVIEW", "DATA_BLOCKED",
]
TriggerType = Literal[
    "FUNDAMENTAL", "EARNINGS", "VALUATION", "PORTFOLIO_RISK", "TECHNICAL", "EVENT",
]
OptionalModuleState = Literal["AVAILABLE", "PARTIAL", "UNAVAILABLE"]
DeliveryState = Literal["GENERATING", "VALIDATING", "VALIDATED", "DELIVERING", "DELIVERED", "BLOCKED"]


class Quote(BaseModel):
    price: float = Field(gt=0)
    prev_close: float = Field(gt=0)
    change_pct: float
    data_date: str  # YYYY-MM-DD

    @field_validator("data_date")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        datetime.strptime(v[:10].replace("/", "-"), "%Y-%m-%d")
        return v


class NamedQuote(Quote):
    name: str
    currency: str = ""
    symbol: str = ""


class InstrumentSpec(BaseModel):
    canonical_symbol: str
    display_name: str
    asset_type: str
    exchange: str
    currency: str
    market: str
    provider_symbols: dict[str, str]
    aliases: list[str] = Field(default_factory=list)
    price_precision: int = 2
    lot_size: float = 1
    session_support: list[Session] = Field(default_factory=list)
    economic_entity: str = ""
    is_portfolio_critical: bool = False


class ProviderHealth(BaseModel):
    provider: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    notes: list[str] = Field(default_factory=list)


class QuoteObservation(BaseModel):
    quote_id: str
    instrument_id: str
    canonical_symbol: str
    price: float = Field(gt=0)
    currency: str
    session: Session
    market_date: str
    observed_at: datetime
    provider_timestamp: datetime | None = None
    retrieved_at: datetime
    provider: str
    quote_type: QuoteType
    is_delayed: bool = True
    quality_status: QualityStatus
    previous_regular_close: float = Field(gt=0)
    change_pct: float
    quality_notes: list[str] = Field(default_factory=list)
    corporate_action_note: str | None = None

    @field_validator("market_date")
    @classmethod
    def _market_iso_date(cls, v: str) -> str:
        datetime.strptime(v[:10].replace("/", "-"), "%Y-%m-%d")
        return v


class Snapshot(BaseModel):
    generated_at: datetime
    report_type: ReportType
    fetch_coverage: float = Field(ge=0, le=1, default=1.0)
    market_context_coverage: float = Field(ge=0, le=1, default=1.0)
    portfolio_quote_coverage: float | None = Field(default=None, ge=0, le=1)
    sources: dict[str, str] = Field(default_factory=dict)          # symbol → provider name
    tw_stocks: dict[str, NamedQuote] = Field(default_factory=dict)
    us_markets: dict[str, NamedQuote] = Field(default_factory=dict)
    forex: dict[str, NamedQuote] = Field(default_factory=dict)
    quote_observations: dict[str, QuoteObservation] = Field(default_factory=dict)
    missing_required_items: list[str] = Field(default_factory=list)
    data_quality: dict[str, str] = Field(default_factory=dict)

    def ground_truth(self) -> dict[str, float]:
        gt: dict[str, float] = {}
        for section in (self.tw_stocks, self.us_markets, self.forex):
            for key, q in section.items():
                gt[key] = q.price
        return gt


class PriceReference(BaseModel):
    instrument_id: str
    canonical_symbol: str
    value: float = Field(gt=0)
    kind: PriceReferenceKind
    quote_id: str | None = None
    session: Session | None = None
    as_of: datetime | None = None
    source: str = "market_context"


class MarketContext(BaseModel):
    run_id: str
    report_type: ReportType
    market_date: str
    generated_at: datetime
    market_session: Session
    quotes: dict[str, QuoteObservation] = Field(default_factory=dict)
    macro_observations: dict[str, QuoteObservation] = Field(default_factory=dict)
    market_quote_coverage: float = Field(ge=0, le=1, default=1.0)
    portfolio_quote_coverage: float | None = Field(default=None, ge=0, le=1)
    provider_health: list[ProviderHealth] = Field(default_factory=list)
    data_quality: dict[str, str] = Field(default_factory=dict)
    event_facts: list[dict] = Field(default_factory=list)
    material_changes: list[str] = Field(default_factory=list)
    missing_required_items: list[str] = Field(default_factory=list)
    degraded_mode: bool = False


class OptionalModule(BaseModel):
    name: str
    state: OptionalModuleState
    summary: str = ""


class MarketReportDraft(BaseModel):
    run_id: str
    report_type: ReportType
    headline: str
    market_state: list[str] = Field(default_factory=list)
    material_changes: list[str] = Field(default_factory=list)
    drivers: list[str] = Field(default_factory=list)
    rotation: str = ""
    event_calendar: list[str] = Field(default_factory=list)
    optional_modules: list[OptionalModule] = Field(default_factory=list)
    watch_signals: list[str] = Field(default_factory=list)
    data_quality: list[str] = Field(default_factory=list)
    price_references: list[PriceReference] = Field(default_factory=list)
    rendered_markdown: str = ""


class Position(BaseModel):
    instrument_id: str | None = None
    ticker: str
    name: str
    shares: float = Field(gt=0)
    quantity: float | None = Field(default=None, gt=0)
    cost_basis: float = Field(gt=0)
    currency: str | None = None
    asset_type: str | None = None
    quote_id: str | None = None
    account: str | None = None
    note: str = ""


class CashContext(BaseModel):
    currency: str
    amount: float
    deployable: bool = True


class PositionContext(BaseModel):
    instrument_id: str
    ticker: str
    name: str
    account: str | None = None
    quantity: float = Field(gt=0)
    cost_basis: float | None = Field(default=None, gt=0)
    currency: str
    asset_type: str
    quote_id: str | None = None
    note: str = ""


class PortfolioContext(BaseModel):
    positions: list[PositionContext] = Field(default_factory=list)
    cash: list[CashContext] = Field(default_factory=list)
    notes: str = ""


class Portfolio(BaseModel):
    tw_positions: list[Position] = Field(default_factory=list)
    us_positions: list[Position] = Field(default_factory=list)
    available_cash: str | float | None = None
    portfolio_notes: str = ""


class Trigger(BaseModel):
    trigger_type: TriggerType
    condition: str
    numeric_value: float | None = None
    basis: str
    source_ids: list[str] = Field(default_factory=list)
    generated_by: str
    valid_until: str | None = None


class PortfolioActionItem(BaseModel):
    instrument_id: str
    ticker: str
    status: PortfolioMonitoringStatus
    quote_id: str | None = None
    reference_price: float | None = None
    session: Session | None = None
    as_of: datetime | None = None
    reason_codes: list[str] = Field(default_factory=list)
    summary: str = ""
    next_step: str = "SIZE_NOT_COMPUTED"
    trigger: Trigger | None = None


class PortfolioActionBrief(BaseModel):
    run_id: str
    as_of: datetime
    market_session: Session
    data_quality: list[str] = Field(default_factory=list)
    action_queue: list[PortfolioActionItem] = Field(default_factory=list)
    watchlist: list[PortfolioActionItem] = Field(default_factory=list)
    no_material_change: list[PortfolioActionItem] = Field(default_factory=list)
    upcoming_events: list[str] = Field(default_factory=list)
    data_issues: list[str] = Field(default_factory=list)


class StructureCheck(BaseModel):
    sections_total: int
    sections_present: int
    sections_missing: list[str]
    truncated: bool
    missing_data_count: int
    missing_data_pct: float
    char_count: int


class PriceCheckRow(BaseModel):
    ticker: str
    reported: float
    actual: float
    diff_pct: float


class PriceCheck(BaseModel):
    passed: list[PriceCheckRow] = []
    failed: list[PriceCheckRow] = []
    unchecked: list[dict] = []


class ValidationResult(BaseModel):
    structure: StructureCheck
    price_check: PriceCheck | None = None
