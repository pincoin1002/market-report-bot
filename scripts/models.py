#!/usr/bin/env python3
"""Data-transfer objects shared by fetch / generate / validate / review scripts.

pydantic v2. These models ARE the schema of data/*.json — change them here only.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReportType = Literal["tw_open", "tw_close", "us_open", "us_close"]


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


class Snapshot(BaseModel):
    generated_at: datetime
    report_type: ReportType
    fetch_coverage: float = Field(ge=0, le=1, default=1.0)
    sources: dict[str, str] = {}          # symbol → provider name
    tw_stocks: dict[str, NamedQuote] = {}
    us_markets: dict[str, NamedQuote] = {}
    forex: dict[str, NamedQuote] = {}

    def ground_truth(self) -> dict[str, float]:
        gt: dict[str, float] = {}
        for section in (self.tw_stocks, self.us_markets, self.forex):
            for key, q in section.items():
                gt[key] = q.price
        return gt


class Position(BaseModel):
    ticker: str
    name: str
    shares: float = Field(gt=0)
    cost_basis: float = Field(gt=0)
    note: str = ""


class Portfolio(BaseModel):
    tw_positions: list[Position] = []
    us_positions: list[Position] = []
    available_cash: str | float | None = None
    portfolio_notes: str = ""


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
