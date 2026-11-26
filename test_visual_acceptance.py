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

from ai_pilot import PilotLimits, run, visual_evidence_diagnostics, visual_usage
from product_evidence import recognition_route
from test_mobile import m
from visual_acceptance import evaluate, main, outcome, REFERENCES
from visual_protocol import VISUAL_TEXT, machine_object, normalize_visual_analysis


MODEL = 'gpt-4o-mini-2024-07-18'


class VisualAcceptanceTests(unittest.TestCase):
    def test_legacy_fenced_and_keyed_json_checkpoint_blocks(self):
        roles = {'roles': {'1': 'EBAY_PRODUCT'}}
        tag = 'PHOTO_ROLES_JSON'
        for source in (
            f'**{tag}**: ```json\n{json.dumps(roles)}\n```',
            json.dumps({tag: roles, 'summary': 'Локальна перевірка'}),
        ):
            with self.subTest(source=source):
                analysis = normalize_visual_analysis(source, 1)
                self.assertEqual(m._photo_roles_from_analysis(analysis, 1), {1: 'EBAY_PRODUCT'})
        broken = 'AUTO_FACTS_JSON: {\nSCOPE_JSON: {"scope":"ONE_PHYSICAL_ITEM"}'
        self.assertEqual(machine_object(broken, 'AUTO_FACTS_JSON'), {})
        self.assertEqual(m._scope_from_analysis(broken)['physical_item_count'], 1)

    def test_invalid_structured_photo_numbers_and_observation_shapes_are_rejected(self):
        photos = [{'photo': 1, 'role': 'EBAY_PRODUCT', 'scene': 'FULL_SET', 'confidence': 96}]
        for report, count in (
            ({'photos': photos * 2, 'observations': []}, 2),
            ({'photos': photos, 'observations': {}}, 1),
            ({'photos': photos, 'observations': []}, 2),
        ):
            with self.subTest(report=report, count=count), self.assertRaises(ValueError):
                normalize_visual_analysis(json.dumps(report), count)

    def test_http_200_structured_visual_contract_survives_usage_guard_and_checkpoint(self):
        report = {
            'photos': [{'photo': 1, 'role': 'EBAY_PRODUCT', 'scene': 'FULL_SET', 'confidence': 97}],
            'observations': [
                {'field': field, 'value': value, 'photos': [source], 'basis': basis,
                 'confidence': 96, 'name': None}
                for field, value, source, basis in (
                    ('type', 'Electric Kettle', 1, 'Jug with heating base and on switch'),
                    ('brand', 'North', 1, 'Clear NORTH maker label'),
                    ('model', 'K400', 1, 'Clear K400 on the model label'),
                    ('material', 'Metal', 1, 'Exposed brushed metal body'),
                    ('defect', 'Scuff near handle', 1, 'Light worn patch near handle'),
                    ('country', 'Fictional', 2, 'Missing frame'),
                    ('period', '1990s', 1, ''),
                )
            ],
            'scope': {'scope': 'ONE_PHYSICAL_ITEM', 'physical_item_count': 1, 'confidence': 96, 'note': ''},
            'facts': {
                'object_type': 'Electric Kettle', 'brand': 'North', 'origin': '', 'confidence': 96,
                'materials': ['Metal'], 'materials_confidence': 96, 'material_evidence': 'Brushed body',
                'tested_status': 'Untested', 'completeness': 'Unknown', 'marking_present': True,
                'brand_evidence': 'NORTH on the label', 'symbols': [],
                'markings': [{'photo': 1, 'literal': 'NORTH K400', 'language': 'English', 'kind': 'MODEL_NUMBER'}],
            },
            'physical': {'scale': None, 'dimension_readings': []},
            'summary': 'Електричний чайник. Видима потертість біля ручки.',
        }
        raw = json.dumps(report, ensure_ascii=False)
        boundary = raw.index('K400') + 1  # Split inside a JSON string.
        body = {'status': 'completed', 'output_text': [], 'output': [
            None, {'type': 'reasoning'}, {'type': 'message', 'content': [
                {'type': 'output_text', 'text': raw[:boundary]},
                {'type': 'output_text', 'text': raw[boundary:]},
            ]},
        ]}
        result, payload = self._mock_visual_response(body)
        self.assertEqual(payload['text'], VISUAL_TEXT)
        self.assertEqual(payload['max_output_tokens'], 2000)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['input'][0]['content'][-1]['detail'], 'high')
        self.assertEqual(result['usage']['calls'], 1)
        self.assertEqual(result['stage_http']['visual'], 200)
        self.assertEqual(result['evidence_diagnostics']['observations_accepted'], 5)
        self.assertEqual([row['reasons'] for row in result['evidence_diagnostics']['field_decisions'][-2:]],
                         [['NO_VALID_PHOTO'], ['MISSING_OR_INVALID_BASIS']])
        self.assertIn('condition', result['evidence_diagnostics']['fields_not_observed'])
        self.assertEqual(result['photo_roles'], {1: 'EBAY_PRODUCT'})
        self.assertEqual(result['main_photo'], 1)
        for field, value in {'identity':'Electric Kettle', 'brand':'North', 'model':'K400',
                             'material':'Metal', 'defects':'Scuff near handle', 'ocr':'NORTH K400'}.items():
            self.assertEqual(result[field], value)
        self.assertIsNone(result['country'])
        self.assertIsNone(result['period'])

    def test_incomplete_and_refused_http_200_keep_usage_without_retry(self):
        for status, kind, issue in (('incomplete', 'output_text', 'incomplete_max_output_tokens'),
                                    ('completed', 'refusal', 'refused')):
            with self.subTest(status=status, kind=kind):
                report, _ = self._mock_visual_response({
                    'status': status, 'incomplete_details': {'reason': 'max_output_tokens'},
                    'output': [{'type': 'message', 'content': [{'type': kind, 'text': '{"photos": ['}]}],
                })
                self.assertEqual(report['request_metrics']['http_status'], 200)
                self.assertEqual(report['request_metrics']['response_issue'], issue)
                self.assertEqual(report['usage']['output_tokens'], 20)
                self.assertEqual(report['stage_http']['visual'], 502)
                self.assertEqual(report['evidence_diagnostics']['status'], 'NO_CACHED_ANALYSIS')
                self.assertFalse(report['product_evidence'])

    def _mock_visual_response(self, body):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(dict(body, model=MODEL, usage={
            'input_tokens': 4000, 'output_tokens': 20,
        })).encode()
        with TemporaryDirectory() as tmp:
            image = Path(tmp) / 'frame.jpg'
            Image.new('RGB', (320, 320), 'tan').save(image)
            limits = PilotLimits(Decimal('.0204'), vision_only=True)
            with (patch.dict('sys.modules', {'app': m}), patch.object(m, 'OPENAI_API_KEY', 'fixture'),
                  patch.object(m, 'OPENAI_CREDITS_EXHAUSTED', False),
                  patch.object(requests.Session, 'request', return_value=response) as transport):
                report = run({'lot': 'fixture'}, [image], limits)
            transport.assert_called_once()
            self.assertFalse(transport.call_args.kwargs['allow_redirects'])
        return report, transport.call_args.kwargs['json']

    def test_diagnostics_distinguish_missing_unreadable_and_rejected_evidence(self):
        marker = 'PRODUCT_OBSERVATIONS_JSON:'
        for analysis, observations, evidence, expected in (
            ('AUTO_FACTS_JSON: {}', {}, {}, 'MISSING_BLOCK'),
            (marker + ' {', {}, {}, 'UNREADABLE_BLOCK'),
            (marker + ' {"fields": []}', {'fields': []}, {}, 'EMPTY_BLOCK'),
            (marker, {'fields': [{'confidence': 60}]}, {}, 'ALL_OBSERVATIONS_REJECTED'),
            (marker, {'fields': [{'confidence': 95}]}, {'type': [{'status': 'PHOTO_OBSERVATION'}]}, 'EVIDENCE_ACCEPTED'),
        ):
            with self.subTest(expected=expected):
                result = visual_evidence_diagnostics(analysis, observations, evidence, 'teapot')
                self.assertEqual(result['status'], expected)

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
        response['usage']['input_tokens_details'] = {'cached_tokens': 1000}
        self.assertEqual(Decimal(visual_usage(response, MODEL)['metered_cost_usd']), Decimal('.020325'))
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
                                  stage_http={'visual': 200},
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

    def test_metered_abstention_continues_but_failed_visual_stage_stops(self):
        for visual_status, expected_calls in ((200, 3), (409, 1)):
            with self.subTest(visual_status=visual_status), TemporaryDirectory() as tmp:
                self._check_metered_abstention(tmp, visual_status, expected_calls)

    def _check_metered_abstention(self, tmp, visual_status, expected_calls):
        root = Path(tmp)
        cases = [dict(lot=f'A0{i}', manifest=root / f'input-{i}.json',
                      report=root / f'report-{i}.json', data={}, photos=[],
                      labels={'identity': ['teapot']}, reference_id=str(i), license='CC0',
                      photo_source='', fact_source='', offline='UNRESOLVED',
                      offline_elapsed_ms=1000) for i in range(1, 4)]
        first = {'mode': 'APPROVED_VISION_ONLY', 'product_evidence': {},
                 'stage_http': {'visual': visual_status},
                 'request_metrics': {'sent': True, 'http_status': 200, 'standard_cost_usd': '0.002',
                                     'response_status': 'completed'}}

        def send(_manifest, report, _data, _photos, _limits):
            report.write_text(json.dumps(first))
            return first

        with (patch('visual_acceptance.cohort', return_value=cases),
              patch('visual_acceptance.execute_pilot', side_effect=send) as execute,
              patch('visual_acceptance.datetime') as clock,
              patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture', 'APPROVE_AI_PILOT': '1'})):
            clock.now.return_value = datetime(2026, 11, 26, tzinfo=timezone.utc)
            result = evaluate(root, execute=True)
        self.assertEqual([call.args[0] for call in execute.call_args_list],
                         [case['manifest'] for case in cases[:expected_calls]])
        self.assertEqual(result['cases'][0]['visual_stage_http'], visual_status)
        self.assertEqual(result['cases'][1]['vision'], 'NO_RESULT' if visual_status == 200 else 'NOT_RUN')
