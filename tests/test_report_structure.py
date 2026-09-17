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


if __name__ == "__main__":
    unittest.main()

