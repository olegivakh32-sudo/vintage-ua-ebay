"""Local subscription and cost accounting. Every money amount is simulated."""
from calendar import monthrange
from datetime import datetime, timezone
from uuid import uuid4

from workspace_db import WorkspaceError, audit

CURRENCY = 'UAH'
TRIAL_SECONDS = 7 * 86400
TRIAL_LIMIT = 10
# Price in kopiykas, billing interval in months, maximum draft preparations.
PLANS = {'monthly': (29000, 1, 100), 'annual': (290000, 12, 1200)}
PREPARATION_COST_MINOR = 2


def plans():
    return {
        'mode': 'local_sandbox', 'currency': CURRENCY,
        'trial': {'days': TRIAL_SECONDS // 86400, 'preparations': TRIAL_LIMIT},
        'plans': [
            {'id': name, 'price_minor': price, 'months': months, 'preparations': limit}
            for name, (price, months, limit) in PLANS.items()
        ],
    }


def _subscription(db, account_id):
    return db.execute('SELECT * FROM subscriptions WHERE account_id=?', (account_id,)).fetchone()


def _add_months(now, months):
    start = datetime.fromtimestamp(now, tz=timezone.utc)
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    end = start.replace(year=year, month=month + 1, day=min(start.day, monthrange(year, month + 1)[1]))
    return int(end.timestamp())


def checkout(db, account_id, plan, now, *, renewal=False):
    if not isinstance(plan, str) or plan not in PLANS:
        raise WorkspaceError('Виберіть доступний тестовий тариф.')
    previous = _subscription(db, account_id)
    if previous and previous['state'] == 'active' and previous['ends_at'] > now:
        raise WorkspaceError('Тестова підписка вже діє.', 409)
    if renewal and (
        not previous or previous['state'] != 'active' or previous['cancel_at_end']
        or previous['plan'] != plan
    ):
        raise WorkspaceError('Цю тестову підписку не можна поновити.', 409)

    minor, months, _ = PLANS[plan]
    # A refund and a completely new purchase can occur in the same second.
    start = max(now, previous['starts_at'] + 1) if previous else now
    end = _add_months(start, months)
    payment_id = uuid4().hex
    db.execute(
        "INSERT INTO subscriptions VALUES (?, ?, ?, ?, 0, 'active') "
        "ON CONFLICT(account_id) DO UPDATE SET plan=excluded.plan, starts_at=excluded.starts_at, "
        "ends_at=excluded.ends_at, cancel_at_end=0, state='active'",
        (account_id, plan, start, end),
    )
    db.execute('INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?)',
               (payment_id, account_id, plan, start, end, minor, now))
    audit(db, account_id, 'billing.renew' if renewal else 'billing.checkout', payment_id, now)
    return {'id': payment_id, 'plan': plan, 'start': start, 'end': end, 'minor': minor,
            'currency': CURRENCY, 'mode': 'local_sandbox'}


def cancel(db, account_id, now):
    sub = _subscription(db, account_id)
    if not sub or sub['state'] != 'active':
        raise WorkspaceError('Діючої тестової підписки немає.', 409)
    if not sub['cancel_at_end']:
        db.execute('UPDATE subscriptions SET cancel_at_end=1 WHERE account_id=?', (account_id,))
        audit(db, account_id, 'billing.cancel', None, now)
    return {'cancel_at_end': True, 'ends_at': sub['ends_at']}


def refund(db, account_id, payment_id, now):
    payment = db.execute('SELECT * FROM payments WHERE account_id=? AND id=?',
                         (account_id, payment_id)).fetchone()
    if payment is None:
        raise WorkspaceError('Тестовий платіж не знайдено.', 404)
    added = db.execute('INSERT OR IGNORE INTO refunds VALUES (?, ?, ?)', (payment_id, account_id, now))
    if added.rowcount:
        db.execute(
            "UPDATE subscriptions SET state='refunded', cancel_at_end=1 "
            "WHERE account_id=? AND starts_at=? AND ends_at=?",
            (account_id, payment['starts_at'], payment['ends_at']),
        )
        audit(db, account_id, 'billing.refund', payment_id, now)
    return {'id': payment_id, 'minor': payment['minor'], 'currency': CURRENCY,
            'mode': 'local_sandbox', 'refunded': True}


def _period(db, account_id, now):
    sub = _subscription(db, account_id)
    if sub and sub['state'] == 'active' and sub['ends_at'] > now:
        return sub['ends_at'], PLANS[sub['plan']][2]
    if sub:
        return None, 0
    trial = db.execute('SELECT ends_at FROM trial WHERE account_id=?', (account_id,)).fetchone()
    if trial and trial['ends_at'] > now:
        return trial['ends_at'], TRIAL_LIMIT
    return None, 0


def meter(db, account_id, store_id, now):
    period, limit = _period(db, account_id, now)
    tally = db.execute('SELECT COUNT(*) FROM usage WHERE account_id=? AND period=?',
                       (account_id, period)).fetchone()[0]
    if tally >= limit:
        raise WorkspaceError('Ліміт тестових підготовок вичерпано.', 409)
    event = uuid4().hex
    db.execute('INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?)',
               (event, account_id, store_id, 'test_preparation', PREPARATION_COST_MINOR, period, now))
    return {'id': event, 'remaining': limit - tally - 1, 'estimated_minor': PREPARATION_COST_MINOR,
            'currency': CURRENCY, 'mode': 'local_sandbox'}


def overview(db, account_id, now):
    sub = _subscription(db, account_id)
    period, limit = _period(db, account_id, now)
    tally, cost = db.execute(
        'SELECT COUNT(*), COALESCE(SUM(minor), 0) FROM usage WHERE account_id=? AND period=?',
        (account_id, period),
    ).fetchone()
    history = db.execute(
        'SELECT p.id, p.plan, p.minor, p.created_at, r.created_at AS refunded_at '
        'FROM payments p LEFT JOIN refunds r ON r.account_id=p.account_id AND r.payment_id=p.id '
        'WHERE p.account_id=? ORDER BY p.created_at, p.id', (account_id,),
    )
    return {'mode': 'local_sandbox', 'currency': CURRENCY, 'subscription': dict(sub) if sub else None,
            'period_end': period, 'limit': limit, 'used': tally, 'estimated_minor': cost,
            'payments': [dict(row) for row in history]}
