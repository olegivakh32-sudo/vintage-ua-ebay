"""Validate the photo-only Terra Live 6 handoff and quote it without network access."""
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
from pathlib import Path
from zipfile import ZipFile

from acceptance_stage import sha
from blind_live_pilot import SEARCH_TOKENS, SHORT_THRESHOLD, WEB_PRICE
from multiview_acceptance import ARTIFACTS, blind_inputs
from s1_acceptance import StopRun


ARCHIVE = ARTIFACTS / 'lotnexo-terra-live6-blind.zip'
REFERENCE = ARTIFACTS / 'lotnexo-terra-live6-reference.json'
ARCHIVE_SHA = '3a088e9789aeb3315c82384c00256231cd6de2bd696bee5c3667269de0abf1a1'
REFERENCE_SHA = '3e64c8f6b7c96c8c38663e172d58088faf501df8bd9f395c697ef741d3f9c024'
PHOTO_COUNTS = {f'TL-{lot}': 1 if lot <= 404 else 2 for lot in range(401, 407)}
BASE_TOKENS = 16_000
MAX_SUPPLEMENTS = 2
STEPS = {
    'photos': ('gpt-6.1-terra', 'low', 2400, 0),
    'research': ('gpt-6.1-terra', 'low', 5000, 2),
    'details': ('gpt-6-luna', 'medium', 2600, 2),
}
RATES = {'gpt-6.1-terra': (Decimal('1.25'), Decimal('5.00')),
         'gpt-6-luna': (Decimal('0.125'), Decimal('0.50'))}


def quote():
    limits = {}
    for step, (model, effort, max_output, searches) in STEPS.items():
        # Cover the request replayed by each tool round, all search contexts and generated history.
        max_input = ((searches + 1) * (BASE_TOKENS + SEARCH_TOKENS) + searches * max_output
                     if searches else BASE_TOKENS)
        input_rate, output_rate = RATES[model]
        if max_input > SHORT_THRESHOLD:
            input_rate *= 2
            output_rate *= Decimal('1.5')
        reserve = (max_input * input_rate + max_output * output_rate) / 1_000_000 + searches * WEB_PRICE
        max_calls = MAX_SUPPLEMENTS if step == 'details' else len(PHOTO_COUNTS)
        limits[step] = dict(model=model, effort=effort, max_output_tokens=max_output, max_tool_calls=searches,
                            max_input_tokens=max_input, max_billable_requests=max_calls,
                            reserve_per_request_usd=format(reserve, '.5f'))
    total = sum(row['max_billable_requests'] * Decimal(row['reserve_per_request_usd']) for row in limits.values())
    return {'base_input_tokens_per_request': BASE_TOKENS, 'photo_counts': PHOTO_COUNTS, 'steps': limits,
            'max_run_usd': format(total, '.5f')}


def validate():
    if sha(ARCHIVE) != ARCHIVE_SHA or sha(REFERENCE) != REFERENCE_SHA:
        raise StopRun('LIVE6_FROZEN_INPUT_CHANGED')
    phase = {'lots': list(PHOTO_COUNTS), 'photo_counts': PHOTO_COUNTS}
    with TemporaryDirectory() as root:
        blind_inputs(ARCHIVE, phase, Path(root))
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


if __name__ == '__main__':
    print(json.dumps(validate(), indent=2))
