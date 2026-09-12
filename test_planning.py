import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch
import planning
from mailroom import db, now
from dashboard import dashboard_page


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,DATA_DIR=self.tmp.name,DASHBOARD_PASSWORD='test')
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_baseline_is_explicit_and_never_reapplied(self):
        with db() as c:
            self.assertIsNone(c.execute("SELECT balance_cents FROM finance_accounts WHERE id='personal-bunq'").fetchone()[0])
            self.assertTrue(planning.baseline(c,now()))
            c.execute("UPDATE finance_accounts SET balance_cents=123 WHERE id='personal-bunq'")
            self.assertFalse(planning.baseline(c,now()))
            self.assertEqual(c.execute("SELECT balance_cents FROM finance_accounts WHERE id='personal-bunq'").fetchone()[0],123)

    def test_transfers_cancel_and_airbnb_monthly_total(self):
        with db() as c:
            planning.baseline(c,now())
            r=planning.forecast(c,dt.date(2026,10,1),30,True)
            airbnb=[e for e in r['rows'] if 'Airbnb' in e['name']]
            self.assertEqual(sum(e['amount'] for e in airbnb),250000)
            transfers=c.execute("SELECT transfer_id,SUM(amount_cents) FROM finance_plan_rules WHERE transfer_id IS NOT NULL GROUP BY transfer_id").fetchall()
            self.assertTrue(all(row[1]==0 for row in transfers))
            self.assertEqual(sum(e['amount'] for e in r['rows'] if e['ledger']=='personal'),715000)
            self.assertTrue(any('Mortgage' in i for i in r['issues']))
            self.assertFalse(any('Miscellaneous' in i for i in r['issues']))
            self.assertTrue(all(x['date'].day==25 for x in r['rows'] if x['name']=='Miscellaneous allowance'))

    def test_rent_expires_and_estimates_excluded(self):
        with db() as c:
            planning.baseline(c,now())
            r=planning.forecast(c,dt.date(2026,12,1),30)
            self.assertFalse(any('Rent' in e['name'] or 'Airbnb' in e['name'] for e in r['rows']))
            self.assertTrue(any('unconfirmed' in i for i in r['issues']))

    def test_month_end_does_not_drift(self):
        self.assertEqual(list(planning.occurrences(31,dt.date(2027,1,1),dt.date(2027,3,31))),
                         [dt.date(2027,1,31),dt.date(2027,2,28),dt.date(2027,3,31)])

    def test_floor_edit_and_no_assumed_topup(self):
        with db() as c:
            planning.baseline(c,now())
            planning.action(c,{'action':'plan-reserve','amount':'1000'},now())
            r=planning.forecast(c,dt.date(2026,9,11),0)
            self.assertEqual(r['funding_gap'],5000)
            self.assertEqual(r['opening']['personal'],95000)
            self.assertEqual(r['first_breach'],dt.date(2026,9,11))
            self.assertEqual(r['rows'],[])

    def test_render(self):
        with db() as c: planning.baseline(c,now())
        page=dashboard_page('test')
        self.assertIn('Personal reserve and cashflow',page)
        self.assertIn('No dividend is assumed',page)


if __name__=='__main__':unittest.main()
