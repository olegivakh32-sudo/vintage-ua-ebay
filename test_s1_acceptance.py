"""Exercise the sealed S1 transport with synthetic responses and real blind inputs."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests

from blind_live_pilot import API, MODEL
from ebay_sandbox_probe import production_taxonomy
from s1_acceptance import APPROVAL_REF, CAP, PLAN_SHA, LotFailure, StageBudget, StopRun, authorize, execute, frozen_plan, main
from s1_review import review
from test_blind_live_pilot import RECOGNITION, response
from test_mobile import m
from visual_protocol import VISUAL_TEXT


PLAN = RECOGNITION / 'lotnexo-acceptance-stages-candidate.json'
ARCHIVE = RECOGNITION / 'lotnexo-s1-10-blind.zip'
ALGORITHM = 'aea6136a4a4767eb85c7497ee8884a016bdc1ebe'
KIND = 'Fixture display stand'
VISUAL = {
    'photos': [{'photo': 1, 'role': 'EBAY_PRODUCT', 'scene': 'GENERAL_PRODUCT', 'confidence': 98}],
    'scope': {'scope': 'ONE_PHYSICAL_ITEM', 'physical_item_count': 1, 'confidence': 99},
    'observations': [{'field': 'type', 'value': KIND, 'photos': [1], 'confidence': 98, 'basis': 'Visible outline'}],
    'facts': {'object_type': KIND},
    'physical': {'scale': None, 'dimension_readings': []},
    'summary': 'Synthetic shape only.',
}
RESEARCH = (
    '=== IDENTIFICATION ===\nIDENTITY_JSON {"type":"Fixture display stand","exact_model_confirmed":false}\n'
    'IDENTITY_CLAIMS_JSON {"claims":[]}\n=== MARKET ===\nPRICE_JSON {"normal":null}\n'
    'CATEGORY_CANDIDATE_JSON {"category_id":null}\nMARKET_COMPS_JSON {"comparables":[]}\n'
    'UA_MARKET_COMPS_JSON {"comparables":[]}'
)


def reply(**changes):
    return {'model': MODEL, 'service_tier': 'default', 'status': 'completed',
            'usage': {'input_tokens': 2500, 'output_tokens': 500},
            'output': [], 'output_text': json.dumps(VISUAL), **changes}


class SealedS1Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.plan = frozen_plan(PLAN, ARCHIVE, ALGORITHM)

    def budget(self, label='result'):
        budget = StageBudget(self.plan, self.root / label)
        wire = budget.bind('P001', {'input': 'fixture-only', 'text': VISUAL_TEXT})
        return budget, wire

    def test_archives_are_blind_and_share_exact_pixels(self):
        with ZipFile(ARCHIVE) as sealed, ZipFile(ARCHIVE.with_name('lotnexo-s1-chatgpt.zip')) as independent:
            lots = self.plan['stages']['S1']['lots']
            self.assertEqual(independent.namelist(), [f'{lot}.jpg' for lot in lots])
            for lot in lots:
                self.assertEqual(independent.read(f'{lot}.jpg'), sealed.read(f'{lot}/01.jpg'))
        with self.assertRaisesRegex(StopRun, 'FROZEN_INPUT_CHANGED'):
            frozen_plan(PLAN, ARCHIVE, '0' * 40)
        changed = self.root / 'changed.json'
        changed.write_bytes(PLAN.read_bytes() + b' ')
        with self.assertRaisesRegex(StopRun, 'FROZEN_PLAN_CHANGED'):
            frozen_plan(changed, ARCHIVE, ALGORITHM)

    def test_rejects_mutation_repeat_and_out_of_order_before_paid_call(self):
        budget, wire = self.budget()
        transport = Mock()
        with self.assertRaisesRegex(StopRun, 'BOUND_REQUEST_CHANGED'):
            budget.send(transport, None, {'json': wire | {'max_output_tokens': 10000}, 'headers': {}})
        transport.assert_not_called()
        with self.assertRaisesRegex(StopRun, 'BOUND_REQUEST_CHANGED'):
            budget.bind('P001', {'input': 'fixture', 'text': VISUAL_TEXT})
        with self.assertRaises(FileExistsError):
            StageBudget(self.plan, budget.path.parent)
        other = StageBudget(self.plan, self.root / 'out-of-order')
        with self.assertRaisesRegex(StopRun, 'DUPLICATE_OR_OUT_OF_ORDER'):
            other.bind('P002', {'input': 'fixture', 'text': VISUAL_TEXT})

    def test_execute_requires_protected_tag_first_attempt_and_reservation(self):
        args = ['s1_acceptance.py', str(PLAN), str(ARCHIVE), '--algorithm-revision', ALGORITHM,
                '--execute', str(self.root / 'result')]
        reserved = {'LOTNEXO_S1_RESERVED': PLAN_SHA, 'ENABLE_LIVE_PUBLISH': '0'}
        for env in ({},
                    reserved | {'GITHUB_REF': 'refs/tags/p094-lotnexo-s1-146950', 'GITHUB_RUN_ATTEMPT': '1'},
                    reserved | {'GITHUB_REF': APPROVAL_REF, 'GITHUB_RUN_ATTEMPT': '2'},
                    {'GITHUB_REF': APPROVAL_REF, 'GITHUB_RUN_ATTEMPT': '1'}):
            with (patch.dict('os.environ', env, clear=True), patch('sys.argv', args),
                  patch('builtins.print'), patch('s1_acceptance.authorize') as preflight):
                self.assertEqual(main(), 1)
                preflight.assert_not_called()
        approved = reserved | {'GITHUB_REF': APPROVAL_REF, 'GITHUB_RUN_ATTEMPT': '1',
                               'P094_PILOT_OPENAI_API_KEY': 'fixture-key'}
        with (patch.dict('os.environ', approved, clear=True), patch('sys.argv', args),
              patch('s1_acceptance.authorize') as preflight, patch('s1_acceptance.execute') as run_s1):
            self.assertEqual(main(), 0)
            preflight.assert_called_once_with('fixture-key')
            run_s1.assert_called_once_with(self.plan, ARCHIVE, self.root / 'result')
        with (patch('s1_acceptance.datetime') as clock,
              patch('s1_acceptance.free_preflight', return_value=({'model': MODEL}, False)) as no_access):
            clock.now.return_value.date.return_value = date(2026, 11, 27)
            report = self.root / 'preflight.json'
            with self.assertRaisesRegex(StopRun, 'MODEL_OR_WRITE_ACCESS_UNVERIFIED'):
                authorize('fixture-key', report)
            no_access.assert_called_once_with('fixture-key')
            self.assertEqual(json.loads(report.read_text()), {'model': MODEL, 'authorized': False})
            clock.now.return_value.date.return_value = date(2026, 12, 11)
            with self.assertRaisesRegex(StopRun, 'RATE_PROFILE_EXPIRED'):
                authorize('fixture-key')
            no_access.assert_called_once()

    def test_failed_free_count_and_missing_budget_do_not_send(self):
        cases = [
            (response({'input_tokens': 24001}), 'INPUT_TOKEN_LIMIT'),
            (response({'input_tokens': True}), 'INPUT_TOKEN_LIMIT'),
            (response({'input_tokens': 0}), 'INPUT_TOKEN_LIMIT'),
            (response([], 200), 'TOKEN_COUNT_UNAVAILABLE'),
            (response({}, 429), 'TOKEN_COUNT_HTTP'),
            (requests.Timeout(), 'TOKEN_COUNT_UNAVAILABLE'),
            (response({'input_tokens': 1200}), 'INSUFFICIENT_APPROVED_BUDGET'),
        ]
        for index, (count, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                budget, wire = self.budget(str(index))
                if reason == 'INSUFFICIENT_APPROVED_BUDGET':
                    budget.state['approved_usd'] = '1.46949'
                transport = Mock(side_effect=[count])
                with self.assertRaisesRegex(StopRun, reason):
                    budget.send(transport, None, {'json': wire, 'headers': {}})
                self.assertEqual(transport.call_count, 1)
                self.assertTrue(transport.call_args.args[-1].endswith('/input_tokens'))
                self.assertEqual(json.loads(budget.path.read_text())['unsettled_reserve_usd'], '0')

    def test_ledger_is_required_before_transport_and_keeps_incomplete_charges(self):
        budget, wire = self.budget()
        timeout_transport = Mock(return_value=response({'input_tokens': 1200}))
        with patch('s1_acceptance.durable_report', side_effect=OSError('simulated I/O failure')):
            with self.assertRaises(OSError):
                budget.send(timeout_transport, None, {'json': wire, 'headers': {}})
        self.assertEqual(timeout_transport.call_count, 1)

        budget, wire = self.budget('partial')
        count = response({'input_tokens': 1200})

        def transport(session, method, url, **kwargs):
            if url.endswith('/input_tokens'):
                return count
            saved = json.loads(budget.path.read_text())
            self.assertEqual(saved['unsettled_reserve_usd'], '0.0042')
            self.assertEqual(saved['attempts'][0]['status'], 'IN_FLIGHT')
            self.assertTrue(saved['attempts'][0]['request_sha256'])
            self.assertFalse(kwargs['allow_redirects'])
            return response(reply(status='incomplete', incomplete_details={'reason': 'max_output_tokens'}))

        with self.assertRaisesRegex(LotFailure, 'INCOMPLETE_RESPONSE'):
            budget.send(transport, None, {'json': wire, 'headers': {}})
        saved = json.loads(budget.path.read_text())
        self.assertGreater(Decimal(saved['metered_usd']), 0)
        self.assertEqual(saved['unsettled_reserve_usd'], '0')
        self.assertEqual(saved['attempts'][0]['status'], 'LOT_FAILED')
        self.assertTrue((budget.path.parent / 'P001-photos-response.json').exists())

    def test_http_network_usage_and_schema_failures_close_approval(self):
        cases = [
            (response({}, status), 'PROVIDER_HTTP_ERROR') for status in (302, 401, 429, 503)
        ] + [
            (requests.Timeout(), 'TRANSPORT_UNCERTAIN'),
            (response([], 200), 'INVALID_RESPONSE_JSON'),
            (response(reply(usage={'input_tokens': 24001, 'output_tokens': 2200})), 'UNEXPECTED_USAGE'),
            (response(reply(model='another-model')), 'UNEXPECTED_USAGE'),
            (response(reply(service_tier='priority')), 'UNEXPECTED_USAGE'),
        ]
        for index, (result, reason) in enumerate(cases):
            with self.subTest(reason=reason, index=index):
                budget, wire = self.budget(str(index))
                transport = Mock(side_effect=[response({'input_tokens': 1200}), result])
                with self.assertRaisesRegex(StopRun, reason):
                    budget.send(transport, None, {'json': wire, 'headers': {}})
                self.assertEqual(transport.call_count, 2)
                data = json.loads(budget.path.read_text())
                self.assertEqual(data['status'], 'STOPPED')
                self.assertTrue(Decimal(data['metered_usd']) + Decimal(data['unsettled_reserve_usd']) > 0)
                with self.assertRaises(StopRun):
                    budget.send(transport, None, {'json': wire})
                self.assertEqual(transport.call_count, 2)

    def test_interrupted_research_is_never_repeated_or_admitted(self):
        counted = response({'input_tokens': 2000})
        for number, (result, reason) in enumerate([
            (reply(output_text=RESEARCH), 'SEARCH_NOT_EXECUTED'),
            (reply(output=[{'type': 'web_search_call'}],
                   output_text=RESEARCH.replace('{"normal":null}', '{"normal":"unknown"}')), 'INVALID_RESEARCH_SCHEMA'),
            (reply(output=[{'type': 'web_search_call'}] * 3, output_text=RESEARCH), 'UNEXPECTED_USAGE'),
        ]):
            with self.subTest(reason=reason):
                budget, wire = self.budget(str(number))
                transport = Mock(side_effect=[counted, response(reply()), counted, response(result)])
                budget.send(transport, None, {'json': wire, 'headers': {}})
                research = budget.bind('P001', {'tools': [{'type': 'web_search'}], 'input': [
                    {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Generic fixture'}]},
                ]}) | {'include': ['web_search_call.action.sources']}
                expected_error = StopRun if reason == 'UNEXPECTED_USAGE' else LotFailure
                with self.assertRaisesRegex(expected_error, reason):
                    budget.send(transport, None, {'json': research, 'headers': {}})
                self.assertEqual(transport.call_count, 4)
                if reason == 'UNEXPECTED_USAGE':
                    with self.assertRaises(StopRun):
                        budget.bind('P002', {'input': 'fixture', 'text': VISUAL_TEXT})
                else:
                    self.assertEqual(budget.status, 'RUNNING')
                    budget.bind('P002', {'input': 'fixture', 'text': VISUAL_TEXT})
                self.assertEqual(transport.call_count, 4)

    def test_full_ten_lot_pipeline_stops_at_independent_review(self):
        sent = []
        counted = []

        def provider(session, method, url, **kwargs):
            self.assertEqual(method, 'POST')
            body = kwargs['json']
            if url == f'{API}/responses/input_tokens':
                counted.append(body)
                return response({'input_tokens': 2000})
            self.assertEqual(url, f'{API}/responses')
            sent.append(body)
            matching = {key: body[key] for key in counted[-1]}
            self.assertEqual(counted[-1], matching)
            self.assertEqual(body['reasoning']['effort'], 'low')
            self.assertFalse(body['store'])
            if body.get('tools'):
                self.assertEqual((body['max_output_tokens'], body['max_tool_calls']), (7000, 2))
                self.assertEqual(body['tools'][0]['search_context_size'], 'low')
                return response(reply(output=[{'type': 'web_search_call', 'action': {'sources': []}}],
                                      output_text=RESEARCH))
            self.assertEqual(body['max_output_tokens'], 2400)
            return response(reply())

        output = self.root / 'sealed'
        with (patch.object(requests.Session, 'request', side_effect=provider),
              patch.object(m, 'OPENAI_API_KEY', 'fixture-only'), patch.dict('sys.modules', {'app': m}),
              patch.multiple(m, EBAY_CLIENT_ID=None, EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None)):
            execute(self.plan, ARCHIVE, output)
        self.assertEqual((len(counted), len(sent)), (20, 20))
        run = json.loads((output / 'run.json').read_text())
        self.assertEqual(run['status'], 'AWAIT_INDEPENDENT_REVIEW')
        self.assertEqual(sum(Decimal(row['reserved_usd']) for row in run['attempts']), CAP)
        self.assertEqual(run['unsettled_reserve_usd'], '0')
        results = json.loads((output / 'predictions.json').read_text())
        self.assertEqual(len(results), 10)
        self.assertTrue(all(not row['admitted'] and row['category_id'] is None for row in results))
        card = json.loads((output / 'P001-card.json').read_text())
        self.assertTrue(card['draft']['hold_preview'])
        self.assertEqual(card['reference'], {})
        self.assertIsNone(card['draft']['market_valuation']['price'])
        self.assertNotIn('shipping_usd', card['draft'])
        baseline = self.root / 'independent.json'
        baseline.write_text(json.dumps([{'lot': row['lot'], 'identity': None, 'admitted': False} for row in results]))
        evaluation = review(PLAN, RECOGNITION / 'lotnexo-acceptance-56-enriched.json', output / 'predictions.json',
                            baseline)
        self.assertFalse(evaluation['preliminary_identity_threshold'])
        self.assertIsNone(evaluation['paired']['meets_chatgpt_baseline'])
        self.assertEqual(evaluation['strict_lotnexo']['fields']['identity']['labelled'], 10)


class TaxonomyAccessTests(unittest.TestCase):
    def test_production_uses_application_oauth_and_reads_only_taxonomy(self):
        transport = Mock()
        transport.post.return_value = response({'access_token': 'app-token-fixture'})
        transport.get.side_effect = [response({'categoryTreeId': '0', 'categoryTreeVersion': '101'}),
                                     response({'aspects': [{'localizedAspectName': 'Type',
                                                           'aspectConstraint': {'aspectRequired': True}}]})]
        config = {'VINTAGE_EBAY_TAXONOMY_CLIENT_ID': 'app-id',
                  'VINTAGE_EBAY_TAXONOMY_CLIENT_SECRET': 'fixture-not-for-logs'}
        result = production_taxonomy(connect=True, category_id='117042', config=config, transport=transport)
        self.assertEqual((result['taxonomy'], result['tree_version'], result['required_aspects']), ('OK', '101', ['Type']))
        self.assertEqual(transport.post.call_args.args[0], 'https://api.ebay.com/identity/v1/oauth2/token')
        self.assertEqual(transport.post.call_args.kwargs['data']['grant_type'], 'client_credentials')
        self.assertTrue(all('/commerce/taxonomy/' in call.args[0] for call in transport.get.call_args_list))
        self.assertNotIn('app-token-fixture', json.dumps(result))

    def test_manual_token_http_error_redacts_provider_content(self):
        transport = Mock()
        transport.get.return_value = response({'error': 'secret-value'}, 401)
        result = production_taxonomy(connect=True, config={'VINTAGE_EBAY_TAXONOMY_TOKEN': 'test-token'}, transport=transport)
        self.assertEqual((result['taxonomy'], result['error']), ('REVIEW', 'Taxonomy: HTTP 401'))
        transport.post.assert_not_called()
