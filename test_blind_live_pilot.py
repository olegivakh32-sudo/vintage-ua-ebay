import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from ai_pilot import PilotLimits, run
from blind_live_pilot import API, BASE_TOKENS, CAP, FULL_RESERVE, MODEL, LiveBudget
from test_mobile import m
from visual_protocol import VISUAL_TEXT


def response(data, status=200):
    reply = requests.Response()
    reply.status_code = status
    reply._content = json.dumps(data).encode()
    return reply


class BlindPilotTests(unittest.TestCase):
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
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(result['stage_http'], {'visual': 200, 'research': 200, 'draft': 409})
        self.assertEqual(result['review_card']['draft_status'], 'HOLD')
        rows = result['review_card']['evidence']['type']
        self.assertIn('CORROBORATED_REFERENCE', {row['status'] for row in rows})
        self.assertEqual(result['draft'], {})
