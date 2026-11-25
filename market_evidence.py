"""Normalize comparable identifiers without treating repeat listings as new evidence."""
import re
from urllib.parse import parse_qsl, urlsplit

_EBAY_DOMAIN = re.compile(r'(?:[a-z0-9-]+\.)*ebay\.(?:com(?:\.au)?|co\.uk|ca|de|fr|it|es|at|ie|ch|be)')
_EBAY_ITEM = re.compile(r'/itm/(?:[^/]+/)?([0-9]{9,15})/?$')
_TRACKING = {'mkcid', 'mkevt', 'mkrid', 'campid', 'toolid', 'customid', '_trksid', '_trkparms'}


def comparable_reference(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
    except ValueError:
        return ('url', url)
    item = _EBAY_ITEM.fullmatch(parsed.path)
    if item and _EBAY_DOMAIN.fullmatch(host):
        return ('ebay-item', item[1])
    query = tuple(sorted((name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True)
                         if name.lower() not in _TRACKING and not name.lower().startswith('utm_')))
    return ('url', parsed.scheme, parsed.netloc, parsed.path, query)
