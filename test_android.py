from decimal import Decimal
from io import BytesIO
import re
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from ai_pilot import PilotLimits
from mobile_photos import measurement_hint
from test_mobile import m


class AndroidTests(unittest.TestCase):
    def setUp(self):
        self.client = m.app.test_client()
        fake_number = patch.object(m, '_sync_current_lot_once', return_value=4)
        fake_number.start()
        self.addCleanup(fake_number.stop)
        no_network = patch.object(m.requests.sessions.Session, 'request', side_effect=AssertionError('Network forbidden'))
        no_network.start()
        self.addCleanup(no_network.stop)
        self.client.get('/mobile')
        with self.client.session_transaction() as current:
            self.csrf = current['hold_csrf']
            current['current_lot_number'] = 4

    def _jpeg(self):
        result = BytesIO()
        self.fixture_images()[0].save(result, 'JPEG')
        result.seek(0)
        return result

    @staticmethod
    def fixture_images():
        product = Image.new('RGB', (400, 220), 'white')
        ImageDraw.Draw(product).ellipse((95, 50, 280, 150), fill='darkgreen', outline='black')
        ruler = product.copy()
        draw = ImageDraw.Draw(ruler)
        for i in range(22):
            x = 35 + 12 * i
            draw.line((x, 176, x, 176 + (13 if i % 5 == 0 else 6)), fill='black', width=2)
        return product, ruler

    def test_mobile_twelve_photo_lot_detects_measurement_hints_and_keeps_main(self):
        product, ruler = self.fixture_images()
        files = []
        for i in range(1, 13):
            result = BytesIO()
            (ruler if i == 3 else product).save(result, 'JPEG')
            result.seek(0)
            files.append((result, 'kitchen_scale.jpg' if i == 7 else f'IMG_{i}.jpg'))
        upload = self.client.post('/mobile/upload', headers={'Accept': 'application/json'},
                                  data={'hold_csrf': self.csrf, 'photos': files})
        self.assertEqual(upload.status_code, 201)
        self.assertEqual(upload.json, {'photos': 12})
        photo_roles = self.client.get('/picker/editor').get_data(as_text=True)
        self.assertIn('Можлива лінійка', photo_roles)
        self.assertIn('Можливі ваги', photo_roles)
        self.assertNotIn('value="3" checked', photo_roles)
        self.assertNotIn('value="7" checked', photo_roles)
        self.assertNotIn('value="11" checked', photo_roles)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': '1'}).status_code, 409)
        selection = re.search(r'name="selection" value="([^"]+)"', photo_roles)[1]
        selected = [str(i) for i in range(1, 13) if i not in {3, 7}]
        self.assertEqual(self.client.post('/picker/editor', data={
            'hold_csrf': self.csrf, 'selection': selection, 'product_photo': selected,
        }).status_code, 303)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': '11'}).status_code, 302)
        with self.client.session_transaction() as current:
            pid = current['picker_session_id']
            self.assertEqual(current['photo_order'], [11, *[int(i) for i in selected if i != '11']])
            self.assertTrue(current['photo_editor_approved'])
        self.assertEqual(m.PHOTO_ROLE_CACHE[pid][3], 'EVIDENCE_ONLY')
        self.assertEqual(m.PHOTO_ROLE_CACHE[pid][7], 'EVIDENCE_ONLY')
        self.assertEqual(len(m.PHOTO_CACHE[pid]), 12)
        self.assertEqual(self.client.get('/picker/approved/11').status_code, 200)
        self.assertEqual(self.client.get('/picker/thumb/3?size=200').status_code, 200)

    def test_ruler_rotation_and_regular_texture(self):
        product, ruler = self.fixture_images()
        self.assertIsNone(measurement_hint(product, 'IMG_001.jpg'))
        self.assertEqual(measurement_hint(ruler.rotate(90, expand=True), 'IMG_002.jpg'), 'ruler')
        stripes = Image.new('L', (400, 220), 255)
        draw = ImageDraw.Draw(stripes)
        for x in range(30, 380, 10):
            draw.line((x, 65, x, 73), fill=0, width=2)
        self.assertIsNone(measurement_hint(stripes, 'IMG_003.jpg'))

    def test_missing_ai_key_shows_navigation_without_losing_photos(self):
        self.client.post('/mobile/upload', data={
            'hold_csrf': self.csrf, 'photos': (self._jpeg(), 'IMG_01.jpg'),
        })
        with patch.object(m, 'OPENAI_API_KEY', None), patch.dict(m.os.environ, {'ALLOW_PAID_AI': '1'}):
            response = self.client.post('/picker/analyze')
        self.assertEqual(response.status_code, 503)
        self.assertIn('Аналіз ШІ зараз недоступний', response.get_data(as_text=True))
        self.assertIn(b'/picker/editor', response.data)
        self.assertNotIn(b'OPENAI_API_KEY', response.data)
        self.assertEqual(self.client.get('/picker/thumb/1').status_code, 200)

    def test_visual_evidence_blocks_pet_category_even_when_draft_agrees(self):
        draft = {
            'title': 'Dog Collar', 'item_specifics': {'Type': 'Dog Collar'},
            'visual_object_type': 'Military Collar Tabs', 'category_path': 'Pet Supplies > Dog Supplies > Dog Collars',
            'category_id': '123', 'category_path_category_id': '123', 'category_guard_version': 'P057',
        }
        valid, explanation = m._p057_category_snapshot_check(draft)
        self.assertFalse(valid)
        self.assertIn('військові відзнаки', explanation)

    def test_overlapping_decorative_shapes_do_not_form_a_digit_row(self):
        decorated = Image.new('L', (160, 90), 'white')
        draw = ImageDraw.Draw(decorated)
        for area in ((16, 20, 32, 45), (53, 20, 57, 45), (53, 20, 69, 23),
                     (75, 20, 79, 45), (63, 42, 79, 45)):
            draw.rectangle(area, fill=0)
        self.assertIsNone(measurement_hint(decorated, 'IMG_004.jpg'))

    def test_stage_a_keeps_visible_marking_separate_from_brand(self):
        facts = m._auto_facts_from_analysis('AUTO_FACTS_JSON: {"object_type":"Pocket Watch", "markings":'
                                            '[{"photo":11,"literal":"СД[?]"}, {"photo":-1,"literal":"fake"}]}')
        self.assertEqual(facts['object_type'], 'Pocket Watch')
        self.assertEqual(facts['markings'], [{'photo': 11, 'literal': 'СД[?]'}])
        self.assertEqual(facts['brand'], '')

    def test_pilot_reserves_full_context_output_cap_and_failed_attempts(self):
        limits = PilotLimits(Decimal('0.25'))
        text = {'input': 'texto', 'max_output_tokens': 5200}
        web = dict(text, tools=[{'type': 'web_search'}], service_tier='priority')
        with self.assertRaisesRegex(RuntimeError, 'Ліміт'):
            limits.bound(dict(text, max_output_tokens=5201))
        outbound = limits.bound(web)
        self.assertNotIn('tools', outbound)
        self.assertEqual(outbound['service_tier'], 'default')
        self.assertEqual(outbound['truncation'], 'disabled')
        with self.assertRaisesRegex(RuntimeError, 'один етап'):
            limits.bound(web)
        for _ in range(4):
            limits.bound(text)
        self.assertEqual(limits.reserved_total, Decimal('0.19968'))
        with self.assertRaisesRegex(RuntimeError, 'Ліміт'):
            limits.bound(text)
        capped = PilotLimits(Decimal('0.025'))
        capped.bound(text)
        with self.assertRaisesRegex(RuntimeError, 'ліміт витрат'):
            capped.bound(text)
        self.assertEqual(capped.attempts, 1)

    def test_pilot_saves_reservation_before_returning_network_payload(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from ai_pilot import write_report

        with TemporaryDirectory() as tmp:
            report = Path(tmp) / 'private.json'
            limits = PilotLimits(Decimal('0.25'))
            limits.on_reserve = lambda: write_report(report, {'reserved': str(limits.reserved_total)})
            limits.bound({'input': 'text', 'max_output_tokens': 100})
            self.assertEqual(Decimal(json.loads(report.read_text())['reserved']), Decimal('0.01926'))
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            with patch.object(limits, 'on_reserve', side_effect=OSError('storage unavailable')):
                with self.assertRaisesRegex(OSError, 'storage unavailable'):
                    limits.bound({'input': 'more text', 'max_output_tokens': 100})

    def test_isolated_pilot_runs_recognition_through_draft_and_recorded_taxonomy(self):
        import tempfile
        from pathlib import Path
        from ai_pilot import run

        snapshot = {
            'tree_id': '0',
            'suggestions': [{
                'category': {'categoryId': '123', 'categoryName': 'Pocket Watches'},
                'categoryTreeNodeLevel': 3,
                'categoryTreeNodeAncestors': [
                    {'categoryName': 'Watches', 'categoryTreeNodeLevel': 2},
                    {'categoryName': 'Jewelry & Watches', 'categoryTreeNodeLevel': 1},
                ],
            }],
            'aspects_by_category': {'123': []},
        }
        replies = [
            'PHOTO_ROLES_JSON: {"roles":{"1":"EBAY_PRODUCT"}}\n'
            'AUTO_FACTS_JSON: {"object_type":"Pocket Watch","materials":["Metal"],"materials_confidence":95,'
            '"markings":[{"photo":1,"literal":"СД[?]"}]}\n'
            'SCOPE_JSON: {"scope":"ONE_PHYSICAL_ITEM","physical_item_count":1,"confidence":95}',
            '=== IDENTIFICATION ===\nIDENTITY_JSON {"type":"Pocket Watch","material":"Metal"}\n'
            '=== MARKET ===\nPRICE_JSON {"normal":25}',
            '{"scale":{"visible":false},"dimension_readings":[]}',
            '{"title":"Pocket Watch with an Inspected Metal Case","price_usd":"25","condition_note":"Untested. Visible wear.",'
            '"item_specifics":{"Type":"Pocket Watch"},"description":"This untested pocket watch has an inspected metal case."}',
        ]
        data = {'lot': 'LOT 004 fixture', 'taxonomy': snapshot, 'seller_measurements':
                {'weight_kg': 0.1, 'length_cm': 5, 'width_cm': 5, 'height_cm': 2}}
        limits = PilotLimits(Decimal('0.25'))
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'photo.jpg'
            image.write_bytes(self._jpeg().getvalue())
            # The test loader uses a distinct module object from the standalone runner.
            with patch.dict('sys.modules', {'app': m}), patch.object(m, 'OPENAI_API_KEY', 'fixture'), patch.object(
                m, '_p054_openai_request', side_effect=replies,
            ):
                report = run(data, [image], limits)
        self.assertEqual(report['stage_http'], {'visual': 200, 'research': 303, 'draft': 303}, (report['stage_messages'], limits.attempts))
        self.assertEqual(report['identity'], 'Pocket Watch')
        self.assertEqual(report['ocr'], 'СД[?]')
        self.assertEqual(report['category_id'], '123')
        self.assertFalse(report['admitted'])
        self.assertIn('Pocket Watch', report['draft']['title'])
        self.assertEqual(limits.attempts, 4)

    def test_generic_filenames_measurement_frames_and_auto_main(self):
        product, ruler = self.fixture_images()
        scale = product.copy()
        draw = ImageDraw.Draw(scale)
        draw.rounded_rectangle((125, 160, 246, 211), radius=5, fill='#aabb99', outline='#333333', width=3)
        for offset in (145, 175, 205):
            draw.rectangle((offset, 175, offset + 14, 199), outline='black', width=3)
            draw.line((offset, 187, offset + 14, 187), fill='black', width=3)
        photos = [scale, *[ruler.rotate(angle, expand=True, fillcolor='grey') for angle in (15, 30, 45, 90)],
                  *[product] * 7]
        files = []
        for number, image in enumerate(photos, 1):
            result = BytesIO()
            image.save(result, 'JPEG')
            result.seek(0)
            files.append((result, f'IMG_{number}.jpg'))
        self.assertEqual(self.client.post('/mobile/upload', data={'hold_csrf': self.csrf, 'photos': files}).status_code, 303)
        with self.client.session_transaction() as session:
            pid = session['picker_session_id']
            session['main_photo_index'] = 1
            session['photo_order'] = list(range(1, 13))
            session['photo_editor_approved'] = True
        # Old Android 2 cached state looked like explicit selections. It is quarantined until re-reviewed.
        m.PHOTO_ROLE_CACHE[pid] = dict.fromkeys(range(1, 13), 'EBAY_PRODUCT')
        page = self.client.get('/picker/editor').get_data(as_text=True)
        key = re.search(r'name="selection" value="([^"]+)"', page)[1]
        self.assertEqual(page.count('Можлива лінійка'), 4)
        self.assertEqual(page.count('Цифровий дисплей'), 1)
        self.assertNotIn(' checked>', page)
        self.assertEqual(self.client.post('/picker/editor/approve', data={}).status_code, 409)
        self.assertEqual(self.client.post('/picker/editor', data={
            'hold_csrf': self.csrf, 'selection': 'from-old-lot', 'product_photo': ['6'],
        }).status_code, 409)
        selected = [str(i) for i in range(6, 13)]
        self.assertEqual(self.client.post('/picker/editor', data={
            'hold_csrf': self.csrf, 'selection': key, 'product_photo': selected,
        }).status_code, 303)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': '1'}).status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIn(session['main_photo_index'], range(6, 13))
            self.assertEqual(set(session['photo_order']), set(range(6, 13)))
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': '11'}).status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session['photo_order'], [11, 6, 7, 8, 9, 10, 12])

    def test_ai_uncertain_photo_cannot_stay_in_approved_eps_snapshot(self):
        product, _ = self.fixture_images()
        photos = []
        for i in range(2):
            photo = BytesIO()
            product.save(photo, 'JPEG')
            photo.seek(0)
            photos.append((photo, f'IMG_{i}.jpg'))
        self.assertEqual(self.client.post('/mobile/upload', data={
            'hold_csrf': self.csrf, 'photos': photos,
        }).status_code, 303)
        page = self.client.get('/picker/editor').get_data(as_text=True)
        key = re.search(r'name="selection" value="([^"]+)"', page)[1]
        self.client.post('/picker/editor', data={
            'hold_csrf': self.csrf, 'selection': key, 'product_photo': ['1', '2'],
        })
        self.client.post('/picker/editor/approve', data={'main_photo': '1'})
        response = 'PHOTO_ROLES_JSON: {"roles":{"1":"NEEDS_REVIEW","2":"EBAY_PRODUCT"}}'
        with patch.dict(m.os.environ, {'ALLOW_PAID_AI': '1'}), patch.object(
            m, 'OPENAI_API_KEY', 'fixture'
        ), patch.object(m, 'call_openai', return_value=response):
            self.assertEqual(self.client.post('/picker/analyze').status_code, 200)
        with self.client.session_transaction() as session:
            updated_session = dict(session)
        pid = updated_session['picker_session_id']
        self.assertEqual(updated_session['photo_order'], [2])
        self.assertEqual(updated_session['main_photo_index'], 2)
        self.assertFalse(updated_session['photo_editor_approved'])
        with m.app.test_request_context():
            m.session.update(updated_session)
            self.assertFalse(m._p056_approved_photo_snapshot_ok(pid, [1, 2]))
            with self.assertRaises(RuntimeError):
                m._current_upload_photos('no-ebay')
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': '2'}).status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session['photo_order'], [2])

    def test_incomplete_roles_fail_closed_even_if_every_jpeg_is_approved(self):
        picker = 'fixture-with-incomplete-roles'
        with m.app.test_request_context(), patch.dict(m.PHOTO_CACHE, {picker: [{}, {}]}), patch.dict(
            m.PHOTO_ROLE_CACHE, {picker: {2: 'EBAY_PRODUCT'}}
        ), patch.dict(m.APPROVED_PHOTO_CACHE, {
            picker: {1: {'bytes': b'first'}, 2: {'bytes': b'second'}}
        }):
            m.session.update(picker_session_id=picker, photo_editor_approved=True, main_photo_index=1)
            self.assertEqual(m._ebay_photo_indices(m.PHOTO_CACHE[picker], picker), [])
            self.assertFalse(m._p056_approved_photo_snapshot_ok(picker, [1, 2]))

    def test_automatic_photo_quality_keeps_originals_and_prioritizes_the_sharp_overview(self):
        from PIL import ImageFilter

        image = Image.new('RGB', (400, 300), 'white')
        ImageDraw.Draw(image).rectangle((110, 75, 295, 231), fill='blue')
        detail = image.crop((100, 60, 310, 245))
        images = [image.filter(ImageFilter.GaussianBlur(9)), image, image, detail]
        photos = []
        for index, item in enumerate(images, 1):
            output = BytesIO()
            item.save(output, 'JPEG')
            output.seek(0)
            photos.append((output, f'IMG_{index}.jpg'))
        result = self.client.post('/mobile/upload', data={
            'hold_csrf': self.csrf, 'photos': photos[:3], 'camera_photo': photos[3],
        })
        self.assertEqual(result.status_code, 303)
        role_page = self.client.get('/picker/editor').get_data(as_text=True)
        self.assertIn('Збігається з фото 2', role_page)
        self.assertIn('Перевірте різкість', role_page)

        report = ('PHOTO_ROLES_JSON: {"roles":{"1":"EBAY_PRODUCT","2":"EBAY_PRODUCT",'
                  '"3":"EBAY_PRODUCT","4":"EBAY_EVIDENCE"}}')
        with (patch.dict(m.os.environ, {'ALLOW_PAID_AI': '1'}),
              patch.object(m, 'OPENAI_API_KEY', 'fixture'),
              patch.object(m, 'call_openai', return_value=report)):
            self.assertEqual(self.client.post('/picker/analyze').status_code, 200)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': 0}).status_code, 302)
        with self.client.session_transaction() as session:
            picker = session['picker_session_id']
            self.assertEqual(session['main_photo_mode'], 'auto')
            self.assertEqual(session['photo_order'], [2, 1, 4])
        self.assertEqual(len(m.PHOTO_CACHE[picker]), 4)
        self.assertEqual(m.PHOTO_ROLE_CACHE[picker][3], 'NEEDS_REVIEW')
        self.assertEqual(self.client.get('/picker/thumb/3').status_code, 200)
        self.assertEqual(self.client.post('/picker/editor/approve', data={'main_photo': 4}).status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session['main_photo_mode'], 'manual')
            self.assertEqual(session['photo_order'][0], 4)
