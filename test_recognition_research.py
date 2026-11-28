from io import BytesIO
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from recognition_research import hypothesis_seeds, numbered_evidence
from test_mobile import m
from test_s1_acceptance import RESEARCH, VISUAL


class RecognitionResearchTests(unittest.TestCase):
    def test_text_clues_survive_missing_images_and_exclude_serial_numbers(self):
        scenes = {4: 'MARKINGS', 7: 'SCALE', 8: 'PACKAGE'}
        marks = [
            {'photo': 4, 'literal': 'KD-2[?]7', 'kind': 'MODEL_NUMBER'},
            {'photo': 4, 'literal': 'AC010234', 'kind': 'SERIAL'},
            {'photo': 8, 'literal': 'Box only', 'kind': 'INSCRIPTION'},
        ]
        observations = {'feature': [{'photos': [4, 7], 'value': 'Three offset attachment holes',
                                     'status': 'PHOTO_OBSERVATION'}]}
        evidence = numbered_evidence([(4, 'AC010234\nS/N: FJ920\nKinetic\nKinetic'), (7, '500 g')],
                                     marks, observations, scenes, 8)
        self.assertEqual(evidence['local_ocr'], [{'photo': 4, 'text': 'Kinetic'}])
        self.assertEqual(hypothesis_seeds(evidence), [
            {'kind': 'readable_mark', 'clue': 'KD-2', 'photos': [4]},
            {'kind': 'observed_construction', 'clue': 'Three offset attachment holes', 'photos': [4]},
        ])

    def test_multi_item_research_is_saved_before_quantity_review(self):
        import json

        report = VISUAL | {'scope': dict(scope='MULTIPLE_PHYSICAL_ITEMS', physical_item_count=2,
                                         confidence=96, note='', components=[
                                             dict(role='PRODUCT', label=label, photos=[1], quantity=1)
                                             for label in ('Guide', 'Bracket')])}
        photo = BytesIO()
        Image.new('RGB', (130, 120), 'blue').save(photo, 'JPEG')
        with (TemporaryDirectory() as root, patch.dict('os.environ', {
            'ACTIVE_LOT_CHECKPOINT_DIR': root, 'HOLD_QUEUE_DIR': root,
            'ALLOW_PAID_AI': '1', 'ENABLE_LIVE_PUBLISH': '0', 'VINTAGE_RESEARCH_ALTERNATE': '0',
        }), patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', root),
              patch.object(m, 'OPENAI_API_KEY', 'mock-transport'),
              patch.object(m, '_discover_next_lot_from_ebay', return_value=4),
              patch.object(m, 'estimate_item_size_weight_from_photos', return_value={}) as measure,
              patch.object(m, 'call_openai', side_effect=[json.dumps(report), RESEARCH]) as provider):
            client = m.app.test_client()
            client.get('/mobile')
            with client.session_transaction() as state:
                csrf = state['hold_csrf']
            self.assertEqual(client.post('/mobile/upload', data={'hold_csrf': csrf,
                             'photos': (BytesIO(photo.getvalue()), 'fixture.jpg')}).status_code, 303)
            stage = '/mobile/prepare-stage'
            token = {'hold_csrf': csrf}
            self.assertEqual(client.post(stage, data=token | {'stage': 'photos'}).status_code, 200)
            result = client.post(stage, data=token | {'stage': 'research'})
            self.assertEqual(result.json, {'open': '/picker/review-item-scope'})
            request = provider.call_args.args[0]
            self.assertEqual(request['tool_choice'], 'required')
            self.assertIn('Identify the photographed physical subjects separately',
                          request['input'][0]['content'][0]['text'])
            self.assertEqual(client.get(result.json['open']).status_code, 409)
            self.assertEqual(client.post('/picker/final-listing', data={'automatic': 'yes'}).status_code, 409)
            self.assertEqual(client.post(stage, data=token | {'stage': 'research'}).status_code, 200)
            self.assertEqual(provider.call_count, 2)
            measure.assert_not_called()
            with client.session_transaction() as state:
                picker = state['picker_session_id']
            self.assertIn('IDENTITY_JSON', m.IDENTIFICATION_CACHE[picker])
            self.assertIsNone(m._market_bin_price(m.MARKET_CACHE[picker]) or None)
            self.assertEqual(client.post('/picker/confirm-item-scope',
                                          data={'physical_item_count': 2}).status_code, 403)
            result = client.post('/picker/confirm-item-scope',
                                 data=token | {'physical_item_count': 2, 'sale_mode': 'ONE_LOT'})
            self.assertEqual(result.status_code, 302)
            self.assertEqual(m.ITEM_STRUCTURE_CACHE[picker]['physical_item_count'], 2)
            self.assertEqual(client.post(stage, data=token | {'stage': 'research'}).json, {'next': 'draft'})
            self.assertEqual(provider.call_count, 2)
