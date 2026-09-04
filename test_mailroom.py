import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import mailroom as m

RECIPIENT='d4c3b2a1-f6e5-4170-a392-c5b4d6e7f809'
PDF=b'%PDF-1.4\n%Synthetic test bytes; not a real letter\n%%EOF\n'

def payload(pdf=PDF):
    return {'uuid':'a1b2c3d4-e5f6-4081-92a3-b4c5d6e7f809','reference':'TEST-001',
            'status':{'id':300,'name':'Ontvangen'},'recipient':{'uuid':RECIPIENT},
            'pdf':base64.b64encode(pdf).decode(),'document_sha256':hashlib.sha256(pdf).hexdigest(),
            'content':'Example sender. Pay EUR 123.45 by 18 September 2026.'}

def analysis(p):
    a=m.review_analysis('')
    a.sender='Example sender'
    a.subject='Synthetic invoice'
    a.category='invoice'
    a.action='pay'
    a.action_detail='Pay the invoice'
    a.review_required=False
    a.deadline=m.Fact(value='2026-09-18',evidence='18 September 2026')
    a.amount=m.Fact(value='123.45',evidence='EUR 123.45')
    a.currency=m.Fact(value='EUR',evidence='EUR 123.45')
    return a

class FakeGoogle:
    archives=0
    calendars=0
    emails=0
    def archive(self,row,a,pdf):
        type(self).archives+=1
        m.update(row['id'],drive_id='file123')
        return 'file123'
    def calendar(self,ident,a,link):
        type(self).calendars+=1
        return 'event123'
    def notify(self,row,a,link):
        type(self).emails+=1
        m.update(row['id'],email_state='sent',email_id='message123')

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'DATA_DIR':self.tmp.name,
            'POSTBODE_WEBHOOK_SECRET':'synthetic-test-secret','POSTBODE_RECIPIENT_UUID':RECIPIENT,
            'ENABLE_CALENDAR':'true','ENABLE_EMAIL':'true','ALERT_EMAIL':'self@example.com'})
        self.env.start()
        FakeGoogle.archives=FakeGoogle.calendars=FakeGoogle.emails=0
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def enqueue(self,p=None):
        raw=json.dumps(payload() if p is None else p).encode()
        sig=hmac.new(b'synthetic-test-secret',raw,hashlib.sha256).hexdigest()
        return m.accept(raw,sig)
    def row(self):
        with m.db() as c:
            return c.execute('SELECT * FROM mail').fetchone()
    def test_signature_is_over_exact_bytes(self):
        raw=json.dumps(payload()).encode()
        sig=hmac.new(b'synthetic-test-secret',raw,hashlib.sha256).hexdigest()
        self.assertEqual(m.accept(raw+b' ',sig)[0],401)
        self.assertEqual(m.accept(raw,'')[0],401)
        self.assertEqual(m.accept(raw,sig)[0],202)
    def test_wrong_recipient_and_checksum_rejected(self):
        p=payload();p['recipient']['uuid']='aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa'
        self.assertEqual(self.enqueue(p)[0],422)
        p=payload();p['document_sha256']='0'*64
        self.assertEqual(self.enqueue(p)[0],422)
    def test_legacy_payload_rejected(self):
        self.assertEqual(self.enqueue({'letter':payload()})[0],422)
    def test_duplicates_and_document_revisions(self):
        self.assertFalse(self.enqueue()[1]['duplicate'])
        self.assertTrue(self.enqueue()[1]['duplicate'])
        self.assertFalse(self.enqueue(payload(PDF+b'updated'))[1]['duplicate'])
        with m.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail').fetchone()[0],2)
    def test_original_is_saved_when_ai_fails(self):
        self.enqueue()
        def fail(p): raise RuntimeError('sensitive body must not be logged')
        m.worker_once(fail,FakeGoogle)
        r=self.row()
        self.assertEqual(r['state'],'pending')
        self.assertEqual(r['error'],'RuntimeError')
        self.assertEqual((Path(self.tmp.name)/'originals'/r['id']/'original.pdf').read_bytes(),PDF)
    def test_full_mock_pipeline_and_no_repeat_side_effects(self):
        self.enqueue()
        m.worker_once(analysis,FakeGoogle)
        self.assertEqual(self.row()['state'],'done')
        self.enqueue()
        self.assertFalse(m.worker_once(analysis,FakeGoogle))
        self.assertEqual((FakeGoogle.archives,FakeGoogle.calendars,FakeGoogle.emails),(1,1,1))
    def test_evidence_missing_blocks_calendar(self):
        self.enqueue()
        def bad(p):
            a=analysis(p)
            a.deadline.evidence='invented evidence'
            return m.validate_evidence(a,p['content'])
        m.worker_once(bad,FakeGoogle)
        self.assertEqual(self.row()['state'],'review')
        self.assertEqual(FakeGoogle.calendars,0)
        self.assertEqual(FakeGoogle.emails,1)
    def test_invalid_calendar_date_flagged(self):
        a=analysis(payload());a.deadline.value='2026-02-30'
        a=m.validate_evidence(a,payload()['content'])
        self.assertTrue(a.review_required)
        self.assertIsNone(a.deadline.value)
    def test_no_ocr_does_not_call_ai(self):
        a=m.classify({'content':None})
        self.assertTrue(a.review_required)
        self.assertEqual(a.action,'review')

    def test_connection_check_is_read_only(self):
        calls=[]
        def request(url, **kwargs):
            calls.append((url, kwargs))
            return {'id':'gpt-4.1-mini'}
        google=[]
        with patch.dict(os.environ, {'OPENAI_API_KEY':'test-key','OPENAI_MODEL':'gpt-4.1-mini'}):
            result=m.check_connections(request=request, google_factory=lambda:google.append(True))
        self.assertEqual(result, {'openai':'ok','google':'ok'})
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0], 'https://api.openai.com/v1/models/gpt-4.1-mini')
        self.assertNotIn('body', calls[0][1])
        self.assertEqual(google,[True])

    def test_synthetic_pilot_exercises_pipeline_without_outbound_actions(self):
        with patch.dict(os.environ, {'PROCESSING_ENABLED': 'false',
                                    'ENABLE_EMAIL': 'false',
                                    'ENABLE_CALENDAR': 'false'}):
            result = m.synthetic_pilot(analysis, FakeGoogle)
        self.assertTrue(result['verified'])
        self.assertFalse(result['duplicate'])
        self.assertEqual(FakeGoogle.archives, 1)
        self.assertEqual((FakeGoogle.emails, FakeGoogle.calendars), (0, 0))
    def test_ambiguous_email_not_sent_twice(self):
        self.enqueue();r=self.row()
        g=object.__new__(m.Google)
        calls=[]
        def fail(*args,**kwargs):
            calls.append(1)
            raise TimeoutError()
        g.call=fail
        with self.assertRaises(TimeoutError): g.notify(r,analysis(payload()),'test')
        self.assertEqual(self.row()['email_state'],'uncertain')
        with self.assertRaises(RuntimeError): g.notify(self.row(),analysis(payload()),'test')
        self.assertEqual(len(calls),1)
    def test_drive_id_persisted_before_upload_and_reused(self):
        self.enqueue()
        g=object.__new__(m.Google)
        g.archive_root=lambda:'root123'
        g.folder=lambda name,parent:'folder123'
        generated=[]
        def call(url,**kw):
            if 'generateIds' in url:
                generated.append(1);return {'ids':['reserved123']}
            if 'fields=id' in url:
                raise urllib.error.HTTPError(url,404,'not found',{},None)
            if 'upload' in url:
                self.assertEqual(self.row()['drive_id'],'reserved123')
                raise TimeoutError()
            raise AssertionError(url)
        g.call=call
        for _ in range(2):
            with self.assertRaises(TimeoutError):g.archive(self.row(),analysis(payload()),PDF)
        self.assertEqual(len(generated),1)
    def test_calendar_retries_treat_conflict_as_success(self):
        g=object.__new__(m.Google)
        bodies=[]
        def call(url,**kw):
            if 'events?' in url:
                bodies.append(kw['body'])
                raise urllib.error.HTTPError(url,409,'already exists',{},None)
            return {}
        g.call=call
        with patch.dict(os.environ,{'GOOGLE_CALENDAR_ID':'personal@example.com'}):
            one=g.calendar('a'*64,analysis(payload()),'link')
            two=g.calendar('a'*64,analysis(payload()),'link')
        self.assertEqual(one,two)
        self.assertEqual(bodies[0]['id'],bodies[1]['id'])
        self.assertEqual(bodies[0]['visibility'],'private')
        self.assertNotIn('attendees',bodies[0])
    def test_body_limits(self):
        statuses=[]
        env={'PATH_INFO':'/webhooks/postbode','REQUEST_METHOD':'POST',
             'CONTENT_LENGTH':str(m.MAX_BODY+1),'wsgi.input':io.BytesIO()}
        m.application(env,lambda s,h:statuses.append(s))
        self.assertTrue(statuses[0].startswith('413'))
    def test_csv_formula_escaped(self):
        self.enqueue()
        a=analysis(payload());a.sender='=DANGEROUS()'
        m.update(self.row()['id'],analysis=a.model_dump_json())
        dest=Path(self.tmp.name)/'register.csv';m.export_register(dest)
        self.assertIn("'=DANGEROUS()",dest.read_text(encoding='utf-8-sig'))
    def test_schema_is_strict_for_api(self):
        schema=m.Analysis.model_json_schema()
        for s in [schema,*schema['$defs'].values()]:
            if s.get('type')=='object':
                self.assertFalse(s['additionalProperties'])
                self.assertEqual(set(s['required']),set(s['properties']))

if __name__=='__main__':
    unittest.main()
