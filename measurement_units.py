"""Normalize seller-entered dimensions and mass to the application's storage units."""

from decimal import Decimal, DecimalException


def metric_value(raw, unit, *, weight):
    text = str(raw or '').strip().replace(',', '.')
    if not text:
        return None
    units = {'kg': Decimal(1), 'g': Decimal('.001')} if weight else {'cm': Decimal(1), 'mm': Decimal('.1')}
    try:
        number = Decimal(text) * units[unit]
        limit = 100 if weight else 1000
        minimum = Decimal('.0001') if weight else Decimal('.01')
        if not number.is_finite() or not minimum <= number <= limit:
            raise ValueError
    except (DecimalException, KeyError, ValueError):
        raise ValueError('Недопустиме значення або одиниця виміру') from None
    return float(number)
