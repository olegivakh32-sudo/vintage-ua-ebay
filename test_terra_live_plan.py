from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests

from acceptance_stage import sha
from ai_pilot import PilotLimits, run
from s1_acceptance import StopRun, run_lots
from terra_live_plan import (API, CAP, LUNA, PRIOR_SPEND, TERRA, Live6Budget, Live6Continuation,
                             approved_environment, main, paid_checkpoint, preflight, sealed_photos, validate)
from test_blind_live_pilot import RECOGNITION, response
from test_mobile import m
from test_s1_acceptance import RESEARCH, VISUAL
from visual_protocol import VISUAL_TEXT


class TerraLive6Tests(unittest.TestCase):
    def test_archived_web_search_telemetry_and_missing_fields(self):
        with (ZipFile(RECOGNITION / 'lotnexo-n12-46069966723-evidence.zip') as archive,
              TemporaryDirectory() as temporary):
            budget = Live6Budget(Path(temporary) / 'results', 'a' * 40)
            ledger = json.loads(archive.read('results/run.json'))['attempts']
            searches = [row for row in ledger if row['step'] in {'research', 'alternate'} and 'response_file' in row]
            self.assertEqual(len(searches), 11)
            for attempt in searches:
                record = json.loads(archive.read('results/' + attempt['response_file']))
                count = attempt['input_tokens_counted']
                metered = budget.metered_usage(record, 'details', {}, count)
                self.assertEqual(Decimal(metered['metered_usd']), Decimal(attempt['metered_usd']))
                self.assertIn(metered['web_calls'], {1, 2})
                self.assertIsNone(budget.check_response(record, 'details', metered))
                if attempt['step'] == 'research':
                    # The archived Luna replies supply actual tool telemetry. Terra uses a different rate.
                    terra = record | {'model': TERRA}
                    metered = budget.metered_usage(terra, 'research', {}, count)
                    self.assertIsNone(budget.check_response(terra, 'research', metered | {'lot': 'TL-401'}))
            original = json.loads(archive.read('results/MV-301-research-response.json'))
            fallback = deepcopy(original)
            del fallback['tool_usage']['web_search']
            self.assertEqual(budget.metered_usage(fallback, 'details', {}, 7266)['web_calls'], 1)
            with self.assertRaisesRegex(RuntimeError, 'Unsettled search usage'):
                budget.metered_usage(fallback | {'output': None}, 'details', {}, 7266)
            with self.assertRaisesRegex(RuntimeError, 'Unsettled search usage'):
                budget.metered_usage(fallback | {'output': []}, 'details', {}, 7266)
            with self.assertRaisesRegex(RuntimeError, 'Unsettled search usage'):
                budget.metered_usage(fallback | {'status': 'incomplete'}, 'details', {}, 7266)
            with self.assertRaises(KeyError):
                budget.metered_usage({'model': LUNA, 'status': 'completed'}, 'details', {}, 7266)
            with self.assertRaisesRegex(RuntimeError, 'Unexpected provider model, tier or usage'):
                budget.metered_usage(original | {'tool_usage': {'web_search': {}}}, 'details', {}, 7266)
            incomplete = original | {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}
            row = budget.metered_usage(incomplete, 'details', {}, 7266) | {'lot': 'TL-401'}
            self.assertEqual(budget.check_response(incomplete, 'details', row), 'INCOMPLETE_RESPONSE')
            self.assertEqual(row['incomplete_reason'], 'max_output_tokens')

    def test_real_archived_terra_reply_passes_the_ledger_and_visual_handoff_offline(self):
        evidence = RECOGNITION / 'lotnexo-live6-46138015708-evidence.zip'
        self.assertEqual(sha(evidence), '0ba2677ae1ca8f01d6c2cf8941b3f1a215e258a676d4cb00d382767848c1f23f')
        with ZipFile(evidence) as archive:
            recorded = json.loads(archive.read('results/TL-401-photos-response.json'))
            attempt = json.loads(archive.read('results/run.json'))['attempts'][0]
        count = attempt['input_tokens_counted']
        urls = []

        def substitute(session, method, url, **kwargs):
            self.assertEqual(method, 'POST')
            urls.append(url)
            if url == API + '/responses/input_tokens':
                return response({'input_tokens': count})
            self.assertEqual(url, API + '/responses')
            return response(recorded)

        with (TemporaryDirectory() as temporary, patch.dict('sys.modules', {'app': m}),
              patch.object(m, 'OPENAI_API_KEY', 'offline-fixture'), patch.object(m, 'OPENAI_CREDITS_EXHAUSTED', False),
              patch.object(requests.Session, 'request', autospec=True, side_effect=substitute)):
            root = Path(temporary)
            corrupted = root / 'unexpected.zip'
            corrupted.write_bytes(b'altered checkpoint')
            with self.assertRaisesRegex(StopRun, 'PAID_ARCHIVE_CHECKSUM_MISMATCH'):
                paid_checkpoint(corrupted)
            photos = sealed_photos(root)['TL-401']
            budget = Live6Continuation(root / 'results', 'a' * 40)
            card = run({'lot': 'TL-401'}, photos, PilotLimits(CAP, vision_only=True), live_web=budget)
            self.assertEqual(card['stage_http'], {'visual': 200, 'research': None, 'draft': None})
            self.assertEqual(card['identity'], 'Decorative ewer')
            self.assertEqual(budget.attempts[0]['metered_usd'], '0.004749')
            self.assertEqual(urls, [])
            budget.state['free_input_count'] = {'lot': 'TL-401', 'step': 'research', 'input_tokens': 1000}
            research_only = Decimal(budget.progress()['next']['reserve_usd'])
            budget.cap = research_only
            self.assertEqual(budget.progress(), {'stop': 'APPROVED_BUDGET_EXCEEDED'})
            for field, value in [('total_tokens', 1), ('output_tokens_details', {'image_tokens': 1})]:
                changed = deepcopy(recorded)
                changed['tool_usage']['image_gen'][field] = value
                with self.assertRaisesRegex(RuntimeError, 'Unexpected provider model, tier or usage'):
                    budget.metered_usage(changed, 'photos', {}, count)

    def test_preflight_requires_valid_ebay_taxonomy_without_generation(self):
        calls = []
        ebay_status = 200

        def transport(session, method, url, **kwargs):
            method = method.upper()
            calls.append((method, url))
            self.assertFalse(kwargs['allow_redirects'])
            if method == 'GET' and url.startswith(API + '/models/'):
                return response({'id': url.rsplit('/', 1)[1]})
            if method == 'POST' and url == API + '/responses/input_tokens':
                return response({'input_tokens': 5})
            self.assertEqual((method, url), ('GET', 'https://api.ebay.com/commerce/taxonomy/v1/'
                                                   'get_default_category_tree_id'))
            self.assertEqual(kwargs['params'], {'marketplace_id': 'EBAY_US'})
            return response({'categoryTreeId': '0', 'categoryTreeVersion': '112'}, status=ebay_status)

        with (TemporaryDirectory() as temp, patch.object(requests.Session, 'request', autospec=True,
                                                          side_effect=transport)):
            out = Path(temp) / 'preflight.json'
            with patch.dict('os.environ', {}, clear=True):
                with self.assertRaisesRegex(StopRun, 'EBAY_TAXONOMY_ACCESS_UNAVAILABLE'):
                    preflight('fixture-openai-key', out, 'a' * 40)
            self.assertEqual(json.loads(out.read_text())['taxonomy']['taxonomy'], 'NOT_RUN')
            self.assertEqual(len(calls), 4)
            with patch.dict('os.environ', {'VINTAGE_EBAY_TAXONOMY_TOKEN': 'fixture-ebay-key'}, clear=True):
                ebay_status = 401
                with self.assertRaisesRegex(StopRun, 'EBAY_TAXONOMY_ACCESS_UNAVAILABLE'):
                    preflight('fixture-openai-key', out, 'a' * 40)
                self.assertEqual(json.loads(out.read_text())['taxonomy']['taxonomy'], 'REVIEW')
                ebay_status = 200
                preflight('fixture-openai-key', out, 'a' * 40)
            report = json.loads(out.read_text())
            self.assertEqual(report['status'], 'AUTHORIZED_UNMETERED')
            self.assertEqual(report['taxonomy']['tree_version'], '112')
            self.assertEqual(set(report['token_count']), {TERRA, LUNA})
            self.assertNotIn('fixture-', out.read_text())
            self.assertNotIn(('POST', API + '/responses'), calls)

    def test_stale_ebay_access_stops_before_the_first_model_request(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = validate() | {'status': 'AUTHORIZED_UNMETERED', 'revision': 'a' * 40,
                                   'budget_usd': str(CAP), 'token_count': {TERRA: 5, LUNA: 5},
                                   'taxonomy': {'taxonomy': 'OK', 'marketplace': 'EBAY_US',
                                                'tree_id': '0', 'tree_version': '112'}}
            (root / 'preflight.json').write_text(json.dumps(report))
            with (patch('sys.argv', ['terra_live_plan.py', '--execute', str(root / 'results')]),
                  patch('terra_live_plan.approved_environment', return_value=('a' * 40, 'fixture')),
                  patch('terra_live_plan.production_taxonomy_token', return_value='expired-ebay-token'),
                  patch('terra_live_plan.production_taxonomy', return_value={'taxonomy': 'REVIEW'}),
                  patch('terra_live_plan.run_lots') as execute):
                with self.assertRaisesRegex(StopRun, 'EBAY_TAXONOMY_RECHECK_FAILED'):
                    main()
                execute.assert_not_called()
                self.assertFalse((root / 'results').exists())

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

    def test_continuation_runs_guard_and_reopens_six_sqlite_drafts(self):
        kind = 'Camera mounting adapter'
        stages = []
        taxonomy = []
        token_counts = []

        def transporter(session, method, url, **kwargs):
            if method == 'GET':
                self.assertTrue(url.startswith('https://api.ebay.com/commerce/taxonomy/v1/'))
                self.assertFalse(kwargs['allow_redirects'])
                taxonomy.append(url)
                if url.endswith('/get_default_category_tree_id'):
                    return response({'categoryTreeId': '0', 'categoryTreeVersion': '112'})
                if url.endswith('/get_category_suggestions'):
                    ewer = 'ewer' in kwargs['params']['q'].lower()
                    return response({'categorySuggestions': [
                        {'category': {'categoryId': '30098' if ewer else '30097',
                                      'categoryName': 'Decorative Ewer' if ewer else kind}, 'categoryTreeNodeLevel': 2,
                         'categoryTreeNodeAncestors': [{'categoryName': 'Collectibles' if ewer else 'Cameras & Photo',
                                                        'categoryTreeNodeLevel': 1}]}]})
                self.assertTrue(url.endswith('/get_item_aspects_for_category'))
                return response({'aspects': [{'localizedAspectName': 'Type', 'aspectConstraint': {'aspectRequired': True}}]})
            self.assertEqual(method, 'POST')
            self.assertFalse(kwargs['allow_redirects'])
            if url == API + '/responses/input_tokens':
                token_counts.append(url)
                return response({'input_tokens': 1400})
            self.assertEqual(url, API + '/responses')
            wire = kwargs['json']
            self.assertEqual(wire['prompt_cache_options'], {'mode': 'explicit'})
            self.assertFalse(wire['store'])
            receipt = json.loads(budget.path.read_text())['attempts'][-1]
            self.assertEqual(receipt['status'], 'IN_FLIGHT')
            stages.append(receipt['step'])
            self.assertNotEqual((receipt['lot'], receipt['step']), ('TL-401', 'photos'))
            if receipt['step'] == 'photos':
                count = sum(part['type'] == 'input_image' for part in wire['input'][0]['content'])
                report = deepcopy(VISUAL)
                report['photos'] = [VISUAL['photos'][0] | {'photo': slot} for slot in range(1, count + 1)]
                report['scope']['components'] = [dict(role='PRODUCT', label=kind, photos=list(range(1, count + 1)),
                                                      quantity=1)]
                report['observations'][0]['value'] = kind
                report['facts'] = {'object_type': kind}
                if receipt['lot'] in {'TL-402', 'TL-403', 'TL-404'}:
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
            subject = 'Decorative ewer' if receipt['lot'] == 'TL-401' else kind
            return response(self.provider(wire, RESEARCH.replace('Fixture display stand', subject), searches=1))

        with (TemporaryDirectory() as tmp, patch.dict('sys.modules', {'app': m}),
              patch.object(m, 'OPENAI_API_KEY', 'fixture-only'), patch.object(m, 'OPENAI_CREDITS_EXHAUSTED', False),
              patch.object(requests.Session, 'request', autospec=True, side_effect=transporter)):
            root = Path(tmp)
            lots = sealed_photos(root)
            budget = Live6Continuation(root / 'results', 'a' * 40, taxonomy_token='fixture-only')
            run_lots(budget, lots, budget.path.parent)
            ledger = json.loads(budget.path.read_text())
            self.assertEqual(ledger['status'], 'AWAIT_INDEPENDENT_REVIEW')
            self.assertEqual(ledger['unsettled_reserve_usd'], '0')
            self.assertTrue(ledger['attempts'][0]['reused'])
            self.assertEqual(ledger['attempts'][0]['metered_usd'], str(PRIOR_SPEND))
            self.assertEqual(ledger['max_additional_usd'], '5.743031')
            self.assertEqual(stages, ['research'] + ['photos', 'research', 'details'] * 2 + ['photos', 'research'] * 3)
            self.assertEqual(len(token_counts), len(stages))
            predictions = json.loads((budget.path.parent / 'predictions.json').read_text())
            self.assertEqual([Decimal(str(row['cost_usd'])) for row in predictions],
                             [Decimal('0.016999')] + [Decimal('0.024725')] * 2 + [Decimal('0.01450')] * 3)
            self.assertTrue(taxonomy)
            for lot in lots:
                card = json.loads((budget.path.parent / f'{lot}-card.json').read_text())
                self.assertTrue(card['review_card']['hold_reopened'])
                draft = card['draft']
                self.assertIn('Decorative ewer' if lot == 'TL-401' else kind, draft['title'])
                self.assertTrue(draft['description'])
                self.assertEqual(draft['category_review']['status'], 'TAXONOMY_CHECKED')
                self.assertEqual(draft['compliance_review']['status'], 'REVIEW')
                self.assertIsNone(draft['market_valuation']['price'])
                self.assertEqual(draft['shipping_status'], 'NEEDS_VERIFIED_MEASUREMENTS')
                self.assertFalse(card['admitted'])

    def test_uncertain_search_retains_a_reserve_and_incomplete_visual_is_billed(self):
        self.assertEqual(CAP, Decimal('5.74778'))
        with TemporaryDirectory() as temp:
            for status in ('incomplete', 'timeout', 401, 429, 500):
                budget = Live6Budget(Path(temp) / str(status), 'a' * 40)
                payload = {'model': 'gpt-6.1-terra', 'reasoning': {'effort': 'low'}, 'max_output_tokens': 2000,
                           'text': VISUAL_TEXT, 'input': [{'role': 'user', 'content': [{'type': 'input_image'}]}]}
                wire = budget.bind('TL-401', payload)
                incomplete = self.provider(wire, VISUAL) | {'status': 'incomplete',
                                                            'incomplete_details': {'reason': 'max_output_tokens'}}
                final = (response(incomplete) if status == 'incomplete' else requests.Timeout() if status == 'timeout'
                         else response({'error': {'code': 'fixture'}}, status=status))
                post = Mock(side_effect=[response({'input_tokens': 2000}), final])
                with self.assertRaisesRegex(RuntimeError, 'INCOMPLETE_RESPONSE|TRANSPORT_UNCERTAIN|PROVIDER_HTTP_ERROR'):
                    budget.send(post, None, {'json': wire, 'headers': {}})
                ledger = json.loads(budget.path.read_text())
                if status == 'incomplete':
                    self.assertEqual(ledger['metered_usd'], '0.00225')
                    self.assertEqual(ledger['attempts'][0]['status'], 'LOT_FAILED')
                else:
                    self.assertEqual(Decimal(ledger['unsettled_reserve_usd']), Decimal('0.01200'))
                    self.assertEqual(ledger['status'], 'STOPPED')
                    if isinstance(status, int):
                        self.assertEqual(ledger['attempts'][0]['http_status'], status)
                self.assertEqual(post.call_count, 2)
