"""Normalize comparable identifiers without treating repeat listings as new evidence."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median
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


def completed_sale_valuation(records, subject, *, as_of):
    """Estimate USD item value from an independently verified completed-sale dataset."""
    fields = ('identity', 'condition', 'completeness')
    key = tuple(str(subject.get(field) or '').strip().casefold() for field in fields)
    result = {'status': 'NEEDS_MATCHED_SALES', 'currency': 'USD', 'price': None, 'sales': []}
    if not all(key) or any(value in {'unknown', 'unresolved', 'untested', 'not specified'} for value in key):
        return result
    accepted, conflicts = {}, set()
    for row in records:
        if (not isinstance(row, dict) or row.get('status') != 'SOLD' or row.get('currency') != 'USD'
                or row.get('verification') != 'MARKETPLACE_COMPLETED_SALE'
                or tuple(str(row.get(field) or '').strip().casefold() for field in fields) != key):
            continue
        url = row.get('url')
        if not isinstance(url, str) or not url.startswith('https://'):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.port not in (None, 443):
                continue
        except ValueError:
            continue
        reference = comparable_reference(url)
        if reference[0] != 'ebay-item':
            continue
        try:
            sold_on = date.fromisoformat(row.get('sold_date') or '')
            value = Decimal(str(row.get('item_price_usd')))
            if not value.is_finite() or not 0 < value <= 999999 or not 0 <= (as_of - sold_on).days <= 180:
                continue
            price = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if not price:
                continue
        except (TypeError, ValueError, InvalidOperation):
            continue
        record = {'item_id': reference[1], 'url': url, 'sold_date': sold_on.isoformat(), 'item_price_usd': str(price)}
        if reference in accepted:
            prior = accepted[reference]
            if (prior['sold_date'], prior['item_price_usd']) != (record['sold_date'], record['item_price_usd']):
                conflicts.add(reference)
        else:
            accepted[reference] = record
    result['sales'] = [row for reference, row in accepted.items() if reference not in conflicts]
    if len(result['sales']) >= 3:
        prices = [Decimal(row['item_price_usd']) for row in result['sales']]
        result.update(status='MEDIAN_COMPLETED_SALES', price=str(median(prices).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)), low=str(min(prices)), high=str(max(prices)))
    return result
