"""Run the sealed S1 acceptance cohort once, with a durable budget ledger."""
import argparse
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import requests

from acceptance_stage import LOCAL_LOT_FAILURES, METERED_LOT_FAILURES, STEPS, check, sha
from ai_pilot import PilotLimits, run, write_report
from blind_live_pilot import API, MODEL, RESEARCH_BLOCKS, compact_research, free_preflight, meter, prediction
from openai_auth_diagnostic import safe_error
from visual_protocol import PHOTO_ROLES, VISUAL_TEXT, machine_object, visual_response_issue


PLAN_SHA = 'c22c46084eb98f2a59b1c3c8ff6880cbfd56528a2e08895fce355aed5929edc4'
CAP = Decimal('1.46950')
RATES_EXPIRE = date(2026, 12, 11)
APPROVAL_REF = 'refs/tags/p094-lotnexo-s1-146950-r2'
COUNT_FIELDS = ('model', 'input', 'reasoning', 'tools', 'tool_choice', 'text', 'truncation')


class StopRun(RuntimeError):
    pass


class LotFailure(RuntimeError):
    pass


def frozen_plan(path, archive, revision):
    if sha(path) != PLAN_SHA:
        raise StopRun('FROZEN_PLAN_CHANGED')
    plan = json.loads(path.read_bytes())
    phase = plan['stages']['S1']
    if (sha(archive) != phase['blind_sha256'] or revision != plan['algorithm_revision']
            or phase['max_requests'] != 20 or Decimal(phase['reserve_usd']) != CAP):
        raise StopRun('FROZEN_INPUT_CHANGED')
    return plan


def blind_inputs(archive, phase, directory):
    expected = {f'{lot}/01.jpg' for lot in phase['lots']}
    with ZipFile(archive) as package:
        if sorted(package.namelist()) != sorted(expected | {'photos.json'}):
            raise StopRun('UNEXPECTED_ARCHIVE_ENTRIES')
        rows = json.loads(package.read('photos.json'))
        if (len(rows) != len(expected) or {row['file'] for row in rows} != expected
                or any(set(row) != {'file', 'sha256'} for row in rows)):
            raise StopRun('UNEXPECTED_PHOTO_MANIFEST')
        digests = {row['file']: row['sha256'] for row in rows}
        lots = {}
        for lot in phase['lots']:
            name = f'{lot}/01.jpg'
            if not 0 < package.getinfo(name).file_size <= 10_000_000:
                raise StopRun('PHOTO_SIZE_LIMIT')
            photo = package.read(name)
            if sha256(photo).hexdigest() != digests[name]:
                raise StopRun('PHOTO_CHECKSUM_MISMATCH')
            target = directory / f'{lot}.jpg'
            target.write_bytes(photo)
            lots[lot] = [target]
        return lots


def durable_report(path, data):
    write_report(path, data)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class StageBudget:
    """Bind the checked wire payload and reserve on disk before each transport call."""

    offline = False

    def __init__(self, plan, directory):
        self.plan = plan
        self.path = directory / 'run.json'
        self.attempts = []
        self.pending = None
        self.status = 'RUNNING'
        self.reason = None
        self.sequence = [(lot, step) for lot in plan['stages']['S1']['lots'] for step in STEPS]
        self.state = {
            'algorithm_revision': plan['algorithm_revision'],
            'reference_sha256': plan['reference_sha256'],
            'blind_sha256': plan['stages']['S1']['blind_sha256'],
            'publication_disabled': True,
            'authenticated_model': MODEL,
            'pricing_verified': True,
            'reviewed_passed_stages': [],
            'approved_usd': str(CAP),
            'attempts': self.attempts,
        }
        # A pre-existing directory, including one left by a killed run, cannot resume.
        directory.mkdir(mode=0o700)
        self.save()

    def save(self):
        known = sum((Decimal(row['metered_usd']) for row in self.attempts
                     if 'metered_usd' in row), Decimal(0))
        unsettled = sum((Decimal(row['reserved_usd']) for row in self.attempts
                         if row['status'] in {'IN_FLIGHT', 'STOPPED'} and 'metered_usd' not in row), Decimal(0))
        durable_report(self.path, {'stage': 'S1', 'status': self.status, 'reason': self.reason, **self.state,
                                  'metered_usd': str(known), 'unsettled_reserve_usd': str(unsettled)})

    def stop(self, reason):
        self.pending = None
        self.status, self.reason = 'STOPPED', reason
        self.save()
        raise StopRun(reason)

    def skip_lot(self, lot, reason):
        if reason not in LOCAL_LOT_FAILURES | METERED_LOT_FAILURES:
            self.stop('INVALID_SKIP_RECORD')
        if self.status != 'RUNNING' or any(row['status'] == 'IN_FLIGHT' for row in self.attempts):
            self.stop('PROVIDER_FAILURE_OR_UNCERTAIN_CHARGE')
        self.pending = None
        position = len(self.attempts)
        for name, step in self.sequence[position:]:
            if name != lot:
                break
            self.attempts.append({'lot': lot, 'step': step, 'status': 'SKIPPED', 'reason': reason})
        self.save()

    def prepare_catalogue(self, lot, photos, vintage):
        pass

    def catalogue_references(self, lot):
        return []

    def review_identification(self, lot, vintage, identification, market):
        return identification

    def usage(self, lot):
        return [row.copy() for row in self.attempts if row['lot'] == lot]

    def bind(self, lot, original):
        if self.status != 'RUNNING':
            raise StopRun(self.reason or 'STAGE_FINISHED')
        if self.pending:
            self.stop('REQUEST_ALREADY_BOUND')
        if original.get('text') == VISUAL_TEXT and not original.get('tools'):
            step = 'photos'
        elif original.get('tools') == [{'type': 'web_search'}]:
            step = 'research'
        else:
            self.stop('UNAPPROVED_STAGE')
        payload = {'model': MODEL, 'input': deepcopy(original['input']),
                   'max_output_tokens': self.plan['max_output_tokens'][step],
                   'reasoning': {'effort': 'low'}, 'store': False, 'truncation': 'disabled',
                   'service_tier': 'default', 'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}}
        if step == 'photos':
            payload['text'] = VISUAL_TEXT
        else:
            payload.update(input=compact_research(payload['input']), tool_choice='required',
                           tools=[{'type': 'web_search', 'external_web_access': True, 'search_context_size': 'low'}],
                           max_tool_calls=self.plan['max_web_calls'])
        # Refuse out-of-order work before even the free token count.
        self.state.pop('free_input_count', None)
        if len(self.attempts) >= len(self.sequence) or self.sequence[len(self.attempts)] != (lot, step):
            self.stop('DUPLICATE_OR_OUT_OF_ORDER')
        self.pending = lot, step, deepcopy(payload)
        return payload

    def send(self, request, session, kwargs):
        if self.status != 'RUNNING':
            raise StopRun(self.reason or 'STAGE_FINISHED')
        if not self.pending:
            self.stop('UNRESERVED_REQUEST')
        lot, step, payload = self.pending
        self.pending = None
        if step == 'research':
            payload['include'] = ['web_search_call.action.sources']
        if kwargs.get('json') != payload:
            self.stop('BOUND_REQUEST_CHANGED')
        actual_count_input = {key: payload[key] for key in COUNT_FIELDS if key in payload}
        try:
            counted = request(session, 'POST', f'{API}/responses/input_tokens', headers=kwargs['headers'],
                              json=actual_count_input, timeout=(4, 20), allow_redirects=False)
            if counted.status_code != 200:
                self.state['preflight_error'] = safe_error(counted)
                self.stop('TOKEN_COUNT_HTTP')
            size = counted.json().get('input_tokens')
        except (ValueError, AttributeError, requests.RequestException):
            self.stop('TOKEN_COUNT_UNAVAILABLE')
        self.state['free_input_count'] = {'lot': lot, 'step': step, 'input_tokens': size}
        decision = check(self.plan, 'S1', self.state)
        if 'next' not in decision:
            self.stop(decision.get('stop', 'BUDGET_GATE_CLOSED'))
        row = {'lot': lot, 'step': step,
            'reserved_usd': decision['next']['reserve_usd'], 'status': 'IN_FLIGHT',
            'input_tokens_counted': size,
            'request_sha256': sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        }
        self.attempts.append(row)
        self.save()
        started = monotonic()
        reason = 'PROVIDER_FAILURE'
        try:
            response = request(session, 'POST', f'{API}/responses', headers=kwargs['headers'], json=payload,
                               timeout=(5, 90 if step == 'photos' else 120), allow_redirects=False)
            row['http_status'] = response.status_code
            if response.status_code != 200:
                row['provider_error'] = safe_error(response)
                reason = 'PROVIDER_HTTP_ERROR'
            else:
                reason = 'INVALID_RESPONSE_JSON'
                result = response.json()
                if isinstance(result, dict):
                    file = f'{lot}-{step}-response.json'
                    durable_report(self.path.parent / file, result)
                    row['response_file'] = file
                    reason = 'UNEXPECTED_USAGE'
                    row.update(meter(result, step, continuation=True))
                    row['service_tier'] = result['service_tier']
                    if Decimal(row['metered_usd']) > Decimal(row['reserved_usd']):
                        raise StopRun(reason)
                    reason = response_issue(result, step, row)
                    if not reason:
                        row['status'] = 'COMPLETE'
                        return response
        except requests.RequestException:
            reason = 'TRANSPORT_UNCERTAIN'
        except (ValueError, AttributeError, KeyError, TypeError, RuntimeError):
            pass
        finally:
            row['elapsed_ms'] = round((monotonic() - started) * 1000)
            if row['status'] != 'COMPLETE':
                if reason in METERED_LOT_FAILURES and 'metered_usd' in row:
                    row.update(status='LOT_FAILED', reason=reason)
                    self.save()
                    raise LotFailure(reason)
                row['status'] = 'STOPPED'
                self.stop(reason)
            self.save()


def response_issue(result, step, row):
    if result.get('status') != 'completed':
        details = result.get('incomplete_details')
        reason = details.get('reason') if isinstance(details, dict) else None
        if reason in {'max_output_tokens', 'content_filter'}:
            row['incomplete_reason'] = reason
        return 'INCOMPLETE_RESPONSE'
    from app import extract_openai_text

    text = extract_openai_text(result)
    if step == 'photos':
        try:
            report = json.loads(text)
            photos = report['photos']
            if (not visual_response_issue(result) and isinstance(photos, list) and len(photos) == 1
                    and photos[0]['photo'] == 1 and photos[0]['role'] in PHOTO_ROLES
                    and isinstance(report['observations'], list)
                    and all(isinstance(report[key], dict) for key in ('facts', 'scope', 'physical'))
                    and isinstance(report['summary'], str)):
                return None
        except (TypeError, ValueError, IndexError, KeyError):
            pass
        return 'INVALID_VISUAL_SCHEMA'
    if not row['web_calls']:
        return 'SEARCH_NOT_EXECUTED'
    blocks = {block: machine_object(text, block) for block in RESEARCH_BLOCKS}
    types = {
        'IDENTITY_JSON': (bool,), 'IDENTITY_CLAIMS_JSON': (list,),
        'PRICE_JSON': (int, float, type(None)), 'CATEGORY_CANDIDATE_JSON': (str, type(None)),
        'MARKET_COMPS_JSON': (list,), 'UA_MARKET_COMPS_JSON': (list,),
    }
    invalid = [block for block, field in RESEARCH_BLOCKS.items()
               if field not in blocks[block] or type(blocks[block][field]) not in types[block]]
    if invalid:
        row['invalid_blocks'] = invalid
        return 'INVALID_RESEARCH_SCHEMA'
    return None


def execute(plan, archive, output):
    budget = StageBudget(plan, output)
    with TemporaryDirectory(prefix='lotnexo-s1-photos-') as temporary:
        lots = blind_inputs(archive, plan['stages']['S1'], Path(temporary))
        rows = []
        for lot, photos in lots.items():
            started = monotonic()
            try:
                result = run({'lot': lot}, photos, PilotLimits(CAP), live_web=budget)
            except StopRun:
                raise
            except OSError:
                budget.stop('EVIDENCE_STORAGE_FAILURE')
            except (RuntimeError, ValueError, TypeError, KeyError) as error:
                if budget.status != 'RUNNING':
                    raise StopRun(budget.reason or 'GATE_CLOSED') from error
                reason = str(error) if isinstance(error, LotFailure) else 'APPLICATION_ERROR'
                budget.skip_lot(lot, reason)
                durable_report(output / f'{lot}-card.json', {
                    'lot': lot, 'status': 'LOT_FAILED', 'reason': reason,
                    'review_card': {'draft_status': 'NO_DRAFT', 'evidence': {}},
                    'elapsed_ms': round((monotonic() - started) * 1000), 'admitted': False,
                })
                continue
            result['elapsed_ms'] = round((monotonic() - started) * 1000)
            durable_report(output / f'{lot}-card.json', result)
            stages = result['stage_http']
            if budget.status != 'RUNNING':
                raise StopRun(budget.reason or 'GATE_CLOSED')
            draft = result['draft']
            if (result['admitted'] or result['category_id'] or (draft and not draft.get('hold_preview'))
                    or any(draft.get(key) for key in ('category_id', 'price', 'shipping_usd', 'seller_reviewed'))):
                budget.state['critical_error'] = True
                budget.stop('CRITICAL_QUALITY_FAILURE')
            if any(status in (401, 403) for status in stages.values()):
                budget.stop('APPLICATION_AUTHORIZATION_FAILURE')
            if stages['visual'] != 200 or stages['research'] != 200 or stages['draft'] not in (200, 409):
                failure = next((row['reason'] for row in reversed(budget.attempts)
                                if row['lot'] == lot and row['status'] == 'LOT_FAILED'), 'APPLICATION_STAGE_FAILED')
                budget.skip_lot(lot, failure)
                continue
            rows.append(prediction(result, budget.attempts))
            durable_report(output / 'predictions.json', rows)
        final = check(plan, 'S1', budget.state)
        if final.get('stop') != 'AWAIT_INDEPENDENT_REVIEW':
            budget.stop(final.get('stop', 'UNEXPECTED_FINAL_STATE'))
        budget.status = 'AWAIT_INDEPENDENT_REVIEW'
        budget.save()


def authorize(secret, report_path=None):
    if datetime.now(ZoneInfo('Europe/Kyiv')).date() >= RATES_EXPIRE:
        raise StopRun('RATE_PROFILE_EXPIRED')
    if not secret or secret != secret.strip() or any(char.isspace() for char in secret):
        raise StopRun('PROJECT_KEY_MISSING_OR_INVALID')
    report, available = free_preflight(secret)
    if report_path is not None:
        durable_report(report_path, report | {'authorized': available})
    if not available:
        raise StopRun('MODEL_OR_WRITE_ACCESS_UNVERIFIED')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--algorithm-revision', required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight', type=Path)
    modes.add_argument('--execute', type=Path)
    args = parser.parse_args()
    try:
        plan = frozen_plan(args.plan, args.archive, args.algorithm_revision)
        with TemporaryDirectory() as temporary:
            blind_inputs(args.archive, plan['stages']['S1'], Path(temporary))
        if not args.preflight and not args.execute:
            print(json.dumps({'stage': 'S1', 'budget_usd': str(CAP), 'approval': 'REQUIRED', 'requests': 0}))
            return 0
        if os.environ.get('GITHUB_REF') != APPROVAL_REF or os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
            raise StopRun('PROTECTED_APPROVAL_REF_REQUIRED')
        secret = os.environ.get('P094_PILOT_OPENAI_API_KEY')
        if args.preflight:
            authorize(secret, args.preflight)
        else:
            if os.environ.get('LOTNEXO_S1_RESERVED') != PLAN_SHA or os.environ.get('ENABLE_LIVE_PUBLISH') != '0':
                raise StopRun('DURABLE_APPROVAL_OR_PUBLICATION_GATE_MISSING')
            authorize(secret)
            os.environ['OPENAI_API_KEY'] = secret
            for name in ('EBAY_CLIENT_ID', 'EBAY_CLIENT_SECRET', 'EBAY_REFRESH_TOKEN',
                         'VINTAGE_VERIFIED_SALES_FILE', 'VINTAGE_PRODUCT_CATALOGUE', 'VINTAGE_WORKSPACE_CATALOGUE'):
                os.environ.pop(name, None)
            execute(plan, args.archive, args.execute)
    except (StopRun, OSError, ValueError, requests.RequestException) as error:
        print(json.dumps({'stop': str(error) if isinstance(error, StopRun) else type(error).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
