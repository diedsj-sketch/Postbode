import os
import tempfile
import unittest
from unittest.mock import patch
from dashboard import dashboard_page,daily_panel
from planning import today


class DailyViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,DATA_DIR=self.tmp.name)
        self.env.start()

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_reference_hidden_and_balances_first(self):
        with patch('dashboard.signing_key',return_value=b'test-only-signing-key'):
            html=dashboard_page('test-token')
        self.assertIn('<details id="reference">',html)
        self.assertNotIn('<details id="reference" open',html)
        self.assertLess(html.index('id="accounts"'),html.index('id="today"'))
        self.assertLess(html.index('id="today"'),html.index('id="reference"'))

    def test_incomplete_data_does_not_clear_due_payment(self):
        result={'issues':['Missing date'],'opening':{'personal':95000},'minimum':{'personal':95000},
                'personal_minimum':95000,'reserve':50000,'rows':[
                    {'date':today(),'ledger':'personal','amount':-10000,'name':'Example bill'}]}
        with patch('planning.forecast',return_value=result):
            html=daily_panel()
        self.assertIn('Payment recommendations paused',html)
        self.assertIn('Due today, not cleared for payment',html)
        self.assertNotIn('Today’s forecast-supported payments',html)
