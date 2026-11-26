"""Optional local matching of a photographed model label to an operator's catalogue."""
import csv
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO, StringIO
from functools import lru_cache
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from PIL import Image

from measurement_units import metric_value


SPEC_FIELDS = ('weight_kg', 'length_cm', 'width_cm', 'height_cm')


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
            entry = dict(reference_id=identity, brand=brand, model=model, type=kind, url=url)
            material = (row.get('material') or '').strip()
            specs = {'material': material} if 0 < len(material) <= 160 else {}
            for field in SPEC_FIELDS:
                if (row.get(field) or '').strip():
                    weight = field == 'weight_kg'
                    specs[field] = metric_value(row[field], 'kg' if weight else 'cm', weight=weight)
            if specs:
                entry['specs'] = specs
            entries.append(entry)
            identifiers.add(identity)
        if not entries:
            raise ValueError('Catalogue has no model records')
        return cls(sha256(raw).hexdigest(), tuple(entries))

    def match(self, observations, *, include_leads=False):
        matches = {}
        leads = {}
        for photo, text in observations:
            lines = [_words(line) for line in text.splitlines() if line.strip()]
            full = ' '.join(lines)
            for entry in self.entries:
                brand, model = _words(entry['brand']), _words(entry['model'])
                # Full label lines preserve variant suffixes such as Pico 2 W.
                suffix = model.removeprefix(f'{brand} ')
                brand_seen = f' {brand} ' in f' {full} '
                has_full = any(line in {model, f'model {model}', f'{brand} {model}', f'{brand} model {model}'}
                               for line in lines)
                has_suffix = (include_leads and len(suffix) >= 4 and any(ch.isdigit() for ch in suffix)
                              and any(line in {suffix, f'model {suffix}'} for line in lines))
                if brand_seen and (has_full or has_suffix):
                    bucket = matches
                elif has_suffix or (include_leads and has_full):
                    bucket = leads
                else:
                    continue
                result = bucket.setdefault(entry['reference_id'], {**entry, 'photos': []})
                if photo not in result['photos']:
                    result['photos'].append(photo)
        unresolved = leads.keys() - matches.keys()
        if include_leads and unresolved:
            # A different possible model in the same album suspends exact-match enrichment.
            return ([{**row, 'status': 'CONTESTED'} for row in matches.values()]
                    + [{**row, 'status': 'CATALOGUE_LEAD'} for key, row in leads.items() if key in unresolved])
        status = 'CATALOGUE_CANDIDATE' if len(matches) == 1 else 'CONTESTED'
        return [{**row, 'status': status} for row in matches.values()]


@lru_cache(maxsize=4)
def load_catalogue(filename, _revision):
    """Cache a parsed catalogue until its file revision changes."""
    return Catalogue.read(filename)


def read_label_texts(frames):
    binary = shutil.which('tesseract')
    if not binary:
        return []
    observations = []
    for slot, preview in frames[:4]:
        try:
            with Image.open(BytesIO(preview)) as image:
                sample = image.convert('L')
                sample.thumbnail((1350, 1350))
                picture = BytesIO()
                sample.save(picture, 'PNG')
            reply = subprocess.run([binary, 'stdin', 'stdout', '-l', 'eng', '--psm', '11'],
                                   input=picture.getvalue(), capture_output=True, timeout=2.5, check=False)
        except (OSError, ValueError, Image.DecompressionBombError, subprocess.TimeoutExpired):
            continue
        if reply.returncode == 0:
            observations.append((slot, reply.stdout[:8000].decode('utf-8', errors='ignore')))
    return observations


def scan_labels(frames, catalogue):
    return catalogue.match(read_label_texts(frames))


def catalogue_claims(matches):
    """Expose exact label matches as documentation for the model, never as specimen measurements."""
    claims = []
    for match in matches:
        if match['status'] != 'CATALOGUE_CANDIDATE':
            continue
        for field, value in {**{key: match[key] for key in ('brand', 'model', 'type')},
                             **match.get('specs', {})}.items():
            claims.append(dict(field=field, value=value, status='CATALOGUE_REFERENCE', photos=match['photos'],
                               references=[dict(url=match['url'], kind='CATALOG',
                                                excerpt=f"{match['model']} • {field}: {value}")],
                               contradictions=[]))
    return claims
