"""Evidence-backed export and draft text for an unpublished HOLD listing."""
from copy import deepcopy
import json
from pathlib import Path
import re

from identity_evidence import PRIMARY, normalized_claim
from product_evidence import AMBIGUOUS_TYPE, PHYSICAL_FIELDS, TYPE_TAIL, concise_type, photo_type, reviewed_product_type, text_value


DIRECT = {'SELLER_VERIFIED', 'PHOTO_OBSERVATION'}
DOCUMENTED = {'CATALOGUE_REFERENCE', 'CORROBORATED_REFERENCE'}
IDENTIFIER = re.compile(r'[A-Z0-9]+(?:-[A-Z0-9]+)+')
VISUAL_SUBSTITUTIONS = {frozenset(pair) for pair in ('I1', 'O0', 'S5', 'B8', 'Z2', 'G6')}
CODE_RECORDS = Path(__file__).with_name('catalogue_identifiers.json')
TITLE_FEATURE = re.compile(r'^(pierced|engraved|embossed|scalloped|folding|retractable|adjustable)\b', re.I)


def evidence_value(evidence, field, statuses):
    rows = evidence.get(field, [])
    if any(row['status'] == 'CONTESTED' for row in rows):
        return None
    values = {normalized_claim(str(row['value'])): row['value'] for row in rows if row['status'] in statuses}
    return next(iter(values.values())) if len(values) == 1 else None


def canonical_product_type(evidence, suggested=''):
    if review := reviewed_product_type(evidence):
        return review['type']
    if any(row['status'] == 'SELLER_VERIFIED' for row in evidence.get('type', [])):
        return photo_type(evidence)
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
    return concise_type(phrase) if len(phrase) > 2 else ''


def _model_marking(evidence):
    """Separate a corroborated model code from additional visible label text."""
    rows = evidence.get('model', [])
    observed = evidence_value(evidence, 'model', DIRECT)
    if not observed or ';' not in observed or any(row['status'] == 'SELLER_VERIFIED' for row in rows):
        return None
    labels = [part.strip() for part in observed.split(';')]
    codes = [part for part in labels if IDENTIFIER.fullmatch(part) and any(char.isdigit() for char in part)]
    if len(labels) != 2 or len(codes) != 1 or not all(labels):
        return None
    code = codes[0]
    visible = {row['value']: set(row.get('photos', [])) for row in evidence.get('marking', [])
               if row['status'] == 'PHOTO_OBSERVATION' and row.get('marking_kind') == 'MODEL_NUMBER'}
    common_photos = visible.get(labels[0], set()) & visible.get(labels[1], set())
    exact_code = re.compile(r'(?<![\w-])' + re.escape(code) + r'(?![\w-])', re.I)
    for row in rows:
        if not common_photos.intersection(row.get('photos', [])) or row.get('contradictions'):
            continue
        for source in row.get('references', []):
            if source['kind'] in PRIMARY and exact_code.search(source['excerpt']):
                return {'code': code, 'visible_text': observed, 'labels': labels, 'url': source['url']}
    return None


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
            'identity_review': deepcopy(reviewed_product_type(evidence)),
            'model_marking': _model_marking(evidence),
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


def _marked_product_family(evidence, kind, brand, model):
    if not (brand and model and len(kind.split()) > 1):
        return None
    head = kind.split()[-1]
    pattern = re.compile(rf'{re.escape(brand)} ([A-Za-z][A-Za-z0-9 -]{{1,24}}) {re.escape(head)}', re.I)
    families = {}
    for row in evidence.get('marking', []):
        if row['status'] != 'PHOTO_OBSERVATION':
            continue
        match = pattern.fullmatch(row['value'])
        if match and not AMBIGUOUS_TYPE.search(match[1]) and normalized_claim(match[1]) not in normalized_claim(model):
            families.setdefault(normalized_claim(match[1]), match[1])
    if len(families) == 1:
        label = f'{brand} {next(iter(families.values()))} {model} {head.capitalize()}'
        if len(label) <= 80:
            return label
    return None


def _observed_title_feature(evidence):
    observed = TITLE_FEATURE.match(photo_type(evidence))
    if observed and any(row['status'] == 'PHOTO_OBSERVATION'
                        and re.search(rf'\b{re.escape(observed[1])}\b', row['value'], re.I)
                        for row in evidence.get('feature', [])):
        return observed[1].capitalize()
    return None


def hold_seo_preview(review):
    """Build internal SEO text from checked fields, without pricing or shipping guesses."""
    kind = review['type']
    if not kind:
        return None
    evidence = review['fields']
    brand = evidence_value(evidence, 'brand', DIRECT)
    model = evidence_value(evidence, 'model', DIRECT)
    model_marking = review.get('model_marking')
    model_title = ' '.join(model_marking['labels']) if model_marking else model
    if model_marking:
        model = model_marking['code']
    material = evidence_value(evidence, 'material', DIRECT)
    reference_material = evidence_value(evidence, 'material', DOCUMENTED)
    title = kind
    for value in (model_title, brand):
        if value and normalized_claim(value) not in normalized_claim(title) and len(value) + len(title) < 80:
            title = f'{value} {title}'
    title = _marked_product_family(evidence, kind, brand, model_title) or title
    title_material = material or reference_material
    feature = _observed_title_feature(evidence)
    if title_material and normalized_claim(title_material) not in normalized_claim(title):
        if title == kind and len(f'{title_material} {title}') <= 80:
            title = f'{title_material.title() if title_material.islower() else title_material} {title}'
        elif len(f'{title} {title_material}') <= 80:
            title += f' {title_material}'
    if feature and not re.search(rf'\b{re.escape(feature)}\b', title, re.I) and len(f'{feature} {title}') <= 80:
        title = f'{feature} {title}'
    specifics = {'Type': kind}
    for label, value in (('Brand', brand), ('Model', model), ('Material', material),
                         ('Country/Region of Manufacture', evidence_value(evidence, 'country', DIRECT))):
        if value:
            specifics[label] = value
    details = [f'Item shown in the photos: {kind}.']
    if model:
        details.append(f'Visible model marking: {model}.')
    for field, label in (('brand', 'maker'), ('model', 'model'), ('country', 'country of manufacture')):
        if evidence_value(evidence, field, DIRECT) is None:
            documented = evidence_value(evidence, field, DOCUMENTED)
            if documented and not any(row['status'] in DIRECT for row in evidence.get(field, [])):
                details.append(f'Documented {label}: {documented}.')
    if material:
        details.append(f'Material: {material}.')
    elif reference_material:
        details.append(f'Documented catalogue material: {reference_material}.')
    period = evidence_value(evidence, 'period', DOCUMENTED | {'SELLER_VERIFIED'})
    if period:
        details.append(f'Recorded period: {period}.')
    for field, label in zip(PHYSICAL_FIELDS, ('Item Weight', 'Item Length', 'Item Width', 'Item Height')):
        measurement = review.get('measurements', {}).get(field, {}).get('actual')
        if type(measurement) in (int, float):
            specifics[label] = f"{measurement:g} {'kg' if field == 'weight_kg' else 'cm'}"
    defects = [row['value'] for row in evidence.get('defect', []) if row['status'] == 'PHOTO_OBSERVATION']
    if defects:
        details.append('Visible condition details: ' + ' '.join(defects))
    details.append('Refer to the photographs for visible condition and included parts.')
    return {'title': title, 'description': ' '.join(details), 'item_specifics': specifics,
            'condition_note': 'Review visible condition and included parts against the photos.',
            'seo_keywords': list(dict.fromkeys([kind, title]))}
