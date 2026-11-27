"""Verify the protected N12 contract without any generation or public network traffic."""
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

from blind_live_pilot import API
from identity_evidence import ALTERNATE_RESEARCH_PREFIX
from multiview_review import review_run
import n12_acceptance as n12
from n12_review import available_fields
from s1_acceptance import StopRun
from test_blind_live_pilot import response
from test_mobile import m
from test_s1_acceptance import RESEARCH, VISUAL, reply


class N12AcceptanceTests(unittest.TestCase):
    def test_wrong_cohort_or_missing_protected_claim_cannot_reach_preflight(self):
        with self.assertRaisesRegex(StopRun, 'FROZEN_PHOTOS_CHANGED'):
            n12.sealed_plan(archive=n12.ARTIFACTS / 'lotnexo-f50-blind.zip')
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'results'
            env = {'GITHUB_REF': n12.APPROVAL_REF, 'GITHUB_RUN_ATTEMPT': '1', 'ENABLE_LIVE_PUBLISH': '0',
                   'LOTNEXO_N12_RESERVED': n12.QUOTE_SHA, 'LOTNEXO_N12_APPROVED_USD': str(n12.CAP)}
            for candidate in ({}, env | {'GITHUB_RUN_ATTEMPT': '2'}, env | {'ENABLE_LIVE_PUBLISH': '1'},
                              env | {'LOTNEXO_N12_APPROVED_USD': '3.43'}, env | {'LOTNEXO_N12_RESERVED': 'changed'}):
                with (patch.dict('os.environ', candidate, clear=True),
                      patch('sys.argv', ['n12_acceptance.py', '--execute', str(target)]),
                      patch('builtins.print'), patch('n12_acceptance.authorize') as authorize):
                    self.assertEqual(n12.main(), 1)
                    authorize.assert_not_called()
            with (patch.dict('os.environ', env, clear=True),
                  patch('sys.argv', ['n12_acceptance.py', '--preflight', str(target)]),
                  patch('n12_acceptance.authorize') as authorize, patch('n12_acceptance.execute') as execute):
                self.assertEqual(n12.main(), 0)
                authorize.assert_called_once_with(None, target)
                execute.assert_not_called()

    def test_all_views_and_alternate_evidence_reach_hold_under_the_sealed_budget(self):
        kind = 'Camera mounting adapter'
        feature = 'Circular quick release latch'
        sources = ['https://manufacturer.example/specs/fx40', 'https://archive.example/record/fx40']
        citations = [dict(url=url, kind='MANUAL', document_id=str(i), match='EXACT',
                          excerpt=f'{kind} FX-40 with {feature}.') for i, url in enumerate(sources)]
        claims = [dict(field='type', value=kind, photos=[2], visual_anchor=feature, sources=citations),
                  dict(field='model', value='FX-40', photos=[2], sources=citations),
                  dict(field='material', value='Aluminum', photos=[2], sources=citations),
                  dict(field='period', value='1999', photos=[2], sources=citations),
                  dict(field='length_cm', value=8, photos=[2], specimen_basis='ITEM', sources=citations),
                  dict(field='weight_kg', value=.225, photos=[2], specimen_basis='NET', sources=citations)]
        earlier = 'fixture display stand'
        initial = RESEARCH.replace('Fixture display stand', earlier)
        alternative = f'IDENTITY_JSON {{"type":"{kind}","model":"FX-40","exact_model_confirmed":true}}\n'
        alternative += 'IDENTITY_CLAIMS_JSON ' + json.dumps({'claims': claims})
        counted, actual = [], []

        def provider(session, method, url, **kwargs):
            self.assertEqual(method, 'POST')
            body = kwargs['json']
            self.assertFalse(kwargs['allow_redirects'])
            if url == f'{API}/responses/input_tokens':
                counted.append(body)
                return response({'input_tokens': 2500})
            self.assertEqual(url, f'{API}/responses')
            self.assertEqual(counted[-1], {key: body[key] for key in counted[-1]})
            parts = body['input'][0]['content']
            self.assertNotIn('150188', parts[0]['text'])
            self.assertNotIn('5b224303e51912dba9', parts[0]['text'])
            image_count = sum(part['type'] == 'input_image' for part in parts)
            self.assertEqual(image_count, 2)
            if body.get('tools'):
                self.assertEqual(body['max_tool_calls'], 2)
                follow_up = parts[0]['text'].startswith(ALTERNATE_RESEARCH_PREFIX)
                self.assertIn(feature, parts[0]['text'])
                self.assertIn('TEST MARK', parts[0]['text'])
                if follow_up:
                    self.assertIn(earlier, parts[0]['text'])
                actual.append('alternate' if follow_up else 'research')
                known = sources if follow_up else ['https://analogs.example/fixture']
                return response(reply(output=[{'type': 'web_search_call', 'action': {'sources': [{'url': u} for u in known]}}],
                                      output_text=alternative if follow_up else initial))
            actual.append('photos')
            visual = VISUAL | {
                'photos': [VISUAL['photos'][0], VISUAL['photos'][0] | {'photo': 2, 'scene': 'MARKINGS'}],
                'facts': {'object_type': VISUAL['facts']['object_type'],
                          'markings': [{'photo': 2, 'literal': 'TEST MARK', 'kind': 'INSCRIPTION'}]},
                'observations': [*VISUAL['observations'], {'field': 'feature', 'value': feature,
                                 'name': 'Latch', 'photos': [2], 'confidence': 98, 'basis': 'Visible in closeup'}],
            }
            return response(reply(output_text=json.dumps(visual)))

        plan = n12.sealed_plan()
        capability = available_fields(plan)
        self.assertEqual(sum('model' in row for row in capability.values()), 2)
        self.assertEqual(sum('documentary_weight' in row for row in capability.values()), 1)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            lots = n12.blind_inputs(n12.ARCHIVE, plan['stages'][n12.STAGE], root)
            output = root / 'results'
            with (patch.object(requests.Session, 'request', side_effect=provider),
                  patch.object(m, 'OPENAI_API_KEY', 'simulated-transport'), patch.dict('sys.modules', {'app': m}),
                  patch.multiple(m, EBAY_CLIENT_ID=None, EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None)):
                n12.execute(plan, lots, output)
            ledger = json.loads((output / 'run.json').read_bytes())
            self.assertEqual(actual, ['photos', 'research', 'alternate'] * 12)
            self.assertEqual(len(counted), 36)
            self.assertEqual(sum(Decimal(row['reserved_usd']) for row in ledger['attempts']), Decimal('3.42540'))
            self.assertEqual((ledger['status'], ledger['unsettled_reserve_usd']), ('AWAIT_INDEPENDENT_REVIEW', '0'))
            report = review_run(plan, n12.STAGE, capability, output)
            self.assertFalse(report['admitted'])
            self.assertFalse(report['actual_measurement_claims_requiring_review'])
            self.assertTrue(all(row['draft_status'] == 'HOLD' for row in report['stage_outcomes'].values()))
            predictions = json.loads((output / 'predictions.json').read_bytes())
            self.assertEqual({row['identity'] for row in predictions}, {kind})
            self.assertEqual({row['model'] for row in predictions}, {'FX-40'})
            self.assertEqual({row['material'] for row in predictions}, {'Aluminum'})
            for row in predictions:
                measurements = row['product_export']['measurements']
                self.assertEqual(measurements['weight_kg']['reference'][0]['value'], .225)
                self.assertEqual(measurements['length_cm']['reference'][0]['value'], 8)


if __name__ == '__main__':
    unittest.main()
