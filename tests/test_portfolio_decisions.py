"""Per-position decision contracts using a synthetic 23-position portfolio."""
import copy
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from models import (HoldingDecisionEvidence, MarketContext, PortfolioContext, PortfolioEventFact,
                    PositionContext, QuoteObservation)
from portfolio_context import PIOSPortfolioProvider
from portfolio_decisions import (ACTIONS_ZH, _holding_text, build_decision_brief, decision_telegram_chunks,
                                 load_policy, render_decision_brief, validate_decision_brief)
from structured_reports import build_public_draft
from generate_report import send_decision_telegram, run_portfolio_advice

NOW = datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc)


def quote(symbol, price=100, change=.2, market="US", interval="PREVIOUS_CLOSE"):
    return QuoteObservation(quote_id=f"{symbol}:synthetic", instrument_id=symbol, canonical_symbol=symbol,
        price=price, previous_regular_close=price / (1 + change / 100), change_pct=change,
        currency="TWD" if symbol == "USDTWD" else "USD", market=market, session="REGULAR",
        market_date="2026-10-07", observed_at=NOW, provider_timestamp=NOW, retrieved_at=NOW,
        provider="fixture", quote_type="OFFICIAL_CLOSE", quality_status="VALID", change_interval=interval)


class PerHoldingDecisionTest(unittest.TestCase):
    def setUp(self):
        self.portfolio = PortfolioContext(snapshot_id="synthetic-pios", source="PIOS_PORTFOLIO_SNAPSHOT", as_of=NOW,
            positions=[PositionContext(position_id=f"p{i}", instrument_id=f"TEST{i:02d}", ticker=f"TEST{i:02d}",
                       name=f"測試部位{i}", quantity=1, currency="USD", asset_type="EQUITY") for i in range(1, 24)])
        self.context = MarketContext(run_id="decisions", report_type="tw_close", market_date="2026-10-07",
            generated_at=NOW, market_session="REGULAR", quotes={p.ticker: quote(p.ticker) for p in self.portfolio.positions})
        self.context.quotes["USDTWD"] = quote("USDTWD", price=31.8, market="TW")

    def brief(self, events=None, evidence=None):
        return build_decision_brief(self.context, self.portfolio, events=events, evidence=evidence, as_of=NOW)

    def replace(self, ticker, asset="EQUITY", price=100, change=.2, interval="PREVIOUS_CLOSE"):
        old = self.portfolio.positions[0]
        self.context.quotes.pop(old.ticker)
        self.portfolio.positions[0] = old.model_copy(update={"ticker": ticker, "instrument_id": ticker, "asset_type": asset, "name": ticker})
        self.context.quotes[ticker] = quote(ticker, price, change, market="GLOBAL" if asset == "CRYPTO" else "US", interval=interval)

    def event(self, **values):
        args = dict(instrument_id=self.portfolio.positions[0].instrument_id, ticker=self.portfolio.positions[0].ticker,
            checked_at=NOW, event_status="EVENT_MATERIAL_FOUND", source_published_at=NOW - timedelta(hours=1),
            publication_date_verified=True, source_name="公司投資人關係網站", source_url="https://example.org/verified",
            title="Guidance revision", summary="Significant capital return to shareholders.", severity="HIGH")
        args.update(values)
        return PortfolioEventFact(**args)

    def evidence(self, **values):
        args = dict(instrument_id=self.portfolio.positions[0].instrument_id, verified_at=NOW, verified=True,
            source_ids=["synthetic-research-source"], thesis_status="INTACT", fundamental_signal="STABLE",
            valuation_signal="FAIR", thesis_basis_zh="已核對的營運資料支持原持有假設")
        args.update(values)
        return HoldingDecisionEvidence(**args)

    def clean_check(self):
        return self.event(event_status="EVENT_CHECKED_NO_MATERIAL_CHANGE", title=None)

    def test_01_every_active_holding_once(self):
        brief = self.brief()
        self.assertEqual({d.position_id for d in brief.decisions}, {p.position_id for p in self.portfolio.positions})
        self.assertEqual(len(brief.decisions), len({d.position_id for d in brief.decisions}))

    def test_02_23_positions_23_decisions(self):
        self.assertEqual(len(self.brief().decisions), 23)

    def test_03_quiet_position_not_omitted(self):
        self.context.quotes["TEST01"] = quote("TEST01", change=0)
        self.assertIn("TEST01", [d.ticker for d in self.brief().decisions])

    def test_04_under_7pct_still_recommended(self):
        self.assertIn(self.brief().decisions[0].recommendation, ACTIONS_ZH)

    def test_05_all_actions_have_chinese_names(self):
        self.assertEqual(set(ACTIONS_ZH.values()), {"加碼", "持有", "減碼", "退出", "觀察"})

    def test_06_no_raw_action_enums(self):
        text = render_decision_brief(self.brief())
        self.assertFalse(re.search(r"\b(?:ADD|HOLD|REDUCE|EXIT|WATCH|LOW|MEDIUM|HIGH)\b", text))

    def test_07_unresolved_basis_no_fake_pl(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"cost_basis": 80})
        decision = self.brief().decisions[0]
        self.assertIsNone(decision.cost_basis)
        self.assertIsNone(decision.unrealized_pnl_pct)

    def test_08_missing_basis_keeps_direction(self):
        self.assertIsNotNone(self.brief().decisions[0].recommendation)

    def test_09_verified_cost_and_pl(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"cost_basis": 80, "basis_quality": "VERIFIED"})
        decision = self.brief().decisions[0]
        self.assertEqual(decision.cost_basis, 80)
        self.assertEqual(decision.unrealized_pnl_pct, 25)
        self.assertIn("+25.00%", _holding_text(decision))

    def test_10_concentration_changes_direction(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"quantity": 100})
        self.assertEqual(self.brief().decisions[0].recommendation, "REDUCE")

    def test_11_price_rise_alone_not_reduce(self):
        self.context.quotes["TEST01"] = quote("TEST01", price=150, change=50)
        self.assertEqual(self.brief().decisions[0].recommendation, "WATCH")

    def test_12_price_drop_alone_not_add(self):
        self.context.quotes["TEST01"] = quote("TEST01", price=50, change=-50)
        self.assertNotEqual(self.brief().decisions[0].recommendation, "ADD")

    def test_13_source_linked_thesis_break_can_exit(self):
        evt = self.event()
        ev = self.evidence(thesis_status="INVALIDATED", event_effect="THESIS_BREAK", event_source_url=evt.source_url)
        brief = self.brief([evt], [ev])
        self.assertEqual(brief.decisions[0].recommendation, "EXIT")
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_14_stale_event_cannot_affect_action(self):
        evt = self.event(source_published_at=NOW - timedelta(days=8))
        ev = self.evidence(thesis_status="WEAKENED", event_effect="WEAKENING", event_source_url=evt.source_url)
        self.assertEqual(self.brief([evt], [ev]).decisions[0].recommendation, "WATCH")

    def test_15_event_missing_lowers_confidence(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"cost_basis": 80, "basis_quality": "VERIFIED"})
        checked = self.brief([self.clean_check()], [self.evidence()]).decisions[0]
        missing = self.brief([], [self.evidence()]).decisions[0]
        self.assertEqual(checked.confidence, "HIGH")
        self.assertEqual(missing.confidence, "LOW")

    def test_16_etf_role_logic(self):
        self.replace("VTI", "ETF")
        self.assertEqual(self.brief().decisions[0].recommendation, "HOLD")
        self.assertIn("分散持有美國", _holding_text(self.brief().decisions[0]))

    def test_17_crypto_logic(self):
        self.replace("BONK", "CRYPTO", .00000375, -7, "ROLLING_24H")
        self.assertIn("協議", _holding_text(self.brief().decisions[0]))

    def test_18_stablecoin_logic(self):
        self.replace("USDC", "CRYPTO", 1, 0)
        d = self.brief().decisions[0]
        self.assertEqual(d.asset_type, "STABLECOIN")
        self.assertEqual(d.recommendation, "HOLD")
        self.assertIn("兌回", _holding_text(d))

    def test_19_crypto_no_earnings_template(self):
        self.replace("BONK", "CRYPTO", .00000375, -7, "ROLLING_24H")
        self.assertNotIn("財報", _holding_text(self.brief().decisions[0]))

    def test_20_etf_no_company_earnings_template(self):
        self.replace("VTI", "ETF")
        self.assertNotIn("財報", _holding_text(self.brief().decisions[0]))

    def test_21_overlap_limits_blind_adds(self):
        self.replace("VTI", "ETF")
        p = self.portfolio.positions[1]
        self.context.quotes.pop(p.ticker)
        self.portfolio.positions[1] = p.model_copy(update={"ticker": "VOO", "instrument_id": "VOO", "asset_type": "ETF"})
        self.context.quotes["VOO"] = quote("VOO")
        d = self.brief().decisions[0]
        self.assertIn("VOO", d.overlap_peers)
        self.assertIn("不建議同時加碼", _holding_text(d))

    def test_22_traditional_chinese(self):
        text = render_decision_brief(self.brief())
        self.assertIn("【逐檔操作建議】", text)
        self.assertNotIn("建议", text)

    def test_23_no_english_interpretation(self):
        text = render_decision_brief(self.brief([self.event()], [self.evidence()]))
        self.assertNotIn("Significant capital", text)
        self.assertNotIn("Guidance revision", text)

    def test_24_reasons_for_every_holding(self):
        self.assertTrue(all(2 <= len(d.reasons) <= 3 for d in self.brief().decisions))

    def test_25_risks_for_every_holding(self):
        self.assertTrue(all(d.risks for d in self.brief().decisions))

    def test_26_watch_has_explicit_wait(self):
        self.assertTrue(all(d.watch_condition for d in self.brief().decisions if d.recommendation == "WATCH"))

    def test_27_every_decision_has_confidence(self):
        self.assertTrue(all(d.confidence in {"HIGH", "MEDIUM", "LOW"} for d in self.brief().decisions))

    def test_28_public_is_portfolio_free(self):
        text = build_public_draft(self.context).rendered_markdown
        for token in ("TEST01", "逐檔操作", "我的成本", "部位占比", "23/23"):
            self.assertNotIn(token, text)

    def test_29_decision_runtime_artifact_ignored_and_encrypted_for_upload(self):
        import subprocess
        result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "data/portfolio_decision_brief.json", "data/decision_brief_replay/private_decision_brief.txt"], capture_output=True)
        self.assertEqual(result.returncode, 0)
        for report in ("tw-open", "tw-close", "us-open", "us-close"):
            workflow = (ROOT / ".github" / "workflows" / f"{report}.yml").read_text()
            self.assertIn("data/portfolio_decision_brief.json.enc", workflow)
            self.assertNotIn("data/portfolio_decision_brief.json\n", workflow)

    def test_30_summary_equals_23(self):
        self.assertEqual(sum(self.brief().summary_counts.values()), 23)

    def test_31_priority_is_deterministic_and_size_before_volatility(self):
        self.context.quotes["TEST01"] = quote("TEST01", price=101, change=.1)
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"quantity": 2})
        self.context.quotes["TEST02"] = quote("TEST02", price=1, change=90)
        first = self.brief()
        self.portfolio.positions.reverse()
        second = self.brief()
        self.assertEqual(first.priority_position_ids, second.priority_position_ids)
        self.assertEqual(first.priority_position_ids[0], "p1")

    def test_32_chunks_complete_under_4096_utf16(self):
        brief = self.brief()
        chunks = decision_telegram_chunks(brief)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks))
        text = "\n".join(chunks)
        for d in brief.decisions:
            self.assertEqual(text.count(f"{d.ticker} {d.name}｜"), 1)
        self.assertIn("今日操作總覽", chunks[0])

    def test_33_verified_positive_case_can_add_without_fake_sizing(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"allocation_verified": True, "target_weight": .1, "max_weight": .2})
        brief = self.brief([self.clean_check()], [self.evidence()])
        self.assertEqual(brief.decisions[0].recommendation, "ADD")
        self.assertIsNone(brief.decisions[0].trade_quantity)
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_34_missing_one_quote_disables_partial_denominator_only(self):
        self.context.quotes.pop("TEST01")
        brief = self.brief()
        self.assertEqual(len(brief.decisions), 23)
        self.assertTrue(all(d.portfolio_weight is None for d in brief.decisions))
        self.assertEqual(brief.decisions[0].recommendation, "WATCH")

    def test_35_private_sender_no_send(self):
        with patch("generate_report.requests.post") as post:
            result = send_decision_telegram(self.brief(), dry_run=True)
        post.assert_not_called()
        self.assertEqual(result["chunk_count"], len(decision_telegram_chunks(self.brief())))

    def test_36_adapter_requires_explicit_basis_verification(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PIOS_PORTFOLIO_SNAPSHOT_JSON": ""}):
            path = Path(tmp) / "pios.json"
            payload = {"snapshot_id": "test", "as_of": NOW.isoformat(), "active_positions": [{"ticker": "TEST01", "quantity": 1, "cost_basis": 80}]}
            path.write_text(json.dumps(payload))
            self.assertEqual(PIOSPortfolioProvider(path).load().positions[0].basis_quality, "UNRESOLVED")
            payload["active_positions"][0]["basis_quality"] = "VERIFIED"
            path.write_text(json.dumps(payload))
            self.assertEqual(PIOSPortfolioProvider(path).load().positions[0].basis_quality, "VERIFIED")

    def test_37_forged_pnl_rejected(self):
        brief = self.brief()
        brief.decisions[0].unrealized_pnl_pct = 99
        self.assertFalse(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_38_global_missing_quotes_still_produces_23_recommendations(self):
        self.context.quotes.clear()
        brief = self.brief()
        self.assertEqual(len(brief.decisions), 23)
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_39_stale_rolling_crypto_is_not_current_price(self):
        self.replace("BONK", "CRYPTO", .00000375, -7, "ROLLING_24H")
        self.context.quotes["BONK"] = self.context.quotes["BONK"].model_copy(update={"provider_timestamp": NOW - timedelta(hours=1)})
        self.assertIsNone(self.brief().decisions[0].current_price)

    def test_40_runtime_does_not_block_all_decisions_on_quote_coverage(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("generate_report.__file__", str(Path(tmp) / "scripts" / "generate_report.py")), \
             patch("generate_report.load_authoritative_portfolio", return_value=self.portfolio), \
             patch("generate_report.load_market_context", return_value=self.context.model_copy(update={"quotes": {}})), \
             patch("generate_report.fetch_portfolio_events", side_effect=TimeoutError), \
             patch("generate_report.requests.post") as post, \
             patch.dict(os.environ, {"PORTFOLIO_KEY": ""}):
            result = run_portfolio_advice("market", "tw_close", None, "fixture", deliver=False)
            data = json.loads((Path(tmp) / "data" / "portfolio_decision_brief.json").read_text())
        self.assertEqual(result, "SKIPPED")
        self.assertEqual(len(data["decisions"]), 23)
        self.assertTrue(all(d["recommendation"] == "WATCH" for d in data["decisions"]))
        post.assert_not_called()

    def test_41_uploaded_payload_encrypted(self):
        key = Fernet.generate_key()
        with tempfile.TemporaryDirectory() as tmp, \
             patch("generate_report.__file__", str(Path(tmp) / "scripts" / "generate_report.py")), \
             patch("generate_report.load_authoritative_portfolio", return_value=self.portfolio), \
             patch("generate_report.load_market_context", return_value=self.context), \
             patch("generate_report.fetch_portfolio_events", return_value=([], [])), \
             patch.dict(os.environ, {"PORTFOLIO_KEY": key.decode()}):
            run_portfolio_advice("market", "tw_close", None, "fixture", deliver=False)
            ciphertext = (Path(tmp) / "data" / "portfolio_decision_brief.json.enc").read_bytes()
        self.assertNotIn(b"TEST01", ciphertext)
        self.assertEqual(len(json.loads(Fernet(key).decrypt(ciphertext))["decisions"]), 23)

    def test_42_runtime_sends_decisions_not_anomaly_text(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("generate_report.__file__", str(Path(tmp) / "scripts" / "generate_report.py")), \
             patch("generate_report.load_authoritative_portfolio", return_value=self.portfolio), \
             patch("generate_report.load_market_context", return_value=self.context), \
             patch("generate_report.fetch_portfolio_events", return_value=([], [])), \
             patch("generate_report.send_decision_telegram") as send, \
             patch("generate_report.send_advice_telegram") as old_send, \
             patch("generate_report.send_advice_email"), \
             patch.dict(os.environ, {"PORTFOLIO_KEY": ""}):
            result = run_portfolio_advice("market", "tw_close", None, "fixture", deliver=True)
        self.assertEqual(result, "SENT")
        self.assertEqual(len(send.call_args.args[0].decisions), 23)
        old_send.assert_not_called()

    def test_43_fx_identity_must_be_usdtwd(self):
        self.context.quotes["USDTWD"] = quote("TEST01")
        self.assertTrue(all(d.portfolio_weight is None for d in self.brief().decisions))

    def test_44_quote_identity_cannot_cross_instruments(self):
        self.context.quotes["TEST01"] = quote("TEST02")
        self.assertIsNone(self.brief().decisions[0].current_price)

    def test_45_verified_pios_limit_overrides_default(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"quantity": 15, "max_weight": .6, "allocation_verified": True})
        d = self.brief().decisions[0]
        self.assertEqual(d.max_weight, .6)
        self.assertEqual(d.allocation_source, "PIOS 已驗證規則")
        self.assertNotEqual(d.recommendation, "REDUCE")

    def test_46_valid_label_does_not_refresh_wrong_session(self):
        self.context.quotes["TEST01"] = self.context.quotes["TEST01"].model_copy(update={"market_date": "2026-10-06"})
        self.assertIsNone(self.brief().decisions[0].current_price)

    def test_47_cost_currency_mismatch_does_not_create_pl(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"cost_basis": 80, "basis_quality": "VERIFIED", "basis_currency": "TWD"})
        d = self.brief().decisions[0]
        self.assertIsNone(d.cost_basis)
        self.assertIsNone(d.unrealized_pnl_pct)

    def test_48_exact_trade_quantity_rejected(self):
        brief = self.brief()
        brief.decisions[0].trade_quantity = 1
        self.assertFalse(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_49_watchlist_quote_is_not_a_holding(self):
        self.context.quotes["WATCHLISTONLY"] = quote("WATCHLISTONLY")
        self.assertEqual(len(self.brief().decisions), 23)
        self.assertNotIn("WATCHLISTONLY", [d.ticker for d in self.brief().decisions])

    def test_50_unclassified_event_does_not_make_tiny_position_top_risk(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"quantity": .001})
        brief = self.brief([self.event()])
        self.assertNotEqual(brief.priority_position_ids[0], "p1")
        self.assertIn("已驗證公告", brief.decisions[0].watch_condition)

    def test_51_material_etf_event_requires_impact_review(self):
        self.replace("VTI", "ETF")
        d = self.brief([self.event(event_type="FUND_RULES", title="基金發布規則變動公告")]).decisions[0]
        self.assertEqual(d.recommendation, "WATCH")
        self.assertIn("基金發布規則變動公告", d.watch_condition)

    def test_52_unassessed_event_prevents_high_confidence(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"basis_quality": "VERIFIED", "cost_basis": 80})
        d = self.brief([self.event()], [self.evidence()]).decisions[0]
        self.assertEqual(d.recommendation, "WATCH")
        self.assertEqual(d.confidence, "LOW")
        self.assertFalse(d.event_impact_verified)

    def test_53_canonical_zero_quantity_never_uses_old_shares(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PIOS_PORTFOLIO_SNAPSHOT_JSON": ""}):
            path = Path(tmp) / "pios.json"
            path.write_text(json.dumps({"snapshot_id": "test", "as_of": NOW.isoformat(),
                "active_positions": [{"ticker": "TEST01", "quantity": 0, "shares": 9}]}))
            self.assertEqual(PIOSPortfolioProvider(path).load().positions, [])

    def test_54_invalid_average_cost_never_uses_stale_unit_cost(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PIOS_PORTFOLIO_SNAPSHOT_JSON": ""}):
            path = Path(tmp) / "pios.json"
            path.write_text(json.dumps({"snapshot_id": "test", "as_of": NOW.isoformat(), "active_positions": [{"ticker": "TEST01", "quantity": 1,
                "cost_basis": {"status": "VERIFIED", "average_cost": 0, "unit_cost": 80}}]}))
            position = PIOSPortfolioProvider(path).load().positions[0]
            self.assertIsNone(position.cost_basis)
            self.assertEqual(position.basis_quality, "UNRESOLVED")

    def test_55_runtime_never_replaces_pios_with_legacy_holdings(self):
        with patch("generate_report.load_authoritative_portfolio", return_value=self.portfolio.model_copy(update={"source": "LEGACY_ENCRYPTED_PORTFOLIO"})), \
             patch("generate_report.write_advice_audit"), \
             patch("generate_report.send_decision_telegram") as send:
            self.assertEqual(run_portfolio_advice("market", "tw_close", None, "fixture", deliver=False), "BLOCKED")
        send.assert_not_called()

    def test_56_etf_add_does_not_require_company_earnings(self):
        self.replace("VTI", "ETF")
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"allocation_verified": True, "target_weight": .1, "max_weight": .4})
        brief = self.brief([self.clean_check()])
        self.assertEqual(brief.decisions[0].recommendation, "ADD")
        self.assertEqual(brief.decisions[0].fundamental_signal, "UNKNOWN")
        self.assertNotIn("財報", _holding_text(brief.decisions[0]))
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_57_crypto_add_uses_protocol_and_liquidity_evidence(self):
        self.replace("BONK", "CRYPTO", 1, .2, "ROLLING_24H")
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"allocation_verified": True, "target_weight": .01, "max_weight": .02})
        evidence = self.evidence(thesis_status="IMPROVING", fundamental_signal="IMPROVING", valuation_signal="UNKNOWN", liquidity_verified=True)
        brief = self.brief([self.clean_check()], [evidence])
        self.assertEqual(brief.decisions[0].recommendation, "ADD")
        self.assertNotIn("財報", _holding_text(brief.decisions[0]))
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_58_stablecoin_add_uses_verified_liquidity_purpose(self):
        self.replace("USDC", "CRYPTO", 1, 0)
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"allocation_verified": True, "target_weight": .01, "max_weight": .2})
        evidence = self.evidence(fundamental_signal="UNKNOWN", valuation_signal="UNKNOWN", liquidity_verified=True)
        brief = self.brief([self.clean_check()], [evidence])
        self.assertEqual(brief.decisions[0].recommendation, "ADD")
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_59_verified_root_targets_and_limits_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PIOS_PORTFOLIO_SNAPSHOT_JSON": ""}):
            path = Path(tmp) / "pios.json"
            payload = {"snapshot_id": "test", "as_of": NOW.isoformat(), "active_positions": [{"ticker": "TEST01", "quantity": 1}],
                "allocation_rules": {"quality": "VERIFIED", "target_weight_by_instrument": {"TEST01": .1}, "max_weight_by_instrument": {"TEST01": .3}}}
            path.write_text(json.dumps(payload))
            before = path.read_bytes()
            position = PIOSPortfolioProvider(path).load().positions[0]
            self.assertEqual(position.target_weight, .1)
            self.assertEqual(position.max_weight, .3)
            self.assertTrue(position.allocation_verified)
            self.assertEqual(path.read_bytes(), before)

    def test_60_verified_zero_target_can_exit_without_fake_thesis_event(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"allocation_verified": True, "target_weight": 0})
        brief = self.brief()
        self.assertEqual(brief.decisions[0].recommendation, "EXIT")
        self.assertIsNone(brief.decisions[0].verified_event)
        self.assertEqual(self.portfolio.positions[0].quantity, 1)
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])

    def test_61_verified_target_drift_reduces_even_below_max(self):
        self.portfolio.positions[0] = self.portfolio.positions[0].model_copy(update={"allocation_verified": True, "target_weight": .02, "max_weight": .2})
        brief = self.brief()
        self.assertEqual(brief.decisions[0].recommendation, "REDUCE")
        self.assertEqual(brief.decisions[0].rule_id, "VERIFIED_TARGET_DRIFT")
        self.assertTrue(validate_decision_brief(brief, self.context, self.portfolio)[0])


if __name__ == "__main__":
    unittest.main()
