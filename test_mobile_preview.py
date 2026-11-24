import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from mobile_preview import preview_runtime


class PreviewStorageTests(unittest.TestCase):
    def test_persistent_runtime_retains_session_key_and_keeps_integrations_disabled(self):
        with tempfile.TemporaryDirectory() as container, patch.dict('os.environ', {
            'OPENAI_API_KEY': 'fixture', 'EBAY_REFRESH_TOKEN': 'fixture',
            'GOOGLE_CLIENT_SECRET': 'fixture', 'ALLOW_PAID_AI': '1', 'ENABLE_LIVE_PUBLISH': '1',
        }):
            root = Path(container) / 'preview'
            with preview_runtime(root) as (initial, _):
                checkpoint = Path(initial['ACTIVE_LOT_CHECKPOINT_DIR'])
                checkpoint.mkdir()
                draft = checkpoint / 'test.json'
                draft.write_text('{"sku":"LOT-004"}')
                self.assertEqual(stat.S_IMODE((root / '.session-secret').stat().st_mode), 0o600)
            with preview_runtime(root) as (restarted, _):
                self.assertEqual(restarted, initial)
                self.assertEqual(draft.read_text(), '{"sku":"LOT-004"}')
                self.assertEqual(restarted['ALLOW_PAID_AI'], '0')
                self.assertEqual(restarted['ENABLE_LIVE_PUBLISH'], '0')
                self.assertTrue({'OPENAI_API_KEY', 'EBAY_REFRESH_TOKEN', 'GOOGLE_CLIENT_SECRET'}.isdisjoint(restarted))

    def test_double_start_is_rejected_while_storage_is_owned(self):
        with tempfile.TemporaryDirectory() as container:
            root = Path(container)
            with preview_runtime(root):
                with self.assertRaisesRegex(ValueError, 'вже використовує'):
                    with preview_runtime(root):
                        self.fail('The same data directory must not have two launchers.')
            with preview_runtime(root):
                pass

    def test_corrupt_session_key_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as container:
            root = Path(container)
            key = root / '.session-secret'
            key.write_text('incomplete')
            with self.assertRaisesRegex(ValueError, 'пошкоджено'):
                with preview_runtime(root):
                    self.fail('A corrupt previous key must stop startup.')
            self.assertEqual(key.read_text(), 'incomplete')

    def test_default_preview_is_disposable(self):
        with preview_runtime(None) as (env, _):
            root = Path(env['XDG_RUNTIME_DIR'])
            self.assertTrue(root.exists())
        self.assertFalse(root.exists())


@unittest.skipUnless(os.environ.get('VINTAGE_RUN_LOAD_TESTS') == '1',
                     'Enable for the local 100-lot upload and HOLD exercise')
class PreviewLoadTests(unittest.TestCase):
    def test_100_lots_upload_and_hold_without_duplicate_records(self):
        from io import BytesIO
        import json
        from time import monotonic

        from PIL import Image
        from test_mobile import m

        started = monotonic()
        image = BytesIO()
        Image.new('RGB', (320, 240), 'blue').save(image, 'JPEG')
        jpeg = image.getvalue()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', str(Path(tmp) / 'active')),
            patch.dict(os.environ, {'HOLD_QUEUE_DIR': str(Path(tmp) / 'hold')}),
            patch.object(m.requests.sessions.Session, 'request', side_effect=AssertionError('No external calls')),
        ):
            for lot in range(100, 200):
                client = m.app.test_client()
                with client.session_transaction() as state:
                    state['current_lot_number'] = lot
                    state['hold_csrf'] = f'fixture-{lot}'
                upload = client.post('/mobile/upload', data={
                    'hold_csrf': f'fixture-{lot}',
                    'photos': [(BytesIO(jpeg), f'IMG_{index}.jpg') for index in range(12)],
                })
                self.assertEqual(upload.status_code, 303, f'LOT {lot} upload')
                for _ in range(2):
                    held = client.post('/ebay/hold-queue/add', data={'hold_csrf': f'fixture-{lot}'})
                    self.assertEqual(held.status_code, 303, f'LOT {lot} HOLD')
            with m._p058_db() as db:
                records = db.execute("SELECT sku, data FROM hold_lots WHERE status='active' ORDER BY sku").fetchall()
            self.assertEqual([row['sku'] for row in records], [f'LOT-{lot}' for lot in range(100, 200)])
            for row in records:
                self.assertEqual(json.loads(row['data'])['photo_count'], 12, row['sku'])
        print(f'Offline upload/HOLD exercise: 100 lots, 1200 synthetic photos, {monotonic() - started:.2f}s')


@unittest.skipUnless(os.environ.get('VINTAGE_RUN_PREVIEW_HTTP_TESTS') == '1',
                     'Enable for real loopback HTTP and subprocess restart verification')
class PreviewRestartTests(unittest.TestCase):
    def test_restart_preserves_session_approved_jpeg_and_hold(self):
        from contextlib import contextmanager
        from io import BytesIO
        import re
        import socket
        import subprocess
        import sys
        from time import monotonic, sleep

        from PIL import Image
        import requests

        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        project = Path(__file__).resolve().parent

        @contextmanager
        def launched(data_dir):
            log = data_dir.parent / 'gunicorn.log'
            with log.open('ab') as capture:
                process = subprocess.Popen(
                    [sys.executable, 'mobile_preview.py', '--port', str(port), '--data-dir', str(data_dir)],
                    cwd=project, stdout=capture, stderr=capture, env={
                        **os.environ, 'ALLOW_PAID_AI': '1', 'ENABLE_LIVE_PUBLISH': '1',
                        'OPENAI_API_KEY': 'integration-fixture', 'EBAY_REFRESH_TOKEN': 'integration-fixture',
                        'VINTAGE_OPERATOR_PASSWORD': 'integration-password',
                    },
                )
                try:
                    deadline = monotonic() + 10
                    while True:
                        try:
                            health = client.get(base + '/health', timeout=0.5)
                            self.assertEqual(health.status_code, 200)
                            self.assertFalse(health.json()['openai_configured'])
                            break
                        except requests.ConnectionError:
                            if process.poll() is not None or monotonic() >= deadline:
                                self.fail(log.read_text())
                            sleep(0.05)
                    yield
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            self.assertEqual(process.wait(timeout=10), 0, log.read_text())
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                            self.fail(log.read_text())
                with socket.socket() as probe:
                    probe.settimeout(0.5)
                    self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)

        def field(markup, name):
            return re.search(fr'name="{name}" value="([^"]+)"', markup)[1]

        def request(method, path, expected, **kwargs):
            response = client.request(method, base + path, timeout=10, allow_redirects=False, **kwargs)
            self.assertEqual(response.status_code, expected, path)
            return response

        with tempfile.TemporaryDirectory() as tmp, requests.Session() as client:
            client.trust_env = False
            root = Path(tmp) / 'data'
            with launched(root):
                request('GET', '/hold/state', 401)
                client.auth = ('vintage', 'integration-password')
                markup = request('GET', '/mobile', 200).text
                csrf = field(markup, 'hold_csrf')
                initial_cookies = client.cookies.copy()
                upload_form = {'hold_csrf': csrf, 'upload_batch': field(markup, 'upload_batch')}
                image = BytesIO()
                Image.new('RGB', (200, 180), 'blue').save(image, 'JPEG')
                photos = [
                    ('photos', (f'IMG_{index}.jpg', image.getvalue(), 'image/jpeg'))
                    for index in range(1, 13)
                ]
                request('POST', '/mobile/upload', 303, files=photos, data=upload_form)
                selection = field(request('GET', '/picker/editor', 200).text, 'selection')
                request('POST', '/picker/editor', 303, data={
                    'hold_csrf': csrf, 'selection': selection, 'product_photo': list(range(6, 13)),
                })
                request('POST', '/picker/editor/approve', 302, data={'main_photo': 11})
                approved = request('GET', '/picker/approved/11', 200).content
                original = request('GET', '/picker/thumb/1', 200).content
                request('POST', '/ebay/hold-queue/add', 303, data={'hold_csrf': csrf})
                record = field(request('GET', '/ebay/hold-queue', 200).text, 'record_id')
                request('POST', '/picker/analyze', 503)
            with launched(root):
                with requests.Session() as retry:
                    retry.trust_env = False
                    retry.auth = client.auth
                    retry.cookies.update(initial_cookies)
                    result = retry.post(base + '/mobile/upload', files=photos, data=upload_form,
                                        headers={'Accept': 'application/json'}, timeout=10)
                    self.assertEqual(result.status_code, 201)
                    self.assertEqual(result.json(), {'photos': 12})
                self.assertEqual(request('GET', '/picker/approved/11', 200).content, approved)
                request('POST', '/ebay/hold-queue/open', 303, data={'hold_csrf': csrf, 'record_id': record})
                self.assertEqual(request('GET', '/picker/approved/11', 200).content, approved)
                self.assertEqual(request('GET', '/picker/thumb/1', 200).content, original)
                editor = request('GET', '/picker/editor', 200).text
                self.assertRegex(editor, r"name='main_photo' value='11' [^>]*checked")
                self.assertIn('7 для eBay · 5 поза оголошенням', editor)
                request('POST', '/ebay/current-publish', 409, data={'hold_csrf': csrf})
