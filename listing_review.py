"""Evidence-backed export and draft text for an unpublished HOLD listing."""
from copy import deepcopy
import json
from pathlib import Path
import re

from identity_evidence import normalized_claim
from product_evidence import PHYSICAL_FIELDS, photo_type, text_value


DIRECT = {'SELLER_VERIFIED', 'PHOTO_OBSERVATION'}
DOCUMENTED = {'CATALOGUE_REFERENCE', 'CORROBORATED_REFERENCE'}
TYPE_TAIL = re.compile(r'\s+(?:with|featuring|equipped with|having|for)\s+|[;\n]', re.I)
AMBIGUOUS_TYPE = re.compile(r'\b(?:or|possibly|probable|likely|assorted|unknown|uncertain)\b', re.I)
IDENTIFIER = re.compile(r'[A-Z0-9]+(?:-[A-Z0-9]+)+')
VISUAL_SUBSTITUTIONS = {frozenset(pair) for pair in ('I1', 'O0', 'S5', 'B8', 'Z2', 'G6')}
CODE_RECORDS = Path(__file__).with_name('catalogue_identifiers.json')


def evidence_value(evidence, field, statuses):
    rows = evidence.get(field, [])
    if any(row['status'] == 'CONTESTED' for row in rows):
        return None
    values = {normalized_claim(str(row['value'])): row['value'] for row in rows if row['status'] in statuses}
    return next(iter(values.values())) if len(values) == 1 else None


def canonical_product_type(evidence, suggested=''):
    observed = photo_type(evidence)
    if not observed:
        return ''
    phrase = TYPE_TAIL.split(observed, maxsplit=1)[0].strip(' .,:/-')
    documented = evidence_value(evidence, 'type', DOCUMENTED)
    normalized = normalized_claim(phrase)
    values = (documented,) if AMBIGUOUS_TYPE.search(phrase) else (documented, suggested)
    for value in values:
        if (text_value(value, 65) and not AMBIGUOUS_TYPE.search(value)
                and re.search(r'(?<!\w)' + re.escape(normalized_claim(value)) + r'(?!\w)', normalized)):
            return value.strip()
    return phrase if 2 < len(phrase) <= 65 and not AMBIGUOUS_TYPE.search(phrase) else ''


def _identifier_corrections(evidence):
    brand = evidence_value(evidence, 'brand', DIRECT)
    model = evidence_value(evidence, 'model', DIRECT)
    if not (brand and model):
        return []
    try:
        records = json.loads(CODE_RECORDS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        records = []
    codes = {code: record['url'] for record in records
             if normalized_claim(record['brand']) == normalized_claim(brand)
             and normalized_claim(record['model']) == normalized_claim(model)
             for code in record['codes']}
    documented_model = evidence_value(evidence, 'model', DOCUMENTED)
    if documented_model and IDENTIFIER.fullmatch(documented_model):
        for row in evidence.get('model', []):
            if row['status'] in DOCUMENTED and row.get('references'):
                codes.setdefault(documented_model, row['references'][0]['url'])
    if not codes:
        return []
    corrections = []
    for observation in evidence.get('marking', []):
        if observation['status'] != 'PHOTO_OBSERVATION':
            continue
        for original in IDENTIFIER.findall(observation['value']):
            if original in codes or not any(c.isdigit() for c in original):
                continue
            matches = []
            for code in codes:
                if len(original) != len(code):
                    continue
                differences = {frozenset((a, b)) for a, b in zip(original, code) if a != b}
                if sum(a != b for a, b in zip(original, code)) == 1 and differences <= VISUAL_SUBSTITUTIONS:
                    matches.append(code)
            if len(matches) == 1:
                code = matches[0]
                correction = {'observed': original, 'value': code, 'status': 'DOCUMENT_MATCH',
                              'photos': observation.get('photos', []), 'url': codes[code]}
                if correction not in corrections:
                    corrections.append(correction)
    return corrections


def export_review(evidence, suggested_type=''):
    """Keep all observations, source statuses and the original OCR in the export."""
    return {'type': canonical_product_type(evidence, suggested_type), 'fields': deepcopy(evidence),
            'ocr_corrections': _identifier_corrections(evidence),
            'measurements': {field: {
                'actual': evidence_value({field: [row for row in evidence.get(field, [])
                                        if row['status'] in {'PHOTO_MEASUREMENT', 'SELLER_VERIFIED'}]},
                                         field, {'PHOTO_MEASUREMENT', 'SELLER_VERIFIED'}),
                'reference': [row for row in evidence.get(field, []) if row['status'] in DOCUMENTED],
                'estimate': [row for row in evidence.get(field, []) if row['status'] == 'SHIPPING_ESTIMATE'],
            } for field in PHYSICAL_FIELDS}}


def corrected_ocr(text, corrections):
    replacements = {row['observed']: row['value'] for row in corrections}
    return IDENTIFIER.sub(lambda match: replacements.get(match[0], match[0]), text or '')


def hold_seo_preview(review):
    """Build internal SEO text from checked fields, without pricing or shipping guesses."""
    kind = review['type']
    if not kind:
        return None
    evidence = review['fields']
    brand = evidence_value(evidence, 'brand', DIRECT)
    model = evidence_value(evidence, 'model', DIRECT)
    material = evidence_value(evidence, 'material', DIRECT)
    reference_material = evidence_value(evidence, 'material', DOCUMENTED)
    title = kind
    for value in (model, brand):
        if value and normalized_claim(value) not in normalized_claim(title) and len(value) + len(title) < 80:
            title = f'{value} {title}'
    title_material = material or reference_material
    if title_material and len(f'{title} {title_material}') <= 80:
        title += f' {title_material}'
    specifics = {'Type': kind}
    for label, value in (('Brand', brand), ('Model', model), ('Material', material),
                         ('Country/Region of Manufacture', evidence_value(evidence, 'country', DIRECT))):
        if value:
            specifics[label] = value
    details = [f'Item shown in the photos: {kind}.']
    if model:
        details.append(f'Visible model marking: {model}.')
    if material:
        details.append(f'Material: {material}.')
    elif reference_material:
        details.append(f'Documented catalogue material: {reference_material}.')
    defects = [row['value'] for row in evidence.get('defect', []) if row['status'] == 'PHOTO_OBSERVATION']
    if defects:
        details.append('Visible condition details: ' + ' '.join(defects))
    details.append('Refer to the photographs for visible condition and included parts. Verify any details that the photos do not establish before listing.')
    return {'title': title, 'description': ' '.join(details), 'item_specifics': specifics,
            'condition_note': 'Review visible condition and included parts against the photos.',
            'seo_keywords': list(dict.fromkeys([kind, title]))}
