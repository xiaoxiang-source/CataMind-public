"""HTTP smoke with disposable SQLite/data and loopback-only test server."""
import json
import base64
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from unittest import mock
from catmat_tools.bader_charge import write_cube

class PostprocessHTTP(unittest.TestCase):
    def test_run_list_result_and_download(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'test.db';data=root/'data';data.mkdir()
            conn=sqlite3.connect(db);conn.executescript(server.SCHEMA);conn.close()
            base=dict(com1='test',com2='test',natoms=1,origin=[0,0,0],nx=1,ny=1,nz=1,vx=[1,0,0],vy=[0,1,0],vz=[0,0,1],atoms=[[1,1,0,0,0]])
            for name,val in [('total',3),('slab',1),('ads',1)]:write_cube(base,[val],data/(name+'.cube'))
            with mock.patch.multiple(server,DB_PATH=str(db),DATA_DIR=str(data),TMP_DIR=str(root),AUTH_ENABLED=False,AII_PY=sys.executable):
                http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
                thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
                url='http://127.0.0.1:%d'%http.server_port
                def request(path,body=None):
                    req=urllib.request.Request(url+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,timeout=5) as r:return r.read()
                try:
                    catalog=json.loads(request('/api/postprocess/tools'))
                    self.assertTrue(catalog['ok']);self.assertEqual(len(catalog['data']['tools']),5)
                    uploaded={}
                    for key,name in [('total','total'),('slab','slab'),('ads','ads')]:
                        payload={'field':key+'_cube','files':[{'name':name+'.cube','content_b64':base64.b64encode((data/(name+'.cube')).read_bytes()).decode()}]}
                        upload=json.loads(request('/api/postprocess/tools/upload',payload))
                        self.assertTrue(upload['ok'],upload)
                        uploaded[key+'_cube']=upload['data']['path']
                    listing=json.loads(request('/api/postprocess/tools/browse',{'field':'total_cube','path':str(Path(uploaded['total_cube']).parent)}))
                    self.assertEqual(listing['data']['entries'][0]['path'],uploaded['total_cube'])
                    with self.assertRaises(urllib.error.HTTPError):
                        request('/api/postprocess/tools/browse',{'field':'total_cube','path':str(root)})
                    result=json.loads(request('/api/postprocess/tools/run',dict(tool='density_difference',inputs=uploaded,params={'density_unit':'e/bohr^3'})))
                    self.assertTrue(result['ok'],result);jid=result['data']['id']
                    for _ in range(100):
                        result=json.loads(request('/api/postprocess/tools/jobs/'+jid))['data']
                        if result['status'] not in ('queued','running'):break
                        time.sleep(.05)
                    self.assertEqual(result['status'],'done',result)
                    self.assertEqual(result['result']['density_integral_e'],1)
                    payload=json.loads(request('/api/postprocess/tools/jobs/'+jid+'/result.json'))
                    self.assertEqual(payload['review_status'],'preview')
                    self.assertIn(b'Delta electron density',request('/api/postprocess/tools/jobs/'+jid+'/delta.cube'))
                    self.assertEqual(len(json.loads(request('/api/postprocess/tools/jobs'))['data']),1)
                    with self.assertRaises(urllib.error.HTTPError):request('/api/postprocess/tools/jobs/'+jid+'/request.json')
                finally:
                    http.shutdown();http.server_close();thread.join(timeout=3)

if __name__=='__main__':unittest.main()
