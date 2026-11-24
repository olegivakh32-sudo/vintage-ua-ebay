from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from test_mobile import m


class MobileUploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.storage = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', str(self.storage / 'checkpoints')))
        self.enterContext(patch.dict(m.os.environ, {'HOLD_QUEUE_DIR': str(self.storage / 'hold')}))
        self.enterContext(patch.object(m, '_discover_next_lot_from_ebay', return_value=4))
        self.enterContext(patch.object(
            m.requests.sessions.Session, 'request', side_effect=AssertionError('External network forbidden')
        ))
        self.client = m.app.test_client()

    @staticmethod
    def photo(color='blue'):
        source = BytesIO()
        Image.new('RGB', (160, 120), color).save(source, 'JPEG')
        return source.getvalue()

    @staticmethod
    def field(markup, name):
        return re.search(fr'name="{name}" value="([^"]+)"', markup)[1]

    def form(self, client):
        markup = client.get('/mobile').get_data(as_text=True)
        return {name: self.field(markup, name) for name in ('hold_csrf', 'upload_batch')}

    def send(self, client, form, files):
        return client.post('/mobile/upload', headers={'Accept': 'application/json'}, data={
            **form, 'photos': [(BytesIO(raw), name) for name, raw in files],
        })

    def paths(self):
        roots = self.storage / 'checkpoints' / 'mobile_photos'
        return {path.relative_to(roots) for path in roots.glob('*/*.jpg')}

    def test_retry_of_first_upload_recovers_without_updated_cookie_or_cache(self):
        form = self.form(self.client)
        old_cookie = self.client.get_cookie('session').value
        self.assertEqual(self.send(self.client, form, [('one.jpg', self.photo())]).json, {'photos': 1})
        files_on_disk = self.paths()
        with self.client.session_transaction() as data:
            picker = data['picker_session_id']
        m.PHOTO_CACHE.pop(picker)
        retry = m.app.test_client()
        retry.set_cookie('session', old_cookie)
        with patch.object(m, '_mobile_prepare_photo', side_effect=AssertionError('Replay must not re-encode')):
            replay = self.send(retry, form, [('one.jpg', self.photo())])
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json, {'photos': 1})
        self.assertEqual(self.paths(), files_on_disk)
        with retry.session_transaction() as data:
            self.assertEqual(data['picker_session_id'], picker)

    def test_completed_batch_rejects_different_payload_and_another_session(self):
        form = self.form(self.client)
        self.assertEqual(self.send(self.client, form, [('one.jpg', self.photo())]).status_code, 201)
        self.assertEqual(self.send(self.client, form, [('one.jpg', self.photo('red'))]).status_code, 409)
        other = m.app.test_client()
        other_form = self.form(other)
        other_form['upload_batch'] = form['upload_batch']
        self.assertEqual(self.send(other, other_form, [('one.jpg', self.photo())]).status_code, 409)
        self.assertEqual(len(self.paths()), 1)

    def test_first_uploads_from_two_tabs_remain_one_lot(self):
        first = self.form(self.client)
        second = self.form(self.client)
        cookie = self.client.get_cookie('session').value

        def upload(form):
            client = m.app.test_client()
            client.set_cookie('session', cookie)
            return self.send(client, form, [('photo.jpg', self.photo())]).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(upload, (first, second))), [201, 409])
        self.assertEqual(len(self.paths()), 1)

    def test_file_or_checkpoint_error_preserves_approved_photos_and_allows_retry(self):
        self.assertEqual(self.send(self.client, self.form(self.client), [('one.jpg', self.photo())]).status_code, 201)
        form = self.form(self.client)
        review = self.client.get('/picker/editor').get_data(as_text=True)
        self.assertEqual(self.client.post('/picker/editor', data={
            'hold_csrf': form['hold_csrf'], 'selection': self.field(review, 'selection'), 'product_photo': '1',
        }).status_code, 303)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': '1'}).status_code, 302)
        before = self.client.get('/picker/approved/1').data
        paths_before = self.paths()
        payload = [('two.jpg', self.photo('red')), ('three.jpg', self.photo('green'))]

        original_write = m._mobile_write
        def disk_failure(path, raw):
            if len(self.paths()) > len(paths_before):
                raise OSError('fixture disk full')
            original_write(path, raw)

        for failing in (
            patch.object(m, '_mobile_write', side_effect=disk_failure),
            patch.object(m, '_save_active_lot_checkpoint', return_value=False),
        ):
            with failing:
                self.assertEqual(self.send(self.client, form, payload).status_code, 503)
            self.assertEqual(self.paths(), paths_before)
            approval = self.client.get('/picker/approved/1')
            self.assertEqual(approval.status_code, 200)
            self.assertEqual(approval.data, before)
            with self.client.session_transaction() as data:
                self.assertTrue(data['photo_editor_approved'])
                self.assertEqual(data['photo_order'], [1])
        self.assertEqual(self.send(self.client, form, payload).json, {'photos': 3})
        self.assertEqual(self.client.get('/picker/approved/1').status_code, 404)
