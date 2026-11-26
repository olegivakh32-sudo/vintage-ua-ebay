"""One isolated, explicitly approved, budgeted LOTNEXO comparison on public photos."""
import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import BadZipFile, ZipFile

import requests

from ai_pilot import PilotLimits, run, write_report
from identity_evidence import normalized_claim
from openai_auth_diagnostic import safe_error
from visual_protocol import VISUAL_TEXT, visual_response_issue


API = 'https://api.openai.com/v1'
MODEL = 'gpt-6-luna'
CANDIDATES = (MODEL, 'gpt-5.6-luna', 'gpt-5.4-mini', 'gpt-5.6-terra', 'gpt-5.6-sol',
              'gpt-6.2-astra', 'gpt-4o-mini-2024-07-18')
ARCHIVE_SHA256 = 'c362598737440888911cde10a9a508367236d16495338aff4b814a747b14764f'
LOTS = ('MV-101', 'MV-102', 'MV-103')
CAP = Decimal('0.50')
VISUAL_OUTPUT = 2400
RESEARCH_OUTPUT = 5000
BASE_TOKENS = 24_000
SEARCH_TOKENS = 128_000
SEARCH_LIMIT = 2
WEB_PRICE = Decimal('0.01')
SHORT_THRESHOLD = 272_000
# Initial prompt + prompt again for each search, previous search contexts and output.
RESEARCH_INPUT_MAX = 3 * BASE_TOKENS + 3 * SEARCH_TOKENS + 2 * RESEARCH_OUTPUT


def usd(inputs, outputs, searches, *, long_context=False, cached=0, written=0):
    input_rate = Decimal('0.10') * (2 if long_context else 1)
    output_rate = Decimal('0.75' if long_context else '0.50')
    billable_input = inputs - Decimal('0.90') * cached + Decimal('0.25') * written
    return (billable_input * input_rate + outputs * output_rate) / 1_000_000 + searches * WEB_PRICE


RESERVE = {
    'photos': usd(BASE_TOKENS, VISUAL_OUTPUT, 0, written=BASE_TOKENS),
    'research': usd(RESEARCH_INPUT_MAX, RESEARCH_OUTPUT, SEARCH_LIMIT,
                    long_context=True, written=RESEARCH_INPUT_MAX),
}
FULL_RESERVE = len(LOTS) * sum(RESERVE.values())


def model_metadata(secret):
    result = {}
    with requests.Session() as session:
        for model in CANDIDATES:
            response = session.get(f'{API}/models/{model}', headers={'Authorization': f'Bearer {secret}'},
                                   timeout=(4, 7), allow_redirects=False)
            result[model] = safe_error(response)
            if response.ok:
                result[model]['returned_model'] = response.json().get('id')
    return result


def frozen_inputs(archive, directory):
    with archive.open('rb') as source:
        if sha256(source.read(20_000_001)).hexdigest() != ARCHIVE_SHA256:
            raise ValueError('Wrong blind archive checksum')
        source.seek(0)
        with ZipFile(source) as package:
            entries = json.loads(package.read('photos.json'))
            lots = {lot: [] for lot in LOTS}
            checked = ['photos.json']
            for row in entries:
                match = re.fullmatch(r'(MV-10[1-3])/([0-9]{2}\.jpg)', row['file'])
                if not match or set(row) != {'file', 'sha256'}:
                    raise ValueError('Unexpected archive entry')
                lot, name = match.groups()
                if name != f'{len(lots[lot]) + 1:02}.jpg' or row['file'] in checked:
                    raise ValueError('Repeated or out-of-order frame')
                photo = package.read(row['file'])
                if not 0 < len(photo) <= 10_000_000 or sha256(photo).hexdigest() != row['sha256']:
                    raise ValueError('Photo checksum mismatch')
                path = directory / row['file']
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(photo)
                lots[lot].append(path)
                checked.append(row['file'])
            if sorted(checked) != sorted(package.namelist()) or any(not 2 <= len(paths) <= 24 for paths in lots.values()):
                raise ValueError('Incomplete blind archive')
    return lots


class LiveBudget:
    def __init__(self, report):
        self.report = report
        self.attempts = []
        self.pending = None
        self.results = []
        self.halted = False

    def save(self):
        write_report(self.report, {'mode': 'LIVE_BLIND_PILOT', 'archive_sha256': ARCHIVE_SHA256,
                                   'budget_usd': str(CAP), 'reserved_usd': str(FULL_RESERVE),
                                   'halted': self.halted, 'attempts': self.attempts, 'lots': self.results})

    def bind(self, lot, original):
        if original.get('text') == VISUAL_TEXT and not original.get('tools'):
            stage, output = 'photos', VISUAL_OUTPUT
        elif original.get('tools') == [{'type': 'web_search'}]:
            stage, output = 'research', RESEARCH_OUTPUT
        else:
            raise RuntimeError('Outside the two approved stages')
        position = len(self.attempts)
        expected = [(item, step) for item in LOTS for step in ('photos', 'research')]
        if self.halted or self.pending or position >= len(expected) or expected[position] != (lot, stage):
            raise RuntimeError('Repeated or out-of-order API request')
        payload = {'model': MODEL, 'input': original['input'], 'max_output_tokens': output,
                   'reasoning': {'effort': 'low' if stage == 'photos' else 'medium'},
                   'store': False, 'truncation': 'disabled', 'service_tier': 'default',
                   'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}}
        if stage == 'photos':
            payload['text'] = VISUAL_TEXT
        else:
            payload.update(tools=[{'type': 'web_search', 'external_web_access': True,
                                   'search_context_size': 'medium'}], tool_choice='required',
                           max_tool_calls=SEARCH_LIMIT)
            payload['input'] = deepcopy(payload['input'])
            payload['input'][0]['content'][0]['text'] += (
                '\nIn the MARKET block also return CATEGORY_CANDIDATE_JSON '
                '{"category_id":null,"path":null,"reason":""}. Use an eBay source or leave it unknown. '
                'A candidate here is for review only.'
            )
        self.pending = (lot, stage, payload)
        return payload

    def send(self, request, session, kwargs):
        if not self.pending or self.halted:
            raise RuntimeError('Unreserved request')
        lot, stage, payload = self.pending
        self.pending = None
        wire = kwargs['json']
        allowed_wire = payload | ({'include': ['web_search_call.action.sources']} if stage == 'research' else {})
        if wire != allowed_wire:
            self.halted = True
            raise RuntimeError('Request changed after authorization')
        row = {'lot': lot, 'stage': stage, 'reserved_usd': str(RESERVE[stage]), 'sent': False}
        started = monotonic()
        try:
            counted = request(session, 'POST', f'{API}/responses/input_tokens', headers=kwargs['headers'],
                              json={key: wire[key] for key in ('model', 'input', 'reasoning', 'tools', 'tool_choice', 'text', 'truncation')
                                    if key in wire}, timeout=(4, 20), allow_redirects=False)
            row['count_http'] = counted.status_code
            size = counted.json().get('input_tokens') if counted.ok else None
            if type(size) is not int or not 1 <= size <= BASE_TOKENS:
                raise RuntimeError('Input token cap not verified')
            row['preflight_input_tokens'] = size
            self.attempts.append(row)
            row['sent'] = True  # Reserve on disk before handing the request to the transport.
            self.save()
            inference_started = monotonic()
            try:
                kwargs['timeout'] = (5, 90 if stage == 'photos' else 120)
                response = request(session, 'POST', f'{API}/responses', **kwargs)
                row['http_status'] = response.status_code
                if not response.ok:
                    row['provider_error'] = safe_error(response)
                    raise RuntimeError('Inference HTTP error')
                result = response.json()
                row.update(meter(result, stage))
                if (result.get('status') != 'completed' or (stage == 'photos' and visual_response_issue(result))
                        or row['web_calls'] < (stage == 'research')):
                    raise RuntimeError('Incomplete model response or missing live search')
                return response
            finally:
                row['inference_ms'] = round((monotonic() - inference_started) * 1000)
        except (ValueError, AttributeError, KeyError, RuntimeError, requests.RequestException) as error:
            self.halted = True
            row['stop'] = str(error) if type(error) is RuntimeError else type(error).__name__
            if not row['sent']:
                self.attempts.append(row)
            raise RuntimeError('Blind pilot halted, no automatic retry') from None
        finally:
            row['stage_ms'] = round((monotonic() - started) * 1000)
            self.save()


def meter(result, stage):
    usage = result.get('usage') or {}
    details = usage.get('input_tokens_details') or {}
    inputs, outputs = usage.get('input_tokens'), usage.get('output_tokens')
    cached, written = details.get('cached_tokens', 0), details.get('cache_write_tokens', 0)
    searches = sum(item.get('type') == 'web_search_call' for item in (result.get('output') or []))
    input_limit = BASE_TOKENS if stage == 'photos' else RESEARCH_INPUT_MAX
    output_limit = VISUAL_OUTPUT if stage == 'photos' else RESEARCH_OUTPUT
    if (result.get('model') != MODEL or result.get('service_tier') != 'default'
            or any(type(value) is not int or value < 0 for value in (inputs, outputs, cached, written))
            or not 0 < inputs <= input_limit or outputs > output_limit or cached + written > inputs
            or searches > (0 if stage == 'photos' else SEARCH_LIMIT)
            or (details.get('cache_write_tokens_details') or {}).get('ttl_12h_tokens')):
        raise RuntimeError('Unexpected provider model, tier or usage')
    return {'model': MODEL, 'input_tokens': inputs, 'output_tokens': outputs,
            'cached_tokens': cached, 'cache_write_tokens': written, 'web_calls': searches,
            'metered_usd': str(usd(inputs, outputs, searches, cached=cached, written=written,
                                  long_context=inputs > SHORT_THRESHOLD))}


def prediction(result, attempts):
    evidence = (result.get('review_card') or {}).get('evidence') or {}

    def verified_value(field, statuses):
        rows = evidence.get(field, [])
        if any(row['status'] == 'CONTESTED' for row in rows):
            return None
        values = {normalized_claim(str(row['value'])): row['value'] for row in rows if row['status'] in statuses}
        return next(iter(values.values())) if len(values) == 1 else None

    observed = {'PHOTO_OBSERVATION', 'SELLER_VERIFIED'}
    documented = observed | {'CORROBORATED_REFERENCE'}
    billed = [attempt.get('metered_usd') for attempt in attempts if attempt['lot'] == result['lot']]
    row = {'lot': result['lot'], 'identity': verified_value('type', documented),
           'ocr': result['ocr'] or None, 'defects': result['defects'], 'category_id': result['category_id'],
           'admitted': False, 'elapsed_ms': result['elapsed_ms'], 'main_photo': result['main_photo'],
           'title': (result.get('review_card') or {}).get('title'),
           'product_photos': result['ebay_photo_order'],
           'evidence_photos': [slot for slot, role in result['photo_roles'].items() if role == 'EVIDENCE_ONLY'],
           'measurement': {field: verified_value(field, {'PHOTO_MEASUREMENT', 'SELLER_VERIFIED'})
                           for field in ('weight_kg', 'length_cm', 'width_cm', 'height_cm')},
           'cost_usd': float(sum(Decimal(cost) for cost in billed)) if billed and all(billed) else None}
    row.update({field: verified_value(field, documented) for field in ('brand', 'model', 'material', 'country', 'period')})
    row.update({field: verified_value(field, observed) for field in ('condition', 'completeness')})
    return row


def approved_run(archive, directory, lots, secret):
    if os.environ.get('APPROVE_LOTNEXO_050') != ARCHIVE_SHA256 or not secret:
        raise ValueError('Explicit budget approval and a project key are required')
    if FULL_RESERVE > CAP or datetime.now(timezone.utc).date() >= date(2026, 12, 11):
        raise ValueError('Price profile needs review')
    metadata = model_metadata(secret).get(MODEL, {})
    if metadata.get('http_status') != 200 or metadata.get('returned_model') != MODEL:
        raise ValueError('Required model access was not verified')
    # One immutable reservation per frozen archive beside the approved input.
    claim = archive.parent / f'.lotnexo-{ARCHIVE_SHA256}.claim'
    with claim.open('x', encoding='utf-8') as handle:
        handle.write(str(directory.resolve()))
        handle.flush()
        os.fsync(handle.fileno())
    directory.mkdir(mode=0o700)
    budget = LiveBudget(directory / 'run.json')
    budget.save()
    os.environ['OPENAI_API_KEY'] = secret
    for lot, photos in lots.items():
        started = monotonic()
        result = run({'lot': lot}, photos, PilotLimits(CAP), live_web=budget)
        result['elapsed_ms'] = round((monotonic() - started) * 1000)
        budget.results.append(result)
        write_report(directory / 'predictions.json', [prediction(item, budget.attempts) for item in budget.results])
        phases = result['stage_http']
        inspected = result.get('review_card', {}).get('evidence')
        if (budget.halted or phases['visual'] != 200 or phases['research'] != 200 or not inspected
                or phases['draft'] not in (200, 409)):
            budget.halted = True
            budget.save()
            return 1
        budget.save()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, nargs='?')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--models', action='store_true', help='Only read OpenAI model metadata. No generation.')
    mode.add_argument('--execute', type=Path, metavar='REPORT_DIRECTORY')
    args = parser.parse_args()
    secret = os.environ.get('P094_PILOT_OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
    try:
        if args.models:
            print(json.dumps(model_metadata(secret) if secret else {'blocked': 'NO_PROJECT_KEY'}, indent=2))
            return int(not secret)
        if args.archive is None:
            parser.error('archive is required')
        with TemporaryDirectory(prefix='lotnexo-blind-') as tmp:
            lots = frozen_inputs(args.archive, Path(tmp))
            if args.execute:
                return approved_run(args.archive, args.execute, lots, secret)
            print(json.dumps({'mode': 'DRY_RUN', 'photos': {lot: len(photos) for lot, photos in lots.items()},
                              'model': MODEL, 'cap_usd': str(CAP), 'reserved_usd': str(FULL_RESERVE),
                              'reserved_per_stage_usd': {key: str(value) for key, value in RESERVE.items()}}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, BadZipFile, requests.RequestException) as error:
        # Exception text from the transport can contain a provider key or request URL.
        print(json.dumps({'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
