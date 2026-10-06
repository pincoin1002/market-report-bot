"""Regression coverage for the 2026-10-06 delivery reliability incident."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import delivery_state
from dry_run_v2 import _snapshot, run_one
from generate_report import (deliver_validated_report, generate_report, send_advice_telegram,
                             send_telegram, telegram_destination_fingerprint)
from market_context import build_market_context
from send_operational_alert import validation_alert
from structured_reports import build_public_draft
from validate_report import (validate_numeric_provenance, validate_render_matches_draft,
                             validate_rendered_report_structure)


class ReliabilityIncidentTest(unittest.TestCase):
    def _public_fixture(self):
        snapshot = _snapshot("us_open")
        context = build_market_context(snapshot, "us_open", run_id="incident-test")
        draft = build_public_draft(context, "verified-only narrative")
        return snapshot, context, draft

    def test_gemini_timeout_falls_back_to_valid_verified_report(self):
        snapshot, context, _ = self._public_fixture()
        with patch("generate_report._call_gemini_api", side_effect=TimeoutError), \
             patch.dict(os.environ, {"ALLOW_UNGROUNDED_NEWS_FALLBACK": "false"}):
            narrative = generate_report("INTERNAL_PROMPT_SECRET", "fixture", "us_open")
        draft = build_public_draft(context, narrative)
        self.assertNotIn("INTERNAL_PROMPT_SECRET", draft.rendered_markdown)
        self.assertTrue(validate_rendered_report_structure(draft.rendered_markdown, "us_open")[0])
        self.assertTrue(validate_numeric_provenance(draft.rendered_markdown, context)[0])
        self.assertTrue(validate_render_matches_draft(draft.rendered_markdown, draft)[0])

    def test_telegram_requires_confirmed_message_and_matching_destination(self):
        chat_id = "-1001234567890"
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": 42, "chat": {"id": int(chat_id)}}}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": chat_id}), \
             patch("generate_report.requests.post", return_value=response) as post:
            public = send_telegram("# Test\nBody", "us_open")
            private = send_advice_telegram("💼 持股監控", "us_open")
            self.assertEqual(post.call_count, 2)
        self.assertEqual(public["message_ids"], [42])
        self.assertEqual(public["destination_fingerprints"], private["destination_fingerprints"])
        self.assertEqual(public["destination_fingerprints"], [telegram_destination_fingerprint(chat_id)])
        self.assertNotIn(chat_id, str(public))
        response.json.return_value = {"ok": True, "result": {"chat": {"id": int(chat_id)}}}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": chat_id}), \
             patch("generate_report.requests.post", return_value=response):
            with self.assertRaises(RuntimeError):
                send_telegram("Body", "us_open")

    def test_dry_run_never_calls_telegram(self):
        with patch("generate_report.requests.post") as post:
            result = send_telegram("Body", "us_open", dry_run=True)
            send_advice_telegram("💼 持股監控", "us_open", dry_run=True)
        self.assertTrue(result["simulated"])
        post.assert_not_called()

    def test_generated_then_delivered_once_and_private_failure_isolated(self):
        snapshot, context, draft = self._public_fixture()
        key = "us_open:20261005"
        confirmed = {"ok": True, "message_ids": [77], "destination_fingerprints": ["deadbeef"]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(delivery_state, "STATE_PATH", Path(tmp) / "state.json"), \
             patch.object(delivery_state, "RECEIPTS_DIR", Path(tmp) / "receipts"), \
             patch("generate_report.send_telegram", return_value=confirmed) as send, \
             patch("generate_report.send_email"), \
             patch("generate_report.run_portfolio_advice", side_effect=TimeoutError):
            delivery_state.mark_state(key, "GENERATED")
            self.assertFalse(delivery_state.already_delivered(key))
            first = deliver_validated_report(draft.rendered_markdown, "us_open", context, draft,
                                             snapshot, "fixture", key)
            second = deliver_validated_report(draft.rendered_markdown, "us_open", context, draft,
                                              snapshot, "fixture", key)
            self.assertTrue(delivery_state.already_delivered(key))
            self.assertTrue(delivery_state.receipt_path(key).exists())
        self.assertEqual(first["public_delivery"], "SENT")
        self.assertEqual(first["private_advice"], "BLOCKED")
        self.assertEqual(second["public_delivery"], "SKIPPED")
        send.assert_called_once()

    def test_four_report_types_full_dry_run(self):
        with patch("generate_report.requests.post") as post:
            for report_type in ("tw_open", "tw_close", "us_open", "us_close"):
                with self.subTest(report_type=report_type):
                    result = run_one(report_type)
                    self.assertTrue(result["public_report_valid"])
                    self.assertTrue(result["numeric_provenance_valid"])
                    self.assertTrue(result["render_valid"])
                    self.assertTrue(result["public_delivery_simulation_valid"])
                    self.assertEqual(result["telegram_messages_sent"], 0)
            post.assert_not_called()

    def test_reason_specific_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "validation_results.json"
            path.write_text('{"numeric_provenance":{"errors":["GOOG price matched GOOGL"]}}', encoding="utf-8")
            self.assertIn("GOOG / GOOGL", validation_alert("us_open", path))
            path.write_text('{"numeric_provenance":{"errors":["淨廣度 -267 ungrounded"]}}', encoding="utf-8")
            self.assertIn("市場廣度衍生數值", validation_alert("tw_close", path))


if __name__ == "__main__":
    unittest.main()
