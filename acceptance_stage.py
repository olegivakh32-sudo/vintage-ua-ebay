"""Freeze and check staged acceptance locally. This module never sends API requests."""
import argparse
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from blind_live_pilot import (
    BASE_TOKENS, CONTINUATION_INPUT, CONTINUATION_OUTPUT, CONTINUATION_RESERVE, MODEL, SEARCH_LIMIT, VISUAL_OUTPUT,
)


SOURCE_SHA = '8185d2597d968b48dca3a1841c3af8d57dbe94f5520b298113785687dcac9db8'
REVISION = '3cef70dbe268b35877de848c2cbe10f9b910f951'
STAGES = {
    'S1': '001 002 003 004 006 010 013 022 039 053',
    'S2': '005 007 009 011 012 014 015 016 017 019 021 024 025 026 028 031 033 036 048 052',
}
STEPS = ('photos', 'research')


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def freeze(archive, references, destination, manifest, *, algorithm_revision=REVISION):
    if not re.fullmatch(r'[a-f0-9]{40}', algorithm_revision):
        raise ValueError('Expected a full Git commit SHA for the recognition algorithm')
    if sha(archive) != SOURCE_SHA:
        raise ValueError('Blind archive changed')
    labelled = json.loads(references.read_text(encoding='utf-8'))
    labels = {row['lot']: row for row in labelled}
    ids = {f'P{index:03}' for index in range(1, 57)}
    if len(labelled) != 56 or labels.keys() != ids or len({row['identity'][0] for row in labelled}) != 19:
        raise ValueError('Unexpected acceptance cohort')
    groups = {key: [f'P{value}' for value in group.split()] for key, group in STAGES.items()}
    groups['S3'] = sorted(ids - set(groups['S1']) - set(groups['S2']))
    if sorted(lot for group in groups.values() for lot in group) != sorted(ids):
        raise ValueError('Invalid stage partition')
    destination.mkdir(parents=True, exist_ok=True)
    plan = {
        'algorithm_revision': algorithm_revision, 'reference_sha256': sha(references), 'input_sha256': SOURCE_SHA,
        'model': MODEL, 'service_tier': 'default', 'research_effort': 'low', 'search_context_size': 'low',
        'max_input_tokens_before_send': BASE_TOKENS, 'max_web_calls': SEARCH_LIMIT,
        'max_billed_input_tokens': {'photos': BASE_TOKENS, 'research': CONTINUATION_INPUT},
        'max_output_tokens': {'photos': VISUAL_OUTPUT, 'research': CONTINUATION_OUTPUT},
        'per_step_reserve_usd': {key: str(CONTINUATION_RESERVE[key]) for key in STEPS},
        'approval_required': True, 'publication_allowed': False, 'automatic_retry': False,
        'stages': {},
    }
    with ZipFile(archive) as source:
        rows = {row['file']: row['sha256'] for row in json.loads(source.read('photos.json'))}
        expected = {f'{lot}/01.jpg' for lot in ids}
        if set(source.namelist()) != expected | {'photos.json'} or rows.keys() != expected:
            raise ValueError('Unexpected blind archive entries')
        for key, group in groups.items():
            path = destination / f'lotnexo-{key.lower()}-{len(group)}-blind.zip'
            photos = []
            with ZipFile(path, 'w') as target:
                for lot in group:
                    name = f'{lot}/01.jpg'
                    raw = source.read(name)
                    digest = sha256(raw).hexdigest()
                    if digest != rows[name] or digest != labels[lot]['photo_sha256']:
                        raise ValueError('Photo and reference mismatch')
                    photos.append({'file': name, 'sha256': digest})
                    target.writestr(ZipInfo(name), raw, compress_type=ZIP_DEFLATED)
                target.writestr(ZipInfo('photos.json'), json.dumps(photos, indent=2) + '\n')
            plan['stages'][key] = {
                'lots': group, 'types': len({labels[lot]['identity'][0] for lot in group}),
                'minimum_correct': (9 * len(group) + 9) // 10, 'max_requests': 2 * len(group),
                'reserve_usd': str(sum(CONTINUATION_RESERVE.values()) * len(group)),
                'blind_file': path.name, 'blind_sha256': sha(path),
            }
    write_json(manifest, plan)
    return plan


def check(plan, stage, state):
    """Return a stop or the single next step. A reviewed result is required after each batch."""
    phase = plan['stages'][stage]
    expected = [(lot, kind) for lot in phase['lots'] for kind in STEPS]
    if state.get('algorithm_revision') != plan['algorithm_revision'] or state.get('reference_sha256') != plan['reference_sha256']:
        return {'stop': 'FROZEN_INPUT_CHANGED'}
    if state.get('blind_sha256') != phase['blind_sha256']:
        return {'stop': 'FROZEN_PHOTOS_CHANGED'}
    if state.get('publication_disabled') is not True:
        return {'stop': 'PUBLICATION_GATE_MISSING'}
    if state.get('authenticated_model') != plan['model'] or state.get('pricing_verified') is not True:
        return {'stop': 'MODEL_OR_RATES_UNVERIFIED'}
    preceding = list(plan['stages'])[:list(plan['stages']).index(stage)]
    if state.get('reviewed_passed_stages') != preceding:
        return {'stop': 'PREVIOUS_STAGE_NOT_ACCEPTED'}
    try:
        approved = Decimal(str(state.get('approved_usd')))
        reserved = Decimal(phase['reserve_usd'])
        if not approved.is_finite() or approved < reserved:
            return {'stop': 'INSUFFICIENT_APPROVED_BUDGET'}
    except InvalidOperation:
        return {'stop': 'PAID_APPROVAL_MISSING'}
    attempts = state.get('attempts', [])
    if not isinstance(attempts, list) or len(attempts) > len(expected):
        return {'stop': 'REQUEST_LIMIT'}
    spent = Decimal(0)
    for position, row in enumerate(attempts):
        lot, kind = expected[position]
        if not isinstance(row, dict) or (row.get('lot'), row.get('step')) != (lot, kind):
            return {'stop': 'DUPLICATE_OR_OUT_OF_ORDER'}
        if row.get('status') != 'COMPLETE' or row.get('model') != plan['model'] or row.get('service_tier') != plan['service_tier']:
            return {'stop': 'PROVIDER_FAILURE_OR_UNCERTAIN_CHARGE'}
        usage = {'input_tokens': (1, plan['max_billed_input_tokens'][kind]),
                 'output_tokens': (0, plan['max_output_tokens'][kind]),
                 'web_calls': (0, plan['max_web_calls'] if kind == 'research' else 0)}
        if any(type(row.get(key)) is not int or not low <= row[key] <= high for key, (low, high) in usage.items()):
            return {'stop': 'UNEXPECTED_USAGE'}
        try:
            amount = Decimal(str(row.get('metered_usd')))
            if not amount.is_finite() or amount < 0 or amount > Decimal(plan['per_step_reserve_usd'][kind]):
                return {'stop': 'UNEXPECTED_USAGE'}
        except InvalidOperation:
            return {'stop': 'PROVIDER_FAILURE_OR_UNCERTAIN_CHARGE'}
        spent += amount
    if state.get('critical_error') or state.get('unverified_category_admitted'):
        return {'stop': 'CRITICAL_QUALITY_FAILURE'}
    if len(attempts) == len(expected):
        summary = state.get('review')
        if not isinstance(summary, dict) or summary.get('independent') is not True:
            return {'stop': 'AWAIT_INDEPENDENT_REVIEW', 'metered_usd': str(spent)}
        correct = summary.get('identity_correct')
        if type(correct) is not int or not phase['minimum_correct'] <= correct <= len(phase['lots']):
            return {'stop': 'IDENTITY_THRESHOLD_FAILED', 'metered_usd': str(spent)}
        return {'result': 'STAGE_PASSED', 'metered_usd': str(spent)}
    lot, kind = expected[len(attempts)]
    left = Decimal(plan['per_step_reserve_usd'][kind])
    if spent + left > min(reserved, approved):
        return {'stop': 'BUDGET_EXHAUSTED'}
    counted = state.get('free_input_count')
    if not isinstance(counted, dict) or (counted.get('lot'), counted.get('step')) != (lot, kind):
        return {'stop': 'AWAIT_FREE_TOKEN_COUNT'}
    size = counted.get('input_tokens')
    if type(size) is not int or not 1 <= size <= plan['max_input_tokens_before_send']:
        return {'stop': 'INPUT_TOKEN_LIMIT'}
    return {'next': {'lot': lot, 'step': kind, 'reserve_usd': str(left)}, 'metered_usd': str(spent)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    prepare = modes.add_parser('freeze')
    for flag in ('archive', 'references', 'destination', 'manifest'):
        prepare.add_argument(f'--{flag}', type=Path, required=True)
    prepare.add_argument('--algorithm-revision', default=REVISION)
    validate = modes.add_parser('check')
    validate.add_argument('--plan', type=Path, required=True)
    validate.add_argument('--stage', choices=('S1', 'S2', 'S3'), required=True)
    validate.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'freeze':
        result = freeze(args.archive, args.references, args.destination, args.manifest,
                        algorithm_revision=args.algorithm_revision)
        print(json.dumps(result['stages'], indent=2))
    else:
        result = check(json.loads(args.plan.read_text()), args.stage, json.loads(args.state.read_text()))
        print(json.dumps(result, indent=2))
        raise SystemExit(int('stop' in result))
