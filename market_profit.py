"""Evidence-aware profitability comparison independent of paid research."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from statistics import median
from time import monotonic
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from market_evidence import comparable_reference

RATE_URL = 'https://bank.gov.ua/NBU_Exchange/exchange_site'
RATE_DOCS = 'https://bank.gov.ua/admin_uploads/article/Instr_API_KURS_VAL_data.pdf'
EXPENSES = ('fee_pct', 'fixed_fee', 'postage_uah', 'packing_uah', 'ad_uah', 'tax_uah',
            'conversion_uah', 'returns_uah', 'other_uah')


def amount(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or (parsed and not Decimal('0.00000001') <= parsed <= 1_000_000_000):
        return None
    return parsed


def kyiv_today():
    return datetime.now(ZoneInfo('Europe/Kyiv')).date()


@lru_cache(maxsize=1)
def _retrieve_rate(quarter_hour, today):
    try:
        first = today - timedelta(days=3)
        params = {'valcode': 'usd', 'start': first.strftime('%Y%m%d'), 'end': today.strftime('%Y%m%d'),
                  'sort': 'exchangedate', 'order': 'desc', 'json': ''}
        response = requests.get(RATE_URL, params=params, timeout=(2, 4), allow_redirects=False)
        if response.status_code != 200:
            return None
        official = {}
        rows = response.json(parse_float=Decimal)
        for row in rows:
            if row.get('cc') != 'USD' or row.get('r030') != 840 or row.get('units') != 1:
                continue
            effective = datetime.strptime(row['exchangedate'], '%d.%m.%Y').date()
            rate = amount(row['rate_per_unit'])
            if rate and first <= effective <= today:
                if effective in official and official[effective] != rate:
                    return None
                official[effective] = rate
        if not official:
            return None
        effective = max(official)
        return {'value': str(official[effective]), 'effective_on': effective.isoformat(), 'source': RATE_URL}
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        return None


def official_usd_rate():
    return _retrieve_rate(int(monotonic() // 900), kyiv_today())


def _has_note(value):
    return isinstance(value, str) and bool(value.strip())


def currency_evidence(rows, source_urls, currency, today):
    if not isinstance(rows, list) or not source_urls:
        return None
    accepted, conflicting = {}, set()
    for row in rows[:40]:
        if not isinstance(row, dict):
            continue
        url, kind = row.get('url'), row.get('status')
        price = amount(row.get('price'))
        if (not isinstance(url, str) or url not in source_urls or row.get('currency') != currency
                or kind not in ('SOLD', 'ACTIVE') or not price or not _has_note(row.get('match_evidence'))):
            continue
        try:
            day = date.fromisoformat(row.get('sold_date') if kind == 'SOLD' else row.get('checked_on'))
            if day > today or (kind == 'SOLD' and not _has_note(row.get('sale_evidence'))):
                continue
            if urlsplit(url).scheme != 'https':
                continue
        except (ValueError, TypeError):
            continue
        reference = comparable_reference(url)
        previous = accepted.get(reference)
        if previous and previous[:2] != (kind, price):
            conflicting.add(reference)
        accepted[reference] = (kind, price, url, day)
    usable = [entry for key, entry in accepted.items() if key not in conflicting]
    if not usable:
        return None
    sold = [entry for entry in usable if entry[0] == 'SOLD']
    selected = sold or usable
    return {'value': str(median([entry[1] for entry in selected])),
            'kind': 'REPORTED_SOLD' if sold else 'ASKING', 'sold_count': len(sold),
            'asking_count': len(usable) - len(sold),
            'observed_on': max(entry[3] for entry in selected).isoformat(),
            'sources': list(dict.fromkeys(entry[2] for entry in selected))}


def projection(values, market):
    currency = 'usd' if market == 'us' else 'uah'
    price = amount(values.get(f'{market}_price_{currency}'))
    fx = amount(values.get('fx_uah_per_usd')) if market == 'us' else Decimal(1)
    if not price or not fx:
        return None
    costs = {name: amount(values.get(f'{market}_{name}')) for name in EXPENSES}
    buyer_shipping = amount(values.get(f'{market}_buyer_shipping_{currency}'))
    cost = amount(values.get('cost_uah'))
    ai = amount(values.get('ai_cost_usd'))
    ai_fx = amount(values.get('fx_uah_per_usd'))
    ai_uah = ai * ai_fx if ai is not None and ai_fx else (Decimal(0) if ai == 0 else None)
    absent = [name for name, value in costs.items() if value is None]
    if buyer_shipping is None:
        absent.append('buyer_shipping')
    if cost is None:
        absent.append('cost')
    if ai_uah is None:
        absent.append('ai')
    gross = (price + (buyer_shipping or 0)) * fx
    fees = gross * (costs['fee_pct'] or 0) / 100 + (costs['fixed_fee'] or 0) * fx
    nonfees = sum((costs[name] or 0) for name in EXPENSES if name.endswith('_uah'))
    total = fees + nonfees + (cost or 0) + (ai_uah or 0)
    net = gross - total
    return {'net': net, 'margin': net * 100 / gross,
            'roi': net * 100 / total if not absent and total else None,
            'missing': absent, 'fees': fees, 'known_costs': total}
