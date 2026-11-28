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
