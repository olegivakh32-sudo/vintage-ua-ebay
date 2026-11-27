"""Conservative validation of identity research citations and visual anchors."""
import re

from measurement_units import metric_value

MEASUREMENTS = frozenset({'weight_kg', 'length_cm', 'width_cm', 'height_cm'})
FIELDS = frozenset({'brand', 'model', 'country', 'period', 'type', 'material', 'battery'}) | MEASUREMENTS
PRIMARY = frozenset({'CATALOG', 'MANUAL', 'MUSEUM', 'ARCHIVE'})
UNKNOWN = frozenset({'unknown', 'unresolved', 'none', 'n/a', '—', 'невідомо'})
FIELD_ALIASES = {'manufacturer': 'brand', 'maker': 'brand', 'model_number': 'model',
                 'country_of_manufacture': 'country', 'year': 'period', 'date': 'period',
                 'era': 'period', 'materials': 'material'}
MEASUREMENT_FIELDS = {
    'weight': ('weight_kg', None), 'mass': ('weight_kg', None), 'weight_g': ('weight_kg', 'g'),
    **{axis: (axis + '_cm', None) for axis in ('length', 'width', 'height')},
    **{axis + '_mm': (axis + '_cm', 'mm') for axis in ('length', 'width', 'height')},
}
TYPED_MEASUREMENT = re.compile(r'([0-9]+(?:[.,][0-9]+)?)\s*(kg|g|cm|mm)', re.I)
UNCERTAIN_CLAIM = re.compile(r'\b(?:possibly|probably|likely|hypothesis|undetermined|unconfirmed|unknown)\b', re.I)
RESEARCH_FIELD_GUIDANCE = (
    'Use exactly one field per claim and only these field names: '
    + ', '.join(sorted(FIELDS))
    + '. Put alternative identifications in contradictions. Keep depicted subjects, tentative matches and '
      'market notes out of the type field. Use scalar numbers in kg or cm for measurements, with NET '
      'for item weight and ITEM for dimensions of the item. Exclude packaging, capacity, and operating ranges.'
)


def _measurement(record, value, field, implied_unit):
    weight = field == 'weight_kg'
    declared_unit = record.get('unit')
    if declared_unit is not None and not isinstance(declared_unit, str):
        return None
    unit = declared_unit.strip().casefold() if declared_unit else None
    parsed = TYPED_MEASUREMENT.fullmatch(value.strip()) if isinstance(value, str) else None
    if parsed:
        value, literal_unit = parsed.groups()
        literal_unit = literal_unit.casefold()
        if unit and unit != literal_unit:
            return None
        unit = literal_unit
    if implied_unit and unit and implied_unit != unit:
        return None
    if not (unit or implied_unit):
        return None
    try:
        return metric_value(value, unit or implied_unit, weight=weight)
    except ValueError:
        return None


def identity_claims(records, source_urls, photo_count, source_host, *, decisions=None):
    if not isinstance(records, list):
        return []
    assessed = []
    for index, record in enumerate(records, 1):
        reasons, source_decisions = [], []
        decision = dict(index=index, field=None, status='REJECTED', reasons=reasons, sources=source_decisions)
        if decisions is not None:
            decisions.append(decision)
        if index > 24:
            reasons.append('CLAIM_LIMIT')
            continue
        if not isinstance(record, dict):
            reasons.append('INVALID_CLAIM')
            continue
        raw_field = record.get('field')
        if not isinstance(raw_field, str) or not raw_field.strip() or len(raw_field) > 80:
            reasons.append('UNKNOWN_FIELD')
            continue
        input_field = re.sub(r'[\s-]+', '_', raw_field.strip().casefold())
        field, implied_unit = MEASUREMENT_FIELDS.get(
            input_field, (FIELD_ALIASES.get(input_field, input_field), input_field.rpartition('_')[2]
                          if input_field in MEASUREMENTS else None))
        scope = 'NET' if field == 'weight_kg' else 'ITEM'
        if field not in FIELDS or (input_field in MEASUREMENT_FIELDS and record.get('specimen_basis') != scope):
            field = 'research_note'
            reasons.append('INTERNAL_RESEARCH_NOTE')
        decision['field'] = field
        if raw_field != field:
            decision['input_field'] = raw_field
        value = record.get('value')
        if field in MEASUREMENTS:
            weight = field == 'weight_kg'
            if record.get('specimen_basis') != ('NET' if weight else 'ITEM'):
                reasons.append('WRONG_MEASUREMENT_SCOPE')
                continue
            value = _measurement(record, value, field, implied_unit)
            if value is None:
                reasons.append('INVALID_MEASUREMENT')
                continue
        else:
            value = value.strip() if isinstance(value, str) else ''
            if not value or len(value) > (400 if field == 'research_note' else 160) or normalized_claim(value) in UNKNOWN:
                reasons.append('MISSING_OR_INVALID_VALUE')
                continue
            if UNCERTAIN_CLAIM.search(value):
                reasons.append('UNCERTAIN_VALUE')
        photos = record.get('photos')
        photos = sorted({item for item in photos if type(item) is int and 1 <= item <= photo_count}) if isinstance(photos, list) else []
        conflicts = record.get('contradictions')
        conflicts = [text.strip()[:300] for text in conflicts[:8]
                     if isinstance(text, str) and text.strip()] if isinstance(conflicts, list) else []
        sources = _claim_sources(record.get('sources'), source_urls, source_host, source_decisions)
        if not photos:
            reasons.append('NO_VALID_PHOTO')
        if len(sources) < 2:
            reasons.append('TOO_FEW_INDEPENDENT_SOURCES')
        if not any(source['kind'] in PRIMARY for source in sources):
            reasons.append('NO_PRIMARY_SOURCE')
        if conflicts:
            status = 'CONTESTED'
            reasons.append('SOURCE_CONTRADICTION')
        else:
            status = 'HYPOTHESIS' if reasons else 'CORROBORATED_REFERENCE'
        decision.update(status=status, photos=photos)
        entry = dict(field=field, value=value, photos=photos, references=sources, contradictions=conflicts, status=status)
        if field == 'research_note':
            entry['name'] = raw_field
        assessed.append((entry, decision))

    variants = {}
    for claim, _ in assessed:
        variants.setdefault(claim['field'], set()).add(normalized_claim(str(claim['value'])))
    for claim, decision in assessed:
        if claim['field'] != 'research_note' and len(variants[claim['field']]) > 1:
            claim['status'] = decision['status'] = 'CONTESTED'
            decision['reasons'].append('CONFLICTING_CLAIM_VALUES')
    return [claim for claim, _ in assessed]


def _claim_sources(sources, source_urls, source_host, decisions):
    if not isinstance(sources, list):
        return []
    hosts, documents, verified = set(), set(), []
    for index, source in enumerate(sources, 1):
        decision = dict(index=index, reason='SOURCE_LIMIT')
        decisions.append(decision)
        if index > 10:
            continue
        if not isinstance(source, dict):
            decision['reason'] = 'INVALID_SOURCE'
            continue
        if source.get('match') != 'EXACT':
            decision['reason'] = 'NONEXACT_SOURCE'
            continue
        url = source.get('url')
        document = source.get('document_id')
        excerpt = source.get('excerpt')
        if not all(isinstance(value, str) and value.strip() for value in (url, document, excerpt)):
            decision['reason'] = 'INCOMPLETE_CITATION'
            continue
        if url not in source_urls:
            decision['reason'] = 'URL_NOT_IN_SEARCH_RESULTS'
            continue
        host = source_host(url)
        document = normalized_claim(document)
        if not host:
            decision['reason'] = 'INVALID_SOURCE'
            continue
        if host in hosts or document in documents:
            decision['reason'] = 'DUPLICATE_SOURCE'
            continue
        hosts.add(host)
        documents.add(document)
        kind = source.get('kind')
        verified.append({'url': url, 'kind': kind if isinstance(kind, str) else 'OTHER',
                         'excerpt': excerpt.strip()[:400]})
        decision['reason'] = 'ACCEPTED_CITATION'
    return verified


def normalized_claim(value):
    return ' '.join(value.casefold().split())


def corroborated_value(claims, field, value):
    return any(claim['field'] == field and claim['status'] == 'CORROBORATED_REFERENCE'
               and normalized_claim(str(claim['value'])) == normalized_claim(str(value))
               for claim in claims)
