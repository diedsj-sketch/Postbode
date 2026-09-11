import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import cases
import finance_sync
from mailroom import db,now


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,DATA_DIR=self.tmp.name)
        self.env.start()
        self.p={'schema_version':1,'generated_at':now(),'sheets':{s:[] for s in finance_sync.SHEETS}}
        self.p['sheets']['Personal Debts']=[{'Debt ID':'P1','Debtor / liability holder':'D.M.J. Sjardijn',
            'Original creditor':'Example','Case / contract':'ABC12345','Arrangement status':'Accepted',
            'Latest claimed balance':900,'Next amount':100,'Next due date':'2026-10-20'}]
        self.p['sheets']['Personal Repayments']=[{'Schedule ID':'S1','Debt ID':'P1','Amount':100,
            'Currency':'EUR','Exact due date':'2026-10-20','Status':'Accepted / unpaid'}]

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_import_carries_schedule_once_not_whole_debt(self):
        with db() as c:
            finance_sync.ingest(c,self.p,now());finance_sync.ingest(c,self.p,now())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_cases').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_case_evidence').fetchone()[0],2)
            events,issues=cases.payments(c,dt.date(2026,10,1),dt.date(2026,10,31))
            self.assertEqual(len(events),1);self.assertEqual(events[0][2],-10000)

    def test_payment_survives_reimport_and_does_not_close_case(self):
        with db() as c:
            finance_sync.ingest(c,self.p,now())
            cases.action(c,{'action':'case-paid','id':'S1'},now())
            finance_sync.ingest(c,self.p,now())
            self.assertEqual(cases.payments(c,dt.date(2026,10,1),dt.date(2026,10,31))[0],[])
            self.assertEqual(c.execute('SELECT arrangement FROM finance_cases').fetchone()[0],'accepted')

    def test_paid_pending_and_missing_dates_not_new_demands(self):
        self.p['sheets']['Personal Repayments'][0]['Status']='User-reported paid; creditor still counts arrears'
        with db() as c:
            finance_sync.ingest(c,self.p,now())
            events,issues=cases.payments(c,dt.date(2026,10,1),dt.date(2026,10,31))
            self.assertEqual(events,[]);self.assertTrue(any('do not pay twice' in i for i in issues))
        self.assertEqual(cases.arrangement('Proposed / not accepted; awaiting reply'),'awaiting')

    def test_exact_match_preserves_arrangement(self):
        rid='60e2ccb0-2650-4af6-aa2a-03354390875d'
        with patch.dict(os.environ,POSTBODE_RECIPIENTS_JSON=json.dumps({rid:'Diederik Sjardijn'})):
            with db() as c:
                finance_sync.ingest(c,self.p,now())
                a={'sender':'Example','reference':{'value':'ABC12345'},'amount':{'value':'950'},'review_required':False}
                c.execute('INSERT INTO mail(id,received,payload,analysis) VALUES(?,?,?,?)',
                          ('m',now(),json.dumps({'recipient':{'uuid':rid}}),json.dumps(a)))
                r=c.execute('SELECT * FROM mail WHERE id=\'m\'').fetchone()
                cases.attach_mail(c,r,now())
                link=c.execute('SELECT * FROM finance_case_mail').fetchone()
                self.assertEqual(link['case_id'],'P1');self.assertIsNotNone(link['conflict'])
                self.assertEqual(c.execute('SELECT arrangement FROM finance_cases').fetchone()[0],'accepted')

    def test_company_identity_not_personal(self):
        f={'Debtor / liability holder':'Cloudstep Holding B.V.'}
        self.assertEqual(cases.ledger(f,'Company Debts'),'cloudstep')
        self.assertIsNone(cases.ledger(f,'Personal Debts'))
        self.assertIsNone(cases.ledger({'Debtor / liability holder':'D. Sollo'},'Unclear Liability'))


if __name__=='__main__':unittest.main()
