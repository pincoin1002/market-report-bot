"""Official TWSE close statistics for Taiwan-close reporting.

These endpoints are exchange-published JSON, not scraped pages.  The module
is intentionally read-only and keeps both session dates and source URLs in
the structured artifact so the renderer never needs to infer a calendar day.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from models import InstitutionalFlows, TaiexMarketSummary


log = logging.getLogger("twse_market_evidence")
MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
INSTITUTIONAL_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
# User-facing Taiwanese market reports render monetary statistics in 億元.
# Keep the conversion explicit: 1 億 = NT$100,000,000.
NTD_PER_YI = 100_000_000


def _compact_number(value: str) -> float:
    return float(str(value).replace(",", "").strip())


def _count(value: str) -> int:
    # TWSE renders e.g. "374(24)" for advances (limit-up count in brackets).
    match = re.match(r"\s*([\d,]+)", str(value))
    if not match:
        raise ValueError(f"invalid TWSE breadth value: {value!r}")
    return int(match.group(1).replace(",", ""))


def _twse_date(session_date: str) -> str:
    return session_date.replace("-", "")


def _get_json(url: str, params: dict[str, str]) -> dict:
    response = requests.get(
        url, params=params, headers={"User-Agent": "market-report-bot/1.0"}, timeout=25
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("stat") != "OK":
        raise ValueError(f"TWSE {url.rsplit('/', 1)[-1]} response unavailable: {payload.get('stat') if isinstance(payload, dict) else 'invalid JSON'}")
    return payload


def _table(payload: dict, title_fragment: str) -> dict:
    for table in payload.get("tables", []):
        if title_fragment in str(table.get("title") or ""):
            return table
    raise ValueError(f"TWSE table missing: {title_fragment}")


def _row(table: dict, label: str) -> list[str]:
    for row in table.get("data", []):
        if row and str(row[0]) == label:
            return row
    raise ValueError(f"TWSE row missing: {label}")


def _market_summary(payload: dict, session_date: str, previous_date: str,
                    retrieved_at: datetime) -> TaiexMarketSummary:
    index_table = _table(payload, "價格指數(臺灣證券交易所)")
    index_row = _row(index_table, "發行量加權股價指數")
    close = _compact_number(index_row[1])
    point_change = _compact_number(index_row[3])
    if "-" in str(index_row[2]) or float(index_row[4]) < 0:
        point_change = -abs(point_change)
    else:
        point_change = abs(point_change)

    stats = _table(payload, "大盤統計資訊")
    total_row = _row(stats, "總計(1~15)")
    turnover_ntd_billions = _compact_number(total_row[1]) / NTD_PER_YI

    breadth = _table(payload, "漲跌證券數合計")
    advances = _count(_row(breadth, "上漲(漲停)")[2])
    declines = _count(_row(breadth, "下跌(跌停)")[2])
    unchanged = _count(_row(breadth, "持平")[2])
    return TaiexMarketSummary(
        close=close,
        point_change=point_change,
        change_pct=float(index_row[4]),
        turnover_ntd_billions=round(turnover_ntd_billions, 2),
        advancing=advances,
        declining=declines,
        unchanged=unchanged,
        session_date=session_date,
        previous_session_date=previous_date,
        source=MI_INDEX_URL,
        retrieved_at=retrieved_at,
    )


def _flow_rows(payload: dict) -> dict[str, float]:
    rows: dict[str, float] = {}
    for row in payload.get("data", []):
        if len(row) >= 4:
            rows[str(row[0])] = _compact_number(row[3]) / NTD_PER_YI
    return rows


def _institutional_flows(current: dict, previous: dict | None, session_date: str,
                         previous_date: str, retrieved_at: datetime) -> InstitutionalFlows:
    current_rows = _flow_rows(current)
    try:
        previous_rows = _flow_rows(previous) if previous else {}
        required = {"外資及陸資(不含外資自營商)", "投信", "自營商(自行買賣)", "自營商(避險)", "合計"}
        if previous and not required.issubset(previous_rows):
            raise ValueError("previous institutional fields incomplete")
    except (ValueError, TypeError, KeyError):
        previous = None
        previous_rows = {}
        log.warning("TWSE previous institutional comparison unavailable")

    def value(rows: dict[str, float], label: str) -> float:
        if label not in rows:
            raise ValueError(f"TWSE institutional row missing: {label}")
        return round(rows[label], 2)

    # BFI82U splits dealer proprietary and hedge trades.  The reported dealer
    # figure is their deterministic sum, consistent with TWSE's total row.
    def dealer(rows: dict[str, float]) -> float:
        return round(rows["自營商(自行買賣)"] + rows["自營商(避險)"], 2)

    foreign_label = "外資及陸資(不含外資自營商)"
    return InstitutionalFlows(
        foreign_buy_sell_ntd_billions=value(current_rows, foreign_label),
        investment_trust_buy_sell_ntd_billions=value(current_rows, "投信"),
        dealer_buy_sell_ntd_billions=dealer(current_rows),
        total_buy_sell_ntd_billions=value(current_rows, "合計"),
        foreign_buy_sell_prev_ntd_billions=value(previous_rows, foreign_label) if previous else None,
        investment_trust_buy_sell_prev_ntd_billions=value(previous_rows, "投信") if previous else None,
        dealer_buy_sell_prev_ntd_billions=dealer(previous_rows) if previous else None,
        total_buy_sell_prev_ntd_billions=value(previous_rows, "合計") if previous else None,
        session_date=session_date,
        previous_session_date=previous_date,
        source=INSTITUTIONAL_URL,
        retrieved_at=retrieved_at,
    )


@dataclass(frozen=True)
class TWSECloseEvidence:
    taiex_summary: TaiexMarketSummary | None
    institutional_flows: InstitutionalFlows | None
    previous_turnover_ntd_billions: float | None


def fetch_twse_close_evidence(session_date: str, previous_session_date: str,
                              retrieved_at: datetime | None = None) -> TWSECloseEvidence | None:
    """Fetch and validate both current and prior completed TWSE sessions."""
    retrieved = retrieved_at or datetime.now(tz=timezone.utc)
    def load(url, date, date_key, kind):
        try:
            payload = _get_json(url, {"response": "json", date_key: _twse_date(date), "type": kind})
            if payload.get("date") != _twse_date(date):
                raise ValueError("response date mismatch")
            return payload
        except Exception as exc:
            log.warning("TWSE close module unavailable", extra={"endpoint": url.rsplit('/', 1)[-1],
                        "session_date": date, "retrieved_at": retrieved.isoformat(), "reason": str(exc)})
            return None
    current_market = load(MI_INDEX_URL, session_date, "date", "ALL")
    previous_market = load(MI_INDEX_URL, previous_session_date, "date", "ALL")
    current_flows = load(INSTITUTIONAL_URL, session_date, "dayDate", "day")
    previous_flows = load(INSTITUTIONAL_URL, previous_session_date, "dayDate", "day")
    summary = previous_summary = flows = None
    try:
        previous_summary = _market_summary(previous_market, previous_session_date, "", retrieved) if previous_market else None
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("TWSE previous summary parser rejected", extra={"reason": str(exc)})
    try:
        if current_market:
            summary = _market_summary(current_market, session_date, previous_session_date, retrieved)
            if previous_summary:
                summary = summary.model_copy(update={"advancing_prev": previous_summary.advancing,
                    "declining_prev": previous_summary.declining, "unchanged_prev": previous_summary.unchanged,
                    "previous_change_pct": previous_summary.change_pct,
                    "turnover_prev_ntd_billions": previous_summary.turnover_ntd_billions})
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("TWSE current summary parser rejected", extra={"reason": str(exc)})
    try:
        if current_flows:
            flows = _institutional_flows(current_flows, previous_flows, session_date, previous_session_date, retrieved)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("TWSE institutional parser rejected", extra={"reason": str(exc)})
    return TWSECloseEvidence(summary, flows, previous_summary.turnover_ntd_billions if previous_summary else None) if summary or flows else None
