"""US-close intake/fetch/render/date guards; no live calls or Telegram sends."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import delivery_state
import fetch_market_data as fetch
import generate_report as generate
from market_session import TPE, NY, get_target_market_date, us_close_session_contract
from models import MarketContext


def exit_with_status(code):
    raise SystemExit(code)


class USCloseCalendarTests(unittest.TestCase):
    def contract(self, publication, hour=13):
        now = datetime.fromisoformat(publication).replace(hour=hour, tzinfo=TPE)
        return us_close_session_contract(now, publication_date=publication)

    def test_oct17_saturday_intends_friday_not_runner_ny_saturday(self):
        for hour in (8, 12, 13, 23):
            d = self.contract('2026-10-17', hour)
            self.assertEqual(d, {'publication_date': '2026-10-17', 'us_trading_date': '2026-10-16',
                                'status': 'READY', 'delivery_key': 'us_close:20261017'})

    def test_exact_two_production_clock_instants_no_false_holiday(self):
        for raw in ('2026-10-17T00:54:08+00:00', '2026-10-17T04:54:15+00:00'):
            with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-17'}), \
                 patch.object(fetch, 'check_market_closed_via_gemini') as gemini:
                self.assertFalse(fetch.is_us_market_closed('us_close', datetime.fromisoformat(raw)))
                gemini.assert_not_called()

    def test_pin_survives_tpe_and_ny_midnight_and_monday_retry(self):
        with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': '2026-10-17'}):
            for day in (17, 18, 19):
                now = datetime(2026, 10, day, 15, tzinfo=TPE)
                self.assertFalse(fetch.is_us_market_closed('us_close', now))
                self.assertEqual(get_target_market_date('us_close', 'US', now), '2026-10-16')

    def test_dst_friday_saturday(self):
        for publication, trading in (('2026-03-14', '2026-03-13'), ('2026-10-17', '2026-10-16')):
            d = self.contract(publication); self.assertEqual(d['us_trading_date'], trading)
            self.assertEqual(d['status'], 'READY')
            self.assertEqual(datetime.fromisoformat(trading).replace(tzinfo=NY).utcoffset().total_seconds(), -4*3600)

    def test_standard_time_friday_saturday(self):
        d = self.contract('2026-11-07')
        self.assertEqual((d['us_trading_date'], d['status']), ('2026-11-06', 'READY'))
        self.assertEqual(datetime(2026, 11, 6, tzinfo=NY).utcoffset().total_seconds(), -5*3600)

    def test_sunday_monday_intend_closed_ny_weekend(self):
        for publication in ('2026-10-18', '2026-10-19'):
            self.assertEqual(self.contract(publication)['status'], 'MARKET_CLOSED')

    def test_nyse_holidays_do_not_roll_back_to_older_close(self):
        for publication, closed in (('2026-04-04', '2026-04-03'), # Good Friday
                                    ('2026-07-04', '2026-07-03'), # observed July4
                                    ('2026-11-27', '2026-11-26'), # Thanksgiving
                                    ('2026-12-26', '2026-12-25')):
            with patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE': publication}):
                now = datetime.fromisoformat(publication).replace(hour=13, tzinfo=TPE)
                d = self.contract(publication)
                self.assertEqual((d['us_trading_date'], d['status']), (closed, 'MARKET_CLOSED'))
                self.assertEqual(get_target_market_date('us_close', 'US', now), closed)
                self.assertTrue(fetch.is_us_market_closed('us_close', now))

    def test_early_close_friday_still_reported_saturday(self):
        self.assertEqual(self.contract('2026-11-28')['status'], 'READY')

    def test_year_boundary_uses_us_dec31_not_tpe_new_year(self):
        self.assertEqual(self.contract('2027-01-01')['us_trading_date'], '2026-12-31')
        self.assertEqual(self.contract('2027-01-01')['status'], 'READY')
        self.assertEqual(self.contract('2027-01-02')['status'], 'MARKET_CLOSED')
        self.assertEqual(self.contract('2026-01-01')['us_trading_date'], '2025-12-31')

    def test_future_session_cannot_be_completed_using_old_prices(self):
        before = datetime(2026, 10, 16, 18, tzinfo=TPE)
        self.assertEqual(us_close_session_contract(before, '2026-10-17')['status'], 'SESSION_NOT_COMPLETED')

    def test_naive_and_invalid_dates_rejected(self):
        with self.assertRaises(ValueError):us_close_session_contract(datetime(2026, 10, 17), '2026-10-17')
        with self.assertRaises(ValueError):us_close_session_contract(datetime(2026, 10, 17, tzinfo=TPE), '20261017')

    def test_close_quote_session_does_not_change_after_ny_midnight(self):
        for hour in (8, 13, 23):
            self.assertEqual(fetch._expected_session('us_close', datetime(2026, 10, 17, hour, tzinfo=TPE)), 'REGULAR')

    def test_us_open_keeps_actual_ny_calendar(self):
        with patch.object(fetch, 'check_market_closed_via_gemini', return_value=False):
            self.assertTrue(fetch.is_us_market_closed('us_open', datetime(2026, 10, 17, 13, tzinfo=TPE)))
            self.assertFalse(fetch.is_us_market_closed('us_open', datetime(2026, 10, 16, 21, tzinfo=TPE)))

    def test_tw_reports_not_subject_to_us_gate(self):
        for kind in ('tw_open', 'tw_close'):
            self.assertFalse(fetch.is_us_market_closed(kind, datetime(2026, 10, 17, 13, tzinfo=TPE)))

    def test_production_fetch_path_no_false_weekend(self):
        contract = self.contract('2026-10-17')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fetch, 'Path', return_value=Path(tmp)/'scripts'/'fetch_market_data.py'), \
             patch.object(fetch, 'setup_logging'), patch.object(fetch, 'sys') as args, \
             patch.object(fetch, 'us_close_session_contract', return_value=contract), \
             patch.object(fetch, 'build_snapshot', side_effect=RuntimeError('REACHED_REAL_FETCH')), \
             patch.object(fetch, 'check_market_closed_via_gemini') as gemini:
            args.argv = ['fetch_market_data.py', 'us_close']
            # If the old closure guard fires this will not reach snapshot fetch.
            args.exit.side_effect = exit_with_status
            with self.assertRaisesRegex(RuntimeError, 'REACHED_REAL_FETCH'): fetch.main()
            gemini.assert_not_called()

    def test_incomplete_session_blocks_before_fetch_and_not_as_holiday(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fetch, 'Path', return_value=Path(tmp)/'scripts'/'fetch_market_data.py'), \
             patch.object(fetch, 'setup_logging'), patch.object(fetch, 'sys') as args, \
             patch.object(fetch, 'us_close_session_contract', return_value={**self.contract('2026-10-17'), 'status':'SESSION_NOT_COMPLETED'}), \
             patch.object(fetch, 'build_snapshot') as builder, patch.object(fetch, '_set_github_output') as output:
            args.argv = ['fetch_market_data.py', 'us_close']; args.exit.side_effect = exit_with_status
            with self.assertRaises(SystemExit) as exc:fetch.main()
            self.assertEqual(exc.exception.code, 8); builder.assert_not_called()
            output.assert_any_call('market_closed', 'false')
            output.assert_any_call('terminal_state', 'SESSION_NOT_COMPLETED')

    def test_generation_rejects_other_trading_date_even_if_same_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'data').mkdir()
            c = MarketContext(run_id='fixed', report_type='us_close', market_date='2026-10-15',
                              generated_at=datetime(2026,10,17,13,tzinfo=TPE), market_session='REGULAR')
            (root/'data/market_context.json').write_text(c.model_dump_json())
            with patch.object(generate, 'Path', return_value=root/'scripts'/'generate_report.py'), \
                 patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE':'2026-10-17'}), \
                 patch.object(generate, 'us_close_session_contract', return_value=self.contract('2026-10-17')):
                with self.assertRaisesRegex(ValueError, 'TRADING_DATE_MISMATCH'):generate.load_market_context('us_close')
                c.market_date='2026-10-16';(root/'data/market_context.json').write_text(c.model_dump_json())
                self.assertEqual(generate.load_market_context('us_close').market_date,'2026-10-16')

    def test_holiday_notice_uses_contract_not_runner_date(self):
        source = (Path(__file__).resolve().parents[1]/'.github/workflows/us-close.yml').read_text()
        block = source.split('name: Send non-trading day notice',1)[1].split('name: Validate report prices',1)[0]
        self.assertIn('us_close_session_contract()', block); self.assertNotIn('datetime.now',block)
        self.assertIn("contract['us_trading_date']",block);self.assertIn('PYTHONPATH: scripts',block)
        self.assertIn('HOLIDAY_NOTICE_CONTRACT_MISMATCH',block)

    def test_existing_receipt_deduplication_not_renamed(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(delivery_state,'STATE_PATH',Path(tmp)/'state.json'), \
             patch.object(delivery_state,'RECEIPTS_DIR',Path(tmp)/'receipts'), \
             patch.dict(os.environ,{'DELIVERY_RECEIPT_REMOTE_CHECK':''}):
            key=self.contract('2026-10-17')['delivery_key']
            self.assertFalse(delivery_state.already_delivered(key))
            delivery_state.mark_state(key,'DELIVERED',telegram_result={'ok':True,'message_ids':[123]})
            self.assertTrue(delivery_state.already_delivered(key))

    def test_no_send_replay_uses_publication_key_not_quote_date(self):
        from dry_run_v2 import _snapshot
        from replay_report_run import replay
        snapshot = _snapshot('us_close')
        instant = datetime(2026,10,17,13,tzinfo=TPE)
        snapshot = snapshot.model_copy(update={'generated_at':instant, 'report_market_date':'2026-10-16',
            'quote_observations':{k:q.model_copy(update={'market_date':'2026-10-16',
                                  'market':'US' if q.currency in ('USD','percent') else 'TW'})
                                  for k,q in snapshot.quote_observations.items()}})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'US_CLOSE_PUBLICATION_DATE':'2026-10-17'}), \
             patch.object(generate,'Path',return_value=Path(tmp)/'scripts'/'generate_report.py'), \
             patch('generate_report.requests.post') as post:
            source=Path(tmp)/'snapshot.json';source.write_text(snapshot.model_dump_json())
            result=replay('us_close',snapshot_path=source,as_of=instant)
            self.assertEqual(result['overall'],'PASS')
            self.assertEqual(result['delivery_key'],'us_close:20261017')
            self.assertEqual(result['us_trading_date'],'2026-10-16')
            self.assertEqual(result['telegram_messages_sent'],0)
            post.assert_not_called()


if __name__ == '__main__':unittest.main()
