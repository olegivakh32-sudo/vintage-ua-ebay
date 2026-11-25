"""Offline retail barcode hints for catalogue searches, never listing assertions."""
import re

from PIL import Image
import zxingcpp

BARCODE_HINT_VERSION = 1
_FORMATS = (zxingcpp.BarcodeFormat.EAN8, zxingcpp.BarcodeFormat.EAN13, zxingcpp.BarcodeFormat.UPCA)


def canonical_gtin(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{8}|[0-9]{12,14}', value) or not value.strip('0'):
        return None
    weighted = sum(int(digit) * (3 if offset % 2 == 0 else 1)
                   for offset, digit in enumerate(reversed(value[:-1])))
    return value.zfill(14) if (weighted + int(value[-1])) % 10 == 0 else None


def read_retail_codes(image):
    sample = image.convert('L')
    sample.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    hits = zxingcpp.read_barcodes(sample, formats=_FORMATS, try_rotate=True,
                                  try_downscale=True, try_invert=False)
    return sorted({hit.text for hit in hits if canonical_gtin(hit.text)})[:8]


def catalogue_hints(observations):
    detected = {}
    for photo, codes in observations:
        for text in codes:
            key = canonical_gtin(text)
            if key:
                clue = detected.setdefault(key, {'code': text, 'photos': []})
                if len(text) < len(clue['code']):
                    clue['code'] = text
                if photo not in clue['photos']:
                    clue['photos'].append(photo)
    return {'status': 'UNIQUE' if len(detected) == 1 else 'CONFLICT' if detected else 'NONE',
            'candidates': list(detected.values())}


def research_identifier(hints):
    if hints['status'] == 'UNIQUE':
        clue, = hints['candidates']
        return (f"Retail barcode search lead only: {clue['code']} on seller photos {clue['photos']}. "
                "Determine whether the label is on the sold item, its matching retail box, "
                "or an unrelated accessory. Verify brand, model, size and variant independently.")
    if hints['status'] == 'CONFLICT':
        return 'Multiple retail barcodes were found. Do not infer a model or variant from any one of them.'
    return 'No independently decoded retail barcode was found.'
