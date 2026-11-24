"""Conservative validation of identity research citations and visual anchors."""

FIELDS = frozenset({'brand', 'model', 'country', 'period', 'type', 'material'})
PRIMARY = frozenset({'CATALOG', 'MANUAL', 'MUSEUM', 'ARCHIVE'})


def identity_claims(records, source_urls, photo_count, source_host):
    if not isinstance(records, list):
        return []
    claims = []
    for record in records[:24]:
        if not isinstance(record, dict) or not isinstance(record.get('field'), str) or record['field'] not in FIELDS:
            continue
        value = record.get('value')
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:160]
        photos = record.get('photos')
        photos = sorted({item for item in photos if type(item) is int and 1 <= item <= photo_count}) if isinstance(photos, list) else []
        conflicts = record.get('contradictions')
        conflicts = [text.strip()[:300] for text in conflicts[:8]
                     if isinstance(text, str) and text.strip()] if isinstance(conflicts, list) else []
        sources = _claim_sources(record.get('sources'), source_urls, source_host)
        if conflicts:
            status = 'CONTESTED'
        elif photos and len(sources) >= 2 and any(source['kind'] in PRIMARY for source in sources):
            status = 'CORROBORATED_REFERENCE'
        else:
            status = 'HYPOTHESIS'
        claims.append({'field': record['field'], 'value': value, 'photos': photos,
                       'references': sources, 'contradictions': conflicts, 'status': status})

    variants = {}
    for claim in claims:
        variants.setdefault(claim['field'], set()).add(normalized_claim(claim['value']))
    for claim in claims:
        if len(variants[claim['field']]) > 1:
            claim['status'] = 'CONTESTED'
    return claims


def _claim_sources(sources, source_urls, source_host):
    if not isinstance(sources, list):
        return []
    hosts, documents, verified = set(), set(), []
    for source in sources[:10]:
        if not isinstance(source, dict) or source.get('match') != 'EXACT':
            continue
        url = source.get('url')
        document = source.get('document_id')
        excerpt = source.get('excerpt')
        if not all(isinstance(value, str) and value.strip() for value in (url, document, excerpt)):
            continue
        host = source_host(url) if url in source_urls else None
        document = normalized_claim(document)
        if not host or host in hosts or document in documents:
            continue
        hosts.add(host)
        documents.add(document)
        kind = source.get('kind')
        verified.append({'url': url, 'kind': kind if isinstance(kind, str) else 'OTHER',
                         'excerpt': excerpt.strip()[:400]})
    return verified


def normalized_claim(value):
    return ' '.join(value.casefold().split())


def corroborated_value(claims, field, value):
    return any(claim['field'] == field and claim['status'] == 'CORROBORATED_REFERENCE'
               and normalized_claim(claim['value']) == normalized_claim(value)
               for claim in claims)
