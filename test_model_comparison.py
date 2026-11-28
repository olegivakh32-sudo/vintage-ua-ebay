"""Exercise the paid runner with an intercepting transport."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

import requests

from model_comparison import CAP, StopComparison, execute, preflight, request_plan, shared_inputs, verify_plan
from model_comparison_review import candidate_for, review


def reply(status_code, **body):
    result = Mock(spec=requests.Response)
    result.status_code = status_code
    result.json.return_value = body
    result.headers = {}
    return result


class ModelComparisonTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = request_plan(shared_inputs())

    def test_paired_payloads_and_absolute_cap(self):
        self.assertEqual(len(self.plan), 96)
        self.assertLess(verify_plan(self.plan), CAP)
        modified = deepcopy(self.plan)
        modified[next(iter(modified))]['tools'] = [{'type': 'web_search'}]
        with self.assertRaisesRegex(StopComparison, 'UNBOUNDED_REQUEST'):
            verify_plan(modified)
        modified = deepcopy(self.plan)
        case = next(iter(modified))
        modified[case]['input'] = deepcopy(modified[case]['input'])
        modified[case]['input'][0]['content'][0]['text'] += 'different text'
        with self.assertRaisesRegex(StopComparison, 'UNPAIRED_INPUT'):
            verify_plan(modified)

    def test_preflight_verifies_all_96_counts_before_any_generation(self):
        session = Mock()
        def free(method, url, **kwargs):
            if '/models/' in url:
                return reply(200, id=url.split('/')[-1])
            if url.endswith('/input_tokens'):
                self.assertIn(kwargs['json']['reasoning']['effort'], {'low', 'medium'})
                return reply(200, input_tokens=3500)
            self.assertEqual(kwargs['json'], {})
            return reply(400, error={'code': 'missing_required_parameter', 'param': 'model'})
        session.request.side_effect = free

        with TemporaryDirectory() as directory:
            target = Path(directory)
            ready = preflight(session, 'sk-mock', self.plan, target / 'preflight.json')
            self.assertEqual(session.request.call_count, 99)
            self.assertEqual(len(ready['counts']), 96)

            session.request.side_effect = requests.Timeout
            count = session.request.call_count
            with self.assertRaisesRegex(StopComparison, 'TRANSPORT_UNCERTAIN'):
                execute(session, 'sk-mock', self.plan, ready, target / 'results')
            self.assertEqual(session.request.call_count, count + 1)
            ledger = (target / 'results' / 'ledger.json').read_text()
            self.assertIn('unsettled_reserve_usd', ledger)
            self.assertIn('TRANSPORT_UNCERTAIN', ledger)

            tampered = deepcopy(ready)
            next(iter(tampered['counts'].values()))['request_sha256'] = 'changed'
            with self.assertRaisesRegex(StopComparison, 'PREFLIGHT_MISMATCH'):
                execute(session, 'sk-mock', self.plan, tampered, target / 'tampered')
            self.assertEqual(session.request.call_count, count + 1)

    def test_successful_mock_run_reconciles_all_arms_with_the_separate_evaluator(self):
        session = Mock()
        def provider(method, url, **kwargs):
            body = kwargs['json']
            if '/models/' in url:
                return reply(200, id=url.split('/')[-1])
            if url.endswith('/input_tokens'):
                return reply(200, input_tokens=3400)
            if not body:
                return reply(400, error={'code': 'missing_required_parameter', 'param': 'model'})
            output = {'identity': None, 'brand': None, 'model': None, 'markings': []}
            return reply(200, model=body['model'], status='completed', service_tier='default', usage={
                'input_tokens': 3400, 'output_tokens': 150, 'input_tokens_details': {},
            }, output=[{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(output)}]}])

        session.request.side_effect = provider
        with TemporaryDirectory() as directory:
            target = Path(directory)
            ready = preflight(session, 'sk-mock', self.plan, target / 'preflight.json')
            execute(session, 'sk-mock', self.plan, ready, target / 'results')
            report = review(target / 'results')
            verified = review(target / 'results', verified_references=True)
        self.assertEqual(report['reference_mode'], 'frozen')
        self.assertEqual(verified['reference_mode'], 'verified')
        field = verified['arms']['luna-low']['visual']['lots']['MV-312']['model']
        self.assertEqual(field['status'], 'UNKNOWN')
        self.assertIn('DMG-01', field['reference'])
        present = verified['known_primary_source_in_common_candidates']
        self.assertEqual({lot for lot in present if present[lot]}, {'MV-306', 'MV-307', 'MV-309', 'MV-311', 'MV-312'})
        self.assertEqual(report['status'], 'COMPLETED')
        self.assertEqual(report['unsettled_reserve_usd'], '0')
        self.assertEqual(session.request.call_count, 99 + 96)
        for by_phase in report['arms'].values():
            for phase in by_phase.values():
                self.assertEqual(phase['attempts'], 12)
                self.assertEqual(phase['fields']['identity']['unknown'], 12)
                self.assertEqual(phase['markings']['eligible'], 5)

    def test_source_url_must_be_an_unambiguous_frozen_candidate(self):
        record = {'id': 'R1', 'url': 'https://museum.example/art/1915.495'}
        candidates = {'R1': record}
        self.assertEqual(candidate_for('R1', candidates), record)
        self.assertEqual(candidate_for(record['url'], candidates), record)
        self.assertIsNone(candidate_for('https://museum.example/art/1915.496', candidates))
        self.assertIsNone(candidate_for(record['url'], candidates | {'R2': record}))

    def test_over_limit_count_ends_preflight_at_first_payload(self):
        session = Mock()
        session.request.side_effect = [
            reply(200, id='gpt-6-luna'), reply(200, id='gpt-6.1-terra'),
            reply(400, error={'code': 'missing_required_parameter', 'param': 'model'}),
            reply(200, input_tokens=6001),
        ]
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(StopComparison, 'TOKEN_COUNT_OUTSIDE_APPROVAL'):
                preflight(session, 'sk-mock', self.plan, Path(directory) / 'preflight.json')
        self.assertEqual(session.request.call_count, 4)
