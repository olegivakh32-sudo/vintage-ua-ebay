import json
import re
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory

from catalogue_labels import Catalogue
from ebay_sandbox_probe import SANDBOX, probe
from product_evidence import assess_photo_observations, build_product_evidence, draft_from_evidence, specific_violations
from test_mobile import m
from visual_protocol import normalize_visual_analysis
from workspace_ebay import TOKEN_API


def observed(field, value, photo=1):
    return {'field': field, 'value': value, 'photos': [photo],
            'basis': 'Visible construction or printed label', 'confidence': 96}


class ProductEvidenceTests(unittest.TestCase):
    def test_decisions_preserve_anchored_candidates_and_separate_no_visible_defect(self):
        rows = [
            {**observed('type', 'Digital Single Lens Reflex Camera'), 'confidence': 86},
            observed('defect', 'None visible'),
            observed('type', 'Invalid frame', 4),
            {**observed('brand', 'Fabricated'), 'basis': ''},
            {**observed('model', 'Wrong'), 'confidence': '95'},
            observed('feature', 'Black'),
            observed('material', 'Metal'), observed('material', 'Metal'),
            None, observed('invalid', 'Test'),
        ]
        evidence, decisions = assess_photo_observations(rows, 1)
        self.assertEqual([row['reasons'] for row in decisions], [
            ['BELOW_CONFIRMATION_THRESHOLD'], ['ABSENCE_IS_SURFACE_NOTE'], ['NO_VALID_PHOTO'],
            ['MISSING_OR_INVALID_BASIS'], ['INVALID_CONFIDENCE'], ['MISSING_FEATURE_NAME'],
            [], ['DUPLICATE'], ['INVALID_ROW'], ['UNKNOWN_FIELD'],
        ])
        self.assertEqual(evidence['type'][0]['status'], 'PHOTO_CANDIDATE')
        self.assertEqual(evidence['feature'][0]['status'], 'PHOTO_CANDIDATE')
        self.assertNotIn('name', evidence['feature'][0])
        self.assertNotIn('defect', evidence)
        self.assertEqual(evidence['condition'][0]['status'], 'PHOTO_SURFACE_NOTE')
        self.assertIsNone(draft_from_evidence(evidence, count=1, material='Metal', brand='', country='',
                                              test_status='Untested'))
        visual_html = m._product_evidence_html(evidence)
        self.assertIn('Огляд поверхні', visual_html)
        self.assertIn('Потребує перевірки', visual_html)
        legacy = m._product_evidence_html({'defect': [{**evidence['condition'][0], 'value': 'None visible',
                                                      'status': 'PHOTO_OBSERVATION'}]})
        self.assertIn('Огляд поверхні', legacy)
        self.assertIn('немає опису видимого доказу', m._observation_diagnostics_html(decisions))
        self.assertEqual(assess_photo_observations([rows[6]] * 37, 1)[1][-1]['reasons'], ['ROW_LIMIT'])

    def test_summary_type_and_visible_parts_are_review_only(self):
        evidence, decisions = assess_photo_observations(
            [observed('completeness', 'Complete')], 1, summary_type='Gaming Console',
        )
        self.assertEqual(evidence['type'][0]['photos'], [])
        self.assertEqual(evidence['type'][0]['status'], 'HYPOTHESIS')
        self.assertEqual(m.photo_type(evidence), '')
        self.assertEqual(decisions[0]['reasons'], ['COMPLETENESS_REQUIRES_INVENTORY'])
        self.assertEqual(evidence['completeness'][0]['status'], 'PHOTO_CANDIDATE')
        self.assertIsNone(draft_from_evidence(evidence, count=1, material='', brand='', country='',
                                              test_status='Untested'))
        confirmed = build_product_evidence([], [], {'completeness': 'Complete'}, {}, 1)
        self.assertEqual(confirmed['completeness'][0]['status'], 'SELLER_VERIFIED')

    def test_catalogue_uses_partial_label_only_as_lead_and_preserves_variant_conflicts(self):
        source = Path(__file__).with_name('benchmark_product_sources.csv')
        catalogue = Catalogue.read(source)
        lead, = catalogue.match([(1, 'Pico 2')], include_leads=True)
        self.assertEqual(lead['status'], 'CATALOGUE_LEAD')
        self.assertEqual(catalogue.match([(1, 'Raspberry Pi\nPico 2 W')], include_leads=True), [])
        exact, = catalogue.match([(1, 'Raspberry Pi\nPico 2')], include_leads=True)
        self.assertEqual(exact['status'], 'CATALOGUE_CANDIDATE')
        conflicting = catalogue.match([(1, 'Raspberry Pi\nPico 2'), (2, 'F-91W-1')], include_leads=True)
        self.assertEqual({row['status'] for row in conflicting}, {'CONTESTED', 'CATALOGUE_LEAD'})

    def test_local_ocr_research_sources_and_nominal_specs_reach_product_card(self):
        picker = 'catalogue-to-draft-fixture'
        path = 'https://maker.example/manual'
        other = 'https://archive.example/camera'
        kind = 'Digital Single Lens Reflex Camera'
        observation = [observed('type', kind), observed('material', 'Metal'), observed('defect', 'None visible')]
        analysis = 'PRODUCT_OBSERVATIONS_JSON: ' + json.dumps({'fields': observation})
        analysis += '\nAUTO_FACTS_JSON: {"markings":[{"photo":1,"literal":"ACME","kind":"MAKER"}]}'
        sources = [dict(url=url, kind='MANUAL', document_id=str(key), match='EXACT', excerpt='Reference manual.')
                   for key, url in enumerate((path, other))]
        claims = [
            dict(field='model', value='K400', photos=[1], sources=sources),
            dict(field='weight_kg', value=.2, specimen_basis='NET', photos=[1], sources=sources),
            dict(field='length_cm', value=17, specimen_basis='PACKAGE', photos=[1], sources=sources),
        ]
        identity = 'IDENTITY_JSON ' + json.dumps({'type': kind})
        identity += '\nIDENTITY_CLAIMS_JSON ' + json.dumps({'claims': claims})
        market = 'PRICE_JSON {"normal":25}\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': [path, other]})
        reply = f'=== IDENTIFICATION ===\n{identity}\n=== MARKET ===\n{market}'
        measured = dict(weight_kg=.125, length_cm=12, width_cm=7, height_cm=5, weight_source='SELLER_VERIFIED')
        with TemporaryDirectory() as directory:
            catalogue_path = Path(directory) / 'catalogue.csv'
            catalogue_path.write_text('reference_id,brand,model,object_type,source_url,material,weight_kg,length_cm\n'
                                      f'CAM,Acme,K400,{kind},{path},Glass,0.2,15\n')
            photos = [{'mobile_file': 'local-photo', 'retail_code_version': m.BARCODE_HINT_VERSION}]
            with (
                m.app.test_request_context('/picker/final-listing', method='POST', data={'hold_csrf': 'fixture'}),
                patch.dict('os.environ', {'VINTAGE_PRODUCT_CATALOGUE': str(catalogue_path)}),
                patch.dict(m.ANALYSIS_CACHE, {picker: analysis}),
                patch.dict(m.IDENTIFICATION_CACHE), patch.dict(m.MARKET_CACHE),
                patch.dict(m.PHOTO_CACHE, {picker: photos}), patch.dict(m.MEASUREMENT_CACHE, {picker: measured}),
                patch.dict(m.ITEM_STRUCTURE_CACHE, {picker: dict(scope='ONE_PHYSICAL_ITEM', physical_item_count=1)}),
                patch.dict(m.LOT002_DRAFT_CACHE),
                patch.object(m, 'read_label_texts', return_value=[(1, 'K400')]) as ocr,
                patch.object(m, '_mobile_local_bytes', return_value=b'fake preview'),
                patch.object(m, 'google_photo_to_data_url', return_value='data:image/jpeg;base64,AA=='),
                patch.object(m, '_save_active_lot_checkpoint', return_value=True),
                patch.object(m, 'call_openai', return_value=reply) as transport,
            ):
                m.session.update(picker_session_id=picker, hold_csrf='fixture', current_lot_number=51)
                m.IDENTIFICATION_CACHE[picker], m.MARKET_CACHE[picker] = m._p026_combined_research_engine(
                    'LOT 051', analysis, photos, [1], m.ITEM_STRUCTURE_CACHE[picker], picker)
                self.assertIn('CATALOGUE_CANDIDATE', transport.call_args.args[0]['input'][0]['content'][0]['text'])
                result = m.app.make_response(m.final_listing(automatic_revision=m._measurement_revision(picker)))
                self.assertEqual(result.status_code, 303)
                ocr.assert_called_once()
                transport.assert_called_once()
                card = m.review_final_draft()
                draft = m.LOT002_DRAFT_CACHE[picker]
                self.assertIn(path, card)
                self.assertIn(other, card)
                self.assertIn('Огляд поверхні', card)
                self.assertIn('Вага · 0.2 kg', card)
                self.assertIn('Довжина · 15.0 cm', card)
                self.assertNotIn('17.0 cm', card)
                self.assertEqual(draft['item_specifics']['Item Weight'], '0.125 kg')
                self.assertEqual(draft['item_specifics']['Material'], 'Metal')
                self.assertNotIn('Model', draft['item_specifics'])

    def test_named_features_require_literal_support_and_dedupe(self):
        literal = {**observed('feature', 'Black'), 'name': 'Color'}
        evidence = build_product_evidence([literal, literal, observed('feature', 'Wireless'),
                                          {**literal, 'photos': [2]}], [], {}, {}, 1)
        self.assertEqual([row for row in evidence['feature'] if row['status'] == 'PHOTO_OBSERVATION'], [{
            'name': 'Color', 'value': 'Black', 'basis': literal['basis'],
            'status': 'PHOTO_OBSERVATION', 'photos': [1],
        }])
        self.assertEqual(evidence['feature'][1]['status'], 'PHOTO_CANDIDATE')
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

    def test_single_item_can_join_exact_markings_across_source_photos(self):
        catalogue = Catalogue.read(Path(__file__).with_name('benchmark_product_sources.csv'))
        marks = [(3, 'CASIO'), (20, 'MODEL F-91W-1')]
        lead, = catalogue.match(marks, include_leads=True)
        self.assertEqual(lead['status'], 'CATALOGUE_LEAD')
        match, = catalogue.match(marks, include_leads=True, single_item=True)
        self.assertEqual((match['status'], match['photos']), ('CATALOGUE_CANDIDATE', [3, 20]))
        self.assertFalse(catalogue.match([(3, 'CASIO'), (20, 'MODEL F-91W-1A')],
                                         include_leads=True, single_item=True))

    def test_existing_research_call_uses_label_outside_representative_24_photos(self):
        scenes = {str(slot): [scene, 96] for slot, scene in
                  {1: 'GENERAL_PRODUCT', 2: 'SCALE', 16: 'DETAIL', 23: 'MARKINGS'}.items()}
        facts = {'markings': [{'photo': 23, 'literal': 'ZX-20', 'kind': 'MODEL_NUMBER'}]}
        analysis = ('PHOTO_SCENES_JSON ' + json.dumps(scenes) + '\nAUTO_FACTS_JSON ' + json.dumps(facts))
        with (
            patch.dict('os.environ', {'VINTAGE_PRODUCT_CATALOGUE': '', 'VINTAGE_WORKSPACE_CATALOGUE': ''}),
            patch.object(m, 'google_photo_to_data_url', return_value='data:image/jpeg;base64,AA==') as image,
            patch.object(m, 'call_openai', return_value='') as request,
        ):
            m._p026_combined_research_engine('fixture', analysis, [{} for _ in range(24)], [1, 2, 3, 12, 24], {})
        request.assert_called_once()
        payload = request.call_args.args[0]
        self.assertEqual(payload['reasoning']['effort'], 'medium')
        content = payload['input'][0]['content']
        self.assertIn('ZX-20', content[0]['text'])
        self.assertEqual([part['text'] for part in content[1:] if part['type'] == 'input_text'],
                         ['SOURCE PHOTO #1', 'SOURCE PHOTO #23', 'SOURCE PHOTO #16', 'SOURCE PHOTO #3', 'SOURCE PHOTO #12'])
        self.assertEqual(image.call_args_list[1].args[1], 1536)
        self.assertTrue(image.call_args_list[1].kwargs['prefer_original'])

    def test_experimental_research_route_requires_consistent_exact_photo_and_catalogue_identity(self):
        kind = 'Film Camera'
        facts = [observed('type', kind), observed('brand', 'Acme'), observed('model', 'ZX-20')]
        catalogue = [dict(status='CATALOGUE_CANDIDATE', brand='Acme', model='Acme ZX-20', type=kind)]
        one = dict(scope='ONE_PHYSICAL_ITEM', physical_item_count=1, source='SELLER')
        cases = [
            ('hybrid', facts, 'UNIQUE', one, 'gpt-5.6-luna', 2),
            ('hybrid', facts + [{**observed('model', 'ZX-20A'), 'confidence': 85}], 'UNIQUE', one, 'gpt-5.6-terra', 4),
            ('hybrid', facts, 'CONFLICT', one, 'gpt-5.6-terra', 4),
            ('hybrid', facts, 'NONE', {}, 'gpt-5.6-terra', 4),
            ('', facts, 'UNIQUE', one, 'gpt-5.6-sol', None),
        ]
        for mode, observations, barcode, structure, model, tool_calls in cases:
            with (
                self.subTest(mode=mode, model=model, barcode=barcode, observations=len(observations)),
                patch.dict('os.environ', {'VINTAGE_RESEARCH_ROUTE': mode}),
                patch.object(m, '_picker_catalogue_matches', return_value=catalogue),
                patch.object(m, '_mobile_catalogue_hints', return_value={
                    'status': barcode, 'candidates': [{'code': '12345670', 'photos': [1]}],
                }),
                patch.object(m, 'google_photo_to_data_url', return_value='data:image/jpeg;base64,AA=='),
                patch.object(m, 'call_openai', return_value='') as request,
            ):
                analysis = 'PRODUCT_OBSERVATIONS_JSON ' + json.dumps({'fields': observations})
                m._p026_combined_research_engine('fixture', analysis, [{}], [1], structure)
                request.assert_called_once()
                payload = request.call_args.args[0]
                self.assertEqual(payload['model'], model)
                self.assertEqual(payload.get('max_tool_calls'), tool_calls)
                self.assertEqual(payload['tools'], [{'type': 'web_search'}])

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
