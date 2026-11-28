"""Summarize archived recognition transport and photo dimensions without network access."""
import argparse
from base64 import b64decode
from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image


ROOT = Path(__file__).parent / 'test_artifacts' / 'recognition'
ARCHIVES = {
    'S1': ('s1-r2-46011779924-evidence', 's1-10-blind'),
    'M12': ('m12-46062461677-evidence', 'multiview-independent-blind'),
    'N12': ('n12-46069966723-evidence', 'next12-blind'),
}


def stage_audit(receipts, photos):
    with ZipFile(photos) as archive:
        album = {}
        for name in archive.namelist():
            if name.endswith('.jpg'):
                with Image.open(BytesIO(archive.read(name))) as image:
                    album.setdefault(name.split('/')[0], []).append(list(image.size))
    with ZipFile(receipts) as archive:
        run = json.loads(archive.read('results/run.json'))
        if sha256(photos.read_bytes()).hexdigest() != run['blind_sha256']:
            raise ValueError('Blind archive differs from the run ledger')
        request_metadata = {}
        for row in run['attempts']:
            filename = row.get('response_file')
            if not filename:
                continue
            response = json.loads(archive.read('results/' + filename))
            if response['model'] != row['model']:
                raise ValueError('Model in ledger differs from the response')
            queries = []
            for entry in response['output']:
                action = entry.get('action') or {}
                if entry['type'] == 'web_search_call' and action.get('type') == 'search':
                    queries.extend(action.get('queries') or [action['query']])
            request_metadata.setdefault(row['step'], []).append({
                'lot': row['lot'], 'status': response['status'], 'request_sha256': row['request_sha256'],
                'model': response['model'], 'service_tier': response['service_tier'],
                'effort': response['reasoning']['effort'], 'output_cap': response['max_output_tokens'],
                'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens'],
                'reasoning_tokens': row['reasoning_tokens'], 'elapsed_ms': row['elapsed_ms'],
                'web_calls': row['web_calls'], 'queries': queries,
            })

    for step, rows in request_metadata.items():
        coverage = {row['lot'] for row in rows}
        request_metadata[step] = dict(
            responses=len(rows), missing=sorted(album.keys() - coverage),
            status=dict(Counter(row['status'] for row in rows)),
            median_ms=median(row['elapsed_ms'] for row in rows),
            web_calls=sum(row['web_calls'] for row in rows), per_lot=rows,
        )
    return dict(revision=run['algorithm_revision'], status=run['status'], stop_reason=run['reason'],
                metered_usd=run['metered_usd'], unsettled_reserve_usd=run['unsettled_reserve_usd'],
                photo_count=sum(len(sizes) for sizes in album.values()),
                over_1024_photos=sum(max(size) > 1024 for sizes in album.values() for size in sizes),
                original_dimensions_px=album, stages=request_metadata)


def n12_research_fingerprints():
    """Freeze the exact common prompt and image inputs using only archived photo analysis."""
    from m12_replay import ArchivedBudget, replay
    from n12_acceptance import ARCHIVE, LOTS, STAGE, sealed_plan

    inputs = {}
    bind = ArchivedBudget.bind

    def capture(budget, lot, payload):
        messages = payload.get('input')
        if isinstance(messages, list) and messages[0]['content'][0]['text'].startswith('P026 COMBINED RESEARCH'):
            if lot in inputs:
                raise ValueError('Research requested twice for the same lot')
            content = messages[0]['content']
            inputs[lot] = {
                'input_sha256': sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True,
                                                  separators=(',', ':')).encode()).hexdigest(),
                'delivered_jpegs_sha256': [
                    sha256(b64decode(item['image_url'].split(',', 1)[1], validate=True)).hexdigest()
                    for item in content if item['type'] == 'input_image'
                ],
            }
        return bind(budget, lot, payload)

    with TemporaryDirectory(prefix='lotnexo-research-fingerprints-') as directory:
        with patch.object(ArchivedBudget, 'bind', capture):
            result = replay(Path(directory) / 'results', stage=STAGE)
    if set(inputs) != set(LOTS) or any(not row['delivered_jpegs_sha256'] for row in inputs.values()):
        raise ValueError('Research inputs do not cover the sealed N12 photos')
    plan = sealed_plan(verify_runtime=False)
    return {'mode': 'offline_fingerprints_only', 'cohort': STAGE,
            'blind_sha256': sha256(ARCHIVE.read_bytes()).hexdigest(),
            'reference_sha256': plan['reference_sha256'], 'new_provider_requests': result['new_provider_requests'],
            'common_input': inputs}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--n12-research-inputs', action='store_true', help='Freeze common research inputs offline.')
    args = parser.parse_args()
    if args.n12_research_inputs:
        audits = n12_research_fingerprints()
    else:
        audits = {stage: stage_audit(ROOT / f'lotnexo-{receipts}.zip', ROOT / f'lotnexo-{photos}.zip')
                  for stage, (receipts, photos) in ARCHIVES.items()}
    print(json.dumps(audits, indent=2, ensure_ascii=False))
