"""Score frozen S1 predictions offline. Keep this command on the evaluator's side."""
import argparse
import json
from pathlib import Path

from acceptance_stage import sha
from recognition_benchmark import FIELDS, compare_systems, score
from s1_acceptance import PLAN_SHA


def review(plan_path, reference_path, prediction_path, chatgpt_path=None):
    if sha(plan_path) != PLAN_SHA:
        raise ValueError('S1 manifest differs from the frozen plan')
    plan = json.loads(plan_path.read_bytes())
    if sha(reference_path) != plan['reference_sha256']:
        raise ValueError('Reference checksum differs from the frozen plan')
    known_ids = plan['stages']['S1']['lots']
    original = {row['lot']: row for row in json.loads(reference_path.read_bytes())}
    labels, coverage = [], []
    for lot in known_ids:
        row = original[lot]
        label = {'lot': lot, **{key: row[key] for key in FIELDS if row.get(key)}}
        category = row['category_reference'].get('verified_leaf_id')
        if category:
            label['category_id'] = [category]
        labels.append(label)
        source = row['documentary_reference'] or {}
        coverage.append({
            'lot': lot, 'documentary_dimensions': bool(source.get('dimensions_cm')
                                                        or source.get('dimension_components_cm')),
            'documentary_net_weight': source.get('nominal_unpacked_weight_kg') is not None,
            'actual_specimen_weight': row.get('specimen_or_package_weight_kg') is not None,
            'verified_category': bool(category),
            'verified_sale_or_parcel_reference': bool(row.get('price_and_shipping_reference')),
        })
    predictions = json.loads(prediction_path.read_bytes())
    result = score(labels, predictions, allow_partial_categories=True)
    report = {
        'algorithm_revision': plan['algorithm_revision'], 'reference_sha256': plan['reference_sha256'],
        'lotnexo_predictions_sha256': sha(prediction_path), 'reference_coverage': coverage,
        'strict_lotnexo': result,
        'preliminary_identity_threshold': result['fields']['identity']['correct'] >= plan['stages']['S1']['minimum_correct'],
        'manual_review_required': [
            'documentary dimensions by named component and configuration', 'documentary net weight',
            'source authenticity and exact-model match', 'SEO and factual claims',
            'category hierarchy and current required aspects', 'verified completed sales',
            'packed measurements and carrier eligibility', 'unknown fields and unsupported asserted facts',
        ],
    }
    if chatgpt_path is not None:
        baseline_hash = sha(chatgpt_path)
        if baseline_hash == report['lotnexo_predictions_sha256']:
            raise ValueError('Independent ChatGPT results must come from a separate run')
        report['chatgpt_predictions_sha256'] = baseline_hash
        report['paired'] = compare_systems(labels, predictions, json.loads(chatgpt_path.read_bytes()))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('references', type=Path)
    parser.add_argument('predictions', type=Path)
    parser.add_argument('--chatgpt', type=Path)
    args = parser.parse_args()
    print(json.dumps(review(args.plan, args.references, args.predictions, args.chatgpt), indent=2, ensure_ascii=False))
