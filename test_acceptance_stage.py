"""Exercise the frozen plan, scope safeguards and product-independent OCR."""
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance_stage import check, sha
from listing_review import corrected_ocr, export_review
from test_mobile import m


ARTIFACTS = Path(__file__).parent / 'test_artifacts' / 'recognition'
REFERENCES = ARTIFACTS / 'lotnexo-acceptance-56-enriched.json'


class AcceptanceStageTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((ARTIFACTS / 'lotnexo-acceptance-stages.json').read_text())
        first = self.plan['stages']['S1']
        self.state = {
            'algorithm_revision': self.plan['algorithm_revision'], 'reference_sha256': self.plan['reference_sha256'],
            'blind_sha256': first['blind_sha256'], 'publication_disabled': True,
            'authenticated_model': self.plan['model'], 'pricing_verified': True, 'reviewed_passed_stages': [],
            'approved_usd': '1.46950', 'attempts': [],
            'free_input_count': {'lot': first['lots'][0], 'step': 'photos', 'input_tokens': 2300},
        }

    def test_frozen_partition_budget_and_sources(self):
        stages = list(self.plan['stages'].values())
        self.assertEqual(self.plan['reference_sha256'], sha(REFERENCES))
        self.assertEqual([len(row['lots']) for row in stages], [10, 20, 26])
        self.assertEqual([row['minimum_correct'] for row in stages], [9, 18, 24])
        self.assertEqual(sum(Decimal(row['reserve_usd']) for row in stages), Decimal('8.22920'))
        all_lots = [lot for row in stages for lot in row['lots']]
        self.assertEqual(len(set(all_lots)), 56)
        references = json.loads(REFERENCES.read_text())
        self.assertEqual(len({row['photo_sha256'] for row in references}), 56)
        self.assertEqual({row['lot'] for row in references}, set(all_lots))
        self.assertTrue(all(row['category_reference']['verified_leaf_id'] is None for row in references))
        self.assertTrue(all('measurement' not in row and row['specimen_or_package_weight_kg'] is None for row in references))

    def test_stop_before_unapproved_or_unbounded_request(self):
        self.assertEqual(check(self.plan, 'S1', self.state)['next']['reserve_usd'], '0.0042')
        for fields, expected in [
            ({'approved_usd': None}, 'PAID_APPROVAL_MISSING'),
            ({'approved_usd': '1.46949'}, 'INSUFFICIENT_APPROVED_BUDGET'),
            ({'publication_disabled': False}, 'PUBLICATION_GATE_MISSING'),
            ({'authenticated_model': None}, 'MODEL_OR_RATES_UNVERIFIED'),
            ({'reference_sha256': 'different'}, 'FROZEN_INPUT_CHANGED'),
            ({'critical_error': True}, 'CRITICAL_QUALITY_FAILURE'),
            ({'free_input_count': None}, 'AWAIT_FREE_TOKEN_COUNT'),
            ({'free_input_count': dict(self.state['free_input_count'], input_tokens=24001)}, 'INPUT_TOKEN_LIMIT'),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(check(self.plan, 'S1', self.state | fields)['stop'], expected)
        second = self.plan['stages']['S2']
        self.assertEqual(check(self.plan, 'S2', self.state | {'blind_sha256': second['blind_sha256']})['stop'],
                         'PREVIOUS_STAGE_NOT_ACCEPTED')

    def test_uncertain_charge_and_stage_result_blocks(self):
        attempts = [
            {'lot': lot, 'step': step, 'status': 'COMPLETE', 'model': self.plan['model'],
             'service_tier': 'default', 'metered_usd': '0.001', 'input_tokens': 2000,
             'output_tokens': 500, 'web_calls': int(step == 'research')}
            for lot in self.plan['stages']['S1']['lots'] for step in ('photos', 'research')
        ]
        self.assertEqual(check(self.plan, 'S1', self.state | {'attempts': attempts})['stop'], 'AWAIT_INDEPENDENT_REVIEW')
        state = self.state | {'attempts': attempts, 'review': {'independent': True, 'identity_correct': 8}}
        self.assertEqual(check(self.plan, 'S1', state)['stop'], 'IDENTITY_THRESHOLD_FAILED')
        state['review']['identity_correct'] = 9
        self.assertEqual(check(self.plan, 'S1', state)['result'], 'STAGE_PASSED')
        attempts[0] = attempts[0] | {'status': 'IN_FLIGHT'}
        self.assertEqual(check(self.plan, 'S1', state)['stop'], 'PROVIDER_FAILURE_OR_UNCERTAIN_CHARGE')

    def test_shipping_calculator_on_three_independent_manufacturer_specs(self):
        cases = {
            'CASIO-F91W1': (0.15, (7.9, 7.6, 4.9), 7, 10.99),
            'LOGITECH-MX-MASTER3S': (0.27, (16.5, 12.5, 9.1), 12, 15.99),
            'NINTENDO-SWITCH-LCD': (0.58, (29.9, 16.2, 8.9), 18, 21.99),
        }
        for row in json.loads(REFERENCES.read_text()):
            if row['reference_id'] not in cases:
                continue
            source = row['documentary_reference']
            dims = source['dimensions_cm']
            _, package, mass, tariff = m._shipping_quote(source['nominal_unpacked_weight_kg'],
                                                        dims['length'], dims['width'], dims['height'])
            self.assertEqual((mass, package, tariff['base_usd'], tariff['recommended_usd']),
                             cases.pop(row['reference_id']))
            self.assertFalse(tariff['includes_duties'])
        self.assertEqual(cases, {})

    def test_shared_ocr_matches_unique_document_on_other_product_types(self):
        for kind, maker, model in [('Tape measure', 'Example Toolworks', 'Model 12'),
                                  ('Film camera', 'Example Optical', 'Model 42'),
                                  ('Digital wristwatch', 'Example Timeworks', 'Model 43')]:
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                catalogue = Path(directory) / 'identifiers.json'
                source_url = 'https://manufacturer.example/technical-manual'
                record = {'brand': maker, 'model': model, 'url': source_url, 'codes': ['AB-01']}
                catalogue.write_text(json.dumps([record]))
                evidence = {field: [{'value': value, 'status': 'PHOTO_OBSERVATION', 'photos': [1]}]
                            for field, value in [('type', kind), ('brand', maker), ('model', model), ('marking', 'AB-O1')]}
                with patch('listing_review.CODE_RECORDS', catalogue):
                    review = export_review(evidence)
                    self.assertEqual(review['type'], kind)
                    self.assertEqual(corrected_ocr('Label AB-O1', review['ocr_corrections']), 'Label AB-01')
                    self.assertEqual(review['fields']['marking'][0]['value'], 'AB-O1')
                    self.assertEqual(review['ocr_corrections'][0]['url'], source_url)
                    record['codes'].append('AB-OI')
                    catalogue.write_text(json.dumps([record]))
                    self.assertEqual(export_review(evidence)['ocr_corrections'], [])
                    catalogue.write_text(json.dumps([record | {'brand': 'Different manufacturer'}]))
                    self.assertEqual(export_review(evidence)['ocr_corrections'], [])
