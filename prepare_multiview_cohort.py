"""Build a blind, identical photo package for both recognition systems."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zipfile import ZipFile, ZipInfo

from PIL import Image, ImageOps
import requests


PUBLIC_IMAGE_HOSTS = {'openaccess-cdn.clevelandart.org', 'upload.wikimedia.org'}
MAX_BYTES = 10 * 1024 * 1024


def image_bytes(url, expected, cache=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in PUBLIC_IMAGE_HOSTS:
        raise ValueError('Unexpected source host')
    cached = cache / expected if cache else None
    if cached and cached.is_file():
        raw = cached.read_bytes()
    else:
        with requests.get(url, stream=True, timeout=(4, 9), allow_redirects=False,
                          headers={'User-Agent': 'LOTNEXO-acceptance/1.0 (public image benchmark)'}) as response:
            if response.status_code != 200:
                raise ValueError(f'Image HTTP {response.status_code}')
            raw = response.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(raw) > MAX_BYTES or sha256(raw).hexdigest() != expected:
        raise ValueError('Source image did not match the frozen hash')
    return raw


def blind_jpeg(raw):
    with Image.open(BytesIO(raw)) as frame:
        if frame.format != 'JPEG' or frame.width * frame.height > 64_000_000:
            raise ValueError('Unsupported image for comparison')
        frame = ImageOps.exif_transpose(frame).convert('RGB')
        frame.thumbnail((2000, 2000))
        output = BytesIO()
        frame.save(output, 'JPEG', quality=95)
    return output.getvalue()


def prepare(manifest, destination, cache=None, *, selected_lots=()):
    rows = json.loads(manifest.read_text(encoding='utf-8'))
    selected = set(selected_lots)
    if len(selected) != len(selected_lots):
        raise ValueError('Duplicate lot selection')
    ids = set()
    tasks = []
    for row in rows:
        lot, photos = row['lot'], row['photos']
        if selected and lot not in selected:
            continue
        if not re.fullmatch(r'MV-[0-9]{3}', lot) or lot in ids or not 2 <= len(photos) <= 24:
            raise ValueError('Invalid or duplicate multi-view lot')
        ids.add(lot)
        hashes = [photo['sha256'] for photo in photos]
        if len(set(hashes)) != len(hashes) or not all(re.fullmatch(r'[a-f0-9]{64}', value) for value in hashes):
            raise ValueError('Invalid or duplicate image')
        tasks.extend((f'{lot}/{number:02d}.jpg', photo) for number, photo in enumerate(photos, 1))
    if not tasks or selected - ids:
        raise ValueError('Empty cohort or selected lot missing from the manifest')

    def encode(task):
        name, photo = task
        return name, blind_jpeg(image_bytes(photo['url'], photo['sha256'], cache))

    with ThreadPoolExecutor(max_workers=5) as pool:
        prepared = list(pool.map(encode, tasks))
    packaged = [{'file': name, 'sha256': sha256(data).hexdigest()} for name, data in prepared]
    with ZipFile(destination, mode='x') as archive:
        for name, data in prepared:
            archive.writestr(ZipInfo(name), data)
        archive.writestr(ZipInfo('photos.json'), json.dumps(packaged, indent=2))
    return {'objects': len(ids), 'photos': len(packaged), 'archive': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--cache', type=Path, help='Optional local copies keyed by original SHA-256.')
    parser.add_argument('--lot', action='append', default=[], help='Select one frozen lot ID, repeat to select more.')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.manifest, args.archive, args.cache, selected_lots=args.lot)))
    except (OSError, KeyError, ValueError, requests.RequestException) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    main()
