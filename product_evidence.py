"""Structured evidence kept with a draft, independent of listing prose."""
import re

from identity_evidence import normalized_claim
from measurement_units import metric_value


FIELDS = ('type', 'brand', 'model', 'material', 'country', 'period', 'condition', 'defect', 'completeness',
          'marking', 'symbol', 'weight_kg', 'length_cm', 'width_cm', 'height_cm')
VISUAL_FIELDS = frozenset(FIELDS[:11])
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


def text_value(value, limit=160):
    if isinstance(value, str) and not any(ord(letter) < 32 for letter in value.strip()):
        value = value.strip()
        if 0 < len(value) <= limit and normalized_claim(value) not in UNKNOWN:
            return value
    return ''


def photo_numbers(values, photo_count):
    return sorted({value for value in values if type(value) is int and 1 <= value <= photo_count}) \
        if isinstance(values, list) else []


def build_product_evidence(observations, references, seller, measurement, photo_count):
    facts = {field: [] for field in FIELDS}
    for row in observations[:36]:
        if not isinstance(row, dict):
            continue
        field = row.get('field')
        value = text_value(row.get('value'))
        photos = photo_numbers(row.get('photos'), photo_count)
        basis = text_value(row.get('basis'), 300)
        if (isinstance(field, str) and field in VISUAL_FIELDS and value and photos and basis
                and type(row.get('confidence')) is int and 90 <= row['confidence'] <= 100):
            facts[field].append(dict(value=value, status='PHOTO_OBSERVATION', photos=photos, basis=basis))
    for reference in references:
        field = reference['field']
        if field in facts:
            facts[field].append({key: reference[key] for key in
                                 ('value', 'status', 'photos', 'references', 'contradictions')})
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
    values = {normalized_claim(row['value']): row['value'] for row in evidence.get('type', [])
              if row['status'] in {'SELLER_VERIFIED', 'PHOTO_OBSERVATION'}}
    return next(iter(values.values())) if len(values) == 1 else ''


def barcode_evidence(hints):
    status = 'CONTESTED' if hints['status'] == 'CONFLICT' else 'BARCODE_DECODED'
    return [dict(value=record['code'], photos=record['photos'], status=status)
            for record in hints['candidates']]


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
        rows = evidence.get(field, [])
        if rows and rows[0]['status'] in {'PHOTO_MEASUREMENT', 'SELLER_VERIFIED'}:
            facts = rows[0]
            label = {'weight_kg': 'Item Weight', 'length_cm': 'Item Length',
                     'width_cm': 'Item Width', 'height_cm': 'Item Height'}[field]
            specifics[label] = f"{facts['value']:g} {facts['unit']}"
    condition = 'Please review the photos for surface condition and included parts.'
    defects = list(dict.fromkeys(row['value'] for row in evidence.get('defect', [])
                                 if row['status'] == 'PHOTO_OBSERVATION'
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
