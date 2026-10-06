import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from datetime import date
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import threading
import edinet_benefits as eb
import server as api_server

def archive(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('XBRL/PublicDoc/report.htm', '<html><body><p>'+text+'</p></body></html>')
    return b.getvalue()

def doc(did, kind='120', when='2026-10-06 10:00', sec='12340'):
    return dict(docID=did, docTypeCode=kind, secCode=sec, filerName='試験株式会社',
                submitDateTime=when, xbrlFlag='1', withdrawalStatus='0', disclosureStatus='0')

class Tests(unittest.TestCase):
    def test_inline_and_hidden(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            z.writestr('XBRL/PublicDoc/report.htm','<ix:hidden>株主優待 廃止</ix:hidden><p>株主優待制度</p><p>毎年3月末日現在の株主名簿に記載された100株以上の株主に商品券を贈呈。</p>')
        passages=eb.extract_zip(b.getvalue())
        rows=eb.make_items(doc('S100TEST'),{'industry':'小売業'},passages,'2026-10-07')
        self.assertEqual(rows[0]['month'],3)
        self.assertEqual(rows[0]['industry'],'小売業')
        self.assertEqual(rows[0]['officialSource'],'')
        self.assertNotIn('廃止',rows[0]['benefit'])
        self.assertEqual(set(rows[0]),{'code','name','industry','month','recordDate','conditions','benefit','officialSource','confirmedAt','status','sourceType'})
    def test_no_inferred_month_and_negative(self):
        p=[{'file':'a','text':'株主優待制度：6か月以上の継続保有を条件に商品券を贈呈。'}]
        self.assertIsNone(eb.make_items(doc('S100TEST'),{},p,'2026-10-07')[0]['month'])
        p=[{'file':'a','text':'株主優待制度は実施しておりません。'}]
        self.assertEqual(eb.make_items(doc('S100TEST'),{},p,'2026-10-07'),[])
    def test_xbrl_fallback(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            z.writestr('XBRL/PublicDoc/report.xbrl','<root><fact contextRef="x">&lt;p&gt;株主優待制度：100株以上に商品券&lt;/p&gt;</fact></root>')
        self.assertTrue(eb.extract_zip(b.getvalue()))
    def test_job_retries_and_negative_supersedes(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'EDINET_BENEFITS_DIR':root}):
            bridge=SimpleNamespace(EDINET_API_KEY='secret-not-output',EDINET_CACHE_DIR=root,USER_AGENT='test',
                _edinet_load_codelist=lambda:{'1234':{'industry':'小売業'}})
            c=eb.Collector(bridge)
            def daily(day):
                return [doc('S100OLD1',when='2026-10-05 10:00'),doc('S100NEW1','180'),dict(doc('S100PDF1'),xbrlFlag='0')]
            c._daily=daily
            c._call=lambda f:f()
            c._download=lambda did:archive('株主優待制度は廃止しました。' if did=='S100NEW1' else '株主優待制度：3月末現在の100株以上の株主に商品券。')
            c.start({'startDate':'2026-10-05','endDate':'2026-10-06'})
            c.worker.join(5)
            self.assertEqual(c.view()['status'],'completed')
            self.assertEqual(c.export()['items'],[])
            self.assertEqual(c.view()['skippedReports'],1)
            self.assertEqual(len(c.evidence()['documents']),2)
            self.assertNotIn(bridge.EDINET_API_KEY,json.dumps(c.evidence()))
            c.state['status']='running';c._save()
            self.assertEqual(eb.Collector(bridge).view()['status'],'interrupted')
            # Failure remains visible; resume retries that document.
            c._download=lambda did: (_ for _ in ()).throw(OSError('url contains secret'))
            c.start({'startDate':'2026-10-05','endDate':'2026-10-06'});c.worker.join(5)
            self.assertEqual(c.view()['status'],'partial')
            self.assertNotIn('url contains secret',json.dumps(c.evidence()))
            c._download=lambda did:archive('株主優待制度：100株以上に商品券。')
            c.start({'action':'resume'});c.worker.join(5)
            self.assertEqual(c.view()['status'],'completed')
            self.assertEqual(len(c.export()['items']),1)
    def test_bad_period_and_api_auth(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'EDINET_BENEFITS_DIR':root}):
            bridge=SimpleNamespace(EDINET_API_KEY='secret',EDINET_CACHE_DIR=root,_edinet_load_codelist=lambda:{'1234':{}})
            c=eb.Collector(bridge)
            with self.assertRaises(ValueError):c.start({'startDate':'2020-01-01','endDate':'2026-10-06'})
            with self.assertRaises(PermissionError):c._call(lambda:(_ for _ in ()).throw(RuntimeError('Access denied due to invalid subscription key')))
    def test_http_existing_and_new_routes(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'EDINET_BENEFITS_DIR':root}):
            eb._instance=None
            server=ThreadingHTTPServer(('127.0.0.1',0),api_server.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                self.assertEqual(json.load(urlopen(base+'/health')),{'status':'ok'})
                response=urlopen(base+'/edinet-benefits');self.assertIn('text/html',response.headers['Content-Type'])
                self.assertIn('株主優待',response.read().decode())
                self.assertEqual(json.load(urlopen(base+'/api/edinet/benefits/status'))['status'],'idle')
                req=Request(base+'/api/edinet/benefits/status',headers={'Origin':'https://ducat595.github.io'})
                with urlopen(req) as response:
                    self.assertEqual(response.headers['Access-Control-Allow-Origin'],'https://ducat595.github.io')
                req=Request(base+'/api/edinet/benefits/job',headers={'Origin':'https://ducat595.github.io'},method='OPTIONS')
                with urlopen(req) as response:
                    self.assertEqual(response.status,204)
                    self.assertIn('Authorization',response.headers['Access-Control-Allow-Headers'])

                req=Request(base+'/api/edinet/benefits/job',data=b'{"action":"start"}',headers={'Content-Type':'application/json'},method='POST')
                with self.assertRaises(HTTPError) as e:urlopen(req)
                self.assertEqual(e.exception.code,403)
                with patch.dict(os.environ,{'EDINET_BENEFITS_ADMIN_TOKEN':'expected'}):
                    req=Request(base+'/api/edinet/benefits/job',data=b'{}',headers={'Authorization':'Bearer wrong'},method='POST')
                    with self.assertRaises(HTTPError) as e:urlopen(req)
                    self.assertEqual(e.exception.code,401)
            finally:
                server.shutdown();server.server_close();eb._instance=None

if __name__=='__main__':unittest.main()
