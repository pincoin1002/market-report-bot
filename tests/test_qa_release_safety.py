"""Isolated release gates; no local Phase 3 / PIOS additions required."""
import ast
import importlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


class ReleaseSafetyTests(unittest.TestCase):
    def test_qa_workflow_absent(self):
        self.assertFalse((ROOT / '.github/workflows/bot.yml').exists())
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            text = path.read_text()
            self.assertNotIn('telegram_bot.py', text)
            self.assertNotIn('requirements-bot.txt', text)
        self.assertFalse(any(x.strip() and not x.startswith('#') for x in (ROOT / 'requirements-bot.txt').read_text().splitlines()))

    def test_qa_cli_and_poll_fail_before_network(self):
        bot = importlib.import_module('telegram_bot')
        with patch.object(bot.requests, 'post') as post:
            with self.assertRaisesRegex(RuntimeError, 'TELEGRAM_QA_DISABLED'):
                bot.poll('', '', '', 240)
            with self.assertRaises(SystemExit):
                bot.main()
            post.assert_not_called()

    def test_report_artifact_allowlist(self):
        forbidden = ('market_snapshot.json', 'market_context.json', 'market_report_draft.json',
                     'portfolio_advice_audit.json', 'portfolio_action_brief.json', 'review_package_*.md')
        for name in ('tw-open', 'tw-close', 'us-open', 'us-close', 'tw-open-dry-run-smoke'):
            text = (ROOT / f'.github/workflows/{name}.yml').read_text()
            self.assertNotIn('prepare_review.py', text)
            for block in text.split('      - name:'):
                if 'actions/upload-artifact@' in block:
                    for field in forbidden:
                        self.assertNotIn(field, block)

    def test_no_unreleased_research_workflow(self):
        self.assertFalse((ROOT / '.github/workflows/holding-research.yml').exists())
        self.assertTrue((ROOT / '.github/workflows/portfolio-events.yml').exists())
        text = (ROOT / 'scripts/generate_report.py').read_text()
        self.assertNotIn('from pios_private_runtime', text)
        self.assertNotIn('from user_language', text)

    def test_validation_logs_do_not_dump_inputs(self):
        for name in ('generate_report', 'validate_report', 'portfolio_context'):
            tree = ast.parse((ROOT / f'scripts/{name}.py').read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                    message = node.args[0]
                    if isinstance(message, ast.Constant) and isinstance(message.value, str) and message.value in (
                        'market_context.json invalid', 'market_report_draft.json invalid', 'snapshot invalid — blocking report generation',
                        'snapshot unavailable during delivery', 'portfolio artifact stage failed'):
                        self.assertFalse(any(kw.arg == 'exc_info' for kw in node.keywords))
