"""Structured evidence kept with a draft, independent of listing prose."""
import re

from identity_evidence import normalized_claim
from measurement_units import metric_value


FIELDS = ('type', 'brand', 'model', 'material', 'country', 'period', 'condition', 'defect', 'completeness',
          'marking', 'symbol', 'weight_kg', 'length_cm', 'width_cm', 'height_cm')
VISUAL_FIELDS = frozenset((*FIELDS[:11], 'feature'))
PHYSICAL_FIELDS = FIELDS[-4:]
SELLER_SOURCES = {'SELLER_ENTERED', 'SELLER_VERIFIED', 'SELLER_EDITED'}
UNKNOWN = {'unknown', 'unverified', 'not confirmed', 'not applicable', 'none', 'n/a', '—'}
ASPECTS = {
    'brand': 'brand', 'maker': 'brand', 'manufacturer': 'brand', 'manufacturerbrand': 'brand',
    'model': 'model', 'modelnumber': 'model', 'mpn': 'model', 'manufacturerpartnumber': 'model',
    'yearmanufactured': 'period', 'yearofmanufacture': 'period', 'manufactureyear': 'period',
    'timeperiodmanufactured': 'period', 'period': 'period', 'era': 'period', 'decade': 'period',
    'condition': 'condition', 'completeness': 'completeness', 'defect': 'defect', 'defects': 'defect',
}

NO_VISIBLE_DEFECT = re.compile(
    r'^(?:none(?:\s+(?:visible|observed|seen|apparent|detected))?|'
    r'no(?:\s+(?:visible|apparent|obvious))?\s+(?:defects?|damage|flaws?)'
    r'(?:\s+(?:visible|observed|seen|apparent|detected))?|'
    r'(?:видимих\s+)?(?:дефектів|пошкоджень)\s+(?:не\s+(?:видно|виявлено)|немає))[.!]?$',
    re.I,
)
TYPE_TAIL = re.compile(r'\s+(?:with|featuring|equipped with|having|for)\s+|[;\n]', re.I)
AMBIGUOUS_TYPE = re.compile(r'\b(?:or|possibly|probable|likely|assorted|unknown|uncertain)\b', re.I)


def concise_type(value):
    phrase = TYPE_TAIL.split(value, maxsplit=1)[0].strip(' .,:/-')
    return phrase if text_value(phrase, 65) and not AMBIGUOUS_TYPE.search(value) else ''


def no_visible_defect(value):
    return isinstance(value, str) and bool(NO_VISIBLE_DEFECT.fullmatch(' '.join(value.split())))


def text_value(value, limit=160):
    if isinstance(value, str) and not any(ord(letter) < 32 for letter in value.strip()):
        value = value.strip()
        if 0 < len(value) <= limit and normalized_claim(value) not in UNKNOWN:
            return value
    return ''


def photo_numbers(values, photo_count):
    return sorted({value for value in values if type(value) is int and 1 <= value <= photo_count}) \
        if isinstance(values, list) else []


def assess_photo_observations(observations, photo_count, *, summary_type=''):
    """Classify each row and retain bounded, non-public hypotheses."""
    facts, decisions = {}, []
    for index, row in enumerate(observations, 1):
        decision = {'index': index, 'field': None, 'status': 'REJECTED', 'reasons': []}
        decisions.append(decision)
        reasons = decision['reasons']
        if index > 36:
            reasons.append('ROW_LIMIT')
            continue
        if not isinstance(row, dict):
            reasons.append('INVALID_ROW')
            continue
        field = row.get('field')
        if not isinstance(field, str) or field not in VISUAL_FIELDS:
            reasons.append('UNKNOWN_FIELD')
            continue
        decision['field'] = field
        value = text_value(row.get('value'))
        no_defect = field == 'defect' and no_visible_defect(row.get('value'))
        photos = photo_numbers(row.get('photos'), photo_count)
        basis = text_value(row.get('basis'), 300)
        confidence = row.get('confidence')
        valid_confidence = type(confidence) is int and 0 <= confidence <= 100
        name = text_value(row.get('name'), 50) if field == 'feature' else None
        if not (value or no_defect):
            reasons.append('MISSING_OR_INVALID_VALUE')
        if not photos:
            reasons.append('NO_VALID_PHOTO')
        if not basis:
            reasons.append('MISSING_OR_INVALID_BASIS')
        if not valid_confidence:
            reasons.append('INVALID_CONFIDENCE')
        else:
            decision['confidence'] = confidence
        if reasons:
            if field == 'feature' and not name:
                reasons.append('MISSING_FEATURE_NAME')
            continue
        if confidence < 90:
            reasons.append('BELOW_CONFIRMATION_THRESHOLD')
        if field == 'feature' and not name:
            reasons.append('MISSING_FEATURE_NAME')
        if field == 'completeness' and normalized_claim(value) == 'complete':
            reasons.append('COMPLETENESS_REQUIRES_INVENTORY')
        status = 'PHOTO_CANDIDATE' if reasons else 'PHOTO_OBSERVATION'
        if no_defect:
            reasons.append('ABSENCE_IS_SURFACE_NOTE')
            field, value = 'condition', 'На цьому фото видимих дефектів не помічено'
            status = 'PHOTO_SURFACE_NOTE'
        entry = dict(value=value, status=status, photos=photos, basis=basis)
        if name:
            entry['name'] = name
        rows = facts.setdefault(field, [])
        if entry in rows:
            reasons.append('DUPLICATE')
            continue
        rows.append(entry)
        decision.update(status=status, photos=photos, output_field=field)
    if 'type' not in facts and (candidate := text_value(summary_type)):
        facts['type'] = [dict(value=candidate, status='HYPOTHESIS', photos=[],
                              basis='Тип зі зведення ШІ без окремого спостереження')]
    return facts, decisions


def build_product_evidence(observations, references, seller, measurement, photo_count, *, assessed=None):
    visual, _ = assessed if assessed is not None else assess_photo_observations(observations, photo_count)
    facts = {field: list(visual.get(field, [])) for field in (*FIELDS, 'feature', 'battery', 'research_note')}
    for reference in references:
        field = reference['field']
        if field in facts:
            entry = {key: reference[key] for key in ('value', 'status', 'photos', 'references', 'contradictions')}
            if reference.get('name'):
                entry['name'] = reference['name']
            if field in PHYSICAL_FIELDS:
                entry['unit'] = 'kg' if field == 'weight_kg' else 'cm'
            facts[field].append(entry)
    for field, value in seller.items():
        if field in VISUAL_FIELDS and text_value(value):
            facts[field].insert(0, dict(value=value.strip(), status='SELLER_VERIFIED'))
    for field in PHYSICAL_FIELDS:
        weight = field == 'weight_kg'
        unit = 'kg' if weight else 'cm'
        try:
            value = metric_value(measurement.get(field), unit, weight=weight)
        except ValueError:
            continue
        if value is None:
            continue
        source = (measurement.get('measurement_field_sources') or {}).get(field) or measurement.get(
            'weight_source' if weight else 'dimensions_source')
        if weight:
            record = ((measurement.get('vision_evidence') or {}).get('scale') or {})
        else:
            record = (measurement.get('ruler_axis_evidence') or {}).get(field) or {}
        photos = photo_numbers([record.get('source_photo')], photo_count)
        physical_source = 'MEASURED_FROM_SCALE' if weight else 'MEASURED_FROM_RULER'
        if source in SELLER_SOURCES:
            status = 'SELLER_VERIFIED'
        elif (source == physical_source and photos and field not in (measurement.get('measurement_conflicts') or [])
              and not (weight and measurement.get('reference_weight_shipping_only'))):
            status = 'PHOTO_MEASUREMENT'
        else:
            status = 'SHIPPING_ESTIMATE'
        facts[field].append(dict(value=value, unit=unit, status=status, photos=photos,
                                 basis=source or 'UNKNOWN'))
    return {field: rows for field, rows in facts.items() if rows}


def photo_type(evidence):
    rows = evidence.get('type', [])
    values = {normalized_claim(row['value']): row['value'] for row in rows
              if row['status'] in {'SELLER_VERIFIED', 'PHOTO_OBSERVATION'}}
    if not values:
        candidates = {normalized_claim(concise_type(row['value'])) for row in rows
                      if row['status'] == 'PHOTO_CANDIDATE'}
        if '' in candidates or any(row['status'] == 'CONTESTED' for row in rows):
            return ''
        if len(candidates) != 1:
            return ''
        for row in rows:
            key = normalized_claim(concise_type(row['value']))
            if row['status'] == 'CORROBORATED_REFERENCE' and key in candidates and any(
                candidate['status'] == 'PHOTO_CANDIDATE'
                and normalized_claim(concise_type(candidate['value'])) == key
                and set(candidate.get('photos', [])) & set(row.get('photos', []))
                for candidate in rows
            ):
                values[key] = row['value']
    return next(iter(values.values())) if len(values) == 1 else ''


def barcode_evidence(hints):
    status = 'CONTESTED' if hints['status'] == 'CONFLICT' else 'BARCODE_DECODED'
    return [dict(value=record['code'], photos=record['photos'], status=status)
            for record in hints['candidates']]


RECOGNITION_ROUTE_VERSION = 1


def recognition_route(photos, codes, catalogue_matches=()):
    """Choose the next evidence step without changing roles, facts or permissions."""
    if (codes['status'] == 'CONFLICT' or len(catalogue_matches) > 1
            or any(row['status'] == 'CONTESTED' for row in catalogue_matches)):
        return 'REVIEW_CONFLICT'
    if not any(row['role'] == 'EBAY_PRODUCT' for row in photos):
        return 'REVIEW_PHOTOS'
    if any(row['status'] == 'CATALOGUE_CANDIDATE' for row in catalogue_matches):
        return 'VERIFY_CATALOGUE'
    if codes['status'] == 'UNIQUE':
        return 'SEARCH_IDENTIFIER'
    return 'VISUAL_REVIEW'


def draft_from_evidence(evidence, *, count, material, brand, country, test_status):
    """Write an English draft from directly anchored facts for the US storefront."""
    kind = photo_type(evidence)
    if not kind or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'()/-]{2,69}", kind):
        return None
    # Do not convert a shape-based hypothesis about a model or era into SEO text.
    if re.search(r'\b(?:unknown|possible|probable|likely|unverified|antique|vintage|replica)\b', kind, re.I):
        return None
    title_parts = list(dict.fromkeys(part for part in (brand, kind, material) if part))
    if count > 1:
        title_parts.append(f'Lot of {count}')
    title = ' '.join(title_parts)
    if not 30 <= len(title) <= 80:
        return None
    specifics = {'Type': kind}
    if material:
        specifics['Material'] = material
    if brand:
        specifics['Brand'] = brand
    if country:
        specifics['Country/Region of Manufacture'] = country
    if count > 1:
        specifics['Number in Pack'] = str(count)
    details = [f'This listing includes {count} item{"s" if count > 1 else ""} shown in the photographs. '
               f'Product type: {kind}.']
    if material:
        details.append(f'Material: {material}.')
    for field in PHYSICAL_FIELDS:
        facts = next((row for row in evidence.get(field, [])
                      if row['status'] in {'PHOTO_MEASUREMENT', 'SELLER_VERIFIED'}), None)
        if facts:
            label = {'weight_kg': 'Item Weight', 'length_cm': 'Item Length',
                     'width_cm': 'Item Width', 'height_cm': 'Item Height'}[field]
            specifics[label] = f"{facts['value']:g} {facts['unit']}"
    condition = 'Please review the photos for surface condition and included parts.'
    defects = list(dict.fromkeys(row['value'] for row in evidence.get('defect', [])
                                 if row['status'] == 'PHOTO_OBSERVATION'
                                 and not no_visible_defect(row['value'])
                                 and not re.search(r'\b(?:no|possibly|likely|uncertain|maybe|hidden|working)\b',
                                                   row['value'], re.I)
                                 and re.fullmatch(r"[A-Za-z][A-Za-z0-9 ,'/()-]{2,119}", row['value'])))
    if defects:
        condition = 'Visible condition details: ' + '. '.join(defects) + '. ' + condition
    if test_status == 'Untested':
        condition += ' Not tested.'
    details.append(condition)
    return dict(title=title, item_specifics=specifics, condition_note=condition,
                description=' '.join(details), seo_keywords=[kind] +
                [f'{part} {kind}' for part in (brand, material) if part])


def specific_violations(evidence, specifics):
    """A public model, era or condition needs a seller assertion with the same value."""
    if evidence is None:
        return []  # Existing drafts predate the provenance protocol.
    failures = []
    for name, raw in specifics.items():
        field = ASPECTS.get(re.sub(r'[^a-z0-9]', '', str(name).casefold()))
        if not field:
            continue
        verified = {normalized_claim(row['value']) for row in evidence.get(field, [])
                    if row['status'] == 'SELLER_VERIFIED'}
        values = raw if isinstance(raw, list) else [raw]
        if not values or any(normalized_claim(str(value)) not in verified for value in values):
            failures.append(name)
    return failures
