"""Synthetic canaries only; no real portfolio or credentials accessed."""
import ast
import io
import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from logging_config import SafeDiagnosticAdapter, setup_logging


class PublicLogSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.handlers = logging.getLogger().handlers[:]
        self.level = logging.getLogger().level
        self.hook = sys.excepthook
        self.out_patch = patch('sys.stdout', self.stdout)
        self.err_patch = patch('sys.stderr', self.stderr)
        self.env_patch = patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'})
        self.out_patch.start(); self.err_patch.start(); self.env_patch.start()
        setup_logging('INFO')
        self.log = SafeDiagnosticAdapter(logging.getLogger('providers'), {})

    def tearDown(self):
        logging.getLogger().handlers = self.handlers
        logging.getLogger().setLevel(self.level)
        sys.excepthook = self.hook
        self.out_patch.stop(); self.err_patch.stop(); self.env_patch.stop()

    def assert_canaries_absent(self):
        combined = self.stdout.getvalue() + self.stderr.getvalue()
        for canary in ('PRIVATE_HOLDING_CANARY', 'PRIVATE_REFERENCE_CANARY', 'ACCOUNT_CANARY',
                       'API_KEY_CANARY', 'COST_CANARY', 'QUANTITY_CANARY', 'secret-query', 'Traceback'):
            self.assertNotIn(canary, combined)

    def test_symbol_anonymous_and_no_sensitive_extra(self):
        self.log.warning('single fetch failed', extra={'symbol': 'PRIVATE_HOLDING_CANARY',
            'quantity': 'QUANTITY_CANARY', 'cost': 'COST_CANARY', 'account': 'ACCOUNT_CANARY',
            'reference': 'PRIVATE_REFERENCE_CANARY', 'request_params': {'apiKey': 'API_KEY_CANARY'}})
        record = json.loads(self.stdout.getvalue())
        self.assertEqual(record['source'], 'providers')
        self.assertEqual(len(record['instrument_ref']), 16)
        self.assert_canaries_absent()

    def test_error_type_without_sensitive_trace(self):
        try:
            raise ValueError('API_KEY_CANARY secret-query PRIVATE_HOLDING_CANARY COST_CANARY')
        except ValueError:
            self.log.warning('batch download failed', exc_info=True)
        self.assertEqual(json.loads(self.stdout.getvalue())['error_type'], 'ValueError')
        self.assert_canaries_absent()

    def test_missing_coverage_only_counts(self):
        self.log.warning('quote observations missing or invalid by role', extra={
            'portfolio_required': ['PRIVATE_HOLDING_CANARY'], 'missing': ['PRIVATE_HOLDING_CANARY'],
            'public_market_core': [], 'optional_context': ['PRIVATE_REFERENCE_CANARY']})
        record = json.loads(self.stdout.getvalue())
        self.assertEqual(record['portfolio_required_count'], 1)
        self.assertEqual(record['missing_count'], 1)
        self.assert_canaries_absent()

    def test_provider_and_failure_counts_preserved(self):
        self.log.info('provider tier done', extra={'provider': 'yfinance_batch', 'hit': 4, 'miss': 2})
        record = json.loads(self.stdout.getvalue())
        self.assertEqual((record['provider'], record['hit'], record['miss']), ('yfinance_batch', 4, 2))

    def test_untrusted_enum_values_dropped(self):
        self.log.info('provider tier done', extra={'provider': 'API_KEY_CANARY', 'status': 'PRIVATE_REFERENCE_CANARY',
            'report_type': 'ACCOUNT_CANARY', 'hit': 'QUANTITY_CANARY'})
        self.assert_canaries_absent()

    def test_http_library_messages_and_annotations_redacted(self):
        for name in ('yfinance', 'urllib3.connectionpool', 'httpx', 'google.genai'):
            try:
                raise RuntimeError('API_KEY_CANARY')
            except RuntimeError:
                logging.getLogger(name).error('https://secret-query/PRIVATE_HOLDING_CANARY?key=API_KEY_CANARY',
                    extra={'request_params': 'PRIVATE_REFERENCE_CANARY'}, exc_info=True)
        self.assertIn('external library diagnostic', self.stdout.getvalue())
        self.assertIn('::error', self.stderr.getvalue())
        self.assert_canaries_absent()

    def test_uncaught_failure_hook_has_type_not_payload(self):
        sys.excepthook(ValueError, ValueError('COST_CANARY API_KEY_CANARY'), None)
        self.assertIn('ValueError', self.stdout.getvalue())
        self.assert_canaries_absent()

    def test_production_log_messages_static_and_no_format_arguments(self):
        for name in ('fetch_market_data', 'providers'):
            tree = ast.parse((ROOT / f'scripts/{name}.py').read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'log':
                    self.assertIsInstance(node.args[0], ast.Constant)
                    self.assertEqual(len(node.args), 1)

    def test_real_batch_failure_does_not_leak_request(self):
        from providers import YFinanceBatchProvider
        with patch('providers.yf.download', side_effect=ValueError('API_KEY_CANARY PRIVATE_HOLDING_CANARY')):
            self.assertEqual(YFinanceBatchProvider().fetch_many(['PRIVATE_HOLDING_CANARY']), {})
        self.assert_canaries_absent()

    def test_gemini_failure_does_not_echo_exception(self):
        from fetch_market_data import check_market_closed_via_gemini
        with patch('fetch_market_data._call_gemini_market_check', side_effect=ValueError('API_KEY_CANARY')):
            self.assertFalse(check_market_closed_via_gemini('TW', '2026-10-16'))
        self.assert_canaries_absent()
