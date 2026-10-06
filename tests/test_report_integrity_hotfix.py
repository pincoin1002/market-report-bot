import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from models import MarketContext, MarketReportDraft, OptionalModule
from structured_reports import _render_tw_close_report
from twse_market_evidence import (
    INSTITUTIONAL_URL,
    MI_INDEX_URL,
    _institutional_flows,
    _market_summary,
    fetch_twse_close_evidence,
)


def market_payload(date: str, turnover_ntd: int = 938_222_196_327):
    return {
        "stat": "OK",
        "date": date,
        "tables": [
            {
                "title": "115年10月02日 價格指數(臺灣證券交易所)",
                "data": [["發行量加權股價指數", "48,475.74", "+", "122.25", "0.25", ""]],
            },
            {
                "title": "115年10月02日 大盤統計資訊",
                "data": [["總計(1~15)", f"{turnover_ntd:,}", "0", "0"]],
            },
            {
                "title": "漲跌證券數合計",
                "data": [
                    ["上漲(漲停)", "8,830(159)", "483(24)"],
                    ["下跌(跌停)", "5,152(54)", "506(1)"],
                    ["持平", "1,115", "91"],
                ],
            },
        ],
    }


def flow_payload(date: str):
    return {
        "stat": "OK",
        "date": date,
        "data": [
            ["自營商(自行買賣)", "0", "0", "4,741,375,500"],
            ["自營商(避險)", "0", "0", "-2,714,959,070"],
            ["投信", "0", "0", "5,768,811,349"],
            ["外資及陸資(不含外資自營商)", "0", "0", "2,621,799,962"],
            ["合計", "0", "0", "10,417,027,741"],
        ],
    }


class TWSEEvidenceHotfixTest(unittest.TestCase):
    def test_rwd_endpoints_are_used(self):
        self.assertIn("/rwd/zh/afterTrading/MI_INDEX", MI_INDEX_URL)
        self.assertIn("/rwd/zh/fund/BFI82U", INSTITUTIONAL_URL)

    def test_turnover_is_rendering_unit_yi_not_ntd_billions(self):
        summary = _market_summary(
            market_payload("20261002"),
            "2026-10-02",
            "2026-10-01",
            datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(summary.turnover_ntd_billions, 9382.22, places=2)
        self.assertEqual(summary.advancing, 483)
        self.assertEqual(summary.declining, 506)
        self.assertEqual(summary.unchanged, 91)

    def test_institutional_flows_are_in_yi(self):
        flows = _institutional_flows(
            flow_payload("20261002"),
            flow_payload("20261001"),
            "2026-10-02",
            "2026-10-01",
            datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(flows.foreign_buy_sell_ntd_billions, 26.22, places=2)
        self.assertAlmostEqual(flows.investment_trust_buy_sell_ntd_billions, 57.69, places=2)
        self.assertAlmostEqual(flows.dealer_buy_sell_ntd_billions, 20.26, places=2)
        self.assertAlmostEqual(flows.total_buy_sell_ntd_billions, 104.17, places=2)

    def test_bfi82u_uses_documented_day_date_parameter(self):
        current_market = market_payload("20261002")
        previous_market = market_payload("20261001", 900_000_000_000)
        current_flow = flow_payload("20261002")
        previous_flow = flow_payload("20261001")

        def fake_get(url, params):
            if "MI_INDEX" in url:
                return current_market if params["date"] == "20261002" else previous_market
            self.assertIn("dayDate", params)
            return current_flow if params["dayDate"] == "20261002" else previous_flow

        with patch("twse_market_evidence._get_json", side_effect=fake_get):
            evidence = fetch_twse_close_evidence(
                "2026-10-02",
                "2026-10-01",
                datetime(2026, 10, 2, tzinfo=timezone.utc),
            )
        self.assertIsNotNone(evidence)
        self.assertAlmostEqual(evidence.taiex_summary.turnover_ntd_billions, 9382.22, places=2)
        self.assertAlmostEqual(evidence.institutional_flows.total_buy_sell_ntd_billions, 104.17, places=2)


class PortfolioPerformanceSafetyTest(unittest.TestCase):
    def test_tw_close_does_not_publish_mixed_window_portfolio_return(self):
        context = MarketContext(
            run_id="tw_close:2026-10-02",
            report_type="tw_close",
            market_date="2026-10-02",
            generated_at=datetime(2026, 10, 2, 13, 45, tzinfo=timezone.utc),
            market_session="REGULAR",
        )
        draft = MarketReportDraft(
            run_id=context.run_id,
            report_type="tw_close",
            headline="test",
            portfolio_section=OptionalModule(
                name="portfolio",
                state="AVAILABLE",
                summary="行情覆蓋：23/23 FULL",
            ),
        )
        rendered = _render_tw_close_report(draft, context)
        self.assertNotIn("行情覆蓋：23/23 FULL", rendered)
        self.assertNotIn("跨市場部位", rendered)
        self.assertNotIn("本次組合變動", rendered)
        self.assertNotIn("相對台股大盤", rendered)
        self.assertNotIn("總資產：", rendered)


if __name__ == "__main__":
    unittest.main()
