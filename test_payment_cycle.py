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
