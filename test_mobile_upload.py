from concurrent.futures import ThreadPoolExecutor
import base64
import json
from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

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

    def test_original_remains_exact_after_restart_and_used_for_measurement_detail(self):
        source = BytesIO()
        Image.new('RGB', (3600, 1000), 'navy').save(source, 'PNG')
        original = source.getvalue()
        self.assertEqual(self.send(self.client, self.form(self.client), [('scale.png', original)]).status_code, 201)
        with self.client.session_transaction() as data:
            picker = data['picker_session_id']
        m.PHOTO_CACHE.pop(picker)
        self.assertEqual(self.client.get('/picker/thumb/1').status_code, 200)
        item = m.PHOTO_CACHE[picker][0]
        self.assertEqual(m._mobile_original_bytes(item), original)
        detail = m.google_photo_to_data_url(item, size=3200)
        self.assertEqual(Image.open(BytesIO(base64.b64decode(detail.split(',')[1]))).width, 3200)

    def test_https_owner_export_contains_only_current_lot_original(self):
        from ai_pilot import inputs

        original = self.photo()
        self.assertEqual(self.send(self.client, self.form(self.client), [('phone.jpg', original)]).status_code, 201)
        self.assertEqual(self.client.get('/mobile/acceptance-package').status_code, 403)
        headers = {'Authorization': 'Basic ' + base64.b64encode(b'vintage:fixture').decode()}
        with patch.dict(m.os.environ, {'VINTAGE_OPERATOR_PASSWORD': 'fixture'}):
            self.assertEqual(self.client.get('/mobile/acceptance-package', base_url='https://localhost').status_code, 401)
            response = self.client.get('/mobile/acceptance-package', base_url='https://localhost', headers=headers)
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.cache_control.no_store)
            self.assertEqual(m.app.test_client().get('/mobile/acceptance-package',
                                                     base_url='https://localhost', headers=headers).status_code, 409)
            with ZipFile(BytesIO(response.data)) as archive:
                self.assertEqual(sorted(archive.namelist()), ['lot.json', 'photos/01.jpg'])
                self.assertEqual(archive.read('photos/01.jpg'), original)
                self.assertEqual(json.loads(archive.read('lot.json'))['photos'], ['photos/01.jpg'])
                archive.extractall(self.storage)
            _, paths = inputs(self.storage / 'lot.json')
            self.assertEqual(paths[0].read_bytes(), original)
            paths[0].write_bytes(self.photo('red'))
            with self.assertRaisesRegex(ValueError, 'контрольними сумами'):
                inputs(self.storage / 'lot.json')

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

    def test_mobile_summary_appends_locally_and_keeps_new_approval(self):
        self.assertEqual(self.send(self.client, self.form(self.client), [('one.jpg', self.photo())]).status_code, 201)
        first = self.client.get('/picker/items').get_data(as_text=True)
        self.assertIn('<a href="/mobile">Додати фото до лота', first)
        self.assertIn('Фото лота (1 з 24)', first)
        form = self.form(self.client)
        self.assertEqual(self.send(self.client, form, [('two.jpg', self.photo('red'))]).status_code, 201)
        editor = self.client.get('/picker/editor').get_data(as_text=True)
        self.assertEqual(self.client.post('/picker/editor', data={
            'hold_csrf': form['hold_csrf'], 'selection': self.field(editor, 'selection'),
            'product_photo': ['1', '2'],
        }).status_code, 303)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': 2}).status_code, 302)
        self.assertIn('Фото затверджено.', self.client.get('/picker/items').get_data(as_text=True))
        self.assertEqual(self.client.get('/picker/approved/2').status_code, 200)

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
        originals_before = set(self.storage.rglob('*.source'))
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
            self.assertEqual(set(self.storage.rglob('*.source')), originals_before)
            approval = self.client.get('/picker/approved/1')
            self.assertEqual(approval.status_code, 200)
            self.assertEqual(approval.data, before)
            with self.client.session_transaction() as data:
                self.assertTrue(data['photo_editor_approved'])
                self.assertEqual(data['photo_order'], [1])
        self.assertEqual(self.send(self.client, form, payload).json, {'photos': 3})
        self.assertEqual(self.client.get('/picker/approved/1').status_code, 404)
