from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from recognition_research import check_documentary_details, details_request, hypothesis_seeds, numbered_evidence
from test_blind_live_pilot import response
from test_mobile import m
from test_product_evidence import observed
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

    def test_paired_mobile_flow_preserves_locked_model_and_documented_details_in_sqlite(self):
        model = 'FX-40'
        kind = 'Camera mounting adapter'
        report = VISUAL | {
            'scope': dict(scope='ONE_PHYSICAL_ITEM', physical_item_count=1, confidence=98, note='',
                          components=[dict(role='PRODUCT', label=kind, photos=[1], quantity=1)]),
            'facts': {'object_type': kind, 'markings': [{'photo': 1, 'literal': model, 'kind': 'MODEL_NUMBER'}]},
            'observations': [observed(field, value) for field, value in
                             [('type', kind), ('brand', 'Northstar'), ('model', model)]],
        }
        documents = ['https://manufacturer.example/manual', 'https://archive.example/catalogue']

        def claims(field, value, excerpt, **extra):
            return dict(field=field, value=value, photos=[1], sources=[
                dict(url=url, kind='MANUAL', document_id=str(i), match='EXACT', excerpt=excerpt)
                for i, url in enumerate(documents)], **extra)

        rows = [claims('material', 'Aluminium', 'FX-40 frame material: Aluminium.'),
                claims('period', '2008', 'FX-40 initial release: 2008.'),
                claims('weight_kg', 0.24, 'FX-40 net weight: 240 g.', unit='kg', specimen_basis='NET'),
                claims('length_cm', 8.0, 'FX-40 length: 80 mm.', unit='cm', specimen_basis='ITEM'),
                claims('width_cm', 5.0, 'FX-400 width: 50 mm.', unit='cm', specimen_basis='ITEM'),
                claims('height_cm', 1.2, 'FX-40 width: 12 mm.', unit='cm', specimen_basis='ITEM'),
                claims('model', 'GB-88', 'GB-88 camera.', specimen_basis='ITEM')]

        def footer(urls):
            return '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': urls, 'completed': True})

        detail = 'IDENTITY_CLAIMS_JSON ' + json.dumps({'claims': rows}) + footer(documents)
        research = RESEARCH.replace('Fixture display stand', kind) + footer([])
        source = BytesIO()
        Image.new('RGB', (150, 120), 'red').save(source, 'JPEG')

        def taxonomy(method, url, **kwargs):
            self.assertEqual(method, 'GET')
            if url.endswith('/get_default_category_tree_id'):
                return response({'categoryTreeId': '0', 'categoryTreeVersion': '112'})
            if url.endswith('/get_category_suggestions'):
                return response({'categorySuggestions': [
                    {'category': {'categoryId': '30097', 'categoryName': kind}, 'categoryTreeNodeLevel': 2,
                     'categoryTreeNodeAncestors': [{'categoryName': 'Cameras & Photo', 'categoryTreeNodeLevel': 1}]}]})
            self.assertTrue(url.endswith('/get_item_aspects_for_category'))
            return response({'aspects': [{'localizedAspectName': 'Type', 'aspectConstraint': {'aspectRequired': True}}]})

        with (TemporaryDirectory() as tmp, patch.dict('os.environ', {
            'ACTIVE_LOT_CHECKPOINT_DIR': tmp, 'HOLD_QUEUE_DIR': tmp, 'ALLOW_PAID_AI': '1',
            'ENABLE_LIVE_PUBLISH': '0', 'VINTAGE_RESEARCH_ROUTE': 'paired-20261128', 'VINTAGE_RESEARCH_DETAILS': '1',
        }), patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', tmp),
              patch.object(m, 'OPENAI_API_KEY', 'mock-only'),
              patch.object(m, '_discover_next_lot_from_ebay', return_value=4),
              patch.object(m, 'get_ebay_access_token', return_value='mock-only'),
              patch.multiple(m, EBAY_CLIENT_ID='fixture', EBAY_CLIENT_SECRET='fixture', EBAY_REFRESH_TOKEN='fixture'),
              patch.object(m, '_lot002_request', side_effect=taxonomy),
              patch.object(m, 'estimate_item_size_weight_from_photos') as measure,
              patch.object(m, 'call_openai', side_effect=[json.dumps(report), research, detail]) as api):
            client = m.app.test_client()
            client.get('/mobile')
            with client.session_transaction() as state:
                csrf = state['hold_csrf']
            self.assertEqual(client.post('/mobile/upload', data={'hold_csrf': csrf,
                             'photos': (BytesIO(source.getvalue()), 'fixture.jpg')}).status_code, 303)
            for stage in ('photos', 'research', 'research', 'draft'):
                result = client.post('/mobile/prepare-stage', data={'hold_csrf': csrf, 'stage': stage})
                self.assertEqual(result.status_code, 200, (stage, result.get_data(as_text=True)))
            self.assertEqual([(c.args[0]['model'], c.args[0]['reasoning']['effort']) for c in api.call_args_list],
                             [('gpt-6.1-terra', 'low'), ('gpt-6.1-terra', 'low'), ('gpt-6-luna', 'medium')])
            self.assertIn('mounting hardware', api.call_args_list[1].args[0]['input'][0]['content'][0]['text'])
            measure.assert_not_called()
            with client.session_transaction() as state:
                picker = state['picker_session_id']
            with m._p058_db() as db:
                saved = db.execute("SELECT id, data FROM hold_lots WHERE status='active' AND picker_id=?", (picker,)).fetchone()
            self.assertIsNotNone(saved)
            draft = json.loads(saved['data'])['snapshot']['draft']
            exported = client.get('/picker/hold-draft.json')
            self.assertEqual(exported.status_code, 200)
            card = exported.json['product_export']
            self.assertEqual(card, draft['product_export'])
            self.assertEqual(card['type'], kind)
            self.assertEqual((card['fields']['brand'][0]['value'], card['fields']['model'][0]['value']),
                             ('Northstar', model))
            self.assertEqual([row['value'] for row in card['measurements']['weight_kg']['reference']], [0.24])
            self.assertEqual([row['value'] for row in card['measurements']['length_cm']['reference']], [8.0])
            self.assertFalse(card['measurements']['width_cm']['reference'])
            self.assertFalse(card['measurements']['height_cm']['reference'])
            self.assertIsNone(card['measurements']['weight_kg']['actual'])
            self.assertIn(model, draft['title'])
            self.assertIn('Aluminium', draft['description'])
            self.assertIn('2008', draft['description'])
            self.assertTrue(draft['hold_preview'])
            self.assertEqual(draft['category_review']['status'], 'TAXONOMY_CHECKED')
            self.assertFalse(draft.get('price'))
            self.assertEqual(draft['shipping_status'], 'NEEDS_VERIFIED_MEASUREMENTS')
            self.assertNotIn('shipping_review', draft)
            self.assertEqual(draft['compliance_review']['status'], 'REVIEW')
            self.assertEqual(client.post('/picker/confirm-final-draft', data={'hold_csrf': csrf}).status_code, 409)

            values = dict(weight_kg=0.25, length_cm=9, width_cm=5, height_cm=2)
            m.SELLER_MEASUREMENT_CACHE[picker] = values | {
                'measurement_field_sources': dict.fromkeys(values, 'SELLER_VERIFIED'),
                'weight_source': 'SELLER_VERIFIED', 'dimensions_source': 'SELLER_VERIFIED',
            }
            with client.session_transaction() as state, m.app.test_request_context():
                m.session.update(state)
                revision = m._measurement_revision(picker)
            result = client.post('/picker/final-listing', data={'hold_csrf': csrf, 'automatic': 'yes',
                                 'measurement_revision': revision})
            self.assertEqual(result.status_code, 303)
            updated = client.get('/picker/hold-draft.json').json
            self.assertEqual(updated['product_export']['measurements']['weight_kg']['actual'], 0.25)
            self.assertEqual([row['value'] for row in updated['product_export']['measurements']['weight_kg']['reference']],
                             [0.24])
            self.assertEqual(updated['shipping_review']['packed_weight_kg'], 0.37)
            self.assertGreater(updated['shipping_review']['estimated_usd'], 0)
            self.assertEqual(updated['shipping_status'], 'NEEDS_LISTING_REVIEW')
            m.LOT002_DRAFT_CACHE.pop(picker)
            self.assertEqual(client.post('/ebay/hold-queue/open', data={'hold_csrf': csrf,
                                        'record_id': saved['id']}).status_code, 303)
            self.assertEqual(client.get('/picker/hold-draft.json').json, updated)
            self.assertEqual(api.call_count, 3)

            sales = Path(tmp) / 'verified-sales.json'
            sales.write_text(json.dumps([
                {'identity': f'Northstar {model}', 'condition': 'Used Good', 'completeness': 'Complete',
                 'status': 'SOLD', 'verification': 'MARKETPLACE_COMPLETED_SALE', 'currency': 'USD',
                 'sold_date': m.kyiv_today().isoformat(), 'item_price_usd': price,
                 'url': f'https://www.ebay.com/itm/{123450000000 + index}'}
                for index, price in enumerate((24, 20, 35))]))
            with patch.dict('os.environ', {'VINTAGE_VERIFIED_SALES_FILE': str(sales)}):
                with client.session_transaction() as state, m.app.test_request_context():
                    m.session.update(state)
                    saved = m._save_hold_listing(picker, m.ANALYSIS_CACHE[picker], m.IDENTIFICATION_CACHE[picker],
                                                 m.MARKET_CACHE[picker], m.SELLER_MEASUREMENT_CACHE[picker],
                                                 shipping_status='NEEDS_LISTING_REVIEW',
                                                 seller={'condition': 'Used Good', 'completeness': 'Complete'})
                    self.assertEqual(saved.status_code, 303)
                priced = client.get('/picker/hold-draft.json').json
            self.assertEqual(priced['market_valuation']['price'], '24.00')
            self.assertTrue(priced['hold_preview'])
            self.assertEqual(api.call_count, 3)

    def test_details_gate_requires_product_model_and_concordant_documents(self):
        def photo(value, **extra):
            return {'value': value, 'status': 'PHOTO_OBSERVATION', 'photos': [1], **extra}
        evidence = {'brand': [photo('Maker')], 'model': [photo('R-42')],
                    'marking': [photo('R-42', marking_kind='MODEL_NUMBER')]}
        self.assertIsNone(details_request(evidence, False, {1}))
        self.assertIsNone(details_request(evidence, True, {2}))
        contested = evidence | {'model': [photo('R-42'), photo('R-44') | {'status': 'CONTESTED'}]}
        self.assertIsNone(details_request(contested, True, {1}))
        request = details_request(evidence, True, {1})
        urls = ['https://maker.example/spec', 'https://library.example/cat']
        anchor = {key: request[key] for key in ('brand', 'model', 'photos')}
        for excerpt in ('R-42 CIPA weight 550 g with battery', 'R-42 package weight 550 g',
                        'XR-42 net weight 550 g'):
            claim = dict(field='weight_kg', value=0.55, unit='kg', photos=[1], specimen_basis='NET', sources=[
                dict(url=url, document_id=str(i), kind='MANUAL', match='EXACT', excerpt=excerpt)
                for i, url in enumerate(urls)])
            packet = dict(status='COMPLETE', anchor=anchor, requested_fields=['weight_kg'], urls=urls, claims=[claim])
            self.assertEqual(check_documentary_details(packet, request, 1, m._p054_source_host), [])
