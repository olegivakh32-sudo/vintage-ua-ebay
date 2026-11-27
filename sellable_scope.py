"""Separate products from parts of their retail presentation."""
import re


PART_ROLES = ('PRODUCT', 'PACKAGING', 'LABEL', 'COMPONENT', 'ACCESSORY', 'UNKNOWN')
PACKAGING = re.compile(r'(?:retail |original |product )?(?:box|carton|packaging)|(?:separate )?(?:molded|moulded) insert')
ATTACHED_LABEL = re.compile(r'attached (?:product |retail )?(?:tag|label)|(?:attached )?hang tag')
AMBIGUOUS = re.compile(r'\b(?:or|separate product|extra|spare|possibly|unknown|uncertain)\b')


def scope_components(value, photo_count):
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        return None
    for component in value:
        if not isinstance(component, dict):
            return None
        label = component.get('label')
        photos = component.get('photos')
        if (not isinstance(label, str) or not label.strip() or len(label) > 80
                or component.get('role') not in PART_ROLES
                or type(component.get('quantity')) is not int or not 1 <= component['quantity'] <= 99
                or not isinstance(photos, list) or not photos
                or any(type(photo) is not int or not 1 <= photo <= photo_count for photo in photos)):
            return None
    return value


def _legacy_packaging(note, physical_count, observations, photo_count):
    """Recover a narrow, explicit inventory from old single-photo checkpoints."""
    if photo_count != 1 or 'not ' in note.casefold() or not isinstance(observations, list):
        return None
    sentence = note.casefold().split(';', 1)[0].strip().removesuffix('.')
    if not sentence.startswith('visible '):
        return None
    labels = [part.removeprefix('and ').strip() for part in re.split(r',\s*|\s+and\s+', sentence[8:])]
    if len(labels) != physical_count or len(set(labels)) != len(labels):
        return None
    # A plain box alone may be another sale item. Require a packaging anchor.
    if not any(ATTACHED_LABEL.fullmatch(label) or 'insert' in label and PACKAGING.fullmatch(label)
               for label in labels):
        return None
    components = []
    for label in labels:
        role = 'LABEL' if ATTACHED_LABEL.fullmatch(label) else 'PACKAGING' if PACKAGING.fullmatch(label) else 'PRODUCT'
        components.append({'label': label, 'role': role, 'quantity': 1, 'photos': [1]})
    products = [part['label'] for part in components if part['role'] == 'PRODUCT']
    if len(products) != 1 or not product_anchor(products[0], observations, [1]):
        return None
    return components


def product_anchor(label, observations, photos):
    """Require the named principal product to be visible independently of the count."""
    if not isinstance(observations, list) or AMBIGUOUS.search(label.casefold()):
        return False
    # Compare complete words so retail accessories cannot match an unrelated item.
    words = re.findall(r'[a-z0-9]+', label.casefold())
    if not words:
        return False
    for row in observations:
        if (not isinstance(row, dict) or row.get('field') != 'type'
                or type(row.get('confidence')) is not int or row['confidence'] < 90
                or not isinstance(row.get('photos'), list) or not any(photo in row['photos'] for photo in photos)):
            continue
        noun = re.split(r'\b(?:for|with)\b|[;.!,]', str(row.get('value') or '').casefold(), 1)[0]
        if AMBIGUOUS.search(noun):
            continue
        tokens = set(re.findall(r'[a-z0-9]+', noun))
        kinds = tokens | {token.removeprefix(prefix) for token in tokens for prefix in ('wrist', 'hand', 'table')
                          if token.startswith(prefix)}
        if set(words) <= kinds:
            return True
    return False


def resolve_scope(scope, observations, photo_count):
    """Count a documented product with its packaging or attached parts as one."""
    components = scope_components(scope.get('components'), photo_count)
    if 'components' not in scope and scope['scope'] == 'MULTIPLE_PHYSICAL_ITEMS':
        components = _legacy_packaging(scope['note'], scope['physical_item_count'], observations, photo_count)
    if not components:
        return scope | {'scope': 'UNCERTAIN', 'physical_item_count': 0} if 'components' in scope else scope
    result = scope | {'components': components}
    products = [part for part in components if part['role'] == 'PRODUCT']
    count = sum(part['quantity'] for part in products)
    if count >= 2:
        return result | {'scope': 'MULTIPLE_PHYSICAL_ITEMS', 'physical_item_count': count}
    if (count != 1 or scope['scope'] == 'UNCERTAIN' or scope['confidence'] < 70
            or any(part['role'] == 'UNKNOWN' for part in components)
            or not product_anchor(products[0]['label'], observations, products[0]['photos'])):
        return result | {'scope': 'UNCERTAIN', 'physical_item_count': 0}
    if scope['scope'] == 'MULTIPLE_PHYSICAL_ITEMS' or scope['physical_item_count'] > 1:
        inventory_count = sum(part['quantity'] for part in components)
        if inventory_count != scope['physical_item_count']:
            return result | {'scope': 'UNCERTAIN', 'physical_item_count': 0}
    return result | {'scope': 'ONE_PHYSICAL_ITEM', 'physical_item_count': 1}
