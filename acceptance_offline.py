"""Run the workspace photo worker against licensed, independently labelled photos."""
import argparse
from collections import Counter
import csv
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from time import monotonic

from PIL import Image, ImageOps
from werkzeug.datastructures import FileStorage

from catalogue_labels import Catalogue
from workspace_media import prepare_photo
from workspace_photo_review import _review


def records(csv_path):
    with csv_path.open(encoding='utf-8', newline='') as source:
        return list(csv.DictReader(source))


def upload_sample(raw):
    """Use a licensed reduced copy only if the untouched original exceeds the app limit."""
    with Image.open(BytesIO(raw)) as image:
        if image.width * image.height <= 50_000_000:
            return raw, False
        if image.format in ('JPEG', 'MPO'):
            image.draft('RGB', (3600, 3600))
        preview = ImageOps.exif_transpose(image).convert('RGB')
        preview.thumbnail((3600, 3600))
        buffer = BytesIO()
        preview.save(buffer, 'JPEG', quality=95)
        return buffer.getvalue(), True

def evaluate(museum_dir, modern_dir):
    base = Path(__file__).resolve().parent
    museum = records(base / 'benchmark_sources.csv')
    available = records(base / 'benchmark_licensed_photos.csv')
    models = records(base / 'benchmark_product_sources.csv')
    catalogue = Catalogue.read(base / 'benchmark_product_sources.csv')
    pending = {row['reference_id'] for row in models}
    outcomes = []
    for row in museum + available:
        label = row['reference_id']
        folder = modern_dir if row in available else museum_dir
        file = folder / row.get('file', f'{label}.jpg')
        raw = file.read_bytes()
        if sha256(raw).hexdigest() != row['sha256']:
            raise ValueError(f'{label}: source file checksum mismatch')
        pending.discard(label)
        # Do not expose the filename, expected answer or catalogue record ID to the worker.
        sample, reduced = upload_sample(raw)
        _, _, _, preview, hint, codes, sharpness = prepare_photo(
            FileStorage(stream=BytesIO(sample), filename='frame.jpg'),
        )
        report = _review([dict(slot=1, preview=preview, hint=hint, codes=codes, sharpness=sharpness)], catalogue)
        evidence = report['product_evidence']
        matches = evidence.get('catalogue_matches', [])
        status = 'UNRESOLVED'
        if len(matches) == 1 and matches[0]['status'] == 'CATALOGUE_CANDIDATE':
            if matches[0]['reference_id'] != label:
                status = 'WRONG_CATALOGUE_MATCH'
            elif row.get('model_scope') == 'exact':
                status = 'CORRECT_EXACT_MODEL'
            else:
                status = 'MATCH_WITH_UNVERIFIED_VARIANT'
        outcomes.append({
            'reference_id': label, 'result': status, 'measurement_hint': report['photos'][0]['hint'],
            'photo_role': report['photos'][0]['role'], 'suggested_main': report['suggested_main'],
            'catalogue_matches': [match['reference_id'] for match in matches],
            'barcode_status': (
                evidence['barcode'][0]['status'] if evidence.get('barcode') else None),
            'source_reduced_for_upload': reduced,
            'admitted': False,
        })
    counts = Counter(row['result'] for row in outcomes)
    return {'mode': 'local_photo_worker', 'documented_objects': len(museum) + len(models),
            'tested_objects': len(outcomes), 'unavailable_photo_reference_ids': sorted(pending),
            'museum_objects': len(museum), 'modern_objects': len(available),
            'exact_modern_references': sum(row.get('model_scope') == 'exact' for row in available),
            'unresolved_fraction': counts['UNRESOLVED'] / len(outcomes),
            'counts': dict(counts), 'admitted': 0, 'critical_category_errors': None, 'outcomes': outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--museum-dir', type=Path, required=True)
    parser.add_argument('--modern-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    started = monotonic()
    report = evaluate(args.museum_dir, args.modern_dir)
    report['elapsed_seconds'] = round(monotonic() - started, 2)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'outcomes'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
