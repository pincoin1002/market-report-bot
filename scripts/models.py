#!/usr/bin/env python3
"""Data-transfer objects shared by fetch / generate / validate / review scripts.

pydantic v2. These models ARE the schema of data/*.json — change them here only.
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReportType = Literal["tw_open", "tw_close", "us_open", "us_close"]
Session = Literal["PREMARKET", "REGULAR", "AFTER_HOURS", "PREVIOUS_CLOSE", "CLOSED_REFERENCE"]
QualityStatus = Literal["VALID", "STALE", "CONFLICTING", "SUSPECT", "UNAVAILABLE", "DATE_MISMATCH", "DATA_BLOCKED"]
PortfolioQuoteState = Literal["QUOTED", "STALE", "MISSING", "UNSUPPORTED"]
PortfolioCoverageStatus = Literal["FULL", "DEGRADED", "UNAVAILABLE", "NOT_APPLICABLE"]
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
PipelineStatus = Literal["FULL", "DEGRADED", "BLOCKED"]
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
    market: str = ""

    @property
    def trading_date(self) -> str:
        return self.market_date

    @field_validator("market_date")
    @classmethod
    def _market_iso_date(cls, v: str) -> str:
        datetime.strptime(v[:10].replace("/", "-"), "%Y-%m-%d")
        return v


class TaiexMarketSummary(BaseModel):
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float
    point_change: float
    change_pct: float
    turnover_ntd_billions: float | None = None
    advancing: int | None = None
    declining: int | None = None
    unchanged: int | None = None


class InstitutionalFlows(BaseModel):
    foreign_buy_sell_ntd_billions: float | None = None
    investment_trust_buy_sell_ntd_billions: float | None = None
    dealer_buy_sell_ntd_billions: float | None = None
    total_buy_sell_ntd_billions: float | None = None
    foreign_futures_net_oi: int | None = None
    foreign_futures_oi_change: int | None = None
    foreign_buy_sell_prev_ntd_billions: float | None = None
    total_buy_sell_prev_ntd_billions: float | None = None
    turnover_prev_ntd_billions: float | None = None
    twd_direction: str | None = None


class PortfolioQuoteCoverageItem(BaseModel):
    """One canonical active position's quote-resolution result.

    This is deliberately runtime diagnostic data, never portfolio accounting
    state.  Every active price-requiring position gets exactly one item.
    """
    position_id: str
    instrument_id: str
    canonical_symbol: str
    quote_identifier: str | None = None
    state: PortfolioQuoteState
    reason: str
    provider: str | None = None
    quote_timestamp: datetime | None = None
    market_date: str | None = None


class PortfolioQuoteCoverage(BaseModel):
    expected_positions: int = Field(ge=0)
    covered_positions: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)
    as_of: datetime
    status: PortfolioCoverageStatus
    items: list[PortfolioQuoteCoverageItem] = Field(default_factory=list)
    missing: list[PortfolioQuoteCoverageItem] = Field(default_factory=list)
    stale: list[PortfolioQuoteCoverageItem] = Field(default_factory=list)
    unsupported: list[PortfolioQuoteCoverageItem] = Field(default_factory=list)

    @property
    def is_full(self) -> bool:
        return self.status == "FULL"


def _coerce_legacy_portfolio_coverage(value):
    """Read old scalar artifacts without treating them as valid diagnostics."""
    if isinstance(value, (int, float)):
        return {
            "expected_positions": 0,
            "covered_positions": 0,
            "coverage_ratio": float(value),
            "as_of": datetime.now(tz=timezone.utc),
            "status": "UNAVAILABLE",
        }
    return value


class Snapshot(BaseModel):
    generated_at: datetime
    report_type: ReportType
    report_market_date: str | None = None
    portfolio_snapshot_id: str | None = None
    portfolio_snapshot_as_of: datetime | None = None
    portfolio_source: str = "UNAVAILABLE"
    fetch_coverage: float = Field(ge=0, le=1, default=1.0)
    market_context_coverage: float = Field(ge=0, le=1, default=1.0)
    # Explicit names for operators.  Legacy aliases above are preserved for
    # stored artifacts and callers that predate the coverage split.
    requested_universe_coverage: float = Field(ge=0, le=1, default=1.0)
    validated_universe_coverage: float = Field(ge=0, le=1, default=1.0)
    portfolio_quote_coverage: PortfolioQuoteCoverage | None = None
    sources: dict[str, str] = Field(default_factory=dict)          # symbol → provider name
    tw_stocks: dict[str, NamedQuote] = Field(default_factory=dict)
    us_markets: dict[str, NamedQuote] = Field(default_factory=dict)
    forex: dict[str, NamedQuote] = Field(default_factory=dict)
    quote_observations: dict[str, QuoteObservation] = Field(default_factory=dict)
    missing_required_items: list[str] = Field(default_factory=list)
    data_quality: dict[str, str] = Field(default_factory=dict)
    taiex_summary: TaiexMarketSummary | None = None
    institutional_flows: InstitutionalFlows | None = None

    @field_validator("portfolio_quote_coverage", mode="before")
    @classmethod
    def _legacy_coverage(cls, value):
        return _coerce_legacy_portfolio_coverage(value)

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
    requested_universe_coverage: float = Field(ge=0, le=1, default=1.0)
    validated_universe_coverage: float = Field(ge=0, le=1, default=1.0)
    portfolio_quote_coverage: PortfolioQuoteCoverage | None = None
    portfolio_snapshot_id: str | None = None
    portfolio_snapshot_as_of: datetime | None = None
    portfolio_source: str = "UNAVAILABLE"
    provider_health: list[ProviderHealth] = Field(default_factory=list)
    data_quality: dict[str, str] = Field(default_factory=dict)
    event_facts: list[dict] = Field(default_factory=list)
    material_changes: list[str] = Field(default_factory=list)
    missing_required_items: list[str] = Field(default_factory=list)
    degraded_mode: bool = False
    pipeline_health: dict[str, str] = Field(default_factory=dict)
    final_status: PipelineStatus = "FULL"
    taiex_summary: TaiexMarketSummary | None = None
    institutional_flows: InstitutionalFlows | None = None

    @field_validator("portfolio_quote_coverage", mode="before")
    @classmethod
    def _legacy_coverage(cls, value):
        return _coerce_legacy_portfolio_coverage(value)


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
    taiex_summary: TaiexMarketSummary | None = None
    institutional_flows: InstitutionalFlows | None = None
    why_drivers: list[str] = Field(default_factory=list)
    portfolio_section: OptionalModule | None = None



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
    deployable: bool | None = None


class LiabilityContext(BaseModel):
    liability_id: str
    name: str
    currency: str
    outstanding_principal: float = Field(ge=0)
    monthly_payment: float | None = Field(default=None, ge=0)


class PositionContext(BaseModel):
    position_id: str
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
    snapshot_id: str | None = None
    as_of: datetime | None = None
    source: str = "UNAVAILABLE"
    positions: list[PositionContext] = Field(default_factory=list)
    cash: list[CashContext] = Field(default_factory=list)
    liabilities: list[LiabilityContext] = Field(default_factory=list)
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
