import os,tempfile,importlib.util,unittest
from unittest.mock import patch
from io import BytesIO
os.environ['APP_SECRET']='local-test-only-secret'
os.environ['ACTIVE_LOT_CHECKPOINT_DIR']=tempfile.mkdtemp()
os.environ['HOLD_QUEUE_DIR']=tempfile.mkdtemp()
os.environ['ENABLE_LIVE_PUBLISH']='0'
os.environ['ALLOW_PAID_AI']='0'
import requests
from PIL import Image
from pathlib import Path
spec=importlib.util.spec_from_file_location('tested_app',Path(__file__).with_name('app.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.app.config['TESTING']=True
class MobileTests(unittest.TestCase):
 def setUp(self):
  self.net=patch.object(requests.sessions.Session,'request',side_effect=AssertionError('External network forbidden'));self.net.start();self.addCleanup(self.net.stop)
  self.client=m.app.test_client();self.lot=patch.object(m,'_sync_current_lot_once',return_value=46);self.lot.start();self.addCleanup(self.lot.stop)
 def photo(self):
  out=BytesIO();Image.new('RGB',(40,30),'red').save(out,'JPEG');return out.getvalue()
 def csrf(self):
  self.client.get('/mobile')
  with self.client.session_transaction() as s:return s['hold_csrf']
 def upload(self,n=1):
  return self.client.post('/mobile/upload',data={'hold_csrf':self.csrf(),'photos':[(BytesIO(self.photo()),'photo.jpg') for _ in range(n)]},content_type='multipart/form-data')
 def test_mobile_home(self):self.assertEqual(self.client.get('/mobile').status_code,200)
 def test_install_manifest(self):
  r=self.client.get('/mobile/manifest.webmanifest');self.assertEqual(r.json['display'],'standalone');self.assertEqual(r.json['start_url'],'/mobile')
 def test_icons(self):
  for size in [192,512]:
   r=self.client.get('/mobile/icon/'+str(size)+'.png');self.assertEqual(Image.open(BytesIO(r.data)).size,(size,size))
 def test_worker(self):self.assertIn(b"mode==='navigate'",self.client.get('/mobile-sw.js').data)
 def test_upload_csrf(self):self.assertEqual(self.client.post('/mobile/upload').status_code,403)
 def test_direct_phone_upload_and_thumbnail(self):
  self.assertEqual(self.upload().status_code,303);self.assertEqual(self.client.get('/picker/thumb/1').status_code,200)
 def test_24_photo_limit(self):self.assertEqual(self.upload(25).status_code,400)
 def test_corrupt_image(self):
  r=self.client.post('/mobile/upload',data={'hold_csrf':self.csrf(),'photos':(BytesIO(b'bad'),'bad.jpg')},content_type='multipart/form-data');self.assertEqual(r.status_code,400)
 def test_local_photo_ai_data_without_google(self):
  self.upload()
  with self.client.session_transaction() as s:pid=s['picker_session_id']
  self.assertTrue(m.google_photo_to_data_url(m.PHOTO_CACHE[pid][0],token='').startswith('data:image/jpeg;base64,'))
 def test_restart_restores_exact_approved_bytes(self):
  self.upload()
  with self.client.session_transaction() as s:state=dict(s)
  pid=state['picker_session_id'];raw=self.photo()
  with m.app.test_request_context('/'):
   m.session.update(state);m.session['photo_editor_approved']=True;m.APPROVED_PHOTO_CACHE[pid]={1:{'bytes':raw}};m.LOT002_DRAFT_CACHE[pid]={'title':'Saved lot'}
   self.assertTrue(m._save_active_lot_checkpoint());m.APPROVED_PHOTO_CACHE.pop(pid);m.LOT002_DRAFT_CACHE.pop(pid);m.PHOTO_CACHE.pop(pid)
   self.assertTrue(m._restore_active_lot_checkpoint());self.assertEqual(m.APPROVED_PHOTO_CACHE[pid][1]['bytes'],raw);self.assertTrue(m.session['photo_editor_approved']);self.assertEqual(m.LOT002_DRAFT_CACHE[pid]['title'],'Saved lot')
 def test_publish_disabled_all_entrypoints(self):
  for path in ['/ebay/current-publish-auto','/ebay/current-publish','/ebay/hold-queue/publish']:
   self.assertEqual(self.client.post(path).status_code,409)
 def test_final_publish_rejects_csrf(self):
  with patch.dict(os.environ,{'ENABLE_LIVE_PUBLISH':'1'}):self.assertEqual(self.client.post('/ebay/current-publish').status_code,403)
 def test_final_publish_rejects_unreviewed(self):
  token=self.csrf()
  with patch.dict(os.environ,{'ENABLE_LIVE_PUBLISH':'1'}):self.assertEqual(self.client.post('/ebay/current-publish',data={'hold_csrf':token}).status_code,409)
 def test_paid_ai_disabled(self):
  with m.app.test_request_context('/'):
   with self.assertRaisesRegex(RuntimeError,'AI'):m.call_openai({'input':'test'})
 def test_existing_photo_order(self):self.assertEqual(m._build_logical_photo_order([{}, {}, {}],2),[2,1,3])


class OperatorAccessTests(unittest.TestCase):
    def test_private_mode_protects_lot_and_hold(self):
        from base64 import b64encode

        client = m.app.test_client()
        headers = {'Authorization': 'Basic ' + b64encode(b'vintage:fixture-password').decode()}
        private_paths = ('/mobile', '/picker/thumb/1', '/lot/state', '/hold/state',
                         '/hold/export', '/ebay/hold-queue')
        with patch.dict(os.environ, {'VINTAGE_OPERATOR_PASSWORD': 'fixture-password'}):
            for path in private_paths:
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/mobile', headers=headers).status_code, 200)
            self.assertEqual(client.get('/hold/state', headers=headers).status_code, 200)
            cross_site = {**headers, 'Sec-Fetch-Site': 'cross-site'}
            self.assertEqual(client.post('/mobile/upload', headers=cross_site).status_code, 403)


if __name__=='__main__':unittest.main(verbosity=2)
