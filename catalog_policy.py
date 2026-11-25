"""Read-only eBay taxonomy rules shared across product directions."""

from dataclasses import dataclass
import re
import unicodedata


LEGACY_MARKETPLACE = 'EBAY_US'

# A sales condition describes the item's state. Category names such as
# "Sewing Machine Parts" and "Watch Repair Kits" describe the item itself.
PARTS_CONDITION_CLAIM = re.compile(
    r'\b(?:for\s+parts(?:\s*(?:[/&]|or\b|and\b)\s*(?:repair|restoration|not working))?'
    r'|for\s+(?:repair|restoration)(?=\s*(?:[,.;:)/-]|$)|\s+(?:only|or|and)\b)'
    r'|(?:needs?|requires?)\s+(?:repair|restoration)'
    r'|(?:parts?|repair)\s+only'
    r'|parts?\s*(?:[/&]|or\b|and\b)\s*repair'
    r'|repair\s*(?:[/&]|or\b|and\b)\s*parts?)\b',
    re.IGNORECASE,
)


class CategoryAmbiguityError(RuntimeError):
    """More than one distinct category path fits the available item facts."""


def marketplace_code(value):
    if not isinstance(value, str) or not re.fullmatch(r'EBAY_[A-Z0-9]{2,16}(?:_[A-Z0-9]{2,16})?', value):
        raise ValueError('Invalid eBay marketplace ID')
    return value


@dataclass(frozen=True)
class TaxonomyTree:
    marketplace_id: str
    tree_id: str
    version: str | None

    @classmethod
    def from_response(cls, marketplace_id, payload):
        marketplace_code(marketplace_id)
        tree_id = str(payload.get('categoryTreeId', '')) if isinstance(payload, dict) else ''
        if not re.fullmatch(r'[0-9]+', tree_id):
            raise ValueError('eBay did not return a valid categoryTreeId')
        version = payload.get('categoryTreeVersion')
        return cls(marketplace_id, tree_id, str(version) if version not in (None, '') else None)


def catalog_key(value):
    return ' '.join(unicodedata.normalize('NFC', str(value)).casefold().split())


def object_words(text):
    """Compare object nouns across scripts without dropping accessory qualifiers."""
    noise = {'the', 'vintage', 'antique', 'used', 'collectible', 'collectibles'}
    words = []
    for word in re.findall(r'[^\W_]+', catalog_key(text)):
        if word in noise:
            continue
        if word.isascii():
            if word.endswith('ies'):
                word = word[:-3] + 'y'
            elif re.search(r'(?:ch|sh|ss|x|lens)es$', word):
                word = word[:-2]
            elif word.endswith('s') and not word.endswith(('ss', 'us', 'is', 'lens')):
                word = word[:-1]
        words.append(word)
    return words


def selectable_aspects(aspects, metadata):
    """Retain explicit values that are valid in this category and aspect context."""
    definitions = {
        catalog_key(row['localizedAspectName']): row
        for row in metadata
        if isinstance(row, dict) and row.get('localizedAspectName')
        and isinstance(row.get('aspectConstraint'), dict)
        and row['aspectConstraint'].get('aspectMode') == 'SELECTION_ONLY'
    }
    if not definitions:
        return dict(aspects)
    options = {}
    for key, definition in definitions.items():
        choices = definition.get('aspectValues')
        options[key] = {
            catalog_key(choice['localizedValue']): choice
            for choice in (choices if isinstance(choices, list) else [])
            if isinstance(choice, dict) and isinstance(choice.get('localizedValue'), str)
            and choice['localizedValue'].strip()
        }

    valid = {}
    for name, raw in aspects.items():
        key = catalog_key(name)
        allowed = options.get(key)
        if allowed is None:
            valid[name] = raw
            continue
        original = definitions[key]['localizedAspectName']
        chosen = valid.get(original, [])
        for value in raw if isinstance(raw, list) else [raw]:
            option = allowed.get(catalog_key(value))
            if option and option['localizedValue'] not in chosen:
                chosen.append(option['localizedValue'])
        if chosen:
            valid[original] = chosen

    while True:
        facts = {}
        for name, values in valid.items():
            raw = values if isinstance(values, list) else [values]
            facts.setdefault(catalog_key(name), set()).update(
                catalog_key(value) for value in raw if isinstance(value, (str, int, float))
            )
        filtered = {}
        for name, values in valid.items():
            allowed = options.get(catalog_key(name))
            if allowed is None:
                filtered[name] = values
                continue
            supported = [
                value for value in values
                if _dependencies_met(allowed[catalog_key(value)].get('valueConstraints'), facts)
            ]
            if supported:
                filtered[name] = supported
        if filtered == valid:
            return valid
        valid = filtered


def _dependencies_met(constraints, facts):
    if constraints is None:
        return True
    if not isinstance(constraints, list):
        return False
    for rule in constraints:
        if not isinstance(rule, dict) or not isinstance(rule.get('applicableForLocalizedAspectName'), str):
            return False
        allowed = rule.get('applicableForLocalizedAspectValues')
        if not isinstance(allowed, list) or not all(isinstance(value, str) for value in allowed):
            return False
        current = facts.get(catalog_key(rule['applicableForLocalizedAspectName']), set())
        if not current.intersection(catalog_key(value) for value in allowed):
            return False
    return True
