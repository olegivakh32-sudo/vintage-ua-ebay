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
EBAY_FEES_URL = 'https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees?id=4822'
EBAY_INTL_FEES_URL = 'https://export.ebay.com/en/fees-regulations-policies/seller-fees/international-fees/'
OLX_FEES_URL = ('https://help.olx.ua/olxuahelp/s/article/'
                'оплата-за-успішні-продажі-по-olx-доставка-V2')
DOMESTIC_URL = 'https://www.ukrposhta.ua/ua/domestic-parcels'
TARIFFS_CHECKED = '25.11.2026'
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


def basic_ebay_fee(path, fee_base):
    """Published standard eBay.com fee for the supported no-Store categories."""
    parts = [part.strip().casefold() for part in str(path).split(' > ')]
    total = amount(fee_base)
    if not total or len(parts) < 2 or any('nft' in part for part in parts):
        return None
    root = parts[0]
    if root == 'coins & paper money' and 'bullion' not in parts:
        rate = Decimal('.1325')
    elif root == 'books & magazines':
        rate = Decimal('.153')
    elif root in {'home & garden', 'cameras & photo', 'computers/tablets & networking'}:
        rate = Decimal('.136')
    else:
        return None
    per_order = Decimal('.30') if total <= 10 else Decimal('.40')
    return min(total, 7500) * rate + max(0, total - 7500) * Decimal('.0235') + per_order


def olx_fee(price, business=False):
    price = amount(price)
    if not price:
        return None
    return min(Decimal(499), price * (Decimal('.03') if business else Decimal('.02')) + 20)


def domestic_base_quote(weight, package_dimensions, declared_value):
    """Ukrposhta Basic intercity, one standard counter parcel, no COD."""
    weight, declared = amount(weight), amount(declared_value)
    parts = str(package_dimensions).removesuffix(' cm').split(' x ')
    axes = [amount(axis) for axis in parts]
    if (not weight or weight > 30 or not declared or declared > 1_000_000
            or len(axes) != 3 or not all(axes) or max(axes) > 100):
        return None
    length, width, height = axes
    volume = length * width * height
    for cap, kilos, uah in ((1500, 5, 50), (4000, 5, 60), (8000, 5, 80), (20000, 30, 110),
                            (80000, 30, 150), (120000, 30, 220)):
        if volume <= cap and weight <= kilos:
            return Decimal(uah) + (declared * Decimal('.005') if declared > 500 else Decimal(0))
    return None


def projection(values, market, *, published_fee=None, pending=()):
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
    if published_fee is not None:
        absent = [name for name in absent if name not in {'fee_pct', 'fixed_fee'}]
    absent.extend(pending)
    if buyer_shipping is None:
        absent.append('buyer_shipping')
    if cost is None:
        absent.append('cost')
    if ai_uah is None:
        absent.append('ai')
    gross = (price + (buyer_shipping or 0)) * fx
    fees = (published_fee * fx if published_fee is not None else
            gross * (costs['fee_pct'] or 0) / 100 + (costs['fixed_fee'] or 0) * fx)
    nonfees = sum((costs[name] or 0) for name in EXPENSES if name.endswith('_uah'))
    total = fees + nonfees + (cost or 0) + (ai_uah or 0)
    net = gross - total
    return {'net': net, 'margin': net * 100 / gross,
            'roi': net * 100 / total if not absent and total else None,
            'missing': absent, 'fees': fees, 'known_costs': total}
