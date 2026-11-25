"""Compare the approved three-photo acceptance cohort with the offline baseline."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path

from acceptance_offline import records
from ai_pilot import (
    VISION_MAXIMUM, PilotLimits, acceptance_gaps, execute_pilot, inputs, inspect_photos,
)
from recognition_benchmark import normalize


COHORT = {'A01': 'CMA-119326', 'A02': 'RP-PICO2', 'A03': 'NINTENDO-SWITCH-LITE'}
BUDGET = Decimal('0.0612')
BASE = Path(__file__).resolve().parent
FIELDS = ('identity', 'brand', 'model', 'material', 'period', 'condition', 'defects', 'category_id')


def cohort(root):
    museum = {row['reference_id']: row for row in records(BASE / 'benchmark_sources.csv')}
    modern = {row['reference_id']: row for row in records(BASE / 'benchmark_licensed_photos.csv')}
    manufacturers = {row['reference_id']: row for row in records(BASE / 'benchmark_product_sources.csv')}
    baseline = json.loads((BASE / 'benchmark_offline_results.json').read_text(encoding='utf-8'))
    offline = {row['reference_id']: row for row in baseline['outcomes']}
    cases = []
    for lot, ref in COHORT.items():
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
        labels = {name: list(values) for name, values in data['reference'].items()}
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
            'labels': labels, 'manifest': manifest,
            'data': data, 'photos': photos, 'report': manifest.parent / 'vision-report.json',
        })
    return cases


def outcome(case, result):
    metrics = result.get('request_metrics') or {}
    attempted = metrics.get('sent') is True
    parsed = result.get('mode') == 'APPROVED_VISION_ONLY' and bool(result.get('product_evidence'))
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
    return {
        **{key: case[key] for key in ('lot', 'reference_id', 'offline', 'offline_elapsed_ms', 'license',
                                    'photo_source', 'fact_source')},
        'vision': 'PARSED' if parsed else 'NO_RESULT' if attempted else 'NOT_RUN',
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
            if not metrics.get('standard_cost_usd'):
                break

    rows = []
    for case in cases:
        report = case['report']
        result = json.loads(report.read_text(encoding='utf-8')) if report.is_file() and report.stat().st_size else {}
        rows.append(outcome(case, result))
    sent = [row for row in rows if row['sent']]
    priced = [row['metrics']['standard_cost_usd'] for row in sent if 'standard_cost_usd' in row['metrics']]
    unpriced = len(sent) - len(priced)
    return {'budget_usd': str(BUDGET), 'requests_sent': len(sent), 'priced_requests': len(priced),
            'standard_cost_usd': str(sum(map(Decimal, priced), Decimal(0))) if not unpriced else None,
            'cases': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(evaluate(args.root.resolve(), execute=args.execute), ensure_ascii=False, indent=2))
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
