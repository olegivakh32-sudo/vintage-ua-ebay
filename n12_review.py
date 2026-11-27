"""Evaluate final N12 cards against the withheld references after the run."""
import argparse
from collections import Counter
import json
from pathlib import Path

from acceptance_stage import sha
from multiview_review import FIELDS, review_run
from n12_acceptance import ARTIFACTS, LOTS, STAGE, sealed_plan


REFERENCES = ARTIFACTS / 'lotnexo-next12-reference.json'


def available_fields(plan, references=REFERENCES):
    if sha(references) != plan['reference_sha256']:
        raise ValueError('Evaluator references changed after freezing')
    raw = json.loads(references.read_bytes())
    rows = {row['lot']: row for row in raw}
    if len(raw) != len(LOTS) or set(rows) != set(LOTS):
        raise ValueError('Evaluator references cover a different cohort')
    values = {}
    for lot in LOTS:
        row = rows[lot]
        values[lot] = {name: row[name] for name in FIELDS if row.get(name)}
        values[lot]['primary_source'] = [row['source']] + ([row['source_record']] if row.get('source_record') else [])
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', nargs='?', type=Path)
    parser.add_argument('--judgments', type=Path)
    args = parser.parse_args()
    plan = sealed_plan(verify_runtime=False)
    expected = available_fields(plan)
    if args.results is None:
        report = dict(Counter(field for row in expected.values() for field in row))
    else:
        judgments = json.loads(args.judgments.read_bytes()) if args.judgments else {}
        report = review_run(plan, STAGE, expected, args.results, judgments)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
