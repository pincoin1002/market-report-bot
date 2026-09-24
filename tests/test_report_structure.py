import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_report import validate_rendered_report_structure


class ReportStructuralValidationTest(unittest.TestCase):
    def setUp(self):
        self.fixture_path = Path(__file__).parent / "fixtures" / "broken_tw_close_20260917.md"

    def test_broken_20260917_fixture_fails_all_structural_contracts(self):
        """The exact 2026-09-17 production failure fixture must fail closed across all criteria."""
        self.assertTrue(self.fixture_path.exists(), f"Fixture missing: {self.fixture_path}")
        text = self.fixture_path.read_text(encoding="utf-8")
        ok, errors = validate_rendered_report_structure(text, "tw_close")

        self.assertFalse(ok)
        error_blob = "\n".join(errors)

        # 1. Multiple titles
        self.assertIn("Expected exactly 1 top-level H1 title", error_blob)

        # 2. Restarted / nested section numbering
        self.assertIn("Non-monotonic or restarting section numbering", error_blob)

        # 3. Forbidden internal diagnostic tokens
        self.assertIn("Forbidden internal diagnostic token found: 'VALID'", error_blob)
        self.assertIn("Forbidden internal diagnostic token found: 'DATE_MISMATCH'", error_blob)
        self.assertIn("Forbidden internal diagnostic token found: 'PARTIAL'", error_blob)
        self.assertIn("Forbidden internal diagnostic token found: 'MarketContext'", error_blob)

        # 4. Forbidden placeholder sections
        self.assertIn("Forbidden placeholder text found: '僅列 verified quote'", error_blob)
        self.assertIn("Forbidden placeholder text found: '等待下一份'", error_blob)
        self.assertIn("Forbidden placeholder text found: '無達 materiality 門檻'", error_blob)

        # 5. DATE_MISMATCH ticker dump
        self.assertIn("DATE_MISMATCH ticker dump found in report", error_blob)

        # 6. Impossible regular session timestamps (11:58 UTC = 19:58 TPE for TW close)
        self.assertIn("Impossible regular session quote timestamp", error_blob)

    def test_valid_production_report_passes(self):
        """A properly formatted, single-hierarchy report with verified prices must pass."""
        valid_text = (
            "# 台股收盤日報 2026-09-17｜正式收盤\n\n"
            "## 1. 市場核心概況 (Executive Market State)\n"
            "| 標的 | 最新報價 | 漲跌幅 | 行情時間 | 狀態 |\n"
            "|---|---:|---:|---|---|\n"
            "| 加權指數 (TAIEX) | 46,288.00 | +0.96% | 2026-09-17 13:30 UTC+08:00 | 正式收盤 |\n"
            "| 台積電 (2330) | 2,425.00 | +1.89% | 2026-09-17 13:30 UTC+08:00 | 正式收盤 |\n"
            "| 鴻海 (2317) | 250.50 | +1.01% | 2026-09-17 13:30 UTC+08:00 | 正式收盤 |\n\n"
            "## 2. 相較上一交易日變化 (What Changed Since Last Report)\n"
            "- 加權指數 (TAIEX) +0.96%\n"
            "- 台積電 (2330) +1.89%\n\n"
            "## 3. 今日走勢與市場驅動 (Top Market Drivers)\n"
            "- 台股今日在台積電領軍下強勢開高，終場收復 46,000 點及月線。\n"
            "- 盤中最高觸及 46,874 點，成交金額 8,194.05 億台幣，市場呈現多方震盪。\n\n"
            "## 4. 產業與資金輪動 (Rotation & Sectors)\n"
            "- AI 供應鏈：先進封裝與散熱族群延續多頭動能。\n"
        )
        ok, errors = validate_rendered_report_structure(valid_text, "tw_close")
        self.assertTrue(ok, f"Validation unexpectedly failed with errors: {errors}")
        self.assertEqual(len(errors), 0)

    def test_nested_title_fails(self):
        text = (
            "# 台股收盤日報 2026-09-17\n\n"
            "## 1. 市場核心概況 (Executive Market State)\n"
            "| 標的 | 最新報價 | 漲跌幅 | 狀態 |\n"
            "|---|---:|---:|---|\n"
            "| 台積電 (2330) | 2,425.00 | +1.89% | 正式收盤 |\n"
            "| TAIEX | 46,288.00 | +0.96% | 正式收盤 |\n\n"
            "## 2. Top Market Drivers\n"
            "# 巢狀台股收盤日報\n"
            "內容\n"
        )
        ok, errors = validate_rendered_report_structure(text, "tw_close")
        self.assertFalse(ok)
        self.assertTrue(any("Expected exactly 1 top-level H1 title" in e for e in errors))

    def test_forbidden_diagnostic_token_rejected(self):
        for token in ("VALID", "DATA_BLOCKED", "DATE_MISMATCH", "MarketContext", "弱資料模組"):
            text = (
                f"# 台股收盤日報 2026-09-17\n\n"
                f"## 1. 市場核心概況 (Executive Market State)\n"
                f"| 標的 | 報價 | 狀態 |\n"
                f"| 2330 | 2425 | {token} |\n"
                f"| TAIEX | 46288 | {token} |\n"
            )
            ok, errors = validate_rendered_report_structure(text, "tw_close")
            self.assertFalse(ok, f"Expected {token} to be rejected")
            self.assertTrue(any(token in e for e in errors))

    def test_impossible_taiwan_regular_quote_timestamp_rejected(self):
        text = (
            "# 台股收盤日報 2026-09-17\n\n"
            "## 1. 市場核心概況 (Executive Market State)\n"
            "| 標的 | 報價 | 時間 | 狀態 |\n"
            "| 台積電 (2330) | 2425 | 2026-09-17 11:58 UTC | 正式收盤 |\n"
            "| TAIEX | 46288 | 2026-09-17 11:58 UTC | 正式收盤 |\n"
        )
        ok, errors = validate_rendered_report_structure(text, "tw_close")
        self.assertFalse(ok)
        self.assertTrue(any("Impossible regular session quote timestamp" in e for e in errors))

    def test_telegram_chunking_preserves_content(self):
        from generate_report import _split_message
        # Generate a large message > 4096 chars
        paragraphs = [f"段落 {i}: 台股市場重要動態與權值股表現分析，成交金額持續維持高檔水準。" * 5 for i in range(50)]
        original = "\n\n".join(paragraphs)
        chunks = _split_message(original, max_len=1000)
        # All chunks must be within limit
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)
        # Joining chunks back (normalizing whitespace) should contain all words
        recombined = " ".join(chunks)
        for i in range(50):
            self.assertIn(f"段落 {i}:", recombined)


    def test_numeric_provenance_taiex_2330_2317_2454(self):
        from models import QuoteObservation, Snapshot, NamedQuote
        from market_context import build_market_context
        from structured_reports import build_public_draft, render_public_report
        from validate_report import validate_numeric_provenance
        from datetime import datetime, timezone

        retrieved = datetime(2026, 9, 17, 15, 5, tzinfo=timezone.utc)
        quotes_data = {
            "TAIEX": (46288.0, 45849.0, 0.96, "加權指數"),
            "2330": (2425.0, 2380.0, 1.89, "台積電"),
            "2317": (250.5, 248.0, 1.01, "鴻海"),
            "2454": (4500.0, 4530.0, -0.66, "聯發科"),
        }

        observations = {}
        tw_stocks = {}
        for sym, (price, prev_close, chg, name) in quotes_data.items():
            obs = QuoteObservation(
                quote_id=f"{sym}:2026-09-17:REGULAR:twse",
                instrument_id=sym,
                canonical_symbol=sym,
                price=price,
                currency="TWD",
                session="REGULAR",
                market_date="2026-09-17",
                observed_at=retrieved,
                provider_timestamp=None,
                retrieved_at=retrieved,
                provider="twse",
                quote_type="OFFICIAL_CLOSE",
                is_delayed=True,
                quality_status="VALID",
                previous_regular_close=prev_close,
                change_pct=chg,
                market="TW",
            )
            observations[sym] = obs
            tw_stocks[sym] = NamedQuote(
                name=name,
                currency="TWD",
                symbol=sym,
                price=price,
                prev_close=prev_close,
                change_pct=chg,
                data_date="2026-09-17",
            )

        snap = Snapshot(
            generated_at=retrieved,
            report_type="tw_close",
            fetch_coverage=1.0,
            market_context_coverage=1.0,
            tw_stocks=tw_stocks,
            quote_observations=observations,
        )

        context = build_market_context(snap, "tw_close", run_id="test_prov")
        draft = build_public_draft(context)
        rendered = render_public_report(draft, context)

        # 1. Provenance equality: public numeric value == MarketContext value == provider observation
        for sym in ("TAIEX", "2330", "2317", "2454"):
            expected_price = observations[sym].price
            ctx_price = context.quotes[sym].price
            self.assertEqual(expected_price, ctx_price)

            # Public numeric representation
            self.assertTrue(
                f"{expected_price:,.2f}" in rendered or
                f"{expected_price:,.1f}" in rendered or
                f"{int(expected_price):,}" in rendered or
                f"{expected_price:g}" in rendered,
                f"Expected price {expected_price} for {sym} missing from rendered report"
            )

        # 2. Specifically verify 2454 = 4500 and NEVER 1485
        self.assertIn("4,500", rendered)
        self.assertNotIn("1485", rendered)
        self.assertNotIn("1,485", rendered)

        # 3. Test that injecting 1485 fails closed
        corrupted_report = rendered.replace("4,500.00", "1,485.00")
        ok, errors = validate_numeric_provenance(corrupted_report, context)
        self.assertFalse(ok)
        self.assertTrue(any("1485" in e for e in errors))

    def test_narrative_numeric_integrity_rejects_ungrounded_claims(self):
        from models import QuoteObservation, Snapshot, NamedQuote
        from market_context import build_market_context
        from structured_reports import build_public_draft
        from validate_report import validate_numeric_provenance
        from datetime import datetime, timezone

        retrieved = datetime(2026, 9, 17, 15, 5, tzinfo=timezone.utc)
        obs = QuoteObservation(
            quote_id="2330:2026-09-17:REGULAR:twse",
            instrument_id="2330", canonical_symbol="2330", price=2425.0,
            currency="TWD", session="REGULAR", market_date="2026-09-17",
            observed_at=retrieved, provider_timestamp=None, retrieved_at=retrieved,
            provider="twse", quote_type="OFFICIAL_CLOSE", is_delayed=True,
            quality_status="VALID", previous_regular_close=2380.0, change_pct=1.89,
            market="TW",
        )
        snap = Snapshot(
            generated_at=retrieved, report_type="tw_close",
            fetch_coverage=1.0, market_context_coverage=1.0,
            tw_stocks={"2330": NamedQuote(name="台積電", price=2425.0, prev_close=2380.0, change_pct=1.89, data_date="2026-09-17")},
            quote_observations={"2330": obs},
        )
        context = build_market_context(snap, "tw_close", run_id="test_narrative")

        # Case A: ungrounded claims in LLM prose are stripped by _extract_grounded_drivers
        hallucinated_narrative = (
            "## 1. 今日一句話\n"
            "三大法人合計買超逾 300 億台幣，資金佔大盤成交比重逾六成，台積電創下波段新高。"
        )
        draft = build_public_draft(context, hallucinated_narrative)
        self.assertEqual(draft.drivers, [])
        self.assertNotIn("300", draft.rendered_markdown)
        self.assertNotIn("六成", draft.rendered_markdown)
        self.assertNotIn("波段新高", draft.rendered_markdown)

        # Case B: if an ungrounded numeric claim is manually inserted into narrative, validate_numeric_provenance fails closed
        bogus_report = draft.rendered_markdown + "\n\n## 3. 今日走勢與市場驅動\n- 外資今日買超 300 億台幣。"
        ok, errors = validate_numeric_provenance(bogus_report, context)
        self.assertFalse(ok)
        self.assertTrue(any("300" in e for e in errors))

    def test_end_to_end_golden_pipeline_tw_close(self):
        """Run the COMPLETE production pipeline on a deterministic TW_CLOSE fixture with known values."""
        from models import QuoteObservation, Snapshot, NamedQuote, TaiexMarketSummary, InstitutionalFlows
        from market_context import build_market_context
        from structured_reports import build_public_draft, render_public_report
        from validate_report import validate_rendered_report_structure, validate_numeric_provenance
        from generate_report import _clean_markdown_for_telegram_report, _split_message
        from datetime import datetime, timezone

        retrieved = datetime(2026, 9, 17, 15, 5, tzinfo=timezone.utc)
        golden_values = {
            "TAIEX": (46288.0, 45849.0, 0.96, "加權指數"),
            "2330": (2425.0, 2380.0, 1.89, "台積電"),
            "2317": (250.5, 248.0, 1.01, "鴻海"),
            "2454": (4500.0, 4530.0, -0.66, "聯發科"),
            "2308": (980.0, 970.0, 1.03, "台達電"),
            "2303": (52.5, 52.0, 0.96, "聯電"),
            "USDTWD": (32.05, 32.10, -0.16, "美元兌台幣"),
        }

        # 1. Fixture/provider layer
        observations = {}
        tw_stocks = {}
        for sym, (price, prev_close, chg, name) in golden_values.items():
            obs = QuoteObservation(
                quote_id=f"{sym}:2026-09-17:REGULAR:twse",
                instrument_id=sym, canonical_symbol=sym, price=price,
                currency="TWD", session="REGULAR", market_date="2026-09-17",
                observed_at=retrieved, provider_timestamp=None, retrieved_at=retrieved,
                provider="twse", quote_type="OFFICIAL_CLOSE", is_delayed=True,
                quality_status="VALID", previous_regular_close=prev_close, change_pct=chg,
                market="TW",
            )
            observations[sym] = obs
            tw_stocks[sym] = NamedQuote(
                name=name, currency="TWD", symbol=sym,
                price=price, prev_close=prev_close, change_pct=chg,
                data_date="2026-09-17",
            )

        taiex_summary = TaiexMarketSummary(
            open=46100.0,
            high=46874.0,
            low=46050.0,
            close=46288.0,
            point_change=439.0,
            change_pct=0.96,
            turnover_ntd_billions=8194.05,
            advancing=612,
            declining=305,
            unchanged=83,
        )

        institutional_flows = InstitutionalFlows(
            foreign_buy_sell_ntd_billions=121.85,
            investment_trust_buy_sell_ntd_billions=35.60,
            dealer_buy_sell_ntd_billions=-18.20,
            total_buy_sell_ntd_billions=139.25,
            foreign_futures_net_oi=-78674,
            foreign_futures_oi_change=-2150,
            foreign_buy_sell_prev_ntd_billions=-45.20,
            turnover_prev_ntd_billions=6944.05,
        )

        # 2. Snapshot
        snap = Snapshot(
            generated_at=retrieved, report_type="tw_close",
            fetch_coverage=1.0, market_context_coverage=1.0,
            tw_stocks=tw_stocks, quote_observations=observations,
            taiex_summary=taiex_summary,
            institutional_flows=institutional_flows,
        )

        # 3. MarketContext
        context = build_market_context(snap, "tw_close", run_id="golden_tw_close", now=retrieved)

        # 4. PublicDraft
        narrative = "## 1. 今日一句話\n台股權值股穩步盤堅，加權指數上漲 0.96% 收復短期均線。"
        draft = build_public_draft(context, narrative)

        # 5. Render
        rendered = render_public_report(draft, context)

        # 6. Structural & numeric validation
        struct_ok, struct_errors = validate_rendered_report_structure(rendered, "tw_close")
        self.assertTrue(struct_ok, f"Structural validation failed: {struct_errors}")

        num_ok, num_errors = validate_numeric_provenance(rendered, context)
        self.assertTrue(num_ok, f"Numeric provenance failed: {num_errors}")

        # 7. Telegram formatting
        telegram_output = _clean_markdown_for_telegram_report(rendered)

        # 8. Telegram chunking
        chunks = _split_message(telegram_output, max_len=4096)
        self.assertGreaterEqual(len(chunks), 1)

        # 9. Assertions on final output
        # A. Every numeric value survives unchanged
        self.assertIn("46,288", telegram_output)
        self.assertIn("2,425", telegram_output)
        self.assertIn("250.5", telegram_output)
        self.assertIn("4,500", telegram_output)
        self.assertIn("+0.96%", telegram_output)
        self.assertIn("+1.89%", telegram_output)
        self.assertIn("+1.01%", telegram_output)
        self.assertIn("-0.66%", telegram_output)

        # Institutional flows numbers
        self.assertIn("121.85", telegram_output)
        self.assertIn("35.60", telegram_output)
        self.assertIn("18.20", telegram_output)
        self.assertIn("139.25", telegram_output)
        self.assertIn("78,674", telegram_output)

        # B. Corrupted 1485 NEVER appears
        self.assertNotIn("1485", telegram_output)
        self.assertNotIn("1,485", telegram_output)

        # C. No nested report (exactly 1 bold title line in telegram)
        self.assertEqual(telegram_output.count("台股收盤日報 2026-09-17"), 1)

        # D. No diagnostic tokens
        for token in ("VALID", "PARTIAL", "DATA_BLOCKED", "DATE_MISMATCH", "MarketContext"):
            self.assertNotIn(token, telegram_output)

        # E. No synthetic market timestamp
        self.assertNotIn("13:30:00", telegram_output)
        self.assertNotIn("13:30", telegram_output)
        self.assertNotIn("19:58", telegram_output)

        # F. Clean human readable status
        self.assertIn("正式收盤", telegram_output)

        # G. No placeholder section
        for ph in ("等待下一份", "僅列 verified quote", "弱資料模組不硬填"):
            self.assertNotIn(ph, telegram_output)

        # H. Verify all 6 core sections exist
        self.assertIn("1. 今日市場", telegram_output)
        self.assertIn("2. 法人與資金", telegram_output)
        self.assertIn("3. 權值與族群", telegram_output)
        self.assertIn("4. 今日關鍵驅動", telegram_output)
        self.assertIn("5. 相較昨日", telegram_output)
        self.assertIn("6. 明日觀察", telegram_output)

        # I. Character length is within expected institutional brief length (~700 - 1300 chars)
        char_count = len(rendered)
        self.assertGreaterEqual(char_count, 650, f"Report too short ({char_count} chars)")
        self.assertLessEqual(char_count, 2400, f"Report too long ({char_count} chars)")

        # J. Zero content loss across chunks
        recombined = " ".join(chunks)
        self.assertEqual(" ".join(telegram_output.split()), " ".join(recombined.split()))


if __name__ == "__main__":
    unittest.main()


