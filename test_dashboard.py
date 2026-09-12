import datetime as dt
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import dashboard
import mailroom


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'DATA_DIR': self.temp.name,
            'DASHBOARD_PASSWORD': 'test-password',
        }, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def request(self, path, method='GET', body='', cookie=''):
        captured = {}
        raw = body.encode()
        environ = {
            'PATH_INFO': path,
            'REQUEST_METHOD': method,
            'QUERY_STRING': '',
            'CONTENT_LENGTH': str(len(raw)),
            'wsgi.input': io.BytesIO(raw),
            'HTTP_COOKIE': cookie,
        }
        def start(status, headers):
            captured['status'] = status
            captured['headers'] = dict(headers)
        result = b''.join(dashboard.handle(environ, start))
        return captured, result

    def login(self):
        captured, _ = self.request('/dashboard/login', 'POST', 'password=test-password')
        return captured['headers']['Set-Cookie'].split(';', 1)[0]

    def test_known_ledgers_accounts_and_income_are_seeded(self):
        with mailroom.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_ledgers').fetchone()[0], 4)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_accounts').fetchone()[0], 6)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM finance_recurring WHERE direction='income'").fetchone()[0], 5)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM finance_recurring WHERE direction='expense'").fetchone()[0], 8)

    def test_dashboard_redirects_unauthenticated_user(self):
        captured, _ = self.request('/dashboard')
        self.assertEqual(captured['status'], '303 See Other')
        self.assertEqual(captured['headers']['Location'], '/dashboard/login')

    def test_login_and_dashboard_render_without_financial_values(self):
        cookie = self.login()
        captured, body = self.request('/dashboard', cookie=cookie)
        self.assertEqual(captured['status'], '200 OK')
        self.assertIn(b'Cloudstep Holding B.V.', body)
        self.assertIn(b'Cashflow cockpit', body)
        self.assertIn(b'/dashboard/reference',body)
        _,reference=self.request('/dashboard/reference',cookie=cookie)
        self.assertIn(b'Airbnb weekly fees', reference)
        self.assertNotIn(b'test-password', body)

    def test_account_update_requires_csrf_and_changes_only_its_ledger(self):
        cookie = self.login()
        token = cookie.split('=', 1)[1]
        body = ('csrf=' + dashboard.csrf(token) +
                '&action=account&id=personal-rabobank&amount=1234%2C56')
        captured, _ = self.request('/dashboard/action', 'POST', body, cookie)
        self.assertEqual(captured['status'], '303 See Other')
        with mailroom.db() as c:
            value = c.execute("SELECT balance_cents FROM finance_accounts WHERE id='personal-rabobank'").fetchone()[0]
            other = c.execute("SELECT balance_cents FROM finance_accounts WHERE id='cloudstep-adyen'").fetchone()[0]
        self.assertEqual(value, 123456)
        self.assertIsNone(other)

    def test_mail_obligation_stays_out_of_cashflow_until_confirmed(self):
        analysis = mailroom.Analysis.model_validate({
            'sender': 'Creditor', 'subject': 'Invoice', 'category': 'invoice',
            'summary': 'Payment requested',
            'letter_date': {'value': None, 'evidence': None},
            'reference': {'value': None, 'evidence': None},
            'amount': {'value': '42.50', 'evidence': 'EUR 42.50'},
            'currency': {'value': 'EUR', 'evidence': 'EUR'},
            'deadline': {'value': (dt.date.today() + dt.timedelta(days=2)).isoformat(), 'evidence': 'date'},
            'action': 'pay', 'action_detail': 'Pay invoice', 'review_required': False,
            'review_reason': '', 'draft_reply': None,
        })
        with mailroom.db() as c:
            c.execute("INSERT INTO mail(id,received,payload,state) VALUES('m1',?,?,?)",
                      (mailroom.now(), json.dumps({}), 'done'))
        mailroom.propose_financial_obligation('m1', 'Diederik Sjardijn', analysis)
        with mailroom.db() as c:
            row = c.execute("SELECT * FROM finance_obligations WHERE mail_id='m1'").fetchone()
            before = dashboard.forecast(c, 'personal', 100000)
            c.execute("UPDATE finance_obligations SET status='confirmed' WHERE mail_id='m1'")
            after = dashboard.forecast(c, 'personal', 100000)
        self.assertEqual(row['status'], 'review')
        self.assertEqual(before, [])
        self.assertEqual(after[-1][3], 95750)


if __name__ == '__main__':
    unittest.main()
