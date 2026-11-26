import json
import re
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from catalogue_labels import Catalogue
from ebay_sandbox_probe import SANDBOX, probe
from product_evidence import build_product_evidence, specific_violations
from test_mobile import m
from visual_protocol import normalize_visual_analysis
from workspace_ebay import TOKEN_API


def observed(field, value, photo=1):
    return {'field': field, 'value': value, 'photos': [photo],
            'basis': 'Visible construction or printed label', 'confidence': 96}


class ProductEvidenceTests(unittest.TestCase):
    def test_named_features_require_literal_support_and_dedupe(self):
        literal = {**observed('feature', 'Black'), 'name': 'Color'}
        evidence = build_product_evidence([literal, literal, observed('feature', 'Wireless'),
                                          {**literal, 'photos': [2]}], [], {}, {}, 1)
        self.assertEqual(evidence['feature'], [{
            'name': 'Color', 'value': 'Black', 'basis': literal['basis'],
            'status': 'PHOTO_OBSERVATION', 'photos': [1],
        }])
        self.assertIn('Color — Black', m._product_evidence_html(evidence))

    def test_complete_direct_readings_skip_separate_measurement_call(self):
        picker = 'direct-reading-fixture'
        photos = [{}] * 5
        roles = {i: 'EVIDENCE_ONLY' for i in range(2, 6)}
        scenes = {'2': ['SCALE', 96], **{str(i): ['RULER', 96] for i in range(3, 6)}}
        scale = dict(source_photo=2, raw_digits='125,0', unit='g', confidence=95, visible=True)
        readings = [
            dict(source_photo=i, axis=axis, start_cm=-.3, end_cm=end, edges_visible=True,
                 scale_marks_visible=True, confidence=96)
            for i, axis, end in ((3, 'length', 8.2), (4, 'width', 4.7), (5, 'thickness', .7))
        ]
        def text(rows):
            return ('PHOTO_SCENES_JSON: ' + json.dumps(scenes) + '\nPHYSICAL_READINGS_JSON: '
                    + json.dumps(dict(scale=scale, dimension_readings=rows)))
        with (patch.dict(m.PHOTO_ROLE_CACHE, {picker: roles}),
              patch.object(m, 'estimate_item_size_weight_from_photos', return_value={'fallback': True}) as separate):
            measured = m._p0249_measurement_engine(picker, photos, None, analysis_text=text(readings))
            separate.assert_not_called()
            self.assertEqual([measured[field] for field in ('weight_kg', 'length_cm', 'width_cm', 'height_cm')],
                             [.125, 8.5, 5., 1.])
            self.assertEqual(measured['ruler_axis_evidence']['length_cm']['source_photo'], 3)
            readings[0]['source_photo'] = 2
            self.assertEqual(m._p0249_measurement_engine(picker, photos, None, analysis_text=text(readings)),
                             {'fallback': True})
            separate.assert_called_once()

    def test_catalogue_requires_exact_variant_and_marks_conflicting_photos(self):
        catalogue = Catalogue.read(Path(__file__).with_name('benchmark_product_sources.csv'))
        watch, = catalogue.match([(1, 'CASIO\nMODEL F-91W-1')])
        self.assertEqual((watch['model'], watch['photos'], watch['status']),
                         ('F-91W-1', [1], 'CATALOGUE_CANDIDATE'))
        self.assertEqual(catalogue.match([(1, 'Raspberry Pi Pico 2 W'), (2, 'F-91W-1')]), [])
        conflict = catalogue.match([(1, 'CASIO F-91W-1'), (2, 'STANLEY\n33-425')])
        self.assertEqual({row['status'] for row in conflict}, {'CONTESTED'})

    def test_conflicts_and_shipping_estimates_remain_out_of_public_aspects(self):
        measurement = {
            'weight_kg': .15, 'weight_source': 'MEASURED_FROM_SCALE',
            'vision_evidence': {'scale': {'source_photo': 2}},
            'length_cm': 16, 'width_cm': 7, 'height_cm': 2,
            'measurement_field_sources': {'length_cm': 'MEASURED_FROM_RULER',
                                          'width_cm': 'REFERENCE_ANALOG_ESTIMATE',
                                          'height_cm': 'SELLER_ENTERED'},
            'ruler_axis_evidence': {'length_cm': {'source_photo': 3, 'confidence': 95, 'value_cm': 16}},
        }
        evidence = build_product_evidence(
            [observed('type', 'Film Camera'), observed('type', 'Lens Cap'), observed('brand', 'Wrong', 9)],
            [], {'brand': 'Real', 'country': 'Japan'}, measurement, 3,
        )
        self.assertEqual(evidence['weight_kg'][0]['status'], 'PHOTO_MEASUREMENT')
        self.assertEqual(evidence['length_cm'][0]['photos'], [3])
        self.assertEqual(evidence['width_cm'][0]['status'], 'SHIPPING_ESTIMATE')
        self.assertEqual(evidence['height_cm'][0]['status'], 'SELLER_VERIFIED')
        self.assertEqual(len(evidence['brand']), 1)
        self.assertEqual(specific_violations(evidence, {'Brand': 'Real', 'Model': 'Fake', 'Era': '2020s'}),
                         ['Model', 'Era'])
        path = 'Cameras & Photo > Cameras > Film Cameras'
        self.assertIn('суперечать', m._visual_category_conflict({'product_evidence': evidence}, path))
        self.assertTrue(m._p057_live_aspect_violations({'product_evidence': evidence}, {'Model': 'Fake'}))

    def test_structured_automatic_draft_and_reopen_need_no_writing_call(self):
        picker = 'product-evidence-case'
        photos = [{}] * 3
        facts = [observed('type', 'Digital Single Lens Reflex Camera'),
                 observed('defect', 'Small scratch near the battery cover'),
                 observed('model', 'Reference Model 82'),
                 observed('condition', 'USED')]
        analysis = 'PRODUCT_OBSERVATIONS_JSON: ' + json.dumps({'fields': facts})
        identification = ('IDENTITY_JSON {"type":"Digital Single Lens Reflex Camera"}\n'
                          'IDENTITY_CLAIMS_JSON {"claims":[{"field":"model","value":"Reference Model 82",'
                          '"photos":[1],"sources":[]}]}')
        measurements = dict(weight_kg=.15, length_cm=12, width_cm=8, height_cm=5,
                            weight_source='MEASURED_FROM_SCALE', vision_evidence={'scale': {'source_photo': 2}},
                            dimensions_source='REFERENCE_ANALOG_ESTIMATE', reference_dimensions_shipping_only=True)
        with (
            m.app.test_request_context('/picker/final-listing', method='POST', data={'hold_csrf': 'fixture'}),
            patch.dict(m.ANALYSIS_CACHE, {picker: analysis}), patch.dict(m.IDENTIFICATION_CACHE, {picker: identification}),
            patch.dict(m.PHOTO_CACHE, {picker: photos}), patch.dict(m.MEASUREMENT_CACHE, {picker: measurements}),
            patch.dict(m.MARKET_CACHE, {picker: 'PRICE_JSON {"normal":25}'}),
            patch.dict(m.ITEM_STRUCTURE_CACHE, {picker: dict(scope='ONE_PHYSICAL_ITEM', physical_item_count=1)}),
            patch.dict(m.LOT002_DRAFT_CACHE, {}),
            patch.object(m, '_save_active_lot_checkpoint', return_value=True),
            patch.object(m, 'call_openai', side_effect=AssertionError('Unexpected paid call')),
        ):
            m.session.update(picker_session_id=picker, hold_csrf='fixture', current_lot_number=51)
            response = m.app.make_response(m.final_listing(automatic_revision=m._measurement_revision(picker)))
            self.assertEqual(response.status_code, 303, re.findall(r'<h2>.*?</h2>', response.get_data(as_text=True)))
            draft = m.LOT002_DRAFT_CACHE[picker]
            self.assertEqual(draft['listing_text_source'], 'STRUCTURED_PRODUCT_EVIDENCE')
            self.assertIn('scratch', draft['description'])
            self.assertNotIn('Reference Model 82', draft['listing_text'])
            self.assertNotIn('Item Length', draft['item_specifics'])
            self.assertNotIn('Brand', draft['item_specifics'])
            self.assertEqual(draft['product_evidence']['model'][1]['status'], 'HYPOTHESIS')
            self.assertEqual(m._draft_primary_type(draft), 'Digital Single Lens Reflex Camera')
            self.assertIn('Докази щодо характеристик', m.review_final_draft())

    def test_visual_fields_flow_to_card_with_direct_readings_across_categories(self):
        picker = 'visual-to-draft-fixture'
        photos = [{} for _ in range(5)]
        roles = {i: 'EVIDENCE_ONLY' if i > 1 else 'EBAY_PRODUCT' for i in range(1, 6)}
        frames = [
            dict(photo=i, role=role, scene='GENERAL_PRODUCT' if i == 1 else 'SCALE' if i == 2 else 'RULER',
                 confidence=96) for i, role in roles.items()
        ]
        physical = {
            'scale': dict(source_photo=2, raw_digits='125,0', unit='g', confidence=95, visible=True),
            'dimension_readings': [
                dict(source_photo=i, axis=axis, start_cm=-.3, end_cm=end, edges_visible=True,
                     scale_marks_visible=True, confidence=96)
                for i, axis, end in ((3, 'length', 18.2), (4, 'width', 9.7), (5, 'thickness', 2.7))
            ],
        }
        scope = dict(scope='ONE_PHYSICAL_ITEM', physical_item_count=1, confidence=96, note='')
        for kind, material, expected_material, leaf, parent, incompatible in (
            ('Digital Single Lens Reflex Camera', 'Metal', 'Metal', 'Digital Cameras',
             'Cameras & Photo > Cameras', 'Dolls & Bears > Doll Accessories'),
            ('Cabin Air Filter', 'Paper', 'Paper/Cardboard', 'Cabin Air Filters',
             'eBay Motors > Car Parts', 'Home & Garden > Household Supplies'),
            ('Running Shoe', 'Fabric and Rubber', 'Fabric/Textile, Rubber', 'Running Shoes',
             "Clothing, Shoes & Accessories > Women's Shoes", 'Dolls & Bears > Doll Accessories'),
        ):
            with self.subTest(kind=kind):
                report = dict(photos=frames, observations=[
                    observed('type', kind), observed('material', material),
                    observed('brand', 'Fixture Maker'), observed('model', 'FM-621'),
                    observed('condition', 'USED'), observed('defect', 'Small scratch near edge'),
                    observed('material', 'Wood', 6), observed('material', 'Plastic or Rubber'),
                ], scope=scope, facts={'object_type': kind, 'materials': []}, physical=physical)
                analysis = normalize_visual_analysis(json.dumps(report), len(photos))
                identification = 'IDENTITY_JSON ' + json.dumps({'type': kind})
                with (
                    m.app.test_request_context('/picker/final-listing', method='POST', data={'hold_csrf': 'fixture'}),
                    patch.dict(m.ANALYSIS_CACHE, {picker: analysis}),
                    patch.dict(m.IDENTIFICATION_CACHE, {picker: identification}),
                    patch.dict(m.PHOTO_CACHE, {picker: photos}), patch.dict(m.PHOTO_ROLE_CACHE, {picker: roles}),
                    patch.dict(m.MEASUREMENT_CACHE), patch.dict(m.LOT002_DRAFT_CACHE),
                    patch.dict(m.MARKET_CACHE, {picker: 'PRICE_JSON {"normal":25}'}),
                    patch.dict(m.ITEM_STRUCTURE_CACHE, {picker: m._scope_from_analysis(analysis)}),
                    patch.object(m, '_save_active_lot_checkpoint', return_value=True),
                    patch.object(m, 'call_openai', side_effect=AssertionError('Unexpected paid call')),
                ):
                    m.session.update(picker_session_id=picker, hold_csrf='fixture', current_lot_number=51)
                    m.MEASUREMENT_CACHE[picker] = m._complete_first_pass_measurements(picker, photos, analysis)
                    response = m.app.make_response(m.final_listing(automatic_revision=m._measurement_revision(picker)))
                    self.assertEqual(response.status_code, 303, re.findall(r'<h2>.*?</h2>', response.get_data(as_text=True)))
                    draft = m.LOT002_DRAFT_CACHE[picker]
                    self.assertIn(kind, draft['title'])
                    specifics = draft['item_specifics']
                    self.assertEqual(specifics['Material'], expected_material)
                    self.assertEqual(specifics['Item Weight'], '0.125 kg')
                    self.assertEqual([specifics['Item ' + label] for label in ('Length', 'Width', 'Height')],
                                     ['18.5 cm', '10 cm', '3 cm'])
                    self.assertIn('Small scratch near edge', draft['description'])
                    self.assertFalse({'Brand', 'Model', 'Condition'} & specifics.keys())
                    card = m.review_final_draft()
                    for value in ('Fixture Maker', 'FM-621', 'USED', '0.125 kg', '18.5 cm'):
                        self.assertIn(value, card)
                    snapshot = dict(draft, category_id='123', category_path_category_id='123',
                                    category_guard_version='P057')
                    self.assertTrue(m._p057_category_snapshot_check(dict(snapshot, category_path=f'{parent} > {leaf}'))[0])
                    self.assertFalse(m._p057_category_snapshot_check(dict(snapshot, category_path=f'{incompatible} > {leaf}'))[0])

    def test_sandbox_probe_is_dry_by_default_and_scoped_to_sandbox(self):
        transport = Mock()
        settings = {'VINTAGE_SANDBOX_CLIENT_ID': 'app', 'VINTAGE_SANDBOX_CLIENT_SECRET': 'secret',
                    'VINTAGE_SANDBOX_SELLER_TOKEN': 'test-token'}
        self.assertEqual(probe(config=settings, transport=transport)['taxonomy'], 'NOT_RUN')
        self.assertEqual(transport.mock_calls, [])
        transport.post.return_value = Mock(status_code=200, json=lambda: {'access_token': 'test-app-token'})
        transport.get.side_effect = [
            Mock(status_code=200, json=lambda: {'categoryTreeId': '0', 'categoryTreeVersion': '7'}),
            Mock(status_code=200, json=lambda: {'total': 2, 'inventoryItems': [{'sku': 'private'}]}),
        ]
        result = probe(connect=True, config=settings, transport=transport)
        self.assertEqual(result['taxonomy'], 'OK')
        self.assertEqual(result['inventory_total'], 2)
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(transport.post.call_args.args[0], TOKEN_API)
        self.assertTrue(all(call.args[0].startswith(SANDBOX + '/') and not call.kwargs['allow_redirects']
                            for call in transport.get.call_args_list))
