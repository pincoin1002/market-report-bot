import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from market_session import TPE, tw_open_snapshot_contract_status
from models import MarketContext
from structured_reports import build_public_draft


class TaiwanOpenIntentContractTest(unittest.TestCase):
    def test_preopen_window_is_ready(self):
        now = datetime(2026, 10, 5, 8, 59, 59, tzinfo=TPE)
        self.assertEqual(tw_open_snapshot_contract_status(now), "READY")

    def test_0900_and_later_is_expired(self):
        for hour, minute in ((9, 0), (10, 26), (13, 0)):
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 10, 5, hour, minute, tzinfo=TPE)
                self.assertEqual(tw_open_snapshot_contract_status(now), "INTENT_EXPIRED")

    def test_twse_holiday_is_market_closed(self):
        now = datetime(2026, 10, 9, 8, 0, tzinfo=TPE)
        self.assertEqual(tw_open_snapshot_contract_status(now), "MARKET_CLOSED")

    def test_tw_open_headline_uses_briefing_date_not_previous_close_date(self):
        context = MarketContext(
            run_id="tw_open:2026-10-05",
            generated_at=datetime(2026, 10, 5, 8, 0, tzinfo=TPE),
            report_type="tw_open",
            market_date="2026-10-02",
            market_session="PREVIOUS_CLOSE",
            quotes={},
        )
        draft = build_public_draft(context)
        self.assertIn("台股開盤戰報 2026-10-05｜開盤前參考", draft.headline)
        self.assertNotIn("台股開盤戰報 2026-10-02｜", draft.headline)

    def test_workflow_is_prestaged_and_blocks_late_delivery(self):
        text = (ROOT / ".github/workflows/tw-open.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "17 3 * * 1-5"', text)
        self.assertIn('timezone: "Asia/Taipei"', text)
        self.assertIn("Stage native Taiwan Open backup", text)
        self.assertIn("Hold Taiwan-open backup until 07:40 Taipei", text)
        self.assertIn("needs: [native-stage]", text)
        self.assertIn("TW_OPEN_INTENT_EXPIRED", text)
        self.assertIn("steps.fetch.outputs.intent_unavailable != 'true'", text)
        self.assertNotIn('cron: "50 23 * * 0-4"', text)

    def test_external_scheduler_dispatches_in_taipei_staging_window(self):
        from scheduler.tw_open_dispatch import dispatch_payload, scheduler_decision

        ready = datetime(2026, 10, 6, 7, 15, tzinfo=TPE)
        late = datetime(2026, 10, 6, 8, 50, tzinfo=TPE)
        holiday = datetime(2026, 10, 9, 7, 15, tzinfo=TPE)

        self.assertEqual(scheduler_decision(ready), "DISPATCH")
        self.assertEqual(scheduler_decision(late), "OUTSIDE_STAGING_WINDOW")
        self.assertEqual(scheduler_decision(holiday), "MARKET_CLOSED")

        payload = dispatch_payload()
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["send_telegram"], "true")
        self.assertEqual(payload["inputs"]["trigger_source"], "vercel_cron")

    def test_workflow_serializes_external_and_native_delivery(self):
        text = (ROOT / ".github/workflows/tw-open.yml").read_text(encoding="utf-8")
        self.assertIn("group: tw-open-canonical-delivery", text)
        self.assertIn("Wait for canonical Taiwan-open snapshot time", text)
        self.assertIn("trigger_source == 'vercel_cron'", text)
        self.assertIn("Check whether today's Taiwan-open report already completed", text)
        self.assertIn("already_reported != 'true'", text)
        self.assertIn("timeout-minutes: 90", text)

    def test_fetch_has_hard_expiry_exit(self):
        text = (ROOT / "scripts/fetch_market_data.py").read_text(encoding="utf-8")
        self.assertIn("tw_open_snapshot_contract_status", text)
        self.assertIn("sys.exit(6)", text)
        self.assertIn("refusing to publish previous-close data after market open", text)

    def test_operational_alert_explains_stale_data_was_blocked(self):
        text = (ROOT / "scripts/send_operational_alert.py").read_text(encoding="utf-8")
        self.assertIn("TW_OPEN_INTENT_EXPIRED", text)
        self.assertIn("未使用前一交易日收盤資料冒充今日開盤", text)


if __name__ == "__main__":
    unittest.main()
