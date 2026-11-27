"""Freeze a photo-only acceptance pack and a separate licensed answer key."""
import argparse
import csv
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import random
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image, ImageOps

from recognition_benchmark import cohort_references


ROOT = Path(__file__).parent


def freeze(museum, modern, output, answer_key):
    manifest = ROOT / 'benchmark_acceptance_cohort.csv'
    with manifest.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    prior = json.loads((ROOT / 'test_artifacts/recognition/lotnexo-live-pilot-sources.json').read_text())
    excluded = {photo['sha256'] for lot in prior for photo in lot['photos']}
    rows = [row for row in rows if row['sha256'] not in excluded]
    if len(rows) < 50 or len({row['sha256'] for row in rows}) != len(rows):
        raise ValueError('Acceptance needs at least 50 distinct photos outside the paid pilot')
    key = {row['lot']: row for row in cohort_references(manifest)}
    random.Random(980704).shuffle(rows)
    photos, labels = [], []
    folders = {'museum': museum, 'modern': modern}
    with ZipFile(output, 'w') as package:
        for number, row in enumerate(rows, 1):
            identity = row['reference_id']
            raw = (folders[row['group']] / f'{identity}.jpg').read_bytes()
            if sha256(raw).hexdigest() != row['sha256']:
                raise ValueError(f'Changed source image: {identity}')
            with Image.open(BytesIO(raw)) as source:
                if source.format in ('JPEG', 'MPO'):
                    source.draft('RGB', (2000, 2000))
                source.thumbnail((2000, 2000))
                image = ImageOps.exif_transpose(source).convert('RGB')
                photo = BytesIO()
                image.save(photo, 'JPEG', quality=95)
            lot = f'P{number:03d}'
            name = f'{lot}/01.jpg'
            encoded = photo.getvalue()
            digest = sha256(encoded).hexdigest()
            package.writestr(ZipInfo(name), encoded, compress_type=ZIP_DEFLATED)
            photos.append({'file': name, 'sha256': digest})
            labels.append(key[identity] | {
                'lot': lot, 'reference_id': identity, 'source_url': row['fact_url'], 'photo_source': row['photo_source'],
                'photo_sha256': digest, 'source_sha256': row['sha256'], 'photo_credit': row['credit'],
                'photo_license': row['license'], 'photo_license_url': row['license_url'],
            })
        package.writestr(ZipInfo('photos.json'), json.dumps(photos, indent=2) + '\n')
    answer_key.write_text(json.dumps(labels, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return {'objects': len(labels), 'sha256': sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('museum', 'modern', 'output', 'answer-key'):
        parser.add_argument(f'--{option}', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.museum, args.modern, args.output, args.answer_key)))
