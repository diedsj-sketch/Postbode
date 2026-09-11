import copy
import os
import tempfile
import unittest
from unittest.mock import patch
import finance_sync as sync
from mailroom import db, now
from dashboard import dashboard_page


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, DATA_DIR=self.tmp.name, DASHBOARD_PASSWORD='test-only-password')
        self.env.start()
        self.payload = {'schema_version':1, 'generated_at':now(),
            'sheets':{s:[] for s in sync.SHEETS}}
        self.payload['sheets']['Personal Debts']=[{'Debt ID':'TEST-1','Original creditor':'Example',
            'Case / contract':'A1','Latest claimed balance':123,'Gmail source':'https://mail.google.com/mail/u/0/#all/test'}]

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_idempotent_and_review_only(self):
        with db() as c:
            sync.ingest(c,self.payload,now())
            sync.ingest(c,self.payload,now())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_gmail_cases').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM finance_obligations').fetchone()[0],0)
        self.assertIn('Example',dashboard_page('test'))

    def test_review_changes_never_overwrite_local_debt(self):
        with db() as c:
            sync.ingest(c,self.payload,now())
            row=c.execute('SELECT * FROM finance_gmail_cases').fetchone()
            form={'id':'TEST-1','fingerprint':row['fingerprint'],'operation':'create','ledger_id':'personal'}
            sync.action(c,form,now())
            with self.assertRaises(ValueError): sync.action(c,form,now())
            debt=c.execute('SELECT * FROM finance_obligations').fetchone()
            self.assertEqual(debt['status'],'review')
            self.assertIsNone(debt['amount_cents'])
            c.execute("UPDATE finance_obligations SET amount_cents=100,status='paid'")
            self.payload['sheets']['Personal Debts'][0]['Latest claimed balance']=900
            sync.ingest(c,self.payload,now())
            debt=c.execute('SELECT * FROM finance_obligations').fetchone()
            self.assertEqual(debt['status'],'paid')
            self.assertEqual(debt['amount_cents'],100)
            row=c.execute('SELECT * FROM finance_gmail_cases').fetchone()
            self.assertNotEqual(row['fingerprint'],row['reviewed_fingerprint'])

    def test_invalid_duplicate_missing_and_old_snapshots(self):
        with db() as c:
            sync.ingest(c,self.payload,now())
            bad=copy.deepcopy(self.payload)
            bad['sheets']['Company Debts']=bad['sheets']['Personal Debts']
            with self.assertRaises(ValueError):sync.ingest(c,bad,now())
            bad=copy.deepcopy(self.payload)
            del bad['sheets']['Unclear Liability']
            with self.assertRaises(ValueError):sync.ingest(c,bad,now())
            bad=copy.deepcopy(self.payload)
            bad['generated_at']='2020-01-01T00:00:00+00:00'
            with self.assertRaises(ValueError):sync.ingest(c,bad,now())

    def test_html_and_links_are_not_executable(self):
        result=sync.source_fields({'x':'<script>alert(1)</script>','Gmail source':'javascript:alert(1)'})
        self.assertNotIn('<script>',result)
        self.assertNotIn('href=',result)
