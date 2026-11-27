"""Carrier eligibility for the current Ukrposhta international route."""
import html
import re


SOURCE = 'https://www.ukrposhta.ua/ua/zaboroneni-vkladennia/'
LITHIUM = re.compile(r'\b(?:lithium|li[- ]?(?:ion|po|pol)|літіє\w*|літій[- ]іон\w*)\b', re.I)
BATTERY = re.compile(r'\b(?:batter(?:y|ies)(?!\s+(?:cover|door|holder|compartment)\b)|'
                     r'coin[- ]cell|rechargeable|charging (?:port|connector)|акумулятор\w*|батаре\w*)\b', re.I)
CELL_CODE = re.compile(r'\b(?:CR|BR)[ -]?\d{4}\b', re.I)
OTHER_CHEMISTRY = re.compile(r'\b(?:alkaline|zinc[- ]carbon|silver[- ]oxide|лужн\w*)\b', re.I)
UNCERTAIN = re.compile(r'\b(?:no|not|without|excluded|removed|optional|requires?|supports?|'
                        r'compatible|unknown|possibly|possible|likely|may|might|either|or|'
                        r'без|невідом\w*|можлив\w*|відсутн\w*)\b', re.I)
CONFIRMED = {'SELLER_VERIFIED', 'PHOTO_OBSERVATION', 'CATALOGUE_REFERENCE', 'CORROBORATED_REFERENCE'}


def ukrposhta_battery_review(*, texts=(), features=(), battery_specs=()):
    """Keep uncertain chemistry or inclusion out of the quoted shipping route."""
    claims = [(value, True, False) for value in texts]
    claims.extend((row['value'], row['status'] in CONFIRMED, True) for row in battery_specs)
    for row in features:
        name = row.get('name') or ''
        claims.append((f"{name} {row['value']}", row['status'] in CONFIRMED, bool(BATTERY.search(name))))
    needs_review = None
    for value, confirmed, battery_field in claims:
        plain = re.sub(r'<[^>]*>', '\n', html.unescape(str(value))).translate(str.maketrans('‐‑–—', '----'))
        for clause in re.split(r'[.!?;\n]+', plain):
            clause = ' '.join(clause.split())
            battery = BATTERY.search(clause)
            cell = CELL_CODE.search(clause)
            lithium = LITHIUM.search(clause)
            if lithium and (battery or battery_field or '-' in lithium[0]):
                if confirmed and not UNCERTAIN.search(clause):
                    return {'status': 'BLOCK', 'rule': 'UKRPOSHTA_INTERNATIONAL_LITHIUM',
                            'trigger': lithium[0], 'source': SOURCE}
                needs_review = lithium[0]
            elif clause and (battery or battery_field):
                if cell or not (confirmed and OTHER_CHEMISTRY.search(clause) and not UNCERTAIN.search(clause)):
                    needs_review = (cell or battery)[0] if cell or battery else clause
    if needs_review:
        return {'status': 'REVIEW', 'rule': 'UKRPOSHTA_BATTERY_REVIEW', 'trigger': needs_review, 'source': SOURCE}
    return None
