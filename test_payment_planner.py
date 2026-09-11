import datetime as dt
import unittest
from payment_planner import safe_date
from unittest.mock import patch
import os
import tempfile
import json
from mailroom import db,now
from payment_planner import refresh


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.day=dt.date(2026,9,12)
        self.end=self.day+dt.timedelta(days=5)

    def test_reserve_is_preserved(self):
        self.assertEqual(safe_date([],95000,'personal',45000,self.day,self.end,50000),self.day)
        self.assertIsNone(safe_date([],95000,'personal',45001,self.day,self.end,50000))

    def test_future_costs_reserved(self):
        rows=[{'date':self.end,'ledger':'personal','amount':-40000}]
        self.assertIsNone(safe_date(rows,95000,'personal',10000,self.day,self.end,50000))

    def test_debit_before_same_day_income(self):
        rows=[{'date':self.day,'ledger':'personal','amount':10000}]
        self.assertEqual(safe_date(rows,50000,'personal',10000,self.day,self.end,50000),self.day+dt.timedelta(days=1))

    def test_company_cash_not_personal_cash(self):
        rows=[{'date':self.day,'ledger':'cloudstep','amount':1000000}]
        self.assertIsNone(safe_date(rows,50000,'personal',1,self.day,self.end,50000))

    def test_multiple_reservations(self):
        rows=[{'date':self.day,'ledger':'personal','amount':-30000}]
        self.assertIsNone(safe_date(rows,95000,'personal',20000,self.day,self.end,50000))

    def test_refresh_preserves_agreement_and_does_not_repeat_sent_draft(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,DATA_DIR=folder):
            with db() as c:
                stamp=now()
                c.execute('INSERT INTO finance_plan_baseline VALUES(1,?)',(stamp,))
                c.execute("INSERT INTO finance_cases VALUES('P','personal','Creditor','REF12345','{}','accepted',?,NULL,'gmail')",(stamp,))
                c.execute("INSERT INTO finance_instalments VALUES('S','P',10000,?,'accepted','{}',?,NULL,0)",(self.day.isoformat(),stamp))
                result={'issues':[],'minimum':{'personal':40000},'opening':{'personal':50000},'reserve':50000,
                        'rows':[{'date':self.day,'ledger':'personal','amount':-10000,'name':'Creditor / S','estimated':False},
                                {'date':self.day,'ledger':'personal','amount':20000,'name':'Income','estimated':False}]}
                with patch('planning.forecast',return_value=result):
                    refresh(c,stamp,self.day)
                    self.assertEqual(c.execute('SELECT state FROM finance_payment_plan').fetchone()[0],'proposal-draft')
                    self.assertEqual(c.execute('SELECT due_date FROM finance_instalments').fetchone()[0],self.day.isoformat())
                    c.execute("UPDATE finance_case_outreach SET status='user-reported-sent'")
                    refresh(c,stamp,self.day)
                    self.assertEqual(c.execute('SELECT state FROM finance_payment_plan').fetchone()[0],'awaiting-reply')
                    self.assertEqual(c.execute('SELECT status FROM finance_case_outreach').fetchone()[0],'user-reported-sent')

    def test_incomplete_forecast_cannot_create_promise(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,DATA_DIR=folder):
            with db() as c:
                stamp=now()
                c.execute("INSERT INTO finance_cases VALUES('P','personal','Creditor','REF12345','{}','accepted',?,NULL,'gmail')",(stamp,))
                c.execute("INSERT INTO finance_instalments VALUES('S','P',10000,?,'accepted','{}',?,NULL,0)",(self.day.isoformat(),stamp))
                result={'issues':['Missing rent date'],'minimum':{'personal':40000},'opening':{'personal':50000},'reserve':50000,
                        'rows':[{'date':self.day,'ledger':'personal','amount':-10000,'name':'Creditor / S'}]}
                with patch('planning.forecast',return_value=result):
                    refresh(c,stamp,self.day)
                    self.assertEqual(c.execute('SELECT state FROM finance_payment_plan').fetchone()[0],'forecast-incomplete')
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_case_outreach').fetchone()[0],0)
