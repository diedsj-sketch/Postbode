import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch
import cockpit
from mailroom import db,now


class CockpitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,DATA_DIR=self.tmp.name,DASHBOARD_PASSWORD='test-only')
        self.env.start()

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_rollover_and_overspending(self):
        with db() as c:
            cockpit.schema(c)
            c.execute("INSERT INTO cockpit_budget VALUES(1,'2026-09',60000,150000)")
            c.execute("INSERT INTO cockpit_events VALUES('e','spending',80000,'{}',?)",(now(),))
            self.assertEqual(cockpit.remaining(c,dt.date(2026,9,20)),-20000)
            self.assertEqual(cockpit.remaining(c,dt.date(2026,10,1)),130000)
            self.assertEqual(cockpit.remaining(c,dt.date(2027,1,1)),580000)

    def test_duplicate_budget_request_is_idempotent(self):
        with db() as c:
            f={'action':'cockpit-budget','request_id':'a','amount':'500'}
            cockpit.action(c,f,now());cockpit.action(c,f,now())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM cockpit_events').fetchone()[0],1)
            with self.assertRaises(ValueError):cockpit.action(c,dict(f,request_id='b'),now())

    def test_balances_atomic_stale_guard_and_reconciliation(self):
        with db() as c:
            c.execute('UPDATE finance_accounts SET balance_cents=10000')
            rows=list(c.execute('SELECT * FROM finance_accounts WHERE active=1 ORDER BY id'))
            f={'action':'cockpit-balances','request_id':'balances','version':cockpit.version(rows)}
            f.update({'balance:'+r['id']:'100' for r in rows})
            f['balance:personal-rabobank']='50'
            cockpit.action(c,f,now())
            self.assertEqual(c.execute('SELECT difference_cents FROM cockpit_reconciliations').fetchone()[0],-5000)
            with self.assertRaises(ValueError):cockpit.action(c,dict(f,request_id='stale'),now())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM cockpit_reconciliations').fetchone()[0],1)

    def test_spending_classification_debits_budget_not_balance(self):
        with db() as c:
            cockpit.schema(c)
            c.execute("INSERT INTO cockpit_budget VALUES(1,'2026-09',50000,150000)")
            c.execute("INSERT INTO cockpit_reconciliations VALUES(1,'personal',-5000,?,NULL,NULL)",(now(),))
            f={'action':'cockpit-reconcile','request_id':'r','id':'1','classification':'spending'}
            cockpit.action(c,f,now());cockpit.action(c,f,now())
            self.assertEqual(cockpit.remaining(c,dt.date(2026,9,12)),45000)
            self.assertIsNone(c.execute("SELECT balance_cents FROM finance_accounts WHERE id='personal-rabobank'").fetchone()[0])

    def test_unknown_balance_never_presented_as_zero_allowance(self):
        with db() as c:
            m=cockpit.model(c,'personal')
            self.assertIsNone(m['available']);self.assertIsNone(m['allowance'])
        html=cockpit.page('test')
        self.assertIn('Balance required',html)
        self.assertNotIn('DEMO-001',html)

    def test_signed_balance_validation(self):
        self.assertEqual(cockpit.euros('-200,50',True),-20050)
        for v in ('NaN','Infinity','1.001','-1'):
            with self.assertRaises(ValueError):cockpit.euros(v)

    def test_company_view_and_invalid_ledger(self):
        self.assertIn('Provisional company cash capacity',cockpit.page('test',ledger='cloudstep'))
        self.assertIn('Provisional spending capacity',cockpit.page('test',ledger='<script>'))

    def test_xss_escaped(self):
        self.assertIn('&lt;script&gt;',cockpit.page('test',notice='<script>'))

    def test_payment_requires_fresh_balance(self):
        with db() as c:
            cockpit.schema(c)
            c.execute("UPDATE finance_accounts SET updated_at='2026-09-01T00:00:00+00:00'")
            c.execute("INSERT INTO finance_cases VALUES('P','personal','Example','REF','{}','accepted',?,NULL,'gmail')",(now(),))
            c.execute("INSERT INTO finance_instalments VALUES('S','P',10000,'2026-09-12','accepted','{}',?,?,0)",(now(),now()))
            m=cockpit.model(c,'personal')
            self.assertIn('Update bank balances after recording a payment or receipt',m['problems'])

    def test_movement_confirmation_does_not_change_balance(self):
        from planning import today
        with db() as c:
            cockpit.schema(c)
            r={'date':today(),'ledger':'personal','amount':15000,'name':'Tax refund'}
            f={'action':'cockpit-movement','request_id':'receipt','movement_id':cockpit.movement_id(r)}
            with patch('planning.forecast',return_value={'rows':[r]}):
                cockpit.action(c,f,now());cockpit.action(c,f,now())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM cockpit_events WHERE kind='movement'").fetchone()[0],1)
            self.assertIsNone(c.execute("SELECT balance_cents FROM finance_accounts LIMIT 1").fetchone()[0])

    def test_future_receipt_cannot_be_confirmed(self):
        from planning import today
        with db() as c:
            cockpit.schema(c)
            r={'date':today()+dt.timedelta(days=1),'ledger':'personal','amount':15000,'name':'Tax refund'}
            with patch('planning.forecast',return_value={'rows':[r]}),self.assertRaises(ValueError):
                cockpit.action(c,{'action':'cockpit-movement','request_id':'future','movement_id':cockpit.movement_id(r)},now())

    def test_allowance_caps_cash_and_does_not_count_living_twice(self):
        from planning import today
        with db() as c:
            cockpit.schema(c)
            c.execute('INSERT INTO finance_plan_baseline VALUES(1,?)',(now(),))
            c.execute('INSERT INTO cockpit_budget VALUES(1,?,60000,150000)',(today().strftime('%Y-%m'),))
            c.execute("UPDATE finance_accounts SET balance_cents=0")
            c.execute("UPDATE finance_accounts SET balance_cents=95000 WHERE id='personal-rabobank'")
            result={'issues':[],'minimum':{'personal':15000},'reserve':50000,
                    'rows':[{'date':today(),'ledger':'personal','amount':-60000,'name':'Food and discretionary spending','estimated':False},
                            {'date':today(),'ledger':'personal','amount':-20000,'name':'Bill','estimated':False}]}
            with patch('planning.forecast',return_value=result):
                m=cockpit.model(c,'personal')
            self.assertEqual(m['allowance'],25000)

    def test_unresolved_cases_do_not_hide_calculated_capacity(self):
        from planning import today
        with db() as c:
            cockpit.schema(c)
            c.execute('INSERT INTO cockpit_budget VALUES(1,?,60000,150000)',(today().strftime('%Y-%m'),))
            c.execute('UPDATE finance_accounts SET balance_cents=0')
            c.execute("UPDATE finance_accounts SET balance_cents=95000 WHERE id='personal-rabobank'")
            result={'issues':['Source reconciliation outstanding'],'minimum':{},'reserve':50000,'rows':[]}
            with patch('planning.forecast',return_value=result):
                m=cockpit.model(c,'personal')
            self.assertEqual(m['allowance'],45000)
            self.assertEqual(m['headroom'],45000)
            self.assertTrue(m['provisional'])
            result['rows']=[{'ledger':'personal','date':today(),'amount':-100000,'name':'Existing commitment','estimated':False}]
            with patch('planning.forecast',return_value=result):
                m=cockpit.model(c,'personal')
            self.assertEqual(m['allowance'],0)
            self.assertEqual(m['headroom'],-55000)
