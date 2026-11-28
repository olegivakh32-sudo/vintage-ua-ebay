from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from s1_acceptance import StopRun, run_lots
from terra_live_plan import API, CAP, Live6Budget, approved_environment, sealed_photos, validate
from test_blind_live_pilot import response
from test_mobile import m
from test_s1_acceptance import RESEARCH, VISUAL
from visual_protocol import VISUAL_TEXT


class TerraLive6Tests(unittest.TestCase):
    def test_guard_blocks_unsigned_over_budget_and_duplicate_requests(self):
        self.assertEqual(Decimal(validate()['max_run_usd']), Decimal('5.74778'))
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(StopRun):
            approved_environment(execute=True)
        original = {'model': 'gpt-6.1-terra', 'reasoning': {'effort': 'low'}, 'max_output_tokens': 2000,
                    'text': VISUAL_TEXT,
                    'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'fixture'}]}]}
        with TemporaryDirectory() as temp:
            for mode, error in [('oversized', 'TOKEN_COUNT_OUTSIDE_APPROVAL'), ('cap', 'APPROVED_BUDGET_EXCEEDED'),
                                ('mutation', 'BOUND_REQUEST_CHANGED'), ('duplicate', 'DUPLICATE_OR_OUT_OF_ORDER')]:
                budget = Live6Budget(Path(temp) / mode, 'a' * 40)
                wire = budget.bind('TL-401', original)
                counter = Mock(return_value=response({'input_tokens': 9000 if mode == 'oversized' else 1000}))
                if mode == 'cap':
                    budget.cap = Decimal('0.00001')
                if mode == 'mutation':
                    wire['input'][0]['content'][0]['image_url'] = 'different'
                kwargs = {'headers': {}, 'json': wire}
                if mode == 'duplicate':
                    counter.side_effect = [response({'input_tokens': 1000}), response(self.provider(wire, VISUAL))]
                    budget.send(counter, None, kwargs)
                    with self.assertRaisesRegex(StopRun, error):
                        budget.bind('TL-401', original)
                    self.assertEqual(counter.call_count, 2)
                    continue
                with self.assertRaisesRegex(StopRun, error):
                    budget.send(counter, None, kwargs)
                self.assertEqual(counter.call_count, int(mode != 'mutation'))

    @staticmethod
    def provider(wire, body, searches=0):
        return {'status': 'completed', 'model': wire['model'], 'service_tier': 'default',
                'usage': {'input_tokens': 1000, 'output_tokens': 250},
                'tool_usage': {'web_search': {'num_requests': searches}} if searches else {},
                'output': [{'type': 'web_search_call', 'status': 'completed',
                            'action': {'sources': [{'url': 'https://manual.example/item'}]}}] * searches,
                'output_text': body if isinstance(body, str) else json.dumps(body)}

    def test_photo_only_batch_runs_guard_and_reopens_six_sqlite_drafts(self):
        kind = 'Camera mounting adapter'
        stages = []
        taxonomy = []

        def transporter(session, method, url, **kwargs):
            if method == 'GET':
                self.assertTrue(url.startswith('https://api.ebay.com/commerce/taxonomy/v1/'))
                self.assertFalse(kwargs['allow_redirects'])
                taxonomy.append(url)
                if url.endswith('/get_default_category_tree_id'):
                    return response({'categoryTreeId': '0', 'categoryTreeVersion': '112'})
                if url.endswith('/get_category_suggestions'):
                    return response({'categorySuggestions': [
                        {'category': {'categoryId': '30097', 'categoryName': kind}, 'categoryTreeNodeLevel': 2,
                         'categoryTreeNodeAncestors': [{'categoryName': 'Cameras & Photo', 'categoryTreeNodeLevel': 1}]}]})
                self.assertTrue(url.endswith('/get_item_aspects_for_category'))
                return response({'aspects': [{'localizedAspectName': 'Type', 'aspectConstraint': {'aspectRequired': True}}]})
            self.assertEqual(method, 'POST')
            self.assertFalse(kwargs['allow_redirects'])
            if url == API + '/responses/input_tokens':
                return response({'input_tokens': 1400})
            self.assertEqual(url, API + '/responses')
            wire = kwargs['json']
            self.assertEqual(wire['prompt_cache_options'], {'mode': 'explicit'})
            self.assertFalse(wire['store'])
            receipt = json.loads(budget.path.read_text())['attempts'][-1]
            self.assertEqual(receipt['status'], 'IN_FLIGHT')
            stages.append(receipt['step'])
            if receipt['step'] == 'photos':
                count = sum(part['type'] == 'input_image' for part in wire['input'][0]['content'])
                report = deepcopy(VISUAL)
                report['photos'] = [VISUAL['photos'][0] | {'photo': slot} for slot in range(1, count + 1)]
                report['scope']['components'] = [dict(role='PRODUCT', label=kind, photos=list(range(1, count + 1)),
                                                      quantity=1)]
                report['observations'][0]['value'] = kind
                report['facts'] = {'object_type': kind}
                if receipt['lot'] in {'TL-401', 'TL-402', 'TL-403'}:
                    report['observations'].extend(dict(field=field, value=value, photos=[1], confidence=99,
                                                       basis='Shown clearly on the product')
                                                  for field, value in [('brand', 'Fixture'), ('model', 'XY-42')])
                    report['facts']['markings'] = [{'photo': 1, 'literal': 'XY-42', 'kind': 'MODEL_NUMBER'}]
                return response(self.provider(wire, report))
            if receipt['step'] == 'details':
                self.assertEqual(wire['reasoning'], {'effort': 'medium'})
                return response(self.provider(wire, 'IDENTITY_CLAIMS_JSON {"claims":[]}', searches=1))
            self.assertEqual(receipt['step'], 'research')
            self.assertTrue(wire['tools'][0]['external_web_access'])
            prompt = wire['input'][0]['content'][0]['text']
            self.assertNotIn('nikonusa.com', prompt.lower())
            self.assertNotIn('playstation.com', prompt.lower())
            self.assertIn('Do not repeat', prompt)
            return response(self.provider(wire, RESEARCH.replace('Fixture display stand', kind), searches=1))

        with (TemporaryDirectory() as tmp, patch.dict('sys.modules', {'app': m}),
              patch.object(m, 'OPENAI_API_KEY', 'fixture-only'), patch.object(m, 'OPENAI_CREDITS_EXHAUSTED', False),
              patch.object(requests.Session, 'request', autospec=True, side_effect=transporter)):
            root = Path(tmp)
            lots = sealed_photos(root)
            budget = Live6Budget(root / 'results', 'a' * 40, taxonomy_token='fixture-only')
            run_lots(budget, lots, budget.path.parent)
            run = json.loads(budget.path.read_text())
            self.assertEqual(run['status'], 'AWAIT_INDEPENDENT_REVIEW')
            self.assertEqual(run['unsettled_reserve_usd'], '0')
            self.assertEqual(stages, ['photos', 'research', 'details'] * 2 + ['photos', 'research'] * 4)
            self.assertTrue(taxonomy)
            for lot in lots:
                card = json.loads((budget.path.parent / f'{lot}-card.json').read_text())
                self.assertTrue(card['review_card']['hold_reopened'])
                draft = card['draft']
                self.assertIn(kind, draft['title'])
                self.assertTrue(draft['description'])
                self.assertEqual(draft['category_review']['status'], 'TAXONOMY_CHECKED')
                self.assertEqual(draft['compliance_review']['status'], 'REVIEW')
                self.assertIsNone(draft['market_valuation']['price'])
                self.assertEqual(draft['shipping_status'], 'NEEDS_VERIFIED_MEASUREMENTS')
                self.assertFalse(card['admitted'])

    def test_uncertain_search_retains_a_reserve_and_incomplete_visual_is_billed(self):
        self.assertEqual(CAP, Decimal('5.74778'))
        with TemporaryDirectory() as temp:
            for status in ('incomplete', 'timeout'):
                budget = Live6Budget(Path(temp) / status, 'a' * 40)
                payload = {'model': 'gpt-6.1-terra', 'reasoning': {'effort': 'low'}, 'max_output_tokens': 2000,
                           'text': VISUAL_TEXT, 'input': [{'role': 'user', 'content': [{'type': 'input_image'}]}]}
                wire = budget.bind('TL-401', payload)
                incomplete = self.provider(wire, VISUAL) | {'status': 'incomplete',
                                                            'incomplete_details': {'reason': 'max_output_tokens'}}
                post = Mock(side_effect=[response({'input_tokens': 2000}), response(incomplete) if status == 'incomplete'
                                         else requests.Timeout()])
                with self.assertRaisesRegex(RuntimeError, 'INCOMPLETE_RESPONSE|TRANSPORT_UNCERTAIN'):
                    budget.send(post, None, {'json': wire, 'headers': {}})
                ledger = json.loads(budget.path.read_text())
                if status == 'incomplete':
                    self.assertEqual(ledger['metered_usd'], '0.00225')
                    self.assertEqual(ledger['attempts'][0]['status'], 'LOT_FAILED')
                else:
                    self.assertEqual(Decimal(ledger['unsettled_reserve_usd']), Decimal('0.01200'))
                    self.assertEqual(ledger['status'], 'STOPPED')
                self.assertEqual(post.call_count, 2)
