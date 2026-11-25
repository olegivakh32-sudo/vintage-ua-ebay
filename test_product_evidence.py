import json
import re
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from catalogue_labels import Catalogue
from ebay_sandbox_probe import SANDBOX, probe
from product_evidence import build_product_evidence, specific_violations
from test_mobile import m
from workspace_ebay import TOKEN_API


def observed(field, value, photo=1):
    return {'field': field, 'value': value, 'photos': [photo],
            'basis': 'Visible construction or printed label', 'confidence': 96}


class ProductEvidenceTests(unittest.TestCase):
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
