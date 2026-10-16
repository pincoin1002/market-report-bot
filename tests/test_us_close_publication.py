"""Publication identity regressions; no network, credentials or Telegram sends."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import delivery_state
import generate_report
from market_context import build_market_context
from market_session import (TPE, report_market_date, get_target_market_date,
                            us_close_publication_date, resolve_us_close_publication_date)
from models import Snapshot
from print_report_result import result_line


class USClosePublicationTests(unittest.TestCase):
    def test_oct15_oct16_before_and_after_ny_midnight_have_one_key(self):
        for day, trading in (('2026-10-15', '2026-10-14'), ('2026-10-16', '2026-10-15')):
            for hour in (8, 13):
                moment = datetime.fromisoformat(day).replace(hour=hour, minute=53, tzinfo=TPE)
                with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': ''}):
                    self.assertEqual(report_market_date('us_close', moment), day)
                    self.assertEqual(get_target_market_date('us_close', 'US', moment), trading)

    def test_pin_survives_tpe_midnight_with_original_quote_session(self):
        with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-16'}):
            for moment in (datetime(2026, 10, 16, 8, 53, tzinfo=TPE), datetime(2026, 10, 17, 8, 53, tzinfo=TPE)):
                self.assertEqual(report_market_date('us_close', moment), '2026-10-16')
                self.assertEqual(get_target_market_date('us_close', 'US', moment), '2026-10-15')

    def test_created_before_midnight_can_resume_after_midnight(self):
        triggered = datetime(2026, 10, 16, 23, 59, tzinfo=TPE)
        publication = resolve_us_close_publication_date(triggered)
        with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': publication}):
            self.assertEqual(report_market_date('us_close', datetime(2026, 10, 17, 0, 1, tzinfo=TPE)), '2026-10-16')

    def test_ambiguous_post_midnight_event_requires_explicit_date(self):
        moment = datetime(2026, 10, 17, 0, 1, tzinfo=TPE)
        with self.assertRaisesRegex(ValueError, 'PUBLICATION_DATE_REQUIRED'):
            resolve_us_close_publication_date(moment)
        self.assertEqual(resolve_us_close_publication_date(moment, '2026-10-16'), '2026-10-16')

    def test_saturday_publishes_friday_close(self):
        moment = datetime(2026, 10, 17, 8, 30, tzinfo=TPE)
        with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-17'}):
            self.assertEqual(resolve_us_close_publication_date(moment), '2026-10-17')
            self.assertEqual(get_target_market_date('us_close', 'US', moment), '2026-10-16')

    def test_sunday_monday_do_not_invent_scheduled_publication(self):
        for day in (18, 19):
            with self.assertRaisesRegex(ValueError, 'PUBLICATION_DATE_REQUIRED'):
                resolve_us_close_publication_date(datetime(2026, 10, day, 8, 30, tzinfo=TPE))

    def test_dst_and_standard_time_keep_tpe_identity(self):
        for day, trading in (('2026-03-10', '2026-03-09'), ('2026-11-03', '2026-11-02')):
            moment = datetime.fromisoformat(day).replace(hour=8, minute=30, tzinfo=TPE)
            with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': day}):
                self.assertEqual(report_market_date('us_close', moment), day)
                self.assertEqual(get_target_market_date('us_close', 'US', moment), trading)

    def test_invalid_date_fails_closed(self):
        for value in ('20261016', '2026-02-30', 'private-input', '2026-10-16\nOTHER=value'):
            with self.assertRaisesRegex(ValueError, 'PUBLICATION_DATE_INVALID'):
                us_close_publication_date(intended_date=value)

    def test_missing_or_naive_trigger_timestamp_fails_closed(self):
        for moment in (None, datetime(2026, 10, 16, 8, 30)):
            with self.assertRaises(ValueError):
                resolve_us_close_publication_date(moment)

    def test_yesterday_receipt_cannot_suppress_today_and_other_source_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(delivery_state, 'STATE_PATH', Path(tmp) / 'state.json'), \
                 patch.object(delivery_state, 'RECEIPTS_DIR', Path(tmp) / 'receipts'), \
                 patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-16', 'DELIVERY_RECEIPT_REMOTE_CHECK': ''}):
                confirmation = {'ok': True, 'message_ids': [42]}
                delivery_state.mark_state('us_close:20261015', 'DELIVERED', telegram_result=confirmation)
                key = 'us_close:' + generate_report.get_market_date('us_close').strftime('%Y%m%d')
                self.assertEqual(key, 'us_close:20261016')
                self.assertFalse(delivery_state.already_delivered(key))
                delivery_state.mark_state(key, 'DELIVERED', telegram_result=confirmation)
                self.assertTrue(delivery_state.already_delivered(key))
                receipt = json.loads(delivery_state.receipt_path(key).read_text())
                self.assertTrue(delivery_state.valid_receipt(receipt, key))
                self.assertEqual(receipt['telegram_message_ids'], [42])

    def test_context_does_not_relabel_trading_date_as_publication(self):
        now = datetime(2026, 10, 16, 8, 53, tzinfo=TPE)
        with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-16'}):
            snapshot = Snapshot(generated_at=now, report_type='us_close')
            self.assertEqual(build_market_context(snapshot, 'us_close', now=now).market_date, '2026-10-15')

    def test_oct15_prior_receipt_does_not_suppress_new_publication(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(delivery_state, 'STATE_PATH', Path(tmp) / 'state.json'), \
             patch.object(delivery_state, 'RECEIPTS_DIR', Path(tmp) / 'receipts'), \
             patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-15', 'DELIVERY_RECEIPT_REMOTE_CHECK': ''}):
            delivery_state.mark_state('us_close:20261014', 'DELIVERED', telegram_result={'ok': True, 'message_ids': [41]})
            key = 'us_close:' + generate_report.get_market_date('us_close').strftime('%Y%m%d')
            self.assertEqual(key, 'us_close:20261015')
            self.assertFalse(delivery_state.already_delivered(key))

    def test_result_exposes_publication_and_us_trading_date_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(generate_report, 'Path', return_value=Path(tmp) / 'scripts' / 'generate_report.py'), \
                 patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
                generate_report._report_result('us_close', '20261016', 'PASS', 'SKIPPED', [], 'SKIPPED', 'SIMULATED', 'NO_SEND', trading_date='2026-10-15')
            data = Path(tmp) / 'data'
            result = json.loads((data / 'report_result.json').read_text())
            self.assertEqual(result['publication_date'], '2026-10-16')
            self.assertEqual(result['us_trading_date'], '2026-10-15')
            self.assertIn('publication_date=2026-10-16', result_line('us_close', data))

    def test_fallback_terminal_log_uses_same_publication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-16'}):
            self.assertIn('market_date=20261016', result_line('us_close', Path(tmp)))

    def test_workflow_intake_python_for_native_and_external(self):
        workflow = (ROOT / '.github/workflows/us-close.yml').read_text()
        block = workflow.split('name: Pin US-close publication date', 1)[1].split('name: Fetch market snapshot', 1)[0]
        self.assertIn('timeout 20s gh api', block)
        self.assertIn('--jq .created_at', block)
        self.assertIn('PYTHONPATH: scripts', block)
        code = re.search(r"python - <<'PY'.*?\n(.*?)\n          PY", block, re.S).group(1)
        code = '\n'.join(line[10:] for line in code.splitlines())
        for source in ('native', 'external'):
            env = {'PYTHONPATH': str(ROOT / 'scripts'), 'INPUT_PUBLICATION_DATE': '2026-10-16' if source == 'external' else '',
                   'US_CLOSE_TRIGGERED_AT': '2026-10-16T00:53:47Z'}
            result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'US_CLOSE_PUBLICATION_DATE=2026-10-16')

    def test_other_reports_unchanged_by_us_close_pin(self):
        now = datetime(2026, 10, 16, 15, 30, tzinfo=TPE)
        expected = {kind: report_market_date(kind, now) for kind in ('tw_open', 'tw_close', 'us_open')}
        with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-15'}):
            self.assertEqual({kind: report_market_date(kind, now) for kind in expected}, expected)
