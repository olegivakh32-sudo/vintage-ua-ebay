"""Compare the approved three-photo acceptance cohort with the offline baseline."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests

from acceptance_offline import records
from ai_pilot import (
    VISION_MAXIMUM, PilotLimits, acceptance_gaps, execute_pilot, inputs, inspect_photos,
)
from recognition_benchmark import normalize


BUDGET = Decimal('0.0612')
BASE = Path(__file__).resolve().parent
REFERENCES = json.loads((BASE / 'visual_pilot_reference.json').read_text(encoding='utf-8'))
FIELDS = ('identity', 'brand', 'model', 'material', 'period', 'condition', 'defects', 'category_id')


def prepare(root, originals=None):
    """Stage the approved public originals and refuse a checksum change."""
    pictures = {row['reference_id']: row for filename in ('benchmark_sources.csv', 'benchmark_licensed_photos.csv')
                for row in records(BASE / filename)}
    for lot, reference in REFERENCES.items():
        picture = pictures[reference['reference_id']]
        limit = 10 * 1024 * 1024
        if originals is not None:
            with (originals / f'{lot}.jpg').open('rb') as source:
                sample = source.read(limit + 1)
        else:
            image_url = picture['image_url']
            url = urlsplit(image_url)
            if url.scheme != 'https' or url.netloc not in {'openaccess-cdn.clevelandart.org', 'upload.wikimedia.org'}:
                raise ValueError(f'{lot}: unexpected photo origin')
            with requests.get(image_url, stream=True, timeout=(5, 12), allow_redirects=False,
                              headers={'User-Agent': 'Vintage-UA-eBay-acceptance/1.0 (public study)'}) as response:
                if response.status_code != 200:
                    raise ValueError(f'{lot}: photo HTTP {response.status_code}')
                sample = response.raw.read(limit + 1, decode_content=True)
        if len(sample) > limit:
            raise ValueError(f'{lot}: photo is larger than the pilot limit')
        if sha256(sample).hexdigest() != picture['sha256']:
            raise ValueError(f'{lot}: public original checksum changed')
        folder = root / lot
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        with (folder / 'frame.jpg').open('xb') as stream:
            stream.write(sample)
        manifest = {'lot': lot, 'photos': ['frame.jpg'],
                    'originals': [{'photo': 1, 'sha256': picture['sha256']}],
                    'reference': {name: reference[name] for name in ('identity', 'model') if name in reference}}
        with (folder / 'manifest.json').open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream)


def cohort(root):
    museum = {row['reference_id']: row for row in records(BASE / 'benchmark_sources.csv')}
    modern = {row['reference_id']: row for row in records(BASE / 'benchmark_licensed_photos.csv')}
    manufacturers = {row['reference_id']: row for row in records(BASE / 'benchmark_product_sources.csv')}
    baseline = json.loads((BASE / 'benchmark_offline_results.json').read_text(encoding='utf-8'))
    offline = {row['reference_id']: row for row in baseline['outcomes']}
    cases = []
    for lot, reference in REFERENCES.items():
        ref = reference['reference_id']
        manifest = root / lot / 'manifest.json'
        data, photos = inputs(manifest)
        picture = (museum if ref in museum else modern)[ref]
        if (data['lot'] != lot or data['_photo_digests'] != [picture['sha256']]
                or len(photos) != 1 or photos[0].name != 'frame.jpg'):
            raise ValueError(f'{lot}: фото не належить до погодженої вибірки.')
        if acceptance_gaps(data, photos, vision_only=True):
            raise ValueError(f'{lot}: бракує незалежного еталона.')
        inspect_photos(photos)
        source = (museum if ref in museum else manufacturers)[ref]
        labels = {name: reference[name] for name in ('identity', 'model', 'brand') if name in reference}
        if ref in museum:
            labels['material'] = ['silver, wood', 'silver and wood', 'wood and silver']
            labels['period'] = [source['period']]
        else:
            labels['brand'] = [source['brand']]
        cases.append({
            'lot': lot, 'reference_id': ref, 'license': picture['license'],
            'photo_source': picture.get('photo_source') or picture['catalog_url'],
            'fact_source': source.get('source_url') or source['catalog_url'],
            'offline': offline[ref]['result'], 'offline_elapsed_ms': offline[ref].get('elapsed_ms'),
            'labels': labels, 'category': reference['category'], 'manifest': manifest,
            'documented_features': source.get('confirmed_attributes') or source.get('catalog_dimensions'),
            'data': data, 'photos': photos, 'report': manifest.parent / 'vision-report.json',
        })
    return cases


def outcome(case, result):
    metrics = result.get('request_metrics') or {}
    attempted = metrics.get('sent') is True
    parsed = result.get('mode') == 'APPROVED_VISION_ONLY' and bool(result.get('product_evidence'))
    evidence = result['product_evidence'] if parsed else {}
    checks = {}
    for field in FIELDS:
        actual = result.get(field) if parsed else None
        labels = case['labels'].get(field)
        if field == 'category_id':
            status = 'OUT_OF_SCOPE'
        elif not parsed:
            status = 'NO_RESULT'
        elif not labels:
            status = 'REQUIRES_INDEPENDENT_REVIEW' if actual else 'NO_REFERENCE'
        elif not actual:
            status = 'ABSTAINED'
        elif normalize(actual, field) in {normalize(label, field) for label in labels}:
            status = 'MATCH'
        else:
            status = 'REQUIRES_INDEPENDENT_REVIEW'
        checks[field] = {'value': actual, 'status': status}
    category = case.get('category')
    if category:
        category_status = category.get('status') or (
            'PUBLIC_PATH_MATCH' if checks['identity']['status'] == 'MATCH'
            and checks['model']['status'] == 'MATCH' else 'PENDING_IDENTIFICATION')
        checks['category_id'] = {'value': category.get('id') if category_status == 'PUBLIC_PATH_MATCH' else None,
                                 'status': category_status, 'reference': category}
    checks['features'] = {'observed': evidence.get('feature', []),
                          'documented_model_or_object': case.get('documented_features')}
    return {
        **{key: case[key] for key in ('lot', 'reference_id', 'offline', 'offline_elapsed_ms', 'license',
                                    'photo_source', 'fact_source')},
        'vision': 'PARSED' if parsed else 'NO_RESULT' if attempted else 'NOT_RUN',
        'visual_stage_http': (result.get('stage_http') or {}).get('visual'),
        'visual_evidence': evidence,
        'physical_readings': result.get('physical_readings') or {},
        'processing_elapsed_ms': result.get('processing_elapsed_ms'),
        'ocr': result.get('ocr') if parsed else None,
        'sent': attempted, 'metrics': metrics, 'comparison': checks, 'admitted': False,
    }


def evaluate(root, *, execute=False):
    cases = cohort(root)
    if len(cases) * VISION_MAXIMUM != BUDGET:
        raise ValueError('Загальний резерв відрізняється від погодженого ліміту.')
    if execute:
        if os.environ.get('APPROVE_AI_PILOT') != '1' or not os.environ.get('OPENAI_API_KEY', '').strip():
            raise ValueError('Для виконання потрібні APPROVE_AI_PILOT=1 і окремий ключ OpenAI.')
        if datetime.now(timezone.utc).date() >= date(2026, 12, 11):
            raise ValueError('Модель і тарифи потребують повторної перевірки.')
        if any(case['report'].exists() or list(case['manifest'].parent.glob('.ai-pilot-*.lock')) for case in cases):
            raise ValueError('Є попередній звіт або резерв. Повторний запуск заборонено.')
        lock = root / '.visual-acceptance-0612.lock'
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            raise ValueError('Загальний ліміт пілота вже зарезервовано.') from None
        for case in cases:
            limits = PilotLimits(VISION_MAXIMUM, vision_only=True)
            result = execute_pilot(case['manifest'], case['report'], case['data'], case['photos'], limits)
            metrics = result.get('request_metrics') or {}
            if (not metrics.get('standard_cost_usd') or not result.get('product_evidence')
                    or metrics.get('response_status') not in (None, 'completed')
                    or (result.get('stage_http') or {}).get('visual') != 200):
                break

    rows = []
    for case in cases:
        report = case['report']
        result = json.loads(report.read_text(encoding='utf-8')) if report.is_file() and report.stat().st_size else {}
        rows.append(outcome(case, result))
    sent = [row for row in rows if row['sent']]
    priced = [row['metrics']['standard_cost_usd'] for row in sent if 'standard_cost_usd' in row['metrics']]
    unpriced = len(sent) - len(priced)
    metered = [row['metrics']['metered_cost_usd'] for row in sent if 'metered_cost_usd' in row['metrics']]
    return {'budget_usd': str(BUDGET), 'requests_sent': len(sent), 'priced_requests': len(priced),
            'standard_cost_usd': str(sum(map(Decimal, priced), Decimal(0))) if not unpriced else None,
            'metered_cost_usd': str(sum(map(Decimal, metered), Decimal(0))) if len(metered) == len(sent) else None,
            'cases': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--prepare', action='store_true')
    parser.add_argument('--originals', type=Path, help='Read verified public originals from this directory.')
    args = parser.parse_args()
    if args.originals and not args.prepare:
        parser.error('--originals requires --prepare')
    try:
        if args.prepare:
            prepare(args.root.resolve(), args.originals)
        report = evaluate(args.root.resolve(), execute=args.execute)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')
    if args.execute and (report['priced_requests'] != len(REFERENCES)
                         or any(row['vision'] != 'PARSED' or row.get('visual_stage_http') != 200
                                or (row.get('metrics') or {}).get('response_status') not in (None, 'completed')
                                for row in report['cases'])):
        parser.exit(1, 'Візуальний пілот завершився без повного набору результатів. Перевірте звіт.\n')


if __name__ == '__main__':
    main()
