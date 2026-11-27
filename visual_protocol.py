"""Responses API contract for seller-photo evidence and legacy checkpoint decoding."""

import json
import re

from photo_assessment import SCENE_TYPES
from product_evidence import VISUAL_FIELDS
from sellable_scope import PART_ROLES


VISUAL_FORMAT_NAME = 'seller_photo_evidence'
PHOTO_ROLES = ('EBAY_PRODUCT', 'EBAY_EVIDENCE', 'EVIDENCE_ONLY', 'NEEDS_REVIEW')

VISUAL_PROMPT = """Extract seller-photo evidence for an eBay.com listing. Return the requested JSON only.
Classify every original photo by number. External scales, rulers, tapes, calipers and packing-only frames are EVIDENCE_ONLY. A tool for sale may be EBAY_PRODUCT. Use NEEDS_REVIEW if unsure. Use EBAY_EVIDENCE for buyer-useful details, marks, defects and barcodes. A digital display alone is not a scale. Look for the platform, item and unit. Check the margins for ticks, tools or cropped readouts. FULL_SET requires every offered physical part to be visible together.
Inventory distinct visible components once across views. PRODUCT is an independent offered item. PACKAGING and LABEL are retail packaging or labels for that item. COMPONENT is a part of it and ACCESSORY is an accessory visibly belonging to it. A separate spare product counts as PRODUCT. Use UNKNOWN for ambiguous parts. Set physical_item_count to the number of independent products. Use UNCERTAIN with count 0 if unclear. Packaging alone cannot prove new condition. Never infer function, authenticity, completeness, age, exact alloy, origin, maker or model without direct evidence. Give a concise generic English noun phrase for the type. Put detailed purpose and appearance in the observation basis. Give up to five named variant-distinguishing features. Omit unknown rows and record only visible defects. Keep brand, exact model, country and date unknown when their corresponding mark is unreadable. Never treat decorative or quality marks as a maker. Use broad visually supported materials.
Transcribe up to six marks in their original script and order. Mark unreadable characters [?] and separate plausible OCR alternatives or conflicting readings in the brief Ukrainian summary. Mark kinds include MAKER, MODEL_NUMBER, SERIAL, QUALITY_MARK, INSCRIPTION and UNKNOWN. Describe up to six visible symbols without asserting attribution. Default Untested or Not applicable.
Record a type observation with the visible object and its purpose, including board versus component, when the photos support it. Give every feature a short name separate from its value. Visible parts alone cannot establish that the full inventory is present. Apparent heft or shine cannot establish a material or alloy.
For equipment, record visible mating connectors, release latches and mounting surfaces as named features. Check whether the visible parts support standalone operation or attachment to another product. When the photos do not distinguish those functions, describe the visible construction and leave the function unresolved.
Record visible battery labels or charging features as named features for carrier review. Do not infer hidden batteries or chemistry.
For physical readings use only separate readable instruments, literal scale digits (g or kg), and signed ruler endpoints (cm). Omit estimates and unreadable values. Do not research, price or assign an eBay category."""


def _object(**fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def _array(items):
    return {'type': 'array', 'items': items}


def _enum(*values):
    return {'type': 'string', 'enum': list(values)}


_STR = {'type': 'string'}
_INT = {'type': 'integer'}
_NUM = {'type': 'number'}
_BOOL = {'type': 'boolean'}
_NULL = {'type': 'null'}
_SCALE = _object(source_photo=_INT, raw_digits=_STR, unit=_enum('g', 'kg'),
                 confidence=_INT, visible=_BOOL)
_RULER = _object(source_photo=_INT, axis=_enum('length', 'width', 'thickness'), start_cm=_NUM,
                 end_cm=_NUM, edges_visible=_BOOL, scale_marks_visible=_BOOL, confidence=_INT)
_OBSERVATION = _object(field=_enum(*sorted(VISUAL_FIELDS)), value=_STR, photos=_array(_INT),
                       basis=_STR, confidence=_INT, name={'type': ['string', 'null']})

VISUAL_TEXT = {'format': {'type': 'json_schema', 'name': VISUAL_FORMAT_NAME, 'strict': True, 'schema': _object(
    photos=_array(_object(photo=_INT, role=_enum(*PHOTO_ROLES), scene=_enum(*sorted(SCENE_TYPES)), confidence=_INT)),
    observations=_array(_OBSERVATION),
    scope=_object(scope=_enum('ONE_PHYSICAL_ITEM', 'MULTIPLE_PHYSICAL_ITEMS', 'UNCERTAIN'),
                  physical_item_count=_INT, confidence=_INT, note=_STR,
                  components=_array(_object(label=_STR, role=_enum(*PART_ROLES), quantity=_INT, photos=_array(_INT)))),
    facts=_object(object_type=_STR, materials=_array(_STR), materials_confidence=_INT, material_evidence=_STR,
                  tested_status=_enum('Untested', 'Tested working', 'Tested not working', 'Not applicable'),
                  completeness=_enum('Unknown', 'Complete', 'Incomplete'), brand=_STR, origin=_STR,
                  marking_present=_BOOL, brand_evidence=_STR, confidence=_INT,
                  markings=_array(_object(photo=_INT, literal=_STR, language=_STR, kind=_STR)),
                  symbols=_array(_object(photo=_INT, description=_STR))),
    physical=_object(scale={'anyOf': [_SCALE, _NULL]}, dimension_readings=_array(_RULER)),
    summary=_STR,
)}}


def visual_response_issue(response):
    """Return a safe diagnostic code without retaining provider text."""
    if not isinstance(response, dict):
        return 'invalid_response'
    status = response.get('status')
    if status not in (None, 'completed'):
        details = response.get('incomplete_details')
        reason = details.get('reason') if isinstance(details, dict) else None
        if status == 'incomplete' and reason in ('max_output_tokens', 'content_filter'):
            return f'incomplete_{reason}'
        return 'response_not_complete'
    output = response.get('output')
    if isinstance(output, list) and any(
        isinstance(part, dict) and part.get('type') == 'refusal'
        for item in output if isinstance(item, dict) and isinstance(item.get('content'), list)
        for part in item['content']
    ):
        return 'refused'
    return ''


def _whole_object(text):
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text[:-3], flags=re.I).strip()
    if not text.startswith('{'):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def machine_object(text, name):
    """Decode a tagged block or an object whose exact key names that block."""
    text = str(text or '')
    root = _whole_object(text)
    if isinstance(root.get(name), dict):
        return root[name]
    for match in re.finditer(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I):
        raw = text[match.end():].lstrip(' \t\r\n:*_=\"')
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.I)
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def normalize_visual_analysis(text, photo_count):
    """Keep the checkpoint protocol used by the editor, research and draft stages."""
    report = _whole_object(text)
    if 'photos' not in report and 'observations' not in report:
        if 'PHOTO_ROLES_JSON' not in report:
            return text  # Legacy tagged text.
        blocks = {name: report.get(name) for name in (
            'PHOTO_ROLES_JSON', 'PHOTO_SCENES_JSON', 'PRODUCT_OBSERVATIONS_JSON',
            'SCOPE_JSON', 'AUTO_FACTS_JSON', 'PHYSICAL_READINGS_JSON',
        )}
    else:
        blocks = _visual_blocks(report, photo_count)
    lines = [f'{name}: {json.dumps(value, ensure_ascii=False, separators=(",", ":"))}'
             for name, value in blocks.items() if isinstance(value, dict)]
    summary = report.get('summary')
    if isinstance(summary, str):
        lines.append(summary)
    return '\n'.join(lines)


def _visual_blocks(report, photo_count):
    photos = report.get('photos')
    observations = report.get('observations')
    if not isinstance(photos, list) or not isinstance(observations, list) or len(photos) != photo_count:
        raise ValueError('Visual response has an invalid photo list or observation list')
    roles, scenes = {}, {}
    for photo in photos:
        number = photo.get('photo') if isinstance(photo, dict) else None
        if type(number) is not int or not 1 <= number <= photo_count or number in roles:
            raise ValueError('Visual response has an invalid or duplicate photo number')
        roles[number] = photo.get('role')
        scenes[number] = [photo.get('scene'), photo.get('confidence')]
    return {
        'PHOTO_ROLES_JSON': {'roles': roles},
        'PHOTO_SCENES_JSON': scenes,
        'PRODUCT_OBSERVATIONS_JSON': {'fields': observations},
        'SCOPE_JSON': report.get('scope'),
        'AUTO_FACTS_JSON': report.get('facts'),
        'PHYSICAL_READINGS_JSON': report.get('physical'),
    }
