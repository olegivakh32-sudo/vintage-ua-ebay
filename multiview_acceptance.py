"""Validate the sealed 12-lot archive and run it only with separate protected approval."""
import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from PIL import Image
import requests

from acceptance_stage import sha
from blind_live_pilot import CONTINUATION_RESERVE, MODEL
from s1_acceptance import StageBudget, StopRun, authorize, run_lots


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / 'test_artifacts' / 'recognition'
ARCHIVE = ARTIFACTS / 'lotnexo-multiview-independent-blind.zip'
MANIFEST = ARTIFACTS / 'lotnexo-m12-plan.json'
CHATGPT_PROMPT = ARTIFACTS / 'M12_CHATGPT_PROMPT.txt'
PLAN_SHA = '9598348a9bc03a72d0402ac1657245c1717c8e110abbdb035012b9611eb8e54a'
STAGE = 'M12'
APPROVAL_REF = 'refs/tags/p094-lotnexo-m12-176340'
CAP = sum(CONTINUATION_RESERVE.values()) * 12


def frozen_plan(manifest, archive):
    if sha(manifest) != PLAN_SHA:
        raise StopRun('FROZEN_PLAN_CHANGED')
    plan = json.loads(manifest.read_bytes())
    phase = plan['stages'][STAGE]
    if sha(archive) != phase['blind_sha256']:
        raise StopRun('FROZEN_PHOTOS_CHANGED')
    if any(sha(ROOT / name) != digest for name, digest in plan['algorithm_files'].items()):
        raise StopRun('FROZEN_ALGORITHM_CHANGED')
    if sha(CHATGPT_PROMPT) != plan['chatgpt_prompt_sha256']:
        raise StopRun('COMPARISON_PROMPT_CHANGED')
    return plan


def blind_inputs(archive, phase, directory):
    expected = {f'{lot}/{slot:02}.jpg' for lot, count in phase['photo_counts'].items()
                for slot in range(1, count + 1)}
    lots = {lot: [] for lot in phase['lots']}
    with ZipFile(archive) as package:
        if sorted(package.namelist()) != sorted(expected | {'photos.json'}):
            raise StopRun('UNEXPECTED_ARCHIVE_ENTRIES')
        rows = json.loads(package.read('photos.json'))
        if (len(rows) != len(expected) or {row['file'] for row in rows} != expected
                or any(set(row) != {'file', 'sha256'} for row in rows)
                or len({row['sha256'] for row in rows}) != len(expected)):
            raise StopRun('UNEXPECTED_PHOTO_MANIFEST')
        for row in sorted(rows, key=lambda item: item['file']):
            name = row['file']
            if not 0 < package.getinfo(name).file_size <= 10_000_000:
                raise StopRun('PHOTO_SIZE_LIMIT')
            raw = package.read(name)
            if sha256(raw).hexdigest() != row['sha256']:
                raise StopRun('PHOTO_CHECKSUM_MISMATCH')
            with Image.open(BytesIO(raw)) as frame:
                if (frame.format != 'JPEG' or frame.getexif() or frame.info.get('comment')
                        or frame.width * frame.height > 64_000_000):
                    raise StopRun('UNEXPECTED_PHOTO_METADATA_OR_FORMAT')
                frame.verify()
            lot, slot = name.split('/')
            dest = directory / f'{lot}-{slot}'
            dest.write_bytes(raw)
            lots[lot].append(dest)
    return lots


class MultiviewBudget(StageBudget):
    # The application may check only a public record the model actually finds.
    verify_public_catalogue = True

    def __init__(self, plan, destination):
        super().__init__(plan, destination, stage=STAGE, cap=CAP)

    def research_input(self, original):
        result = super().research_input(original)
        result[0]['content'][0]['text'] += (
            '\nIf a primary document gives different dimensions or starting weights by configuration, include\n'
            'DOCUMENTARY_CONFIGURATION_JSON {"source_url":null,"dimensions":[],"weights":[]}\n'
            'Use short strings naming configuration, value and unit, and keep these values in that block. '
            'Keep unknown specimen measurements null. Put the block before === MARKET ===.\n'
        )
        return result


def execute(plan, lots, output):
    run_lots(MultiviewBudget(plan, output), lots, output)


def chatgpt_package(lots, destination):
    prompt = CHATGPT_PROMPT.read_bytes()
    with ZipFile(destination, 'x') as package:
        for photos in lots.values():
            for photo in photos:
                package.writestr(ZipInfo(photo.name), photo.read_bytes(), compress_type=ZIP_STORED)
        package.writestr(ZipInfo('CHATGPT-PROMPT.txt'), prompt, compress_type=ZIP_DEFLATED)


def _require_protected_tag():
    if os.environ.get('GITHUB_REF') != APPROVAL_REF or os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise StopRun('PROTECTED_APPROVAL_REF_REQUIRED')


def _require_approval():
    _require_protected_tag()
    if (os.environ.get('LOTNEXO_M12_RESERVED') != PLAN_SHA
            or os.environ.get('LOTNEXO_M12_APPROVED_USD') != str(CAP)
            or os.environ.get('ENABLE_LIVE_PUBLISH') != '0'):
        raise StopRun('DURABLE_APPROVAL_OR_PUBLICATION_GATE_MISSING')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    parser.add_argument('--archive', type=Path, default=ARCHIVE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', type=Path)
    mode.add_argument('--preflight', type=Path)
    mode.add_argument('--chatgpt-package', type=Path)
    args = parser.parse_args()
    try:
        plan = frozen_plan(args.manifest, args.archive)
        with TemporaryDirectory(prefix='lotnexo-m12-') as work:
            lots = blind_inputs(args.archive, plan['stages'][STAGE], Path(work))
            if args.preflight:
                _require_protected_tag()
                authorize(os.environ.get('P094_PILOT_OPENAI_API_KEY'), args.preflight)
                return 0
            if not args.execute:
                if args.chatgpt_package:
                    chatgpt_package(lots, args.chatgpt_package)
                print(json.dumps({'stage': STAGE, 'model': MODEL, 'lots': len(lots),
                                  'photos': sum(map(len, lots.values())), 'budget_usd': str(CAP),
                                  'approval': 'REQUIRED', 'paid_requests': 0}))
                return 0
            _require_approval()
            secret = os.environ.get('P094_PILOT_OPENAI_API_KEY')
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
