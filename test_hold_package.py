from io import BytesIO
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

from test_mobile import m


class MobileHoldPackageTests(unittest.TestCase):
    def test_phone_and_workspace_accept_mpo_jpeg_and_keep_the_original(self):
        from werkzeug.datastructures import FileStorage
        from workspace_media import prepare_photo

        output = BytesIO()
        first = Image.new('RGB', (80, 40), 'red')
        additional = Image.new('RGB', (40, 40), 'blue')
        first.save(output, 'MPO', save_all=True, append_images=[additional])
        raw = output.getvalue()
        _, preview, _, _ = m._mobile_prepare_photo(raw, 'IMG.jpg')
        _, mime, original, shop_preview, *_ = prepare_photo(FileStorage(stream=BytesIO(raw), filename='IMG.jpg'))
        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual(original, raw)
        for encoded in (preview, shop_preview):
            with Image.open(BytesIO(encoded)) as image:
                self.assertEqual(image.format, 'JPEG')
                self.assertEqual(image.size, (80, 40))
                self.assertGreater(image.getpixel((5, 5))[0], 245)

    def test_owner_restores_analysis_and_exact_photos_into_fresh_storage(self):
        origin = 'https://hold-package.test'
        owner = ('vintage', 'local-test-password')
        originals = []
        for color in ('blue', 'orange'):
            photo = BytesIO()
            Image.new('RGB', (180, 130), color).save(photo, 'JPEG')
            originals.append(photo.getvalue())

        def field(page, name):
            return re.search(fr'name="{name}" value="([^"]+)"', page.get_data(as_text=True))[1]

        def login(client):
            mobile = client.get('/mobile', base_url=origin, auth=owner)
            self.assertEqual(mobile.status_code, 200)
            return field(mobile, 'hold_csrf'), field(mobile, 'upload_batch')

        with (
            TemporaryDirectory() as tmp,
            patch.dict(os.environ, {'APP_SECRET': 'local-test-only-shared-key', 'VINTAGE_OPERATOR_PASSWORD': owner[1]}),
            patch.dict(m.app.config, {'SECRET_KEY': 'local-test-only-shared-key'}),
            patch.object(m, '_sync_current_lot_once', return_value=4),
            patch.object(m.requests.sessions.Session, 'request', side_effect=AssertionError('No network')),
        ):
            with (patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', str(Path(tmp) / 'before')),
                  patch.dict(os.environ, {'HOLD_QUEUE_DIR': str(Path(tmp) / 'before')})):
                client = m.app.test_client()
                csrf, batch = login(client)
                with client.session_transaction(base_url=origin) as state:
                    state['current_lot_number'] = 4
                response = client.post('/mobile/upload', base_url=origin, auth=owner, data={
                    'hold_csrf': csrf, 'upload_batch': batch,
                    'photos': [(BytesIO(raw), f'IMG_{number}.jpg') for number, raw in enumerate(originals, 1)],
                })
                self.assertEqual(response.status_code, 303)
                editor = client.get('/picker/editor', base_url=origin, auth=owner)
                response = client.post('/picker/editor', base_url=origin, auth=owner, data={
                    'hold_csrf': csrf, 'selection': field(editor, 'selection'), 'product_photo': [1, 2],
                })
                self.assertEqual(response.status_code, 303)
                response = client.post('/picker/editor/approve', base_url=origin, auth=owner, data={'main_photo': 2})
                self.assertEqual(response.status_code, 302)
                approved = client.get('/picker/approved/2', base_url=origin, auth=owner).data
                with client.session_transaction(base_url=origin) as state:
                    pid = state['picker_session_id']
                m.ANALYSIS_CACHE[pid] = 'Existing analysis from this lot'
                m.LOT002_DRAFT_CACHE[pid] = {'title': 'Existing draft', 'seller_reviewed': True}
                result = client.post('/ebay/hold-queue/add', base_url=origin, auth=owner, data={'hold_csrf': csrf})
                self.assertEqual(result.status_code, 303, re.findall(r'<h2>.*?</h2>|<p>[^<]*</p>',
                                                                    result.get_data(as_text=True))[:10])
                queue = client.get('/ebay/hold-queue', base_url=origin, auth=owner)
                rid = field(queue, 'record_id')
                metadata = client.get('/hold/export', base_url=origin, auth=owner).json
                self.assertEqual(client.get(f'/hold/package/{rid}', base_url=origin).status_code, 401)
                with client.get(f'/hold/package/{rid}', base_url=origin, auth=owner) as result:
                    self.assertEqual(result.status_code, 200, result.get_data(as_text=True) if result.status_code != 200 else '')
                    self.assertIn('no-store', result.headers['Cache-Control'])
                    backup = result.data

            for cache in (m.PHOTO_CACHE, m.APPROVED_PHOTO_CACHE, m.PHOTO_ROLE_CACHE, m.ANALYSIS_CACHE,
                          m.LOT002_DRAFT_CACHE):
                cache.pop(pid, None)
            corrupt = BytesIO()
            with ZipFile(BytesIO(backup)) as source, ZipFile(corrupt, 'w') as destination:
                for name in source.namelist():
                    raw = source.read(name)
                    destination.writestr(name, b'altered' if name.endswith('.source') else raw)

            with (patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', str(Path(tmp) / 'after')),
                  patch.dict(os.environ, {'HOLD_QUEUE_DIR': str(Path(tmp) / 'after')})):
                restored = m.app.test_client()
                csrf, _ = login(restored)

                def import_archive(data):
                    return restored.post('/hold/package', base_url=origin, auth=owner, data={
                        'package': (BytesIO(data), 'test.zip'), 'hold_csrf': csrf,
                    })

                self.assertEqual(import_archive(corrupt.getvalue()).status_code, 400)
                state = restored.get('/hold/state', base_url=origin, auth=owner).json
                self.assertTrue(state['fresh'])
                self.assertEqual(restored.post('/hold/restore', base_url=origin, auth=owner, json={
                    'signed_state': metadata['signed_state'], 'expected_revision': state['revision'],
                }, headers={'X-Hold-CSRF': csrf}).status_code, 200)
                m._mobile_photo_root(pid)  # A prior browser probe may have made an empty directory.
                self.assertEqual(import_archive(backup).status_code, 303)
                self.assertEqual(import_archive(backup).status_code, 409)
                self.assertEqual(restored.post('/ebay/hold-queue/open', base_url=origin, auth=owner,
                                               data={'record_id': rid, 'hold_csrf': csrf}).status_code, 303)
                self.assertEqual(restored.get('/picker/approved/2', base_url=origin, auth=owner).data, approved)
                self.assertEqual([m._mobile_original_bytes(photo) for photo in m.PHOTO_CACHE[pid]], originals)
                self.assertEqual(m.ANALYSIS_CACHE[pid], 'Existing analysis from this lot')
                self.assertFalse(m.LOT002_DRAFT_CACHE[pid]['seller_reviewed'])
                for cache in (m.PHOTO_CACHE, m.APPROVED_PHOTO_CACHE, m.PHOTO_ROLE_CACHE, m.ANALYSIS_CACHE,
                              m.LOT002_DRAFT_CACHE):
                    cache.pop(pid, None)
