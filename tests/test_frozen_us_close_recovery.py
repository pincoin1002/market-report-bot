"""Synthetic public-market fixtures only; no frozen production payload/secrets."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from cryptography.fernet import Fernet
from unittest.mock import patch, Mock
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import delivery_state as state
import frozen_us_close_recovery as frozen
import generate_report as generate
from models import MarketContext, QuoteObservation
from structured_reports import build_public_draft


def fixture():
    at = datetime.fromisoformat(frozen.FROZEN_AT)
    retrieved = datetime.fromisoformat(frozen.RETRIEVED_AT)
    quotes = {s: QuoteObservation(quote_id=f'{s}:2026-10-16:REGULAR:yfinance_batch',
              instrument_id=s, canonical_symbol=s, price=105, currency='USD',
              session='REGULAR', market_date='2026-10-16', observed_at=retrieved,
              retrieved_at=retrieved, provider='yfinance_batch', quote_type='OFFICIAL_CLOSE',
              quality_status='VALID', previous_regular_close=100, change_pct=5,
              market='GLOBAL' if s=='CL' else 'US') for s in frozen.PUBLIC_SYMBOLS}
    context = MarketContext(run_id='us_close:2026-10-16', report_type='us_close',
                            market_date='2026-10-16', generated_at=at,
                            market_session='REGULAR', quotes=quotes)
    draft = build_public_draft(context, generate._verified_data_only_report('', 'us_close'))
    draft.headline = '補報｜' + draft.headline
    draft.rendered_markdown = draft.rendered_markdown.replace('# 美股收盤日報', '# 補報｜美股收盤日報', 1)
    draft.rendered_markdown += '\n新聞尚未完成驗證。\n'
    return {'schema_version':'1', 'target': dict(frozen.TARGET),
            'source_manifest': dict(frozen.SOURCE_MANIFEST),
            'frozen_at': frozen.FROZEN_AT,
            'quotes': {s:q.model_dump(mode='json') for s,q in quotes.items()},
            'draft': draft.model_dump(mode='json'),
            'telegram_sha256': hashlib.sha256(generate.public_telegram_payload(
                draft.rendered_markdown,'us_close',at=at).encode()).hexdigest()}


class FrozenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.p = fixture()
        self.raw = frozen.canonical_bytes(self.p)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        patches = [patch.object(frozen,'EXPECTED_SHA256',hashlib.sha256(self.raw).hexdigest()),
                   patch.object(frozen,'OUTCOME_PATH',root/'outcome.json'),
                   patch.object(state,'STATE_PATH',root/'state.json'),
                   patch.object(state,'RECEIPTS_DIR',root/'receipts'),
                   patch.object(generate,'_report_result'),
                   patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/main',
                      'GITHUB_REPOSITORY':'pincoin1002/market-report-bot',
                      'DELIVERY_RECEIPT_REMOTE_CHECK':'true','TELEGRAM_BOT_TOKEN':'fixture',
                      'TELEGRAM_CHAT_ID':'123','GITHUB_STEP_SUMMARY':'',
                      'US_CLOSE_PUBLICATION_DATE':'2026-10-17'})]
        for p in patches:p.start();self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def validate_mutated(self, change, reason):
        p=copy.deepcopy(self.p);change(p);raw=frozen.canonical_bytes(p)
        with patch.object(frozen,'EXPECTED_SHA256',hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(ValueError,reason):frozen.validate_payload(raw)

    def test_exact_fixed_payload_and_calendar_accepted(self):
        c,d,at=frozen.validate_payload(self.raw)
        self.assertEqual(c.market_date,'2026-10-16');self.assertIn('補報',d.headline)
        self.assertEqual(frozen.TARGET['receipt_key'],'us_close:20261017')

    def test_corruption_stops_before_claim(self):
        with patch.object(state,'put_remote_receipt') as put:
            with self.assertRaisesRegex(ValueError,'CHECKSUM'):frozen.execute(self.raw+b' ', 'deliver_once')
            put.assert_not_called()

    def test_other_publication_and_receipt_target_rejected(self):
        for field in ('publication_date','trading_date','receipt_key','market','session'):
            with self.subTest(field=field):
                self.validate_mutated(lambda p:p['target'].update({field:'other'}),'TARGET')

    def test_future_quote_or_wrong_source_rejected(self):
        for field,value in (('market_date','2026-10-17'),('provider','unverified'),
                            ('quote_type','TRADE'),('retrieved_at','2026-10-18T00:00:00Z'),
                            ('observed_at','2026-10-18T00:00:00Z')):
            with self.subTest(field=field):
                self.validate_mutated(lambda p:p['quotes']['SPX'].update({field:value}),'PROVENANCE')

    def test_private_holdings_and_extra_fields_not_admitted(self):
        self.validate_mutated(lambda p:p.update({'positions':[]}), 'SCHEMA')
        self.validate_mutated(lambda p:p['quotes']['SPX'].update({'quantity':42}), 'FIELDS')
        self.validate_mutated(lambda p:p['quotes'].update({'PRIVATE':p['quotes']['SPX']}), 'UNIVERSE')

    def test_events_and_private_metadata_rejected(self):
        self.validate_mutated(lambda p:p['draft'].update({'event_calendar':['new event']}),'SCOPE')
        self.validate_mutated(lambda p:p['source_manifest'].update({'events':'CHECKED'}),'SOURCE')

    def test_changed_report_fails_telegram_checksum(self):
        self.validate_mutated(lambda p:p['draft'].update({'rendered_markdown':p['draft']['rendered_markdown']+'\n文字變更'}),'TELEGRAM')

    def test_no_send_and_duplicate_simulation_no_network_or_private(self):
        with patch('requests.post') as post, patch.object(state,'remote_receipt') as remote, \
             patch.object(generate,'run_portfolio_advice') as private:
            result=frozen.execute(self.raw,'dry_run')
            self.assertEqual(result['status'],'DRY_RUN_VALIDATED_NO_SEND')
            post.assert_not_called();remote.assert_not_called();private.assert_not_called()

    def test_existing_delivered_stops_without_sender(self):
        receipt={'key':frozen.TARGET['receipt_key'],'state':'DELIVERED','telegram_message_ids':[91]}
        with patch.object(state,'remote_receipt',return_value=receipt), patch.object(generate,'send_telegram') as send:
            result=frozen.execute(self.raw,'deliver_once')
            self.assertEqual(result['status'],'ALREADY_DELIVERED_BLOCKED');send.assert_not_called()

    def test_pending_or_uncertain_claim_cannot_be_bypassed(self):
        with patch.object(state,'remote_receipt',side_effect=RuntimeError('DELIVERY_RECEIPT_UNCERTAIN')), \
             patch.object(generate,'send_telegram') as send:
            with self.assertRaises(RuntimeError):frozen.execute(self.raw,'deliver_once')
            send.assert_not_called()

    def test_send_once_then_duplicate_stops_via_real_state_pipeline(self):
        remote={};calls=[]
        def put(key, receipt, *,sha=None):
            if not sha and key in remote:raise RuntimeError('claim conflict')
            remote[key]=receipt;calls.append(receipt['state']);return 'fixture-sha'
        telegram={'ok':True,'message_ids':[93],'confirmations':[{'http_status':200,'api_ok':True,'message_id':93}]}
        with patch.object(state,'remote_receipt',side_effect=lambda key:remote.get(key)), \
             patch.object(state,'put_remote_receipt',side_effect=put), \
             patch.object(generate,'send_telegram',return_value=telegram) as send, \
             patch.object(generate,'run_portfolio_advice') as private, patch.object(generate,'send_email') as email:
            result=frozen.execute(self.raw,'deliver_once')
            self.assertEqual(result['status'],'DELIVERED_RECEIPT_VERIFIED')
            again=frozen.execute(self.raw,'deliver_once')
            self.assertEqual(again['status'],'ALREADY_DELIVERED_BLOCKED')
            self.assertEqual(calls,['DELIVERING','DELIVERED']);send.assert_called_once()
            self.assertTrue(send.call_args.kwargs['single_attempt']);private.assert_not_called();email.assert_not_called()

    def test_api_accepted_receipt_failure_records_ambiguity_and_keeps_claim(self):
        remote={}
        def put(key, receipt, *,sha=None):
            if sha:raise RuntimeError('receipt write uncertain')
            remote[key]=receipt;return 'fixture-claim-sha'
        with patch.object(state,'remote_receipt',side_effect=lambda key:remote.get(key)), \
             patch.object(state,'put_remote_receipt',side_effect=put), \
             patch.object(generate,'send_telegram',return_value={'ok':True,'message_ids':[94]}):
            with self.assertRaises(RuntimeError):frozen.execute(self.raw,'deliver_once')
        result=json.loads(frozen.OUTCOME_PATH.read_text())
        self.assertEqual(result['status'],'AMBIGUOUS_DELIVERY_STOP_NO_RETRY')
        self.assertTrue(result['telegram_api_accepted']);self.assertEqual(result['telegram_message_ids'],[94])
        self.assertEqual(remote[frozen.TARGET['receipt_key']]['state'],'DELIVERING')

    def test_claim_write_failure_never_calls_sender(self):
        with patch.object(state,'remote_receipt',return_value=None), \
             patch.object(state,'put_remote_receipt',side_effect=RuntimeError('conflict')), \
             patch.object(generate,'send_telegram') as send:
            with self.assertRaises(RuntimeError):frozen.execute(self.raw,'deliver_once')
            send.assert_not_called()

    def test_single_attempt_has_no_format_or_http_retry(self):
        response=Mock(status_code=500);response.raise_for_status.side_effect=generate.requests.HTTPError()
        with patch('requests.post',return_value=response) as post:
            with self.assertRaises(RuntimeError):generate.send_telegram('fixture','us_close',single_attempt=True)
            post.assert_called_once()

    def test_verify_only_never_sends_when_missing(self):
        with patch.object(state,'remote_receipt',return_value=None), patch.object(generate,'send_telegram') as send:
            with self.assertRaisesRegex(ValueError,'RECEIPT_NOT_FOUND'):frozen.execute(self.raw,'verify_delivered')
            send.assert_not_called()

    def test_multiple_destinations_stop_before_claim(self):
        with patch.dict(os.environ,{'TELEGRAM_CHAT_ID':'123,456'}), \
             patch.object(state,'remote_receipt',return_value=None), patch.object(state,'put_remote_receipt') as put:
            with self.assertRaisesRegex(ValueError,'SINGLE_DESTINATION'):frozen.execute(self.raw,'deliver_once')
            put.assert_not_called()

    def test_no_auto_schedule_fetch_or_artifacts_in_workflow(self):
        root=Path(__file__).resolve().parents[1]
        workflow=(root/'.github/workflows/us-close-recovery.yml').read_text()
        self.assertIn('group: us-close-canonical-delivery',workflow)
        for token in ('schedule:', 'upload-artifact', 'PIOS_PORTFOLIO_SNAPSHOT_JSON', 'fetch_market_data.py', 'GEMINI_API_KEY'):
            self.assertNotIn(token,workflow)
        self.assertNotIn('encrypted_payload }}',workflow)

    def test_encrypted_manual_intake_roundtrip_no_plaintext_log(self):
        key=Fernet.generate_key();encrypted=Fernet(key).encrypt(self.raw).decode()
        event=Path(self.temp.name)/'event.json'
        event.write_text(json.dumps({'inputs':{'mode':'dry_run','encrypted_payload':encrypted}}))
        with patch.dict(os.environ,{'PORTFOLIO_KEY':key.decode(),'GITHUB_EVENT_NAME':'workflow_dispatch',
                                   'GITHUB_EVENT_PATH':str(event)}), \
             patch.object(sys,'argv',['frozen_us_close_recovery.py']), \
             patch('requests.post') as post, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(frozen.main(),0);post.assert_not_called()
        for forbidden in (encrypted,key.decode(),self.p['draft']['headline']):
            self.assertNotIn(forbidden,output.getvalue())

    def test_wrong_encryption_key_fails_closed_no_trace_or_input(self):
        key=Fernet.generate_key();encrypted=Fernet(Fernet.generate_key()).encrypt(self.raw).decode()
        event=Path(self.temp.name)/'event.json'
        event.write_text(json.dumps({'inputs':{'mode':'deliver_once','encrypted_payload':encrypted}}))
        with patch.dict(os.environ,{'PORTFOLIO_KEY':key.decode(),'GITHUB_EVENT_NAME':'workflow_dispatch',
                                   'GITHUB_EVENT_PATH':str(event)}), \
             patch.object(sys,'argv',['frozen_us_close_recovery.py']), \
             patch('requests.post') as post, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(frozen.main(),1);post.assert_not_called()
        for forbidden in (encrypted,key.decode(),'Traceback'):
            self.assertNotIn(forbidden,output.getvalue())


if __name__=='__main__':unittest.main()
