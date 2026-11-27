"""Verify a cited Cleveland Museum record against the original uploaded photograph."""
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import json
import re
from urllib.parse import urlencode, urlsplit

from PIL import Image, ImageOps
import requests
from urllib3.exceptions import HTTPError

from identity_evidence import UNCERTAIN_CLAIM, normalized_claim


API = 'https://openaccess-api.clevelandart.org/api/artworks/'
IMAGE_HOST = 'openaccess-cdn.clevelandart.org'
ACCESSION = r'[0-9]{4}\.[0-9]+(?:\.[A-Za-z0-9-]+)*'
MAX_RECORDS_TO_VERIFY = 3


def record_key(url):
    if not isinstance(url, str) or len(url) > 300:
        return None
    try:
        page = urlsplit(url)
    except ValueError:
        return None
    if page.scheme != 'https' or page.query or page.fragment:
        return None
    if page.netloc in {'clevelandart.org', 'www.clevelandart.org'}:
        match = re.fullmatch(r'/(?:print/)?art/(' + ACCESSION + r')/?', page.path)
        return ('accession_number', match[1]) if match else None
    if page.netloc == 'openaccess-api.clevelandart.org':
        match = re.fullmatch(r'/api/artworks/([0-9]{1,10})/?', page.path)
        return ('id', match[1]) if match else None
    return None


def _read(url, limit):
    with requests.get(url, timeout=(2, 3), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            return b''
        data = response.raw.read(limit + 1, decode_content=True)
        return data if len(data) <= limit else b''


def image_digests(raw):
    """Accept identical files or the exact standard JPEG derivative. No similarity score."""
    if not raw or len(raw) > 10_000_000:
        return set()
    with Image.open(BytesIO(raw)) as source:
        if source.format != 'JPEG' or source.width * source.height > 16_000_000:
            return set()
        source.draft('RGB', (2000, 2000))
        source.thumbnail((2000, 2000))
        image = ImageOps.exif_transpose(source).convert('RGB')
        encoded = BytesIO()
        image.save(encoded, 'JPEG', quality=95)
    return {sha256(raw).hexdigest(), sha256(encoded.getvalue()).hexdigest()}


@lru_cache(maxsize=32)
def verified_record(key):
    field, value = key
    url = API + (value if field == 'id' else '?' + urlencode({field: value, 'limit': 2}))
    try:
        data = json.loads(_read(url, 200_000))['data']
        if isinstance(data, list):
            data = next((row for row in data if row.get(field) == value), {})
        accession = data.get('accession_number', '')
        title = data.get('title')
        if (str(data.get(field)) != value or data.get('share_license_status') != 'CC0'
                or not re.fullmatch(ACCESSION, accession) or not isinstance(title, str) or not title.strip()):
            return None
        image_url = data['images']['web']['url']
        image = urlsplit(image_url)
        if (image.scheme != 'https' or image.netloc != IMAGE_HOST or image.query or image.fragment
                or not re.fullmatch(r'/' + re.escape(accession) + r'/[A-Za-z0-9._-]+\.jpg', image.path)):
            return None
        digests = image_digests(_read(image_url, 3_000_000))
        return (data, digests) if digests else None
    except (requests.RequestException, HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError,
            Image.DecompressionBombError):
        return None


def museum_claims(candidate, observed_urls, photo_digests, *, load=verified_record):
    """Build internal documentation only when the search result and photo really match."""
    if not photo_digests:
        return []
    observed = {key for url in observed_urls if (key := record_key(url))}
    proposed = record_key(candidate.get('url'))
    # Try the model's observed candidate first, then other official records from the same search.
    ordered = sorted(observed, key=lambda key: (key != proposed, key))[:MAX_RECORDS_TO_VERIFY]
    matches, matched_keys = {}, set()
    for key in ordered:
        if record := load(key):
            data, digests = record
            photos = sorted(slot for slot, digest in photo_digests.items() if digest in digests)
            if photos:
                accession = data['accession_number']
                matches.setdefault(accession, (data, photos))
                matched_keys.add(key)
    if len(matches) != 1:
        return []
    accession, (data, photos) = next(iter(matches.items()))
    source = f'https://www.clevelandart.org/art/{accession}'
    fields = [('research_note', 'Catalogue record', f"{accession} · {data['title']}")]
    fields.append(('research_note', 'Catalogue dimensions', data.get('measurements')))
    technique = data.get('technique')
    if isinstance(technique, str) and UNCERTAIN_CLAIM.search(technique):
        fields.append(('research_note', 'Catalogue material qualification', technique))
    else:
        fields.append(('material', None, technique))
    fields.append(('period', None, data.get('creation_date')))
    kind = candidate.get('type') if proposed in matched_keys else None
    title_head = re.split(r'\s+(?:of|with|commemorating|depicting|showing)\s+', data['title'], maxsplit=1, flags=re.I)[0]
    type_labels = {normalized_claim(label) for label in (title_head, data.get('type')) if isinstance(label, str)}
    if isinstance(kind, str) and 3 <= len(kind) <= 65 and not UNCERTAIN_CLAIM.search(kind):
        if normalized_claim(kind) in type_labels:
            fields.append(('type', None, kind))
    return [dict(field=field, name=name, value=value.strip(), photos=photos, status='CATALOGUE_REFERENCE',
                 catalogue_photo_match=True, contradictions=[],
                 references=[dict(url=source, kind='MUSEUM', excerpt=f'{accession} · {value.strip()}')])
            for field, name, value in fields if isinstance(value, str) and 0 < len(value.strip()) <= 300]
