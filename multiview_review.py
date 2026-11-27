"""Grade only sealed results against the evaluator's separate references."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

from acceptance_stage import sha
from multiview_acceptance import ARCHIVE, ARTIFACTS, MANIFEST, STAGE, frozen_plan
from visual_protocol import machine_object


REFERENCES = ARTIFACTS / 'lotnexo-multiview-independent-reference.json'
SUPPLEMENT = ARTIFACTS / 'lotnexo-m12-reference-supplement.json'
FIELDS = ('identity', 'primary_source', 'brand', 'model', 'material', 'period',
          'documentary_dimensions', 'documentary_weight')
MEASUREMENTS = ('weight_kg', 'length_cm', 'width_cm', 'height_cm')
VERDICTS = {'CORRECT', 'INCORRECT', 'UNCONFIRMED'}


def available_fields(plan):
    if sha(REFERENCES) != plan['reference_sha256'] or sha(SUPPLEMENT) != plan['reference_supplement_sha256']:
        raise ValueError('Evaluator references changed after freezing')
    raw = json.loads(REFERENCES.read_bytes())
    supplement = json.loads(SUPPLEMENT.read_bytes())
    lots = plan['stages'][STAGE]['lots']
    references = {row['lot']: row for row in raw}
    if len(raw) != len(lots) or set(references) != set(lots) or set(supplement) - set(lots):
        raise ValueError('Evaluator references cover a different cohort')
    result = {}
    for lot in lots:
        source = references[lot]
        extra = supplement.get(lot, {})
        result[lot] = {field: source[field] for field in FIELDS if source.get(field)}
        result[lot]['primary_source'] = [url for url in (source['source'], source.get('source_record'), extra.get('source')) if url]
        if extra:
            result[lot].update(documentary_dimensions=extra['documentary_dimensions'],
                               documentary_weight=extra['documentary_weight'])
    return result


def reported_fields(prediction, card):
    export = prediction.get('product_export') or {}
    fields = export.get('fields', {})
    physical = export.get('measurements', {})
    result = {field: prediction.get(field) for field in ('identity', 'brand', 'model', 'material', 'period')}
    result['primary_source'] = sorted({reference['url'] for records in fields.values() for record in records
                                       for reference in record.get('references', []) if reference.get('url')})
    candidate = machine_object(card.get('identification') or '', 'REFERENCE_OBJECT_JSON').get('url')
    if isinstance(candidate, str) and candidate and candidate not in result['primary_source']:
        result['primary_source'].append(candidate)
    notes = fields.get('research_note', [])
    result['documentary_dimensions'] = (
        {key: physical[key]['reference'] for key in MEASUREMENTS[1:] if physical.get(key, {}).get('reference')}
        or [row['value'] for row in notes if row.get('name') == 'Catalogue dimensions'])
    result['documentary_weight'] = physical.get('weight_kg', {}).get('reference') or []
    by_configuration = machine_object(card.get('identification') or '', 'DOCUMENTARY_CONFIGURATION_JSON')
    if isinstance(by_configuration.get('source_url'), str) and by_configuration['source_url']:
        for field, key in (('documentary_dimensions', 'dimensions'), ('documentary_weight', 'weights')):
            values = by_configuration.get(key)
            if isinstance(values, list) and values and all(isinstance(value, str) and value.strip() for value in values):
                result[field] = {'other_reported_references': result[field], 'values_by_configuration': values,
                                 'claimed_source': by_configuration['source_url']}
    return {name: value for name, value in result.items() if value}


def grade(expected, reported, judgments):
    """Count coverage separately from sampled accuracy and verified completeness."""
    if set(reported) - set(expected) or set(judgments) - set(expected):
        raise ValueError('Result or judgment outside the sealed cohort')
    metrics = {name: Counter(eligible=0, filled=0, correct=0, incorrect=0, unconfirmed=0,
                             unknown=0, pending=0, asserted_without_reference=0) for name in FIELDS}
    rows = {}
    for lot, reference in expected.items():
        values = reported.get(lot, {})
        judgment = judgments.get(lot, {})
        if not isinstance(judgment, dict) or set(judgment) - (reference.keys() & values.keys()):
            raise ValueError('A verdict requires a reported value and a known reference')
        row = rows[lot] = {}
        for field in FIELDS:
            stats = metrics[field]
            value = values.get(field)
            if field not in reference:
                status = 'ASSERTED_WITHOUT_REFERENCE' if value else 'NO_REFERENCE'
            else:
                stats['eligible'] += 1
                if value:
                    stats['filled'] += 1
                    finding = judgment.get(field)
                    if finding is None:
                        status = 'PENDING'
                    else:
                        status = finding.get('verdict') if isinstance(finding, dict) else None
                        if (status not in VERDICTS or not isinstance(finding.get('evidence'), str)
                                or not finding['evidence'].strip()):
                            raise ValueError('Each judgment needs a supported verdict and an evidence note')
                else:
                    status = 'UNKNOWN'
            if status != 'NO_REFERENCE':
                stats[status.lower()] += 1
            row[field] = {'reference': reference.get(field), 'reported': value, 'status': status,
                          'review': judgment.get(field)}
    for stats in metrics.values():
        stats['reported_coverage'] = stats['filled'] / stats['eligible'] if stats['eligible'] else None
        stats['confirmed_completeness'] = (stats['correct'] / stats['eligible']
                                           if stats['eligible'] and not stats['pending'] else None)
        stats['accuracy_of_answers'] = (stats['correct'] / stats['filled']
                                         if stats['filled'] and not stats['pending'] else None)
    return {'fields': metrics, 'lots': rows}


def cost_summary(ledger, lots):
    by_lot = {lot: [] for lot in lots}
    for row in ledger['attempts']:
        by_lot[row['lot']].append(row)
    per_lot = {}
    for lot, attempts in by_lot.items():
        charged = [row for row in attempts if 'metered_usd' in row]
        unsettled = [row for row in attempts if row['status'] in {'IN_FLIGHT', 'STOPPED'} and 'metered_usd' not in row]
        per_lot[lot] = {
            'metered_usd': str(sum((Decimal(row['metered_usd']) for row in charged), Decimal(0))),
            'unsettled_reserve_usd': str(sum((Decimal(row['reserved_usd']) for row in unsettled), Decimal(0))),
            'metered_responses': len(charged),
            'web_calls': sum(row['web_calls'] for row in charged),
            'elapsed_request_ms': sum(row.get('elapsed_ms', 0) for row in attempts),
            'step_statuses': {row['step']: {'status': row['status'], 'reason': row.get('reason')}
                              for row in attempts},
        }
    for field in ('metered_usd', 'unsettled_reserve_usd'):
        if sum(Decimal(row[field]) for row in per_lot.values()) != Decimal(ledger[field]):
            raise ValueError('Per-lot amounts disagree with the protected ledger')
    return {field: ledger[field] for field in ('metered_usd', 'unsettled_reserve_usd')} | {'lots': per_lot}


def review(plan, results, judgments=None):
    expected = available_fields(plan)
    ledger = json.loads((results / 'run.json').read_bytes())
    phase = plan['stages'][STAGE]
    for key, value in {'stage': STAGE, 'algorithm_revision': plan['algorithm_revision'],
                       'blind_sha256': phase['blind_sha256'], 'reference_sha256': plan['reference_sha256']}.items():
        if ledger.get(key) != value:
            raise ValueError('Results do not match the sealed test')
    predictions_file = results / 'predictions.json'
    raw = json.loads(predictions_file.read_bytes()) if predictions_file.exists() else []
    predictions = {row['lot']: row for row in raw}
    if len(predictions) != len(raw) or predictions.keys() - expected.keys():
        raise ValueError('Unexpected or duplicate prediction lot')
    reported, claimed_actual, admitted, stages = {}, {}, [], {}
    for lot in expected:
        row = predictions.get(lot, {})
        card_file = results / f'{lot}-card.json'
        card = json.loads(card_file.read_bytes()) if card_file.exists() else {}
        reported[lot] = reported_fields(row, card)
        stages[lot] = {'http': card.get('stage_http'), 'messages': card.get('stage_messages', []),
                       'draft_status': (card.get('review_card') or {}).get('draft_status', 'NO_DRAFT')}
        if row.get('admitted') or card.get('admitted'):
            admitted.append(lot)
        if claims := {field: value for field, value in (row.get('measurement') or {}).items() if value is not None}:
            claimed_actual[lot] = claims
    result = grade(expected, reported, judgments or {})
    identity = result['fields']['identity']
    result.update(cost=cost_summary(ledger, expected), stage_outcomes=stages, admitted=admitted,
                  actual_measurement_claims_requiring_review=claimed_actual,
                  pilot_identity_checkpoint_met=(not identity['pending'] and not admitted
                                                 and identity['correct'] >= phase['minimum_correct']),
                  paid_run_status=ledger['status'], launch_acceptance_decided=False)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', nargs='?', type=Path)
    parser.add_argument('--judgments', type=Path)
    args = parser.parse_args()
    plan = frozen_plan(MANIFEST, ARCHIVE)
    if args.results is None:
        expected = available_fields(plan)
        print(json.dumps({name: sum(name in fields for fields in expected.values()) for name in FIELDS}, indent=2))
    else:
        marks = json.loads(args.judgments.read_bytes()) if args.judgments else None
        print(json.dumps(review(plan, args.results, marks), ensure_ascii=False, indent=2))
