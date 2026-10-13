import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import delivery_state


class ReceiptTests(unittest.TestCase):
    def test_empty_ids_do_not_mark_delivered(self):
        with tempfile.TemporaryDirectory() as d, patch.object(delivery_state, 'STATE_PATH', Path(d)/'s.json'):
            with self.assertRaises(ValueError):delivery_state.mark_state('tw_close:20261013','DELIVERED',telegram_result={'ok':True,'message_ids':[]})
    def test_fresh_remote_receipt_suppresses_old_checkout(self):
        receipt={'key':'tw_close:20261013','state':'DELIVERED','telegram_message_ids':[42]}
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'content':base64.b64encode(json.dumps(receipt).encode()).decode()}).encode()
        with tempfile.TemporaryDirectory() as d, patch.object(delivery_state,'STATE_PATH',Path(d)/'state.json'), patch.object(delivery_state,'RECEIPTS_DIR',Path(d)/'receipts'), patch.dict(os.environ,{'DELIVERY_RECEIPT_REMOTE_CHECK':'true','GITHUB_TOKEN':'fixture','GITHUB_REPOSITORY':'pincoin1002/market-report-bot'}),patch('delivery_state.urllib.request.urlopen',return_value=response):
            self.assertTrue(delivery_state.already_delivered('tw_close:20261013'))
    def test_uncertain_receipt_does_not_authorize_resend(self):
        with tempfile.TemporaryDirectory() as d, patch.object(delivery_state,'STATE_PATH',Path(d)/'state.json'),patch.object(delivery_state,'RECEIPTS_DIR',Path(d)/'receipts'):
            p=delivery_state.receipt_path('tw_close:20261013');p.parent.mkdir();p.write_text(json.dumps({'key':'tw_close:20261013','state':'DELIVERED','telegram_message_ids':[]}))
            with self.assertRaises(RuntimeError):delivery_state.already_delivered('tw_close:20261013')
    def test_all_workflows_serialize_and_check_remote_receipt(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('tw-open','tw-close','us-open','us-close'):
            text=(root/'.github/workflows'/f'{name}.yml').read_text()
            self.assertIn(f'group: {name}-canonical-delivery',text)
            self.assertIn('DELIVERY_RECEIPT_REMOTE_CHECK: "true"',text)
            self.assertNotIn('time.sleep(seconds)',text)
