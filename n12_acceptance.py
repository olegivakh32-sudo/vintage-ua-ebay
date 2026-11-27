"""Run the sealed N12 cohort once, with a separate protected budget and photo-only input."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import BadZipFile

import requests

from acceptance_stage import sha, stage_steps
from blind_live_pilot import (
    ALTERNATE_INPUT, ALTERNATE_RESERVE, BASE_TOKENS, CONTINUATION_INPUT, CONTINUATION_OUTPUT,
    CONTINUATION_RESERVE, MODEL, SEARCH_LIMIT, VISUAL_OUTPUT,
)
from identity_evidence import ALTERNATE_OUTPUT_TOKENS, ALTERNATE_SEARCH_CALLS
from multiview_acceptance import ARTIFACTS, ROOT, MultiviewBudget, blind_inputs
from s1_acceptance import StopRun, authorize, run_lots


STAGE = 'N12'
REVISION = '2327c280a3605d8827b6745121c0df978566d12e'
QUOTE = ARTIFACTS / 'lotnexo-next-acceptance-plan.json'
QUOTE_SHA = 'b14c08ebecb895c4ed8d957dadda0981fa29b2a873541c473e8dfd07c3bb564a'
ARCHIVE = ARTIFACTS / 'lotnexo-next12-blind.zip'
LOTS = tuple(f'MV-{number}' for number in range(301, 313))
APPROVAL_REF = 'refs/tags/p094-lotnexo-n12-342540'
RESERVES = CONTINUATION_RESERVE | {'alternate': ALTERNATE_RESERVE}
CAP = sum(RESERVES.values(), Decimal('0')) * len(LOTS)


def sealed_plan(quote=QUOTE, archive=ARCHIVE, *, verify_runtime=True):
    if sha(quote) != QUOTE_SHA:
        raise StopRun('FROZEN_PLAN_CHANGED')
    prices = json.loads(quote.read_bytes())
    cohort = prices['stages'][STAGE]
    if sha(archive) != cohort['blind_sha256']:
        raise StopRun('FROZEN_PHOTOS_CHANGED')
    if verify_runtime:
        files = prices['recognition_algorithm_sha256'] | prices['budget_runtime_sha256']
        if any(sha(ROOT / name) != digest for name, digest in files.items()):
            raise StopRun('FROZEN_ALGORITHM_CHANGED')
    if (MODEL != prices['model'] or SEARCH_LIMIT != ALTERNATE_SEARCH_CALLS
            or BASE_TOKENS != prices['max_base_input_tokens']
            or SEARCH_LIMIT != prices['max_tool_calls_per_research']
            or ALTERNATE_INPUT != prices['alternate_max_billed_input_tokens']
            or ALTERNATE_OUTPUT_TOKENS != prices['alternate_max_output_tokens']
            or cohort['max_with_alternate_usd'] != str(CAP) or CAP > Decimal('3.43')
            or cohort['lots'] != len(LOTS) or cohort['photos'] != len(LOTS) * 2
            or cohort['max_web_calls'] != len(LOTS) * (SEARCH_LIMIT + ALTERNATE_SEARCH_CALLS)):
        raise StopRun('FROZEN_BUDGET_CHANGED')
    plan = {
        'algorithm_revision': REVISION, 'reference_sha256': cohort['reference_sha256'],
        'model': MODEL, 'service_tier': 'default', 'allow_alternate_identity_search': True,
        'max_input_tokens_before_send': BASE_TOKENS, 'max_web_calls': SEARCH_LIMIT,
        'max_billed_input_tokens': {'photos': BASE_TOKENS, 'research': CONTINUATION_INPUT, 'alternate': ALTERNATE_INPUT},
        'max_output_tokens': {'photos': VISUAL_OUTPUT, 'research': CONTINUATION_OUTPUT, 'alternate': ALTERNATE_OUTPUT_TOKENS},
        'per_step_reserve_usd': {step: str(amount) for step, amount in RESERVES.items()},
        'stages': {STAGE: {'lots': list(LOTS), 'photo_counts': dict.fromkeys(LOTS, 2),
                           'minimum_correct': cohort['minimum_correct'], 'blind_sha256': cohort['blind_sha256'],
                           'reserve_usd': str(CAP)}},
    }
    if cohort['max_generations'] != len(LOTS) * len(stage_steps(plan)):
        raise StopRun('FROZEN_REQUEST_LIMIT_CHANGED')
    return plan


def execute(plan, lots, output):
    budget = MultiviewBudget(plan, output, stage=STAGE, cap=CAP)
    with patch.dict(os.environ, {'VINTAGE_RESEARCH_ALTERNATE': '1'}):
        run_lots(budget, lots, output)


def require_tag():
    if os.environ.get('GITHUB_REF') != APPROVAL_REF or os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise StopRun('PROTECTED_APPROVAL_REF_REQUIRED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preflight', type=Path)
    mode.add_argument('--execute', type=Path)
    args = parser.parse_args()
    try:
        plan = sealed_plan()
        with TemporaryDirectory(prefix='lotnexo-n12-') as work:
            lots = blind_inputs(ARCHIVE, plan['stages'][STAGE], Path(work))
            if not (args.preflight or args.execute):
                print(json.dumps({'stage': STAGE, 'model': MODEL, 'lots': len(lots),
                                  'photos': sum(map(len, lots.values())), 'budget_usd': str(CAP), 'paid_requests': 0}))
                return 0
            require_tag()
            secret = os.environ.get('P094_PILOT_OPENAI_API_KEY')
            if args.preflight:
                authorize(secret, args.preflight)
                return 0
            if (os.environ.get('LOTNEXO_N12_RESERVED') != QUOTE_SHA
                    or os.environ.get('LOTNEXO_N12_APPROVED_USD') != str(CAP)
                    or os.environ.get('ENABLE_LIVE_PUBLISH') != '0'):
                raise StopRun('DURABLE_APPROVAL_OR_PUBLICATION_GATE_MISSING')
            authorize(secret)
            os.environ['OPENAI_API_KEY'] = secret
            for name in ('EBAY_CLIENT_ID', 'EBAY_CLIENT_SECRET', 'EBAY_REFRESH_TOKEN',
                         'VINTAGE_VERIFIED_SALES_FILE', 'VINTAGE_PRODUCT_CATALOGUE', 'VINTAGE_WORKSPACE_CATALOGUE'):
                os.environ.pop(name, None)
            execute(plan, lots, args.execute)
    except (StopRun, OSError, BadZipFile, ValueError, requests.RequestException) as error:
        print(json.dumps({'stop': str(error) if isinstance(error, StopRun) else type(error).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
