#!/usr/bin/env python3
"""Data-transfer objects shared by fetch / generate / validate / review scripts.

pydantic v2. These models ARE the schema of data/*.json — change them here only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

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
DeliveryState = Literal["NOT_GENERATED", "GENERATING", "GENERATED", "VALIDATING", "VALIDATED", "DELIVERING", "DELIVERED", "BLOCKED", "FAILED"]


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
    change_interval: Literal["PREVIOUS_CLOSE", "ROLLING_24H", "SESSION_TO_SESSION", "UNKNOWN"] = "PREVIOUS_CLOSE"
    quality_notes: list[str] = Field(default_factory=list)
    corporate_action_note: str | None = None
    market: str = ""

    @model_validator(mode="before")
    @classmethod
    def _legacy_interval(cls, values):
        if isinstance(values, dict) and "change_interval" not in values:
            values = dict(values)
            if values.get("provider") in {"coingecko_simple_price", "coinbase_exchange"}:
                values["change_interval"] = "ROLLING_24H"
        return values

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
    advancing_prev: int | None = None
    declining_prev: int | None = None
    unchanged_prev: int | None = None
    session_date: str | None = None
    previous_session_date: str | None = None
    source: str | None = None
    retrieved_at: datetime | None = None
    previous_change_pct: float | None = None
    turnover_prev_ntd_billions: float | None = None

    @computed_field
    @property
    def net_breadth(self) -> int | None:
        if self.advancing is None or self.declining is None:
            return None
        return self.advancing - self.declining

    @computed_field
    @property
    def previous_net_breadth(self) -> int | None:
        if self.advancing_prev is None or self.declining_prev is None:
            return None
        return self.advancing_prev - self.declining_prev

    @computed_field
    @property
    def net_breadth_change(self) -> int | None:
        if self.net_breadth is None or self.previous_net_breadth is None:
            return None
        return self.net_breadth - self.previous_net_breadth


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
    investment_trust_buy_sell_prev_ntd_billions: float | None = None
    dealer_buy_sell_prev_ntd_billions: float | None = None
    twd_direction: str | None = None
    session_date: str | None = None
    previous_session_date: str | None = None
    source: str | None = None
    retrieved_at: datetime | None = None


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
    missing_portfolio_items: list[str] = Field(default_factory=list)
    missing_core_market_items: list[str] = Field(default_factory=list)
    missing_optional_context_items: list[str] = Field(default_factory=list)
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
    missing_portfolio_items: list[str] = Field(default_factory=list)
    missing_core_market_items: list[str] = Field(default_factory=list)
    missing_optional_context_items: list[str] = Field(default_factory=list)
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
    basis_quality: Literal["VERIFIED", "UNRESOLVED"] = "UNRESOLVED"
    basis_currency: str | None = None
    target_weight: float | None = Field(default=None, ge=0, le=1)
    max_weight: float | None = Field(default=None, gt=0, le=1)
    allocation_verified: bool = False


class PortfolioContext(BaseModel):
    snapshot_id: str | None = None
    as_of: datetime | None = None
    source: str = "UNAVAILABLE"
    positions: list[PositionContext] = Field(default_factory=list)
    cash: list[CashContext] = Field(default_factory=list)
    liabilities: list[LiabilityContext] = Field(default_factory=list)
    notes: str = ""
    allocation_limits: dict[str, float] = Field(default_factory=dict)


DecisionAction = Literal["ADD", "HOLD", "REDUCE", "EXIT", "WATCH"]
DecisionConfidence = Literal["HIGH", "MEDIUM", "LOW"]


class HoldingDecisionEvidence(BaseModel):
    """Optional private, source-tied research input; never Gemini's action vote."""
    instrument_id: str
    verified_at: datetime
    verified: bool = False
    source_ids: list[str] = Field(default_factory=list)
    thesis_status: Literal["UNKNOWN", "INTACT", "IMPROVING", "WEAKENED", "INVALIDATED"] = "UNKNOWN"
    fundamental_signal: Literal["UNKNOWN", "STABLE", "IMPROVING", "WEAKENING"] = "UNKNOWN"
    valuation_signal: Literal["UNKNOWN", "ATTRACTIVE", "FAIR", "EXPENSIVE"] = "UNKNOWN"
    event_effect: Literal["NONE", "IMPROVING", "WEAKENING", "THESIS_BREAK", "RESERVE_RISK", "REDEMPTION_BLOCK"] = "NONE"
    event_source_url: str = ""
    thesis_basis_zh: str = ""
    liquidity_verified: bool = False


class PortfolioDecision(BaseModel):
    position_id: str
    ticker: str
    instrument_id: str
    name: str
    asset_type: str
    market: str
    currency: str
    recommendation: DecisionAction
    confidence: DecisionConfidence
    quantity: float
    quote_id: str | None = None
    quote_as_of: datetime | None = None
    quote_market_date: str | None = None
    quote_session: Session | None = None
    current_price: float | None = None
    change_pct: float | None = None
    change_interval: str = "UNKNOWN"
    cost_basis: float | None = None
    basis_quality: Literal["VERIFIED", "UNRESOLVED"] = "UNRESOLVED"
    unrealized_pnl_pct: float | None = None
    position_market_value: float | None = None
    position_market_value_twd: float | None = None
    portfolio_market_value_twd: float | None = None
    portfolio_weight: float | None = None
    weight_quality: str = "UNAVAILABLE"
    max_weight: float | None = None
    target_weight: float | None = None
    liquidity_verified: bool = False
    allocation_source: str = ""
    price_signal: str = "UNKNOWN"
    valuation_signal: str = "UNKNOWN"
    fundamental_signal: str = "UNKNOWN"
    event_signal: str = "UNKNOWN"
    event_impact_verified: bool = False
    concentration_signal: str = "UNKNOWN"
    thesis_status: str = "UNKNOWN"
    rule_id: str
    reasons: list[str]
    risks: list[str]
    add_trigger: str
    reduce_trigger: str
    exit_trigger: str
    watch_condition: str = ""
    data_gaps: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    verified_event: PortfolioEventFact | None = None
    event_checks: list[PortfolioEventFact] = Field(default_factory=list)
    overlap_peers: list[str] = Field(default_factory=list)
    event_severity: int = 0
    sizing_status: str = "NOT_COMPUTED"
    trade_quantity: float | None = None


class PortfolioDecisionBrief(BaseModel):
    schema_version: str = "portfolio-decision-v1"
    run_id: str
    as_of: datetime
    portfolio_snapshot_id: str | None = None
    policy_version: str
    weight_basis: str
    policy_description_zh: str
    expected_positions: int
    quote_covered_positions: int
    event_checked_positions: int
    decisions: list[PortfolioDecision]
    summary_counts: dict[str, int]
    priority_position_ids: list[str]
    cash_status_zh: str
    valuation_notes: list[str] = Field(default_factory=list)


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


PortfolioEventStatus = Literal[
    "EVENT_CHECKED_NO_MATERIAL_CHANGE",
    "EVENT_MATERIAL_FOUND",
    "EVENT_CHECK_FAILED",
    "EVENT_UNCHECKED",
]


class PortfolioEventFact(BaseModel):
    instrument_id: str
    ticker: str
    checked_at: datetime
    event_status: PortfolioEventStatus
    event_type: str | None = None
    title: str | None = None
    event_date: str | None = None
    published_at: datetime | None = None
    summary: str = ""
    impact: str = ""
    severity: str = "LOW"
    source_name: str = ""
    source_url: str = ""
    source_type: str = ""
    is_upcoming: bool = False
    checked_window_start: datetime | None = None
    checked_window_end: datetime | None = None
    source_published_at: datetime | None = None
    publication_date_verified: bool = False
    display_title_zh: str = ""
    raw_source_title: str = ""
    fact_summary: str = ""
    investment_interpretation: str = ""
    uncertainty_note: str = ""


class PortfolioActionItem(BaseModel):
    instrument_id: str
    ticker: str
    status: PortfolioMonitoringStatus
    quote_id: str | None = None
    reference_price: float | None = None
    change_pct: float | None = None
    session: Session | None = None
    as_of: datetime | None = None
    reason_codes: list[str] = Field(default_factory=list)
    summary: str = ""
    next_step: str = ""
    trigger: Trigger | None = None
    price_status: str = "PRICE_NORMAL"
    event_status: str = "EVENT_UNCHECKED"
    verified_event: dict | PortfolioEventFact | None = None
    asset_type: str = "EQUITY"
    change_interval: Literal["PREVIOUS_CLOSE", "ROLLING_24H", "SESSION_TO_SESSION", "UNKNOWN"] = "PREVIOUS_CLOSE"


class PortfolioActionBrief(BaseModel):
    run_id: str
    as_of: datetime
    market_session: Session
    data_quality: list[str] = Field(default_factory=list)
    action_queue: list[PortfolioActionItem] = Field(default_factory=list)
    watchlist: list[PortfolioActionItem] = Field(default_factory=list)
    no_material_change: list[PortfolioActionItem] = Field(default_factory=list)
    upcoming_events: list[dict | str] = Field(default_factory=list)
    data_issues: list[str] = Field(default_factory=list)
    events_verified: bool = False
    covered_positions: int | None = None
    total_positions: int | None = None
    event_checked_positions: int = 0
    event_total_positions: int = 0
    event_check_failed_positions: int = 0
    event_coverage_ratio: float = 0.0
    event_facts: list[PortfolioEventFact] = Field(default_factory=list)
    suppressed_events: list[dict] = Field(default_factory=list)


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
