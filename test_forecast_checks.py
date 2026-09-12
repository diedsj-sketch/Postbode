import datetime as dt
import unittest
import test_cockpit
from mailroom import db,now
from forecast_checks import classify,shortfall
import planning

class ForecastCheckTests(unittest.TestCase):
    setUp=test_cockpit.CockpitTests.setUp
    tearDown=test_cockpit.CockpitTests.tearDown
    def test_unrelated_company_and_awaiting_are_not_universal_blocks(self):
        with db() as c:
            c.execute("INSERT INTO finance_cases VALUES('C','personal','Example','R','{}','awaiting',?,NULL,'gmail')",(now(),))
            checks=classify(c,{'issues':['Missing balance: ING (growth)','Existing proposal awaiting reply, no new request: Example / R']},'personal')
            self.assertEqual(checks['blocking'],[])
            self.assertEqual(len(checks['groups']),1)

    def test_unknown_claims_remain_one_visible_constraint(self):
        with db() as c:
            for n in range(4):
                c.execute("INSERT INTO finance_cases VALUES(?,'personal','Example',?,'{}','unknown',?,NULL,'gmail')",(str(n),str(n),now()))
            checks=classify(c,{'issues':[f'Unscheduled claim, not a payment instruction: Example / {n}' for n in range(4)]},'personal')
            self.assertEqual(len(checks['blocking']),1)
            self.assertIn('4 case records',checks['blocking'][0])

    def test_transfer_source_remains_relevant(self):
        with db() as c:
            planning.baseline(c,now())
            checks=classify(c,{'issues':['Missing balance: Adyen (cloudstep)','Missing balance: ING (growth)']},'personal')
            self.assertEqual(checks['blocking'],['Missing balance: Adyen (cloudstep)'])

    def test_breach_contains_exact_cause_and_largest_gap(self):
        day=dt.date(2026,9,12)
        rows=[{'ledger':'personal','amount':-60000,'date':day,'name':'Rent'}, {'ledger':'personal','amount':-10000,'date':day,'name':'Bill'}]
        b=shortfall(rows,100000,'personal',50000,day)
        self.assertEqual((b['date'],b['name'],b['gap'],b['total_gap']),(day,'Rent',10000,20000))

    def test_misc_migration_preserves_user_edits(self):
        with db() as c:
            planning.baseline(c,now())
            c.execute("UPDATE finance_plan_rules SET day=NULL WHERE id='cloudstep-misc'")
            planning.schema(c)
            self.assertEqual(c.execute("SELECT day FROM finance_plan_rules WHERE id='cloudstep-misc'").fetchone()[0],25)
            c.execute("UPDATE finance_plan_rules SET day=26,amount_cents=-55000 WHERE id='cloudstep-misc'")
            planning.schema(c)
            self.assertEqual(tuple(c.execute("SELECT day,amount_cents FROM finance_plan_rules WHERE id='cloudstep-misc'").fetchone()),(26,-55000))

    def test_month_window_reserves_without_invented_due_date(self):
        import json
        import cases
        with db() as c:
            c.execute("INSERT INTO finance_cases VALUES('M','personal','Example','REF','{}','accepted',?,NULL,'gmail')",(now(),))
            fields=json.dumps({'Period':'September 2026','Date precision':'Month only'})
            c.execute("INSERT INTO finance_instalments VALUES('MI','M',40000,NULL,'accepted',?,?,NULL,0)",(fields,now()))
            rows,issues=cases.payments(c,dt.date(2026,9,12),dt.date(2026,12,12))
            self.assertEqual(rows[0][0],dt.date(2026,9,12))
            self.assertEqual(rows[0][2],-40000)
            self.assertTrue(rows[0][3].startswith('Reserve only'))
            self.assertIsNone(c.execute("SELECT due_date FROM finance_instalments WHERE id='MI'").fetchone()[0])
            self.assertEqual(classify(c,{'issues':issues},'personal')['blocking'],[])
            c.execute("UPDATE finance_instalments SET status='proposed'")
            rows,_=cases.payments(c,dt.date(2026,9,12),dt.date(2026,12,12))
            self.assertEqual(rows,[])
