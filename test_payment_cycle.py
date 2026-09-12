import datetime as dt
import unittest
import test_cockpit
from mailroom import db,now
from payment_cycle import model,window

class CycleTests(unittest.TestCase):
    setUp=test_cockpit.CockpitTests.setUp
    tearDown=test_cockpit.CockpitTests.tearDown

    def test_year_rollover(self):
        first,last,end=window(dt.date(2026,12,26))
        self.assertEqual((first,last,end),(dt.date(2027,1,22),dt.date(2027,1,25),dt.date(2027,2,22)))

    def test_preserves_agreed_date_and_awaiting_reply(self):
        with db() as c:
            c.execute("INSERT INTO finance_cases VALUES('C','personal','Example','R','{}','awaiting',?,NULL,'gmail')",(now(),))
            c.execute("INSERT INTO finance_instalments VALUES('I','C',10000,'2026-09-15','accepted','{}',?,NULL,0)",(now(),))
            m=model(c,'personal',dt.date(2026,9,12))
            item=m['groups']['Communication to review'][0]
            self.assertEqual(item['due_date'],'2026-09-15')
            self.assertIsNone(item['planned'])
            self.assertIn('No repeat',item['explanation'])
            self.assertTrue(all(not rows for rows in model(c,'cloudstep',dt.date(2026,9,12))['groups'].values()))

    def test_joint_replan_waits_for_income_without_changing_agreements(self):
        from payment_cycle import allocate
        day=dt.date(2026,9,12)
        items=[dict(id=str(n),due_date='2026-09-15',amount_cents=30000,event_name='Debt '+str(n)) for n in range(2)]
        rows=[dict(date=dt.date(2026,9,15),ledger='personal',amount=-30000,name=i['event_name']) for i in items]
        rows.append(dict(date=dt.date(2026,9,22),ledger='personal',amount=100000,name='Salary'))
        result=dict(rows=rows,opening={'personal':50000,'cloudstep':0},reserve=50000,dividend={'balances_known':True})
        planned=allocate(result,items,'personal',day)
        self.assertEqual(len(planned['selected']),2)
        self.assertEqual([r['proposed'] for r in planned['selected']],[dt.date(2026,9,23)]*2)
        self.assertTrue(all(r['due_date']=='2026-09-15' for r in items))
        self.assertGreaterEqual(planned['forecast']['personal_minimum'],50000)

    def test_unfunded_debt_is_explicit_and_not_double_allocated(self):
        from payment_cycle import allocate
        day=dt.date(2026,9,12)
        items=[dict(id=str(n),due_date='2026-09-15',amount_cents=30000,event_name='Debt '+str(n)) for n in range(2)]
        result=dict(rows=[],opening={'personal':90000,'cloudstep':0},reserve=50000,dividend={'balances_known':True})
        planned=allocate(result,items,'personal',day)
        self.assertEqual(len(planned['selected']),1)
        self.assertEqual(len(planned['unfunded']),1)
