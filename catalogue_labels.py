"""Optional local matching of a photographed model label to an operator's catalogue."""
import csv
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from PIL import Image


def _words(text):
    return ' '.join(re.findall(r'[^\W_]+', text.casefold()))


@dataclass(frozen=True)
class Catalogue:
    digest: str
    entries: tuple

    @classmethod
    def read(cls, filename):
        with Path(filename).open('rb') as stream:
            raw = stream.read(2_000_001)
        if not raw or len(raw) > 2_000_000:
            raise ValueError('Catalogue size is out of range')
        entries = []
        identifiers = set()
        for row in csv.DictReader(StringIO(raw.decode('utf-8-sig'))):
            identity, brand, model, kind, url = (
                (row.get(key) or '').strip()
                for key in ('reference_id', 'brand', 'model', 'object_type', 'source_url')
            )
            page = urlsplit(url)
            if (not identity or identity in identifiers or len(_words(brand)) < 3 or len(_words(model)) < 4
                    or not kind or page.scheme != 'https' or not page.hostname or page.username or page.password):
                raise ValueError('Catalogue record or source is invalid')
            entries.append(dict(reference_id=identity, brand=brand, model=model, type=kind, url=url))
            identifiers.add(identity)
        if not entries:
            raise ValueError('Catalogue has no model records')
        return cls(sha256(raw).hexdigest(), tuple(entries))

    def match(self, observations):
        matches = {}
        for photo, text in observations:
            lines = [_words(line) for line in text.splitlines() if line.strip()]
            full = ' '.join(lines)
            for entry in self.entries:
                brand, model = _words(entry['brand']), _words(entry['model'])
                if f' {brand} ' not in f' {full} ':
                    continue
                # Full label lines preserve variant suffixes such as Pico 2 W.
                if any(line in {model, f'model {model}', f'{brand} {model}', f'{brand} model {model}'}
                       for line in lines):
                    result = matches.setdefault(entry['reference_id'], {**entry, 'photos': []})
                    if photo not in result['photos']:
                        result['photos'].append(photo)
        status = 'CATALOGUE_CANDIDATE' if len(matches) == 1 else 'CONTESTED'
        return [{**row, 'status': status} for row in matches.values()]


def scan_labels(frames, catalogue):
    binary = shutil.which('tesseract')
    if not binary:
        return []
    observations = []
    for slot, preview in frames[:4]:
        with Image.open(BytesIO(preview)) as image:
            sample = image.convert('L')
            sample.thumbnail((1350, 1350))
            picture = BytesIO()
            sample.save(picture, 'PNG')
        try:
            reply = subprocess.run([binary, 'stdin', 'stdout', '-l', 'eng', '--psm', '11'],
                                   input=picture.getvalue(), capture_output=True, timeout=2.5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if reply.returncode == 0:
            observations.append((slot, reply.stdout[:8000].decode('utf-8', errors='ignore')))
    return catalogue.match(observations)
