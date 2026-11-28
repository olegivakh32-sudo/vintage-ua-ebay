"""Quote or run the sealed Live 6 photo-only pilot in its protected environment."""
import argparse
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import os
import re
from tempfile import TemporaryDirectory
from pathlib import Path
from zipfile import ZipFile

import requests

from acceptance_stage import sha
from blind_live_pilot import API, SEARCH_TOKENS, SHORT_THRESHOLD, WEB_PRICE, model_metadata
from ebay_sandbox_probe import production_taxonomy, production_taxonomy_token
from multiview_acceptance import ARTIFACTS, blind_inputs
from recognition_research import DETAILS_RESEARCH_PREFIX
from s1_acceptance import RATES_EXPIRE, LotFailure, StageBudget, StopRun, durable_report, run_lots
from visual_protocol import VISUAL_TEXT, machine_object


ARCHIVE = ARTIFACTS / 'lotnexo-terra-live6-blind.zip'
REFERENCE = ARTIFACTS / 'lotnexo-terra-live6-reference.json'
ARCHIVE_SHA = '3a088e9789aeb3315c82384c00256231cd6de2bd696bee5c3667269de0abf1a1'
REFERENCE_SHA = '3e64c8f6b7c96c8c38663e172d58088faf501df8bd9f395c697ef741d3f9c024'
PHOTO_COUNTS = {f'TL-{lot}': 1 if lot <= 404 else 2 for lot in range(401, 407)}
TAG = 'refs/tags/p094-lotnexo-live6'
CONTINUATION_TAG = TAG + '-continuation'
PRIOR_RUN = 46138015708
PRIOR_REVISION = '0bdd73e3d94a0d79886a0cf3435aeb2f5f4cedda'
PRIOR_ARCHIVE = ARTIFACTS / f'lotnexo-live6-{PRIOR_RUN}-evidence.zip'
PRIOR_SHA = '0ba2677ae1ca8f01d6c2cf8941b3f1a215e258a676d4cb00d382767848c1f23f'
PRIOR_SPEND = Decimal('0.004749')
PAID_STEP = ('TL-401', 'photos')
REPOSITORY = 'olegivakh32-sudo/vintage-ua-ebay'
TERRA = 'gpt-6.1-terra'
LUNA = 'gpt-6-luna'
MAX_SUPPLEMENTS = 2
STEPS = {
    'photos': (TERRA, 'low', 8000, 2000, 0),
    'research': (TERRA, 'low', 10000, 5000, 2),
    'details': (LUNA, 'medium', 6000, 2600, 2),
}
RATES = {TERRA: (Decimal('1.00'), Decimal('5.00')), LUNA: (Decimal('0.10'), Decimal('0.50'))}


def billable_input(count, output, searches):
    # Keep the conservative envelope used by the earlier live pilots.
    return (searches + 1) * (count + SEARCH_TOKENS) + searches * output if searches else count


def unbilled_image_usage(usage):
    """Responses may include zero image-generation counters when no image tool was used."""
    counters = {'input_tokens', 'output_tokens', 'total_tokens'}
    breakdowns = {'input_tokens_details', 'output_tokens_details'}
    if not isinstance(usage, dict) or not counters <= usage.keys() <= counters | breakdowns:
        return False
    for key in breakdowns & usage.keys():
        breakdown = usage[key]
        if (not isinstance(breakdown, dict) or breakdown.keys() - {'image_tokens', 'text_tokens'}
                or any(type(value) is not int or value != 0 for value in breakdown.values())):
            return False
    return all(type(usage[key]) is int and usage[key] == 0 for key in counters)


def cost(model, inputs, outputs, searches):
    input_rate, output_rate = RATES[model]
    if inputs > SHORT_THRESHOLD:
        input_rate *= 2
        output_rate *= Decimal('1.5')
    return (inputs * input_rate + outputs * output_rate) / 1_000_000 + searches * WEB_PRICE


def quote():
    limits = {}
    for step, (model, effort, base, max_output, searches) in STEPS.items():
        max_input = billable_input(base, max_output, searches)
        reserve = cost(model, max_input, max_output, searches)
        max_calls = MAX_SUPPLEMENTS if step == 'details' else len(PHOTO_COUNTS)
        limits[step] = dict(model=model, effort=effort, max_output_tokens=max_output, max_tool_calls=searches,
                            max_base_input_tokens=base, max_input_tokens=max_input, max_billable_requests=max_calls,
                            reserve_per_request_usd=format(reserve, '.5f'))
    total = sum(row['max_billable_requests'] * Decimal(row['reserve_per_request_usd']) for row in limits.values())
    photos = len(PHOTO_COUNTS) * cost(TERRA, 3000, 900, 0)
    one_search = cost(TERRA, 16500, 1100, 1)
    two_searches = cost(TERRA, 24000, 1500, 2)
    low = photos + 6 * one_search
    typical = photos + 4 * one_search + 2 * two_searches + cost(LUNA, 16500, 700, 1)
    high = photos + 6 * two_searches + 2 * cost(LUNA, 24000, 900, 2)
    return {'photo_counts': PHOTO_COUNTS, 'steps': limits, 'max_run_usd': format(total, '.5f'),
            'estimated_usd': {'low': str(low), 'typical': str(typical), 'high': str(high)}}


CAP = Decimal(quote()['max_run_usd'])


def paid_checkpoint(path=PRIOR_ARCHIVE):
    if sha(path) != PRIOR_SHA:
        raise StopRun('PAID_ARCHIVE_CHECKSUM_MISMATCH')
    with ZipFile(path) as archive:
        prior = json.loads(archive.read('results/run.json'))
        completed = json.loads(archive.read('results/TL-401-photos-response.json'))
    attempts = prior['attempts']
    if (prior['algorithm_revision'] != PRIOR_REVISION or prior['blind_sha256'] != ARCHIVE_SHA
            or prior['reference_sha256'] != REFERENCE_SHA or prior['status'] != 'STOPPED'
            or prior['reason'] != 'UNEXPECTED_USAGE' or len(attempts) != 1
            or (attempts[0]['lot'], attempts[0]['step']) != PAID_STEP or attempts[0]['http_status'] != 200
            or completed['status'] != 'completed'):
        raise StopRun('PAID_CHECKPOINT_INVALID')
    return attempts[0], completed


def sealed_photos(root):
    if sha(ARCHIVE) != ARCHIVE_SHA:
        raise StopRun('LIVE6_FROZEN_PHOTOS_CHANGED')
    return blind_inputs(ARCHIVE, {'lots': list(PHOTO_COUNTS), 'photo_counts': PHOTO_COUNTS}, root)


def validate():
    with TemporaryDirectory() as root:
        sealed_photos(Path(root))
    if sha(REFERENCE) != REFERENCE_SHA:
        raise StopRun('LIVE6_FROZEN_REFERENCE_CHANGED')
    # Reference access is limited to offline acceptance preparation. It is absent from the photo package.
    rows = json.loads(REFERENCE.read_bytes())
    if ([(row['lot'], len(row['photos'])) for row in rows] != list(PHOTO_COUNTS.items())
            or len({row['reference_id'] for row in rows}) != len(PHOTO_COUNTS)):
        raise StopRun('LIVE6_REFERENCE_MISMATCH')
    photo_digests = {}
    for row in rows:
        for slot, photo in enumerate(row['photos'], 1):
            photo_digests[f"{row['lot']}/{slot:02}.jpg"] = photo['sha256']
    with ZipFile(ARCHIVE) as package:
        manifest = json.loads(package.read('photos.json'))
    if photo_digests != {row['file']: row['sha256'] for row in manifest}:
        raise StopRun('LIVE6_REFERENCE_PHOTO_MISMATCH')
    return {'status': 'OFFLINE_VALIDATED', 'blind_sha256': ARCHIVE_SHA,
            'reference_sha256': REFERENCE_SHA} | quote()


class Live6Budget(StageBudget):
    verify_public_catalogue = True
    verify_hold_reopen = True

    def __init__(self, destination, revision, taxonomy_token=None):
        phase = {'lots': list(PHOTO_COUNTS), 'photo_counts': PHOTO_COUNTS, 'blind_sha256': ARCHIVE_SHA}
        self.taxonomy_token = taxonomy_token
        super().__init__({'stages': {'LIVE6': phase}, 'algorithm_revision': revision,
                          'reference_sha256': REFERENCE_SHA}, destination, stage='LIVE6', cap=CAP)
        self.sequence = [(lot, step) for lot in PHOTO_COUNTS for step in STEPS]
        self.state['authenticated_model'] = TERRA
        self.state['models'] = sorted(RATES)
        self.state['taxonomy_mode'] = 'APP_TOKEN' if taxonomy_token else 'UNAVAILABLE'
        self.save()

    def live_environment(self):
        return {'VINTAGE_RESEARCH_ROUTE': 'paired-20261128', 'VINTAGE_VERIFIED_SALES_FILE': '',
                'VINTAGE_RESEARCH_DETAILS': '1' if self.details_used() < MAX_SUPPLEMENTS else '0',
                'VINTAGE_RESEARCH_ALTERNATE': '0'}

    def details_used(self):
        return sum(row['step'] == 'details' and row['status'] != 'SKIPPED' for row in self.attempts)

    def finish_lot(self, lot):
        index = len(self.attempts)
        if index < len(self.sequence) and self.sequence[index] == (lot, 'details'):
            reason = ('OPTIONAL_LIMIT_REACHED' if self.details_used() >= MAX_SUPPLEMENTS else 'OPTIONAL_SEARCH_NOT_NEEDED')
            self.attempts.append({'lot': lot, 'step': 'details', 'status': 'SKIPPED', 'reason': reason})
            self.save()

    def bind(self, lot, original):
        if self.status != 'RUNNING' or self.pending:
            self.stop('REQUEST_ALREADY_BOUND_OR_RUN_CLOSED')
        content = original['input'][0]['content']
        if original.get('text') == VISUAL_TEXT and not original.get('tools'):
            step = 'photos'
        elif original.get('tools') == [{'type': 'web_search'}]:
            step = 'details' if content[0]['text'].startswith(DETAILS_RESEARCH_PREFIX) else 'research'
        else:
            self.stop('UNAPPROVED_STAGE')
        index = len(self.attempts)
        if index >= len(self.sequence) or self.sequence[index] != (lot, step):
            self.stop('DUPLICATE_OR_OUT_OF_ORDER')
        model, effort, _, output, searches = STEPS[step]
        images = sum(part.get('type') == 'input_image' for part in content)
        if (original.get('model') != model or original.get('reasoning') != {'effort': effort}
                or original.get('max_output_tokens') != output or original.get('max_tool_calls', 0) != searches
                or len(original['input']) != 1 or original['input'][0].get('role') != 'user'
                or not 1 <= images <= (1 if step == 'details' else PHOTO_COUNTS[lot])
                or (step == 'photos' and images != PHOTO_COUNTS[lot])
                or (step == 'details' and self.details_used() >= MAX_SUPPLEMENTS)):
            self.stop('UNAPPROVED_REQUEST_SHAPE')
        payload = {'model': model, 'reasoning': {'effort': effort}, 'input': original['input'],
                   'max_output_tokens': output, 'store': False, 'truncation': 'disabled',
                   'service_tier': 'default', 'prompt_cache_options': {'mode': 'explicit'}}
        if searches:
            payload.update(tools=[{'type': 'web_search', 'external_web_access': True}],
                           tool_choice='required', max_tool_calls=searches)
        else:
            payload['text'] = VISUAL_TEXT
        self.state.pop('free_input_count', None)
        # Do not share nested input objects with the request the application will send.
        self.pending = lot, step, json.loads(json.dumps(payload))
        return payload

    def progress(self):
        if self.status != 'RUNNING' or any(row['status'] not in {'COMPLETE', 'LOT_FAILED', 'SKIPPED'}
                                           for row in self.attempts):
            return {'stop': 'UNSETTLED_OR_CLOSED_RUN'}
        index = len(self.attempts)
        if index == len(self.sequence):
            return {'stop': 'AWAIT_INDEPENDENT_REVIEW'}
        lot, step = self.sequence[index]
        count = self.state.get('free_input_count', {})
        model, _, base, output, searches = STEPS[step]
        size = count.get('input_tokens')
        if (count.get('lot') != lot or count.get('step') != step or type(size) is not int or not 0 < size <= base):
            return {'stop': 'TOKEN_COUNT_OUTSIDE_APPROVAL'}
        reserve = cost(model, billable_input(size, output, searches), output, searches)
        spent = sum(Decimal(row.get('metered_usd', '0')) for row in self.attempts)
        if spent + reserve > self.cap:
            return {'stop': 'APPROVED_BUDGET_EXCEEDED'}
        return {'next': {'reserve_usd': str(reserve)}}

    def reject_gate(self, reason, lot, step):
        count = self.state.get('free_input_count') or {}
        size = count.get('input_tokens')
        if (reason == 'TOKEN_COUNT_OUTSIDE_APPROVAL' and step == 'details'
                and (count.get('lot'), count.get('step')) == (lot, step)
                and type(size) is int and size > STEPS[step][2]):
            reason = 'OPTIONAL_TOKEN_COUNT_OUTSIDE_APPROVAL'
            self.attempts.append({'lot': lot, 'step': step, 'status': 'SKIPPED', 'reason': reason,
                                  'input_tokens_counted': size})
            self.save()
            raise LotFailure(reason)
        super().reject_gate(reason, lot, step)

    def metered_usage(self, result, step, payload, counted):
        usage = result['usage']
        inputs, outputs = usage['input_tokens'], usage['output_tokens']
        details = usage.get('input_tokens_details') or {}
        tools = result.get('tool_usage') or {}
        web_usage = tools.get('web_search')
        output_items = result.get('output')
        model, _, _, output, limit = STEPS[step]
        if web_usage is not None:
            searches = web_usage.get('num_requests')
        else:
            if limit and (result.get('status') != 'completed' or not isinstance(output_items, list)):
                raise RuntimeError('Unsettled search usage')
            search_items = [item for item in (output_items or []) if item.get('type') == 'web_search_call']
            if limit and (not search_items or any(item.get('status') != 'completed' for item in search_items)):
                raise RuntimeError('Unsettled search usage')
            searches = len(search_items)
        if (result.get('model') != model or result.get('service_tier') != 'default'
                or tools.keys() - {'web_search', 'image_gen'}
                or ('image_gen' in tools and not unbilled_image_usage(tools['image_gen']))
                or details.get('cached_tokens', 0) != 0 or details.get('cache_write_tokens', 0) != 0
                or any(type(value) is not int or value < 0 for value in (inputs, outputs, searches))
                or not 0 < inputs <= billable_input(counted, output, limit) or outputs > output or searches > limit):
            raise RuntimeError('Unexpected provider model, tier or usage')
        return {'model': model, 'input_tokens': inputs, 'output_tokens': outputs, 'web_calls': searches,
                'metered_usd': str(cost(model, inputs, outputs, searches))}

    def check_response(self, result, step, row):
        if step != 'details' or result.get('status') != 'completed':
            return super().check_response(result, step, row)
        from app import extract_openai_text

        if not row['web_calls']:
            return 'SEARCH_NOT_EXECUTED'
        claims = machine_object(extract_openai_text(result), 'IDENTITY_CLAIMS_JSON').get('claims')
        return None if isinstance(claims, list) else 'INVALID_RESEARCH_SCHEMA'


class Live6Continuation(Live6Budget):
    def __init__(self, destination, revision, taxonomy_token=None):
        self.paid_attempt, self.saved_photo_reply = paid_checkpoint()
        super().__init__(destination, revision, taxonomy_token)
        self.state.update(previous_run=PRIOR_RUN, previous_spend_usd=str(PRIOR_SPEND),
                          max_additional_usd=str(CAP - PRIOR_SPEND))
        self.save()

    def send(self, request, session, kwargs):
        if self.status != 'RUNNING' or not self.pending:
            return super().send(request, session, kwargs)
        lot, step, payload = self.pending
        if (lot, step) != PAID_STEP:
            if not self.attempts or self.attempts[0].get('reused') is not True:
                self.stop('PAID_PHOTO_MUST_BE_RESTORED_FIRST')
            return super().send(request, session, kwargs)

        self.pending = None
        if self.attempts or kwargs.get('json') != payload:
            self.stop('PAID_PHOTO_REPLAY_BINDING_INVALID')
        count = self.paid_attempt['input_tokens_counted']
        usage = self.metered_usage(self.saved_photo_reply, step, payload, count)
        reply_file = self.paid_attempt['response_file']
        request_digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if (Decimal(usage['metered_usd']) != PRIOR_SPEND or request_digest != self.paid_attempt['request_sha256']
                or self.check_response(self.saved_photo_reply, step, {'lot': lot, **usage})):
            self.stop('PAID_PHOTO_REPLAY_MISMATCH')
        durable_report(self.path.parent / reply_file, self.saved_photo_reply)
        self.attempts.append(self.paid_attempt | usage | {'status': 'COMPLETE', 'reused': True,
                                                        'service_tier': 'default', 'source_run': PRIOR_RUN})
        self.save()
        restored = requests.Response()
        restored.status_code = 200
        restored._content = json.dumps(self.saved_photo_reply).encode()
        return restored


def approved_environment(*, execute):
    revision = os.environ.get('GITHUB_SHA', '')
    if (os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or os.environ.get('GITHUB_REF') not in {TAG, CONTINUATION_TAG}
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1' or not re.fullmatch(r'[a-f0-9]{40}', revision)
            or os.environ.get('ENABLE_LIVE_PUBLISH') != '0' or os.environ.get('ALLOW_PAID_AI') != '0'
            or os.environ.get('LOTNEXO_LIVE6_APPROVED_USD') != str(CAP) or date.today() > RATES_EXPIRE
            or (execute and os.environ.get('LOTNEXO_LIVE6_RESERVED') != revision)):
        raise StopRun('PROTECTED_TAG_APPROVAL_OR_RESERVATION_MISSING')
    key = os.environ.get('P094_PILOT_OPENAI_API_KEY', '')
    if not key or any(char.isspace() for char in key):
        raise StopRun('PILOT_AUTHORIZATION_UNAVAILABLE')
    return revision, key


def preflight(secret, output, revision):
    report = {'revision': revision, 'blind_sha256': ARCHIVE_SHA, 'budget_usd': str(CAP), 'models': {},
              'token_count': {}, 'taxonomy': {'taxonomy': 'NOT_RUN'}, 'status': 'UNAVAILABLE'}
    if os.environ.get('GITHUB_REF') == CONTINUATION_TAG:
        report.update(previous_run=PRIOR_RUN, paid_archive_sha256=PRIOR_SHA,
                      previous_spend_usd=str(PRIOR_SPEND), max_additional_usd=str(CAP - PRIOR_SPEND))
    try:
        available = model_metadata(secret, tuple(RATES))
        report['models'] = available
        with requests.Session() as session:
            for model, info in available.items():
                if info.get('http_status') != 200 or info.get('returned_model') != model:
                    raise StopRun('MODEL_UNAVAILABLE')
                response = session.post(API + '/responses/input_tokens',
                                        headers={'Authorization': 'Bearer ' + secret},
                                        json={'model': model, 'input': 'Authorization preflight'},
                                        timeout=(4, 20), allow_redirects=False)
                count = response.json().get('input_tokens') if response.status_code == 200 else None
                if type(count) is not int or count <= 0:
                    raise StopRun('TOKEN_COUNT_UNAVAILABLE')
                report['token_count'][model] = count
        report['taxonomy'] = production_taxonomy(connect=True)
        if not taxonomy_ready(report['taxonomy']):
            raise StopRun('EBAY_TAXONOMY_ACCESS_UNAVAILABLE')
        report['status'] = 'AUTHORIZED_UNMETERED'
    except StopRun as error:
        report['reason'] = str(error)
        raise
    finally:
        durable_report(output, report)


def taxonomy_ready(report):
    return (isinstance(report, dict) and report.get('taxonomy') == 'OK' and report.get('marketplace') == 'EBAY_US'
            and bool(report.get('tree_id')) and bool(report.get('tree_version')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight', type=Path)
    modes.add_argument('--execute', type=Path)
    args = parser.parse_args()
    if not (args.preflight or args.execute):
        print(json.dumps(validate(), indent=2))
        return 0
    revision, secret = approved_environment(execute=bool(args.execute))
    continuation = os.environ.get('GITHUB_REF') == CONTINUATION_TAG
    with TemporaryDirectory() as root:
        lots = sealed_photos(Path(root))
        if continuation:
            paid_checkpoint()
        if args.preflight:
            preflight(secret, args.preflight, revision)
            return 0
        receipt = json.loads((args.execute.parent / 'preflight.json').read_bytes())
        if (receipt.get('status') != 'AUTHORIZED_UNMETERED' or receipt.get('revision') != revision
                or receipt.get('blind_sha256') != ARCHIVE_SHA or receipt.get('budget_usd') != str(CAP)
                or set(receipt.get('token_count', {})) != set(RATES) or not taxonomy_ready(receipt.get('taxonomy'))
                or (continuation and (receipt.get('paid_archive_sha256') != PRIOR_SHA
                                      or receipt.get('previous_spend_usd') != str(PRIOR_SPEND)))):
            raise StopRun('PREFLIGHT_RECEIPT_MISMATCH')
        try:
            taxonomy_token = production_taxonomy_token(os.environ)
            current_taxonomy = production_taxonomy(connect=True, config={'VINTAGE_EBAY_TAXONOMY_TOKEN': taxonomy_token})
        except (ValueError, requests.RequestException) as error:
            raise StopRun('EBAY_TAXONOMY_RECHECK_FAILED') from error
        if not taxonomy_ready(current_taxonomy):
            raise StopRun('EBAY_TAXONOMY_RECHECK_FAILED')
        import app

        app.OPENAI_API_KEY = secret
        budget_class = Live6Continuation if continuation else Live6Budget
        run_lots(budget_class(args.execute, revision, taxonomy_token), lots, args.execute)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (StopRun, OSError, ValueError, requests.RequestException):
        # Never print request headers, provider bodies, credentials or local configuration.
        print('Live 6 stopped. See the isolated preflight or run report.')
        raise SystemExit(1)
