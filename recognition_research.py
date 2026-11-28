"""Grounded search inputs and query strategies shared by both research passes."""
import re

from identity_evidence import normalized_claim

_SERIAL_LINE = re.compile(r"^\s*(?:s\s*/\s*n|ser\.?\s+no\.?|serial(?:\s+(?:no\.?|number))?)\b", re.I)


def numbered_evidence(labels, marks, observations, scenes, photo_count):
    """Keep frame-bound clues even if a research thumbnail could not be loaded."""
    usable = set(range(1, photo_count + 1)) - {slot for slot, scene in scenes.items()
                                             if scene in {'SCALE', 'RULER', 'OTHER'}}
    serials = {normalized_claim(row['literal']) for row in marks if row.get('kind', '').upper() == 'SERIAL'}
    local_ocr = []
    for slot, text in labels:
        if slot not in usable:
            continue
        lines = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()
                                   and not _SERIAL_LINE.match(line)
                                   and not any(serial and re.search(r'(?<!\w)' + re.escape(serial) + r'(?!\w)',
                                                                   normalized_claim(line)) for serial in serials)))
        if lines:
            local_ocr.append({'photo': slot, 'text': '\n'.join(lines)[:320]})
    literal_marks = [row for row in marks if row['photo'] in usable and row.get('kind', '').upper() != 'SERIAL']
    clues = []
    for field in ('feature', 'marking', 'symbol'):
        for row in observations.get(field, []):
            slots = sorted(set(row['photos']) & usable)
            if (slots and row['status'] in {'PHOTO_OBSERVATION', 'PHOTO_CANDIDATE'}
                    and row.get('marking_kind') != 'SERIAL'
                    and normalized_claim(row['value']) not in serials):
                clues.append({'field': field, 'value': row['value'], 'status': row['status'], 'photos': slots})
    return {'scenes': scenes, 'observations': clues[:16], 'marks': literal_marks[:8], 'local_ocr': local_ocr[:8]}


def hypothesis_seeds(evidence):
    """Offer differently grounded leads even if there is no named identity."""
    marks, geometry = [], []
    for row in evidence['marks']:
        value = row['literal'].split('[?]', 1)[0].strip()
        if len(value) >= 4 and evidence['scenes'].get(row['photo']) != 'PACKAGE':
            marks.append({'kind': 'readable_mark', 'clue': value, 'photos': [row['photo']]})
    for row in evidence['observations']:
        if row['field'] != 'feature':
            continue
        photos = [slot for slot in row['photos'] if evidence['scenes'].get(slot) != 'PACKAGE']
        if photos:
            geometry.append({'kind': 'observed_construction', 'clue': row['value'], 'photos': photos})
    return marks[:1] + geometry[:2] + marks[1:2]


QUERY_HYPOTHESES = (
    'Make two competing physical-function hypotheses from different observed discriminators where possible. '
    'Keep the observed clues separate from the inferred hypotheses. If there is no confirmed type, maker, '
    'model or material, still search: use the strongest literal mark, or distinctive geometry, construction '
    'and attachment points. Compare each hypothesis against exact pages, photographs and diagrams. '
    'Use the next query for a different discriminator or a rival function if the first hits are only analogues. '
    'A rejected candidate is an alternative; report a contradiction only if present evidence disputes '
    'the exact claim under consideration. Treat references to a box or accessory as evidence about that '
    'component and apply them to the product only when the actual photographs establish the connection.'
)

UNCONFIRMED_SCOPE = (
    'The sale composition is awaiting seller confirmation. Identify the photographed physical subjects '
    'separately. Mark each claim with only the photos of its subject. Do not infer completeness, quantity '
    'offered, shipping measurements, price or market comparables for an unconfirmed sale unit. '
    'Leave each market and shipping JSON block empty with null prices.'
)


# A supplementary search can fill this short list. Identity, market and shipping
# remain owned by the primary stages.
DOCUMENTARY_DETAILS = ('material', 'period', 'weight_kg', 'length_cm', 'width_cm', 'height_cm')
DETAILS_RESEARCH_PREFIX = 'Find missing documentary specifications for the locked item.'


def details_request(evidence, scope_confirmed, product_photos, *, include_existing=False):
    """Authorize a supplement only for a maker and an exact code on the item."""
    from listing_review import DIRECT, DOCUMENTED, evidence_value

    if not scope_confirmed:
        return None
    brand = evidence_value(evidence, 'brand', {'PHOTO_OBSERVATION'})
    model = evidence_value(evidence, 'model', {'PHOTO_OBSERVATION'})
    if (not brand or not model or not any(char.isdigit() for char in model)
            or evidence_value(evidence, 'brand', DIRECT) != brand or evidence_value(evidence, 'model', DIRECT) != model
            or any(row['status'] in DIRECT | DOCUMENTED | {'CONTESTED'}
                   and normalized_claim(row['value']) != normalized_claim(value)
                   for field, value in (('brand', brand), ('model', model)) for row in evidence.get(field, []))):
        return None
    model_slots = {slot for row in evidence.get('model', [])
                   if row['status'] == 'PHOTO_OBSERVATION' and normalized_claim(row['value']) == normalized_claim(model)
                   for slot in row['photos']}
    marked = {slot for row in evidence.get('marking', [])
              if row['status'] == 'PHOTO_OBSERVATION' and row.get('marking_kind') == 'MODEL_NUMBER'
              and normalized_claim(row['value']) == normalized_claim(model)
              for slot in row['photos']}
    slots = sorted(model_slots & marked & set(product_photos))
    if not slots:
        return None
    blocking = DIRECT | DOCUMENTED | {'PHOTO_MEASUREMENT', 'CONTESTED'}
    fields = [field for field in DOCUMENTARY_DETAILS
              if include_existing or not any(row['status'] in blocking for row in evidence.get(field, []))]
    return {'brand': brand, 'model': model, 'photos': slots[:1], 'fields': fields} if fields else None


def check_documentary_details(packet, request, photo_count, source_host):
    """Revalidate the photo anchor and require every surviving source to quote it."""
    from identity_evidence import PRIMARY, TYPED_MEASUREMENT, identity_claims
    from measurement_units import metric_value

    if not (request and isinstance(packet, dict) and packet.get('status') == 'COMPLETE'):
        return []
    if packet.get('anchor') != {key: request[key] for key in ('brand', 'model', 'photos')}:
        return []
    fields = packet.get('requested_fields')
    if not isinstance(fields, list) or not fields or any(field not in request['fields'] for field in fields):
        return []
    known = packet.get('urls')
    urls = {url for url in known if isinstance(url, str)} if isinstance(known, list) else set()
    rows = packet.get('claims')
    if not isinstance(rows, list):
        return []

    def literal(text, value):
        return re.search(r'(?<![\w-])' + re.escape(normalized_claim(value)) + r'(?![\w-])',
                         normalized_claim(text)) is not None

    def supports(source, claim):
        excerpt, field, value = source['excerpt'], claim['field'], claim['value']
        if not literal(excerpt, request['model']):
            return False
        if field in ('material', 'period'):
            return literal(excerpt, value)
        if field == 'weight_kg':
            if (not re.search(r'\b(?:net|body only|tool only|item only)\b', excerpt, re.I)
                    or re.search(r'\b(?:gross|package|packaged|shipping|cipa|with (?:the )?batter(?:y|ies))\b',
                                 excerpt, re.I)):
                return False
            if sum(unit.lower() in {'kg', 'g'} for _, unit in TYPED_MEASUREMENT.findall(excerpt)) != 1:
                return False
        if re.search(r'\b(?:package size|packaging|packed dimensions)\b', excerpt, re.I):
            return False
        if field != 'weight_kg':
            axis = field.split('_', 1)[0]
            labelled = re.search(r'\b' + axis + r'\s*[:=]?\s*(' + TYPED_MEASUREMENT.pattern + r')\b',
                                 excerpt, re.I)
            if not labelled:
                return False
            excerpt = labelled[1]
        for quantity, unit in TYPED_MEASUREMENT.findall(excerpt):
            try:
                if metric_value(quantity, unit, weight=field == 'weight_kg') == value:
                    return True
            except ValueError:
                continue
        return False

    candidates = [row for row in rows[:18] if isinstance(row, dict) and row.get('field') in fields]
    result = []
    for claim in identity_claims(candidates, urls, photo_count, source_host):
        if claim['status'] != 'CORROBORATED_REFERENCE' or claim['photos'] != request['photos']:
            continue
        proven = [source for source in claim['references'] if supports(source, claim)]
        if len(proven) >= 2 and any(source['kind'] in PRIMARY for source in proven):
            result.append(claim | {'references': proven})
    return result
