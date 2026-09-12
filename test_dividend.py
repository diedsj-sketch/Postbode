import datetime as dt
import unittest
from planning import dividend_scenario

class DividendTests(unittest.TestCase):
    def result(self,company=200000):
        day=dt.date(2026,9,22)
        return {'opening':{'personal':50000,'cloudstep':company},'reserve':50000,'rows':[{'date':day,'ledger':'personal','amount':-85000}]},day

    def test_minimum_net_and_company_gross(self):
        r,day=self.result()
        d=dividend_scenario(r,day)
        self.assertEqual((d['net'],d['gross'],d['remaining_gap']),(85000,100000,0))
        self.assertEqual(d['transfers'][0]['withholding'],15000)
        self.assertEqual(r['opening']['personal'],50000)

    def test_company_commitments_cap_dividend(self):
        r,day=self.result()
        r['rows'].append({'date':day+dt.timedelta(days=2),'ledger':'cloudstep','amount':-150000})
        d=dividend_scenario(r,day)
        self.assertEqual((d['net'],d['gross'],d['remaining_gap']),(42500,50000,42500))

    def test_unknown_balances_no_invented_funding(self):
        r,day=self.result()
        self.assertEqual(dividend_scenario(r,day,False)['net'],0)

    def test_same_day_income_cannot_erase_earlier_gap(self):
        r,day=self.result(0)
        r['rows'].append({'date':day,'ledger':'cloudstep','amount':200000})
        r['rows'].append({'date':day+dt.timedelta(days=1),'ledger':'personal','amount':0})
        d=dividend_scenario(r,day)
        self.assertEqual(d['transfers'][0]['date'],day+dt.timedelta(days=1))
        self.assertEqual(d['remaining_gap'],85000)

    def test_no_gap_no_dividend(self):
        r,day=self.result();r['opening']['personal']=200000
        self.assertEqual(dividend_scenario(r,day)['transfers'],[])
