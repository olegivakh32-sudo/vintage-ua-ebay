from decimal import Decimal
from io import BytesIO
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
        self.assertIn('можлива лінійка', photo_roles)
        self.assertIn('можливі ваги', photo_roles)
        self.assertNotIn('value="3" checked', photo_roles)
        self.assertNotIn('value="7" checked', photo_roles)
        self.assertIn('value="11" checked', photo_roles)
        selected = [str(i) for i in range(1, 13) if i not in {3, 7}]
        self.assertEqual(self.client.post('/picker/editor', data={
            'hold_csrf': self.csrf, 'product_photo': selected,
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

    def test_stage_a_keeps_visible_marking_separate_from_brand(self):
        facts = m._auto_facts_from_analysis('AUTO_FACTS_JSON: {"object_type":"Pocket Watch", "markings":'
                                            '[{"photo":11,"literal":"СД[?]"}, {"photo":-1,"literal":"fake"}]}')
        self.assertEqual(facts['object_type'], 'Pocket Watch')
        self.assertEqual(facts['markings'], [{'photo': 11, 'literal': 'СД[?]'}])
        self.assertEqual(facts['brand'], '')

    def test_pilot_caps_attempts_web_and_cost_before_sending(self):
        limits = PilotLimits(Decimal('0.25'))
        text = {'input': 'texto', 'max_output_tokens': 1200}
        web = dict(text, tools=[{'type': 'web_search'}])
        with self.assertRaisesRegex(RuntimeError, 'ліміт'):
            limits.bound(dict(text, max_output_tokens=5201), 0)
        self.assertEqual(limits.bound(web, 0)['max_tool_calls'], 2)
        with self.assertRaisesRegex(RuntimeError, 'Вебпошук'):
            limits.bound(web, 0)
        with self.assertRaisesRegex(RuntimeError, 'Ліміт'):
            limits.bound(text, 0.25)
        for _ in range(4):
            self.assertEqual(limits.bound(text, 0.24), text)
        with self.assertRaisesRegex(RuntimeError, 'Ліміт'):
            limits.bound(text, 0.24)

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
