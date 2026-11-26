"""Regression tests for the isolated, non-generating authentication probe."""

from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

from openai_auth_diagnostic import RESPONSES_URL, check_app_request, key_format, main, probe, safe_error


class AuthDiagnosticTests(unittest.TestCase):
    def test_detects_wrappers_without_disclosing_input(self):
        raw = ' "Bearer sk-fixture"\r\n'
        flags = key_format(raw)
        for name in ('leading_whitespace', 'trailing_whitespace', 'line_break', 'quotes', 'internal_whitespace'):
            self.assertTrue(flags[name])
        self.assertTrue(key_format('Bearer sk-fixture')['bearer_prefix'])
        self.assertNotIn('sk-fixture', json.dumps(flags))

    def test_intercepts_the_actual_app_authorization_header(self):
        with (patch('app.OPENAI_API_KEY', 'sk-fixture'),
              patch.dict('os.environ', {'OPENAI_API_KEY': 'sk-fixture'})):
            result = check_app_request('sk-fixture')
        self.assertEqual(result['calls_intercepted'], 1)
        self.assertTrue(all(result.values()))

    def test_provider_error_never_reports_raw_message_or_key(self):
        response = Mock(status_code=401, headers={'x-request-id': 'req_example123'})
        response.json.return_value = {'error': {'message': 'Incorrect API key provided: sk-fixture',
                                               'code': 'invalid_api_key', 'type': 'invalid_request_error'}}
        error = safe_error(response)
        self.assertEqual(error['error_code'], 'invalid_api_key')
        self.assertNotIn('sk-fixture', json.dumps(error))
        response.json.return_value = {'error': {'message': 'Missing scopes: api.responses.write',
                                               'type': 'invalid_request_error'}}
        self.assertEqual(safe_error(response)['missing_scopes'], ['api.responses.write'])

    def test_responses_probe_has_no_model_or_input(self):
        session = Mock()
        session.request.return_value = Mock(status_code=400, headers={})
        session.request.return_value.json.return_value = {
            'error': {'code': 'missing_required_parameter', 'param': 'model'},
        }
        result = probe(session, 'POST', RESPONSES_URL, 'sk-fixture')
        session.request.assert_called_once_with(
            'POST', RESPONSES_URL, headers={'Authorization': 'Bearer sk-fixture'},
            allow_redirects=False, timeout=(4, 7), json={},
        )
        self.assertEqual(result['reason'], 'expected_schema_rejection')

    def test_paid_precondition_requires_responses_write_evidence(self):
        with (patch('sys.argv', ['openai_auth_diagnostic.py', '--require-responses-write']),
              redirect_stdout(StringIO())):
            for evidence in (False, True):
                with patch('openai_auth_diagnostic.diagnose', return_value={'responses_write_evidence': evidence}):
                    self.assertEqual(main(), int(not evidence))
