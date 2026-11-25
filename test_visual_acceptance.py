from datetime import datetime, timezone
from decimal import Decimal
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
import requests

from ai_pilot import PilotLimits, run, visual_usage
from product_evidence import recognition_route
from test_mobile import m
from visual_acceptance import evaluate, main, outcome, REFERENCES


MODEL = 'gpt-4o-mini-2024-07-18'


class VisualAcceptanceTests(unittest.TestCase):
    def test_execution_reports_partial_run_but_exits_unsuccessfully(self):
        report = {
            'requests_sent': 1, 'priced_requests': 0, 'standard_cost_usd': None,
            'cases': [{'vision': 'NO_RESULT'}, {'vision': 'NOT_RUN'}, {'vision': 'NOT_RUN'}],
        }
        stdout = StringIO()
        with (patch('sys.argv', ['visual_acceptance.py', '/tmp/fixture', '--execute']),
              patch('visual_acceptance.evaluate', return_value=report),
              redirect_stdout(stdout), redirect_stderr(StringIO())):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), report)

    def test_public_category_requires_independent_type_and_exact_model(self):
        expected = REFERENCES['A02']
        case = dict(lot='A02', category=expected['category'], labels=expected,
                    reference_id='RP-PICO2', offline='UNRESOLVED', offline_elapsed_ms=1661,
                    license='CC BY-SA 4.0', photo_source='', fact_source='')
        result = dict(mode='APPROVED_VISION_ONLY', identity='Microcontroller board',
                      model='Raspberry Pi Pico 2', product_evidence={'type': [{'value': 'microcontroller board'}]})
        row = outcome(case, result)
        self.assertEqual(row['comparison']['category_id']['status'], 'PUBLIC_PATH_MATCH')
        self.assertEqual(row['comparison']['category_id']['value'], '65507')
        self.assertFalse(row['admitted'])
        result['model'] += ' W'
        self.assertEqual(outcome(case, result)['comparison']['category_id']['status'], 'PENDING_IDENTIFICATION')

    def test_reported_tokens_are_priced_at_standard_rate(self):
        response = {'model': MODEL, 'service_tier': 'default',
                    'usage': {'input_tokens': 128000, 'output_tokens': 2000}}
        self.assertEqual(Decimal(visual_usage(response, MODEL)['standard_cost_usd']), Decimal('.0204'))
        for changed in ({'usage': {}}, {'model': 'different'}, {'service_tier': 'priority'},
                        {'output': [{'type': 'web_search_call'}]}):
            with self.subTest(changed=changed):
                self.assertEqual(visual_usage(dict(response, **changed), MODEL), {})

    def test_combined_routing_requires_a_product_frame_and_keeps_conflicts_for_review(self):
        frame = [{'role': 'EBAY_PRODUCT'}]
        labels = [{'status': 'CATALOGUE_CANDIDATE'}]
        self.assertEqual(recognition_route(frame, {'status': 'NONE'}), 'VISUAL_REVIEW')
        self.assertEqual(recognition_route(frame, {'status': 'UNIQUE'}), 'SEARCH_IDENTIFIER')
        self.assertEqual(recognition_route(frame, {'status': 'UNIQUE'}, labels), 'VERIFY_CATALOGUE')
        self.assertEqual(recognition_route(frame, {'status': 'CONFLICT'}, labels), 'REVIEW_CONFLICT')
        self.assertEqual(recognition_route([{'role': 'EVIDENCE_ONLY'}], {'status': 'UNIQUE'}),
                         'REVIEW_PHOTOS')

    def test_visual_http_reports_metered_use_even_if_no_observation_is_usable(self):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'model': MODEL, 'usage': {'input_tokens': 4000, 'output_tokens': 20},
                                        'output': []}).encode()
        with TemporaryDirectory() as tmp:
            image = Path(tmp) / 'frame.jpg'
            preview = BytesIO()
            Image.new('RGB', (320, 320), 'tan').save(preview, format='JPEG')
            image.write_bytes(preview.getvalue())
            limits = PilotLimits(Decimal('.0204'), vision_only=True)
            events = []
            limits.on_reserve = lambda: events.append(dict(limits.response_metrics or {}))
            with (patch.dict('sys.modules', {'app': m}), patch.object(m, 'OPENAI_API_KEY', 'fixture'),
                  patch.object(m, 'OPENAI_CREDITS_EXHAUSTED', False),
                  patch.object(requests.Session, 'request', return_value=response) as transport):
                report = run({'lot': 'A01'}, [image], limits)
        transport.assert_called_once()
        self.assertFalse(transport.call_args.kwargs['allow_redirects'])
        request = transport.call_args.kwargs['json']
        self.assertEqual(request['model'], MODEL)
        self.assertNotIn('tools', request)
        self.assertEqual(events[1], {'sent': True})
        metrics = report['request_metrics']
        self.assertEqual(metrics['http_status'], 200)
        self.assertEqual(Decimal(metrics['standard_cost_usd']), Decimal('.000612'))
        self.assertGreaterEqual(metrics['elapsed_ms'], 0)
        self.assertFalse(report['product_evidence'])

    def test_aggregate_reserves_once_and_stops_after_an_uncertain_charge(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = []
            for i in range(1, 4):
                manifest = root / str(i) / 'manifest.json'
                manifest.parent.mkdir()
                cases.append(dict(lot=f'A0{i}', manifest=manifest, report=manifest.with_name('report.json'),
                                  data={}, photos=[], labels={'identity': ['teapot']}, reference_id=str(i),
                                  license='CC0', photo_source='', fact_source='', offline='UNRESOLVED',
                                  offline_elapsed_ms=1000))

            def send(manifest, report, data, photos, limits):
                self.assertTrue((root / '.visual-acceptance-0612.lock').is_file())
                self.assertTrue(limits.vision_only)
                self.assertEqual(limits.budget_usd, Decimal('.0204'))
                result = {'request_metrics': {'sent': True}}
                if manifest.parent.name == '1':
                    result = dict(mode='APPROVED_VISION_ONLY', identity='Teapot', material='plastic',
                                  product_evidence={'type': [{'value': 'Teapot'}]},
                                  request_metrics={'sent': True, 'standard_cost_usd': '0.002'})
                report.write_text(json.dumps(result))
                return result

            with (patch('visual_acceptance.cohort', return_value=cases),
                  patch('visual_acceptance.execute_pilot', side_effect=send) as execute,
                  patch('visual_acceptance.datetime') as clock,
                  patch.dict('os.environ', {'OPENAI_API_KEY': '', 'APPROVE_AI_PILOT': '1'})):
                clock.now.return_value = datetime(2026, 11, 25, tzinfo=timezone.utc)
                dry = evaluate(root)
                self.assertEqual((dry['requests_sent'], dry['standard_cost_usd']), (0, '0'))
                with self.assertRaisesRegex(ValueError, 'ключ'):
                    evaluate(root, execute=True)
                self.assertFalse((root / '.visual-acceptance-0612.lock').exists())
                with patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture'}):
                    result = evaluate(root, execute=True)
                    with self.assertRaisesRegex(ValueError, 'Повторний'):
                        evaluate(root, execute=True)
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(result['requests_sent'], 2)
            self.assertEqual(result['priced_requests'], 1)
            self.assertIsNone(result['standard_cost_usd'])
            first, second, third = result['cases']
            self.assertEqual(first['comparison']['identity']['status'], 'MATCH')
            self.assertEqual(first['comparison']['material']['status'], 'REQUIRES_INDEPENDENT_REVIEW')
            self.assertEqual(first['comparison']['category_id']['status'], 'OUT_OF_SCOPE')
            self.assertEqual(second['vision'], 'NO_RESULT')
            self.assertEqual(third['vision'], 'NOT_RUN')
            self.assertFalse(any(row['admitted'] for row in result['cases']))
