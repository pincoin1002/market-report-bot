"""Production-shaped regressions for the 2026-09-29 incident classes."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from instrument_registry import resolve_instrument
from models import InstitutionalFlows, PortfolioContext, TaiexMarketSummary
from providers import CoinbaseCryptoProvider, _quote_from_closes
from scheduler.us_open_dispatch import NY, handle_cron_request, scheduler_decision, should_dispatch
from twse_market_evidence import fetch_twse_close_evidence


class USOpenStagingWindowRegressionTest(unittest.TestCase):
    def test_both_dst_slots_can_stage_before_0905_but_never_after_0900(self):
        self.assertTrue(should_dispatch("edt", datetime(2026, 9, 29, 8, 0, tzinfo=NY)))
        self.assertTrue(should_dispatch("edt", datetime(2026, 9, 29, 8, 59, 59, tzinfo=NY)))
        self.assertTrue(should_dispatch("est", datetime(2026, 12, 29, 8, 0, tzinfo=NY)))
        self.assertFalse(should_dispatch("edt", datetime(2026, 9, 29, 9, 0, tzinfo=NY)))
        self.assertEqual(
            scheduler_decision("edt", datetime(2026, 9, 29, 9, 44, 45, tzinfo=NY)),
            "OUTSIDE_STAGING_WINDOW",
        )

    def test_late_external_invocation_dispatches_only_terminal_alert_workflow(self):
        late = datetime(2026, 9, 29, 9, 44, 45, tzinfo=NY)
        with patch("scheduler.us_open_dispatch.dispatch_workflow", return_value=204) as dispatch, patch.dict(
            os.environ,
            {"CRON_SECRET": "cron", "GITHUB_WORKFLOW_DISPATCH_TOKEN": "token"},
            clear=False,
        ):
            status, payload = handle_cron_request("edt", "Bearer cron", now=late)
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "SCHEDULER_WINDOW_EXPIRED")
        submitted = dispatch.call_args.args[1]
        self.assertEqual(submitted["inputs"]["scheduler_terminal_state"], "SCHEDULER_WINDOW_EXPIRED")
        self.assertEqual(submitted["inputs"]["intended_market_time"], "09:05")


class CrossMarketDailyCloseRegressionTest(unittest.TestCase):
    def test_completed_us_close_is_selected_instead_of_current_live_daily_bar(self):
        quote = _quote_from_closes([
            ("2026-09-25", 100.0),
            ("2026-09-28", 102.0),
            ("2026-09-29", 105.0),  # current US session, still live at TW close
        ], expected_date="2026-09-28")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.data_date, "2026-09-28")
        self.assertEqual(quote.price, 102.0)
        self.assertEqual(quote.prev_close, 100.0)


class CoinbaseBonkFallbackRegressionTest(unittest.TestCase):
    def test_registered_bonk_pair_keeps_coinbase_provenance(self):
        now = datetime.now(tz=timezone.utc)
        ticker = Mock()
        ticker.raise_for_status.return_value = None
        ticker.json.return_value = {"price": "0.000012", "time": now.isoformat().replace("+00:00", "Z")}
        stats = Mock()
        stats.raise_for_status.return_value = None
        stats.json.return_value = {"open": "0.000010"}
        with patch("providers.requests.get", side_effect=[ticker, stats]):
            observations = CoinbaseCryptoProvider().fetch_many([resolve_instrument("BONK")])
        bonk = observations["BONK"]
        self.assertEqual(bonk.provider, "coinbase_exchange")
        self.assertEqual(bonk.canonical_symbol, "BONK")
        self.assertIsNotNone(bonk.provider_timestamp)
        self.assertEqual(bonk.quality_status, "VALID")


class TWSEEvidenceRegressionTest(unittest.TestCase):
    @staticmethod
    def _market(date: str, turnover: str, advance: str, decline: str, unchanged: str) -> dict:
        return {
            "stat": "OK", "date": date,
            "tables": [
                {"title": "價格指數(臺灣證券交易所)", "data": [["發行量加權股價指數", "47,631.96", "-", "392.64", "-0.82"]]},
                {"title": "大盤統計資訊", "data": [["總計(1~15)", turnover, "1", "1"]]},
                {"title": "漲跌證券數合計", "data": [["上漲(漲停)", "0", advance], ["下跌(跌停)", "0", decline], ["持平", "0", unchanged]]},
            ],
        }

    @staticmethod
    def _flows(date: str, foreign: str) -> dict:
        return {
            "stat": "OK", "date": date,
            "data": [
                ["自營商(自行買賣)", "0", "0", "-100000000"],
                ["自營商(避險)", "0", "0", "-200000000"],
                ["投信", "0", "0", "300000000"],
                ["外資及陸資(不含外資自營商)", "0", "0", foreign],
                ["合計", "0", "0", "-600000000"],
            ],
        }

    def test_official_turnover_breadth_and_flows_require_both_session_dates(self):
        payloads = [
            self._market("20260929", "836,144,707,733", "374(24)", "586(0)", "112"),
            self._market("20260928", "700,000,000,000", "400(1)", "300(1)", "100"),
            self._flows("20260929", "-62582550525"),
            self._flows("20260928", "10000000000"),
        ]
        responses = []
        for payload in payloads:
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = payload
            responses.append(response)
        with patch("twse_market_evidence.requests.get", side_effect=responses):
            evidence = fetch_twse_close_evidence("2026-09-29", "2026-09-28")
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.taiex_summary.session_date, "2026-09-29")
        self.assertEqual(evidence.taiex_summary.previous_session_date, "2026-09-28")
        self.assertEqual(evidence.taiex_summary.advancing, 374)
        self.assertEqual(evidence.taiex_summary.declining_prev, 300)
        self.assertEqual(evidence.previous_turnover_ntd_billions, 700.0)
        self.assertEqual(evidence.institutional_flows.foreign_buy_sell_ntd_billions, -62.58)
        self.assertEqual(evidence.institutional_flows.dealer_buy_sell_ntd_billions, -0.3)

    def test_official_taiex_summary_also_enters_quote_and_renderer_contract(self):
        import fetch_market_data
        from twse_market_evidence import TWSECloseEvidence

        as_of = datetime(2026, 9, 30, 15, 5, tzinfo=timezone(timedelta(hours=8)))
        summary = TaiexMarketSummary(
            close=47940.13, point_change=308.17, change_pct=0.65,
            session_date="2026-09-30", previous_session_date="2026-09-29",
        )
        evidence = TWSECloseEvidence(summary, InstitutionalFlows(), 836.14)
        with patch("fetch_market_data.load_authoritative_portfolio", return_value=PortfolioContext(source="fixture")), patch(
            "fetch_market_data.build_universe", return_value={"TAIEX": resolve_instrument("TAIEX")}
        ), patch("fetch_market_data.fetch_session_observations", return_value=({}, {})), patch(
            "fetch_market_data.get_target_market_date",
            side_effect=["2026-09-30", "2026-09-29", "2026-09-30"],
        ), patch("fetch_market_data.get_previous_completed_session_date", return_value="2026-09-29"), patch(
            "fetch_market_data.fetch_twse_close_evidence", return_value=evidence
        ):
            snapshot = fetch_market_data.build_snapshot("tw_close", retrieved_at=as_of)
        self.assertEqual(snapshot.quote_observations["TAIEX"].provider, "twse_mi_index")
        self.assertEqual(snapshot.quote_observations["TAIEX"].market_date, "2026-09-30")
        self.assertEqual(snapshot.tw_stocks["TAIEX"].price, 47940.13)
