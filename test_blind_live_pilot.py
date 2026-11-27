import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from ai_pilot import PilotLimits, run
from blind_live_pilot import (
    API, BASE_TOKENS, CAP, CONTINUATION_TOTAL, FULL_RESERVE, MODEL, LiveBudget,
    free_preflight, frozen_inputs, meter, read_paid_checkpoint,
)
from test_mobile import m
from visual_protocol import VISUAL_TEXT


RECOGNITION = Path(__file__).parent / 'test_artifacts' / 'recognition'


def response(data, status=200):
    reply = requests.Response()
    reply.status_code = status
    reply._content = json.dumps(data).encode()
    return reply


class BlindPilotTests(unittest.TestCase):
    def test_paid_checkpoint_reaches_hold_without_transport_or_shipping_prefill(self):
        prior = read_paid_checkpoint(RECOGNITION / 'lotnexo-paid-45888499679.zip')
        self.assertEqual(prior['previous_usd'], Decimal('0.0254528'))
        usage = meter(prior['research'], 'research')
        self.assertEqual((usage['output_tokens'], usage['reasoning_tokens'], usage['web_calls']), (4375, 3454, 2))
        self.assertEqual(usage['metered_usd'], '0.0248227')
        self.assertEqual(prior['previous_usd'] + CONTINUATION_TOTAL, Decimal('0.4621028'))
        with (TemporaryDirectory() as tmp, patch.object(requests.Session, 'request') as transport,
              patch.object(m, 'OPENAI_API_KEY', 'offline-fixture'), patch.dict('sys.modules', {'app': m})):
            directory = Path(tmp)
            photos = frozen_inputs(RECOGNITION / 'lotnexo-live-pilot-blind.zip', directory)['MV-101']
            budget = LiveBudget(directory / 'run.json', checkpoint=prior, offline=True)
            result = run({'lot': 'MV-101'}, photos, PilotLimits(CAP), live_web=budget)
            transport.assert_not_called()
            self.assertEqual(result['stage_http'], {'visual': 200, 'research': None, 'draft': None})
            self.assertEqual(result['ocr'], '1998.257')
            self.assertFalse(result['admitted'])
            self.assertEqual(result['measurement'], {})
            self.assertEqual(result['usage'], [{'stage': 'photos', 'sent': False, 'reused': True, 'metered_usd': '0'}])
            card = result['review_card']
            self.assertEqual(card['draft_status'], 'NO_DRAFT')
            self.assertEqual(card['unverified_category'], {})
            self.assertEqual(len(card['reference_decisions']), 8)
            self.assertEqual({row['status'] for row in card['reference_decisions']}, {'HYPOTHESIS'})
            fields = {field: row['value'] for field, rows in card['evidence'].items()
                      for row in rows if row['status'] == 'CATALOGUE_REFERENCE'}
            self.assertEqual(fields, {'type': 'Plate', 'material': 'salt-glazed stoneware', 'country': 'England',
                                      'period': 'c. 1760', 'length_cm': 30.5, 'width_cm': 30.5, 'height_cm': 4.3})
            with self.assertRaisesRegex(ValueError, 'images differ'):
                budget.prepare_catalogue('MV-101', photos[::-1], m)

    def test_continuation_sends_only_market_request_and_keeps_partial_usage(self):
        prior = read_paid_checkpoint(RECOGNITION / 'lotnexo-paid-45888499679.zip')
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            photos = frozen_inputs(RECOGNITION / 'lotnexo-live-pilot-blind.zip', directory)['MV-101']
            budget = LiveBudget(directory / 'run.json', checkpoint=prior)
            budget.prepare_catalogue('MV-101', photos, m)
            request = Mock(side_effect=[response({'input_tokens': 900}), response(prior['research'])])
            ask = {'input': 'already billed', 'text': VISUAL_TEXT}
            bound = budget.bind('MV-101', ask)
            self.assertEqual(budget.send(request, None, {'json': bound}).status_code, 200)
            request.assert_not_called()
            ask = {'tools': [{'type': 'web_search'}], 'input': [{'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'verbose prompt'},
                {'type': 'input_image', 'image_url': 'data:fixture'},
            ]}]}
            bound = budget.bind('MV-101', ask)
            self.assertEqual(bound['max_output_tokens'], 7000)
            self.assertEqual(bound['reasoning']['effort'], 'low')
            self.assertEqual(bound['tools'][0]['search_context_size'], 'low')
            self.assertEqual(bound['max_tool_calls'], 2)
            content = bound['input'][0]['content']
            self.assertEqual(len(content), 1)
            self.assertIn('Research only missing market comparables', content[0]['text'])
            self.assertNotIn('verbose prompt', content[0]['text'])
            wire = bound | {'include': ['web_search_call.action.sources']}
            with self.assertRaisesRegex(RuntimeError, 'no automatic retry'):
                budget.send(request, None, {'json': wire, 'headers': {}})
            self.assertEqual(request.call_count, 2)
            report = json.loads(budget.report.read_text())
            self.assertEqual(report['metered_usd'], '0.0248227')
            self.assertEqual(report['unsettled_reserve_usd'], '0')
            research = report['attempts'][1]
            self.assertEqual(research['incomplete_reason'], 'max_output_tokens')
            self.assertEqual(json.loads((directory / research['response_file']).read_text()), prior['research'])
            with self.assertRaisesRegex(RuntimeError, 'Repeated'):
                budget.bind('MV-102', {'input': 'second lot', 'text': VISUAL_TEXT})

    def test_meter_uses_billable_search_count_for_partial_responses(self):
        result = {'model': MODEL, 'service_tier': 'default', 'status': 'incomplete',
                  'usage': {'input_tokens': 26352, 'output_tokens': 4375},
                  'tool_usage': {'web_search': {'num_requests': 2}},
                  'output': [{'type': 'web_search_call', 'status': state}
                             for state in ('completed', 'completed', 'searching')]}
        measured = meter(result, 'research')
        self.assertEqual(measured['web_calls'], 2)
        self.assertEqual(measured['metered_usd'], '0.0248227')
        result['tool_usage']['web_search']['num_requests'] = 3
        with self.assertRaisesRegex(RuntimeError, 'Unexpected provider'):
            meter(result, 'research')

    def test_free_preflight_only_counts_when_model_access_is_confirmed(self):
        empty = response({'error': {'code': 'missing_required_parameter', 'param': 'model'}}, 400)
        with (patch.object(requests.Session, 'get', return_value=response({'id': MODEL})) as metadata,
              patch('requests.post', side_effect=[empty, response({'input_tokens': 7})]) as post):
            report, ready = free_preflight('fixture-only')
            self.assertTrue(ready)
            self.assertEqual(report['token_count']['input_tokens'], 7)
            self.assertEqual(metadata.call_args.args, (f'{API}/models/{MODEL}',))
            self.assertEqual(post.call_args_list[0].args, (f'{API}/responses',))
            self.assertEqual(post.call_args_list[0].kwargs['json'], {})
            self.assertEqual(post.call_args_list[1].args, (f'{API}/responses/input_tokens',))
            post.reset_mock()
            metadata.return_value = response({'error': {'code': 'invalid_api_key'}}, 401)
            self.assertFalse(free_preflight('fixture-only')[1])
            post.assert_not_called()

    def test_token_count_failure_cannot_generate_or_retry(self):
        with TemporaryDirectory() as tmp:
            budget = LiveBudget(Path(tmp) / 'report.json')
            request = Mock(return_value=response({'input_tokens': BASE_TOKENS + 1}))
            ask = {'input': 'fixture', 'text': VISUAL_TEXT}
            payload = budget.bind('MV-101', ask)
            with self.assertRaisesRegex(RuntimeError, 'no automatic retry'):
                budget.send(request, None, {'json': payload, 'headers': {}})
            request.assert_called_once()
            self.assertEqual(request.call_args.args[2], f'{API}/responses/input_tokens')
            self.assertFalse(budget.attempts[0]['sent'])
            with self.assertRaisesRegex(RuntimeError, 'Repeated'):
                budget.bind('MV-101', ask)
            self.assertLess(FULL_RESERVE, CAP)

    def test_live_search_transport_feeds_card_without_removing_shipping_guard(self):
        kind = 'Product under evaluation'
        visual = {'photos': [{'photo': i, 'role': 'EBAY_PRODUCT', 'scene': 'GENERAL_PRODUCT', 'confidence': 97}
                             for i in (1, 2)],
                  'scope': {'scope': 'ONE_PHYSICAL_ITEM', 'physical_item_count': 1, 'confidence': 99},
                  'observations': [{'field': 'type', 'value': kind, 'photos': [1], 'confidence': 99, 'basis': 'Photo'}],
                  'facts': {'object_type': kind}}
        urls = [{'url': 'https://manufacturer.example/item'}, {'url': 'https://archive.example/item'}]
        claim = {'field': 'type', 'value': kind, 'photos': [1], 'sources': [
            {**source, 'match': 'EXACT', 'kind': 'CATALOG', 'document_id': str(idx), 'excerpt': kind}
            for idx, source in enumerate(urls)
        ]}

        def provider(_session, method, url, **kwargs):
            self.assertEqual(method, 'POST')
            if url == f'{API}/responses/input_tokens':
                return response({'input_tokens': 1200})
            self.assertEqual(url, f'{API}/responses')
            sent = kwargs['json']
            research = bool(sent.get('tools'))
            self.assertEqual(sent['model'], MODEL)
            if research:
                self.assertEqual(sent['max_tool_calls'], 2)
                self.assertEqual(sent['tool_choice'], 'required')
            output = [{'type': 'web_search_call', 'action': {'sources': urls}}] if research else []
            text = ('=== IDENTIFICATION ===\nIDENTITY_JSON ' + json.dumps({'type': kind})
                    + '\nIDENTITY_CLAIMS_JSON ' + json.dumps({'claims': [claim]})
                    + '\n=== MARKET ===\nPRICE_JSON {}') if research else json.dumps(visual)
            return response({'model': MODEL, 'service_tier': 'default', 'status': 'completed',
                             'usage': {'input_tokens': 5000, 'output_tokens': 500},
                             'output': output, 'output_text': text})

        root = Path(__file__).with_name('test_artifacts') / 'vision-pilot'
        with (TemporaryDirectory() as tmp, patch.object(requests.Session, 'request', side_effect=provider) as transport,
              patch.object(m, 'OPENAI_API_KEY', 'fixture-only'), patch.dict('sys.modules', {'app': m})):
            budget = LiveBudget(Path(tmp) / 'report.json')
            result = run({'lot': 'MV-101'}, [root / 'A01.jpg', root / 'A02.jpg'], PilotLimits(CAP), live_web=budget)
            raw_path = Path(tmp) / budget.attempts[1]['response_file']
            raw = json.loads(raw_path.read_text())
            self.assertEqual(raw['output'][0]['action']['sources'], urls)
            self.assertIn('IDENTITY_CLAIMS_JSON', raw['output_text'])
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(result['stage_http'], {'visual': 200, 'research': 200, 'draft': 200})
        self.assertEqual(result['review_card']['draft_status'], 'HOLD')
        rows = result['review_card']['evidence']['type']
        self.assertIn('CORROBORATED_REFERENCE', {row['status'] for row in rows})
        self.assertTrue(result['draft']['hold_preview'])
        self.assertNotIn('shipping_usd', result['draft'])
        self.assertFalse(result['admitted'])
