"""Score the photo-only and search phases with private-to-the-evaluator N12 references."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re
from statistics import median
from urllib.parse import urlsplit

from model_comparison import ARMS, LIMITS, LOTS, PACK
from multiview_review import FIELDS, grade
from n12_review import available_fields
from n12_acceptance import sealed_plan


VISIBLE_MARKS = {
    'MV-306': [(2, '15.495')],
    'MV-311': [(1, 'Canon'), (1, 'EOS 650D')],
    'MV-312': [(1, 'Nintendo GAME BOY'), (1, 'DOT MATRIX WITH STEREO SOUND')],
}
VISUAL_FIELDS = frozenset({'identity', 'brand', 'model'})


def normalized(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def comparable_url(value):
    parsed = urlsplit(value)
    return (parsed.hostname or '').removeprefix('www.'), parsed.path.removeprefix('/print').rstrip('/')


def automatic_judgment(field, value, expected):
    if field in VISUAL_FIELDS or field in {'material', 'period'}:
        options = expected if isinstance(expected, list) else [expected]
        if isinstance(value, str) and normalized(value) in {normalized(entry) for entry in options}:
            return True
    elif field == 'primary_source' and isinstance(value, str):
        equivalents = [url for url in expected if 'GBA_Manual' not in url]
        if comparable_url(value) in {comparable_url(url) for url in equivalents}:
            return True
    return False


def output_text(response):
    try:
        text = ''.join(part['text'] for item in response['output'] if item['type'] == 'message'
                       for part in item['content'] if part['type'] == 'output_text')
        document = json.loads(text)
        return document if isinstance(document, dict) else {}
    except (KeyError, TypeError, ValueError):
        return {}


def parse_prediction(row, directory, results):
    if row.get('status') != 'completed':
        return {}, [], []
    response = output_text(json.loads((directory / row['response_file']).read_bytes()))
    reported = {field: response[field] for field in VISUAL_FIELDS if response.get(field)}
    supported = []
    if row['phase'] == 'research':
        selected = results.get(response.get('exact_source'))
        if selected:
            reported['primary_source'] = selected['url']
        for claim in response.get('claims', []):
            evidence = results.get(claim['result_id'])
            if evidence and claim['field'] in FIELDS and claim['matching_feature'].strip():
                supported.append(claim | {'source_url': evidence['url']})
        for field in ('material', 'period', 'documentary_dimensions', 'documentary_weight'):
            statements = list(dict.fromkeys(claim['value'] for claim in supported if claim['field'] == field))
            if statements:
                reported[field] = statements[0] if len(statements) == 1 else statements
    return reported, response.get('markings') or [], supported


def review(directory, judgments=None):
    ledger = json.loads((directory / 'ledger.json').read_bytes())
    expected = available_fields(sealed_plan(verify_runtime=False))
    packet = json.loads(PACK.read_bytes())
    references_found = {}
    for lot, row in expected.items():
        references_found[lot] = [found['url'] for found in packet[lot]['results']
                                 if automatic_judgment('primary_source', found['url'], row['primary_source'])]
    observed = {(row['lot'], row['arm'], row['phase']): row for row in ledger['attempts']}
    if len(observed) != len(ledger['attempts']) or any(lot not in LOTS or arm not in ARMS or phase not in LIMITS
                                                     for lot, arm, phase in observed):
        raise ValueError('Ledger contains duplicate or unexpected attempts')
    evaluations = {}
    for arm in ARMS:
        evaluations[arm] = {}
        for phase in LIMITS:
            predictions, checks, mark_rows, claims, rows = {}, {}, {}, {}, []
            fields = set(FIELDS) if phase == 'research' else VISUAL_FIELDS
            facts = {lot: {field: value for field, value in reference.items() if field in fields}
                     for lot, reference in expected.items()}
            for lot in LOTS:
                if row := observed.get((lot, arm, phase)):
                    rows.append(row)
                    sources = {item['id']: item for item in packet[lot]['results']}
                    predictions[lot], markings, claims[lot] = parse_prediction(row, directory, sources)
                else:
                    markings = []
                if lot in VISIBLE_MARKS:
                    words = [(mark['photo'], normalized(mark['literal'])) for mark in markings]
                    mark_rows[lot] = [{'photo': photo, 'expected': text, 'correct': any(
                        slot == photo and f' {normalized(text)} ' in f' {literal} ' for slot, literal in words),
                        'reported': [mark['literal'] for mark in markings if mark['photo'] == photo]}
                        for photo, text in VISIBLE_MARKS[lot]]
                for field, value in predictions.get(lot, {}).items():
                    if field in facts[lot] and automatic_judgment(field, value, facts[lot][field]):
                        checks.setdefault(lot, {})[field] = {
                            'verdict': 'CORRECT', 'evidence': 'Exact normalized match with the sealed reference.',
                        }
                checks.setdefault(lot, {}).update((judgments or {}).get(arm, {}).get(phase, {}).get(lot, {}))
            scores = grade(facts, predictions, checks)
            marks = [mark for group in mark_rows.values() for mark in group]
            evaluations[arm][phase] = dict(
                scores, markings={'eligible': len(marks), 'correct': sum(mark['correct'] for mark in marks),
                                  'by_lot': mark_rows}, claim_sources=claims, attempts=len(rows),
                usage_estimate_usd=str(sum(Decimal(row.get('usage_estimate_usd', 0)) for row in rows)),
                elapsed_request_ms=sum(row.get('elapsed_ms', 0) for row in rows),
                median_request_ms=median([row['elapsed_ms'] for row in rows if 'elapsed_ms' in row])
                if any('elapsed_ms' in row for row in rows) else None,
            )
    return {'status': ledger['status'], 'usage_estimate_usd': ledger['usage_estimate_usd'],
            'unsettled_reserve_usd': ledger['unsettled_reserve_usd'],
            'known_primary_source_in_common_candidates': references_found, 'arms': evaluations}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--judgments', type=Path)
    args = parser.parse_args()
    checks = json.loads(args.judgments.read_bytes()) if args.judgments else None
    print(json.dumps(review(args.directory, checks), ensure_ascii=False, indent=2))
