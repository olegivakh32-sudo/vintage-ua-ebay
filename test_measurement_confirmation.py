import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_mobile import m


class MeasurementConfirmationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', str(root / 'checkpoints')))
        self.enterContext(patch.dict(m.os.environ, {'HOLD_QUEUE_DIR': str(root / 'hold')}))
        self.enterContext(patch.object(
            m.requests.sessions.Session, 'request', side_effect=AssertionError('External network forbidden')
        ))
        self.picker = 'measurement-confirmation-fixture'
        self.base = {'weight_kg': .41, 'length_cm': 15, 'width_cm': 9, 'height_cm': 4,
                     'weight_source': 'ESTIMATED', 'dimensions_source': 'REFERENCE_ANALOG_ESTIMATE',
                     'reference_weight_shipping_only': True, 'reference_dimensions_shipping_only': True}
        self.caches = (m.PHOTO_CACHE, m.ANALYSIS_CACHE, m.IDENTIFICATION_CACHE, m.MARKET_CACHE,
                       m.MEASUREMENT_CACHE, m.SELLER_MEASUREMENT_CACHE, m.PHOTO_ROLE_CACHE)
        for cache, value in zip(self.caches, (
                [{}], 'Visual analysis', 'IDENTITY_JSON {"type":"Film Camera"}', 'Market analysis',
                self.base.copy(), {}, {1: 'EBAY_PRODUCT'})):
            self.enterContext(patch.dict(cache, {self.picker: value}))
        self.client = m.app.test_client()
        with self.client.session_transaction() as session:
            session.update(picker_session_id=self.picker, current_lot_number=501)

    @staticmethod
    def fields(markup):
        return {name: re.search(fr'<textarea name="{name}"[^>]*>([^<]+)', markup)[1]
                for name in ('hold_csrf', 'measurement_revision')}

    def form(self):
        response = self.client.get('/picker/resume-measurements')
        self.assertEqual(response.status_code, 200)
        return self.fields(response.get_data(as_text=True))

    def save(self, form, route='/picker/shipping-submit', **overrides):
        return self.client.post(route, data={
            **form, 'item_weight': '.50', 'dim_l': '16', 'dim_w': '9', 'dim_h': '4',
            'measurement_accepted': 'yes', **overrides,
        })

    def test_older_form_cannot_change_new_measurement_or_start_draft(self):
        form = self.form()
        self.assertEqual(self.save(form).status_code, 303)
        for endpoint in ('/picker/shipping-submit', '/picker/shipping', '/picker/recover-saved-measurement',
                         '/picker/rerun-measurement', '/picker/final-listing'):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.save(form, endpoint, item_weight='.77').status_code, 409)
        result = m.SELLER_MEASUREMENT_CACHE[self.picker]
        self.assertEqual(result['weight_kg'], .50)
        self.assertEqual(result['weight_source'], 'SELLER_ENTERED')
        self.assertEqual(result['measurement_field_sources']['width_cm'], 'REFERENCE_ANALOG_ESTIMATE')
        self.assertTrue(result['reference_dimensions_shipping_only'])
        self.assertNotIn('reference_weight_shipping_only', result)

    def test_failed_checkpoint_preserves_last_values_and_allows_retry(self):
        form = self.form()
        before = m.MEASUREMENT_CACHE[self.picker].copy()
        with patch.object(m, '_save_active_lot_checkpoint', return_value=False):
            self.assertEqual(self.save(form).status_code, 503)
        self.assertEqual(m.MEASUREMENT_CACHE[self.picker], before)
        self.assertEqual(m.SELLER_MEASUREMENT_CACHE[self.picker], {})
        self.assertEqual(self.save(form).status_code, 303)

    def test_draft_uses_server_research_and_confirmed_shipping_quote(self):
        self.assertEqual(self.save(self.form()).status_code, 303)
        form = self.fields(self.client.get('/picker/resume-shipping').get_data(as_text=True))
        with (patch.dict(m.ITEM_STRUCTURE_CACHE, {self.picker: {
                'scope': 'ONE_PHYSICAL_ITEM', 'physical_item_count': 1, 'sale_mode': 'ONE_LOT',
              }}), patch.object(m, 'call_openai', side_effect=RuntimeError('stub')) as ai):
            self.client.post('/picker/final-listing', data={
                **form, 'analysis': 'Forged visual report', 'market': 'Forged market report',
                'item_weight': '7', 'shipping_usd': '0', 'dimensions': 'Forged dimensions',
                'material_other': 'likely synthetic polymer', 'confirm_material_other': 'yes',
            })
        ai.assert_called_once()
        prompt = ai.call_args.args[0]['input']
        self.assertIn('VISUAL ANALYSIS:\nVisual analysis', prompt)
        self.assertIn('item_weight=0.5 kg', prompt)
        self.assertIn('buyer_shipping=$21.99', prompt)
        self.assertIn('materials=Unknown', prompt)
        self.assertNotIn('Forged', prompt)

    def test_carrier_block_preserves_seo_and_measurements_without_quote_or_generation(self):
        observed_type = {'field': 'type', 'value': 'Handheld console', 'photos': [1],
                         'confidence': 95, 'basis': 'D-pad and built-in display'}
        sources = [
            {'url': 'https://manufacturer.example/manual', 'kind': 'MANUAL', 'document_id': 'Official manual'},
            {'url': 'https://reviewer.example/specs', 'kind': 'OTHER', 'document_id': 'Independent specifications'},
        ]
        for source in sources:
            source.update(match='EXACT', excerpt='Integrated Lithium-ion rechargeable battery')
        claim = {'field': 'battery', 'value': 'Integrated Li‑ion battery', 'photos': [1], 'sources': sources}
        analysis = 'PRODUCT_OBSERVATIONS_JSON ' + json.dumps({'fields': [observed_type]})
        identification = 'IDENTITY_CLAIMS_JSON ' + json.dumps({'claims': [claim]})
        identification += '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': [row['url'] for row in sources]})
        with (patch.dict(m.ANALYSIS_CACHE, {self.picker: analysis}),
              patch.dict(m.IDENTIFICATION_CACHE, {self.picker: identification}),
              patch.dict(m.LOT002_DRAFT_CACHE, clear=True),
              patch.multiple(m, EBAY_CLIENT_ID=None, EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None),
              patch.object(m, '_p058_capture_current', wraps=m._p058_capture_current) as capture,
              patch.object(m, 'call_openai', side_effect=AssertionError('Paid generation forbidden'))):
            self.assertEqual(self.save(self.form()).location, '/picker/review-final-draft')
            saved = m.LOT002_DRAFT_CACHE[self.picker]
            self.assertTrue(m._p058_has_saved_draft(self.picker, saved))
            self.assertEqual(saved['shipping_status'], 'NEEDS_CARRIER_REVIEW')
            self.assertEqual(saved['compliance_review']['status'], 'BLOCK')
            self.assertEqual(saved['product_export']['fields']['battery'][0]['status'], 'CORROBORATED_REFERENCE')
            self.assertNotIn('shipping_usd', saved)
            self.assertEqual(m.SELLER_MEASUREMENT_CACHE[self.picker]['weight_kg'], .50)
            shipping = self.client.get('/picker/resume-shipping')
            self.assertEqual(shipping.status_code, 409)
            self.assertIn('BLOCK — Укрпошта', shipping.get_data(as_text=True))
            self.assertNotIn('Рекомендована вартість доставки', shipping.get_data(as_text=True))
            self.assertEqual(self.client.get('/picker/review-final-draft').status_code, 200)
            capture.assert_called_once_with('SEO_HOLD')
            self.assertEqual(self.client.post('/picker/final-listing',
                                             data=self.fields(shipping.get_data(as_text=True))).status_code, 303)
            capture.assert_called_with('SEO_HOLD')
    def test_checkpoint_restores_provenance_and_shipping_get_does_not_rewrite(self):
        form = self.form()
        self.assertEqual(self.save(form).status_code, 303)
        expected = m.SELLER_MEASUREMENT_CACHE[self.picker].copy()
        for cache in self.caches:
            cache.pop(self.picker, None)
        with patch.object(m, '_save_active_lot_checkpoint', side_effect=AssertionError('GET rewrites state')):
            response = self.client.get('/picker/resume-shipping')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(m.SELLER_MEASUREMENT_CACHE[self.picker], expected)
        restored_form = self.fields(response.get_data(as_text=True))
        self.assertEqual(self.save(form, item_weight='.9').status_code, 409)
        self.assertEqual(self.save(restored_form, item_weight='.54').status_code, 303)

    def test_uncertain_values_require_explicit_acceptance(self):
        form = self.form()
        response = self.save(form, measurement_accepted='')
        self.assertEqual(response.status_code, 409)
        self.assertIn('Погодьте оцінки', response.get_data(as_text=True))
        self.assertEqual(m.SELLER_MEASUREMENT_CACHE[self.picker], {})
        self.assertEqual(self.save(self.fields(response.get_data(as_text=True))).status_code, 303)

    def test_both_forms_persist_grams_and_millimetres_in_canonical_units(self):
        form = self.form()
        self.assertEqual(self.save(form, item_weight='125,5', dim_l='165', dim_w='90', dim_h='40',
                                   mass_unit='g', dimension_unit='mm').status_code, 303)
        saved = m.SELLER_MEASUREMENT_CACHE[self.picker]
        self.assertEqual(saved['weight_kg'], .1255)
        self.assertEqual(saved['length_cm'], 16.5)
        self.assertEqual(saved['width_cm'], 9)
        self.assertEqual(saved['measurement_field_sources']['width_cm'], 'REFERENCE_ANALOG_ESTIMATE')

    def test_rejects_unknown_unit_and_out_of_range_conversion(self):
        form = self.form()
        for fields in ({'mass_unit': 'oz'}, {'dimension_unit': 'in'},
                       {'item_weight': '100001', 'mass_unit': 'g'}, {'dim_w': 'NaN'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.save(form, **fields).status_code, 400)
        self.assertEqual(m.SELLER_MEASUREMENT_CACHE[self.picker], {})
