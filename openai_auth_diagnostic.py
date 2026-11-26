"""Check OpenAI credentials using metadata and a deliberately incomplete request."""

import json
import os
import re
from unittest.mock import patch

import requests

from ai_pilot import PILOT_MODELS


RESPONSES_URL = 'https://api.openai.com/v1/responses'
MODEL_ID = next(iter(PILOT_MODELS))
MODEL_URL = f'https://api.openai.com/v1/models/{MODEL_ID}'
SCOPES = frozenset({
    'api.responses.read', 'api.responses.write', 'api.model.read', 'api.models.read',
    'api.organization.read', 'api.projects.read',
})


def key_format(secret):
    stripped = secret.strip()
    return {
        'present': bool(secret),
        'nonblank': bool(stripped),
        'leading_whitespace': secret != secret.lstrip(),
        'trailing_whitespace': secret != secret.rstrip(),
        'internal_whitespace': any(char.isspace() for char in stripped),
        'line_break': '\r' in secret or '\n' in secret,
        'quotes': any(char in secret for char in '"\'“”‘’'),
        'bearer_prefix': bool(re.match(r'(?i)bearer\b', stripped)),
        'non_ascii_or_control': any(not 33 <= ord(char) < 127 and char != ' ' for char in secret),
        'openai_secret_key_prefix': stripped.startswith('sk-'),
    }


def check_app_request(secret):
    """Intercept the real application's request before it can contact the network."""
    checks = {'calls_intercepted': 0}

    def intercept(_session, method, url, **kwargs):
        checks['calls_intercepted'] += 1
        checks.update(
            responses_url_matches=url == RESPONSES_URL,
            post_method=method.upper() == 'POST',
            bearer_matches=kwargs.get('headers', {}).get('Authorization') == f'Bearer {secret}',
            holds_empty_body=kwargs.get('json') == {},
        )
        response = requests.Response()
        response.status_code = 400
        response._content = b'{}'
        return response

    with patch.object(requests.Session, 'request', intercept):
        import app

        checks['app_received_secret_unchanged'] = app.OPENAI_API_KEY == os.environ.get('OPENAI_API_KEY')
        try:
            app._p054_openai_request({}, timeout=5, usage_picker_id='_no_picker')
        except RuntimeError:
            if checks['calls_intercepted'] != 1:
                raise
    return checks


def safe_error(response):
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get('error') if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get('code')
    kind = error.get('type')
    # OpenAI puts credential fragments in error.message. Only report recognized facts.
    result = {'http_status': response.status_code}
    for name, value in (('error_code', code), ('error_type', kind)):
        result[name] = value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,47}', value) else None
    request_id = response.headers.get('x-request-id', '')
    if re.fullmatch(r'req_[a-zA-Z0-9]{4,96}', request_id):
        result['request_id'] = request_id
    message = str(error.get('message') or '').casefold()
    if code == 'invalid_api_key' or 'incorrect api key provided' in message:
        result['reason'] = 'invalid_api_key'
    elif 'missing scopes' in message:
        result['reason'] = 'missing_scopes'
        result['missing_scopes'] = sorted(SCOPES.intersection(re.findall(r'api\.[a-z.]+', message)))
    elif 'ip' in message and ('allowlist' in message or 'not authorized' in message):
        result['reason'] = 'ip_not_authorized'
    elif 'must be a member of an organization' in message:
        result['reason'] = 'organization_membership_required'
    elif response.status_code == 400 and (
        code == 'missing_required_parameter' or 'missing required parameter' in message
    ) and (error.get('param') in ('model', 'input') or "'model'" in message or "'input'" in message):
        result['reason'] = 'expected_schema_rejection'
        if error.get('param') in ('model', 'input'):
            result['missing_parameter'] = error['param']
    elif response.status_code >= 400:
        result['reason'] = 'provider_rejected_request'
    else:
        result['reason'] = 'model_metadata_received'
    return result


def probe(session, method, url, secret=None):
    headers = {'Authorization': f'Bearer {secret}'} if secret else {}
    try:
        response = session.request(method, url, headers=headers, allow_redirects=False,
                                   timeout=(4, 7), **({'json': {}} if method == 'POST' else {}))
    except requests.RequestException as exc:
        return {'reason': 'transport_error', 'error_type': type(exc).__name__}
    return safe_error(response)


def diagnose():
    secret = os.environ.get('OPENAI_API_KEY') or ''
    flags = key_format(secret)
    report = {'key_format': flags, 'application': check_app_request(secret), 'model': MODEL_ID}
    defects = ('leading_whitespace', 'trailing_whitespace', 'internal_whitespace', 'line_break',
               'quotes', 'bearer_prefix', 'non_ascii_or_control')
    if not flags['nonblank'] or any(flags[name] for name in defects):
        report['live_status'] = 'skipped_due_to_key_format'
        return report
    if report['application']['calls_intercepted'] != 1 or not all(report['application'].values()):
        report['live_status'] = 'skipped_due_to_application_request'
        return report
    with requests.Session() as session:
        # No input, model, tools, prior response, prompt, or photos in the POST.
        report['models_metadata'] = probe(session, 'GET', MODEL_URL, secret)
        report['responses_without_key'] = probe(session, 'POST', RESPONSES_URL)
        report['responses_with_key'] = probe(session, 'POST', RESPONSES_URL, secret)
    report['responses_write_evidence'] = (
        report['responses_without_key'].get('http_status') == 401
        and report['responses_with_key'].get('reason') == 'expected_schema_rejection'
    )
    report['live_status'] = 'completed'
    return report


if __name__ == '__main__':
    try:
        result = diagnose()
    except Exception as exc:
        # Some transports include the header or raw provider message in exceptions.
        print(json.dumps({'diagnostic_error_type': type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
