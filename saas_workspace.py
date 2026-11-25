"""Separate HTTPS application for testing accounts, store drafts and simulated billing."""
from datetime import timedelta
from hashlib import sha256
import hmac
import json
import os
import re
import secrets
import sqlite3
from tempfile import SpooledTemporaryFile
import time
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

from flask import Flask, Response, g, jsonify, redirect, request, send_file, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import sandbox_billing as billing
from catalog_policy import marketplace_code
from workspace_db import WorkspaceDB, WorkspaceError, audit, replay, save_response
from workspace_ebay import SandboxEBay
from workspace_media import MAX_PHOTO_BYTES, album, prepare_photo, store_photo

COOKIE = '__Host-vintage_workspace'
SESSION_SECONDS = int(timedelta(hours=12).total_seconds())
TERMS_VERSION = 'workspace-sandbox-2026-11-25'
MARKETPLACES = {'EBAY_US', 'EBAY_GB', 'EBAY_DE', 'EBAY_FR', 'EBAY_IT', 'EBAY_ES', 'EBAY_CA', 'EBAY_AU'}
PUBLIC_ENDPOINTS = {'home', 'static', 'legal', 'health', 'registration', 'login', 'plan_list'}
CONDITIONS = {'NEW', 'USED', 'REFURBISHED', 'FOR_PARTS', 'UNVERIFIED'}
EMAIL = re.compile(r'[a-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}', re.ASCII)
KEY = re.compile(r'[A-Za-z0-9_.:-]{8,128}', re.ASCII)
SKU = re.compile(r'[A-Z0-9][A-Z0-9_.-]{0,49}', re.ASCII)


def _json_fields(*fields):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - set(fields):
        raise WorkspaceError('Формат або поля JSON недійсні.')
    return data


def _text(value, limit, *, required=True):
    if isinstance(value, str):
        checked = value.strip()
        if (checked or not required) and len(checked) <= limit and '\x00' not in checked:
            return checked
    raise WorkspaceError('Заповніть поле в допустимих межах.')


def _email(value):
    address = _text(value, 254).lower()
    if not EMAIL.fullmatch(address):
        raise WorkspaceError('Перевірте адресу електронної пошти.')
    return address


def _password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or '\x00' in value:
        raise WorkspaceError('Пароль має містити від 12 до 128 символів.')
    return value


def _digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _rate(store, action, identity, limit):
    now = g.now
    window = now // 900
    bucket = _digest(f'{action}:{identity}')
    with store.connect(write=True) as db:
        db.execute('DELETE FROM attempts WHERE window < ?', (window - 1,))
        count = db.execute(
            'INSERT INTO attempts VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET '
            'count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END, window=excluded.window '
            'RETURNING count', (bucket, window),
        ).fetchone()[0]
    if count > limit:
        raise WorkspaceError('Забагато спроб. Повторіть пізніше.', 429)


def _store(db, account, store_id):
    row = db.execute('SELECT id, marketplace, name FROM stores WHERE account_id=? AND id=?',
                     (account, store_id)).fetchone()
    if row is None:
        raise WorkspaceError('Магазин не знайдено.', 404)
    return dict(row)


def _draft(db, account, store, lot_id):
    row = db.execute(
        'SELECT id, store_id, sku, title, description, condition, revision FROM drafts '
        'WHERE account_id=? AND store_id=? AND id=?', (account, store, lot_id),
    ).fetchone()
    if row is None:
        raise WorkspaceError('Чернетку не знайдено.', 404)
    return dict(row)


def _draft_values(data):
    title = _text(data.get('title'), 80)
    description = _text(data.get('description', ''), 10000, required=False)
    condition = data.get('condition', 'UNVERIFIED')
    if not isinstance(condition, str) or condition not in CONDITIONS:
        raise WorkspaceError('Виберіть підтримуваний стан товару.')
    return title, description, condition


def _rowset(db, statement, parameters):
    return [dict(row) for row in db.execute(statement, parameters)]


def _account_snapshot(db, account):
    return {
        'account': dict(db.execute(
            'SELECT id, email, terms_version, created_at FROM accounts WHERE id=?', (account,),
        ).fetchone()),
        'stores': _rowset(db, 'SELECT id, marketplace, name FROM stores WHERE account_id=? ORDER BY id', (account,)),
        'ebay': _rowset(db, 'SELECT store_id, seller_id, expires_at FROM ebay_connections WHERE account_id=? '
                         'ORDER BY store_id', (account,)),
        'drafts': _rowset(db, 'SELECT id, store_id, sku, title, description, condition, revision FROM drafts '
                          'WHERE account_id=? ORDER BY store_id, sku', (account,)),
        'photos': _rowset(db, 'SELECT store_id, draft_id, slot, digest AS sha256, mime FROM media WHERE account_id=? '
                          'ORDER BY store_id, draft_id, slot', (account,)),
        'support': _rowset(db, 'SELECT id, detail, created_at FROM tickets WHERE account_id=? ORDER BY created_at', (account,)),
        'usage': _rowset(db, 'SELECT id, store_id, feature, minor, created_at FROM usage WHERE account_id=? '
                         'ORDER BY created_at, id', (account,)),
        'actions': _rowset(db, 'SELECT action, resource_id, created_at FROM audit WHERE account_id=? ORDER BY id', (account,)),
        'billing': billing.overview(db, account, g.now),
    }


def create_app(config=None):
    app = Flask(__name__, static_folder='workspace_web', static_url_path='/ui')
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=32 * 1024,
        WORKSPACE_DB=os.environ.get('VINTAGE_WORKSPACE_DB'),
        WORKSPACE_ERASURES=os.environ.get('VINTAGE_WORKSPACE_ERASURES'),
        WORKSPACE_INVITE=os.environ.get('VINTAGE_WORKSPACE_INVITE'),
        WORKSPACE_CLOCK=time.time,
        EBAY_SANDBOX_CLIENT_ID=os.environ.get('VINTAGE_SANDBOX_CLIENT_ID'),
        EBAY_SANDBOX_CLIENT_SECRET=os.environ.get('VINTAGE_SANDBOX_CLIENT_SECRET'),
        EBAY_SANDBOX_RUNAME=os.environ.get('VINTAGE_SANDBOX_RUNAME'),
        EBAY_SANDBOX_ENCRYPTION_KEY=os.environ.get('VINTAGE_SANDBOX_ENCRYPTION_KEY'),
    )
    app.config.update(config or {})
    invite = app.config['WORKSPACE_INVITE']
    if not isinstance(invite, str) or len(invite) < 32 or not app.config['WORKSPACE_DB']:
        raise ValueError('Set a dedicated VINTAGE_WORKSPACE_DB and a random VINTAGE_WORKSPACE_INVITE of at least 32 characters')
    store = WorkspaceDB(app.config['WORKSPACE_DB'], app.config['WORKSPACE_ERASURES'])
    app.extensions['vintage_workspace'] = store
    dummy_password = generate_password_hash(secrets.token_hex(32))
    options = [app.config[name] for name in (
        'EBAY_SANDBOX_CLIENT_ID', 'EBAY_SANDBOX_CLIENT_SECRET', 'EBAY_SANDBOX_RUNAME', 'EBAY_SANDBOX_ENCRYPTION_KEY',
    )]
    ebay = SandboxEBay(*options) if any(options) else None

    @app.before_request
    def check_request():
        if not request.is_secure:
            raise WorkspaceError('Для кабінету потрібне HTTPS.', 403)
        g.now = int(app.config['WORKSPACE_CLOCK']())
        unsafe = request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}
        origin = request.headers.get('Origin')
        if unsafe:
            if (origin and origin != request.host_url.rstrip('/')) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise WorkspaceError('Неприпустиме джерело запиту.', 403)
            if request.endpoint == 'draft_photos' and request.method == 'POST':
                if request.mimetype != 'multipart/form-data':
                    raise WorkspaceError('Додайте фото як файл.', 415)
                request.max_content_length = MAX_PHOTO_BYTES + 65536
                request.max_form_parts = 2
            elif not request.is_json:
                raise WorkspaceError('Використовуйте JSON.', 415)
        if request.endpoint in PUBLIC_ENDPOINTS or not request.endpoint:
            return
        token = request.cookies.get(COOKIE, '')
        if not KEY.fullmatch(token):
            raise WorkspaceError('Увійдіть у тестовий кабінет.', 401)
        g.token_hash = _digest(token)
        with store.connect() as db:
            session = db.execute(
                'SELECT account_id, csrf FROM sessions WHERE token_hash=? AND expires_at>?',
                (g.token_hash, g.now),
            ).fetchone()
        if not session:
            raise WorkspaceError('Увійдіть у тестовий кабінет.', 401)
        g.account = session['account_id']
        g.csrf = session['csrf']
        if unsafe and not hmac.compare_digest(request.headers.get('X-CSRF-Token', '').encode(), g.csrf.encode()):
            raise WorkspaceError('Оновіть сесію й повторіть дію.', 403)

    @app.after_request
    def privacy_headers(response):
        response.headers.update({
            'Cache-Control': 'private, no-store',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                       "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
        })
        return response

    @app.errorhandler(WorkspaceError)
    def workspace_error(error):
        return {'error': str(error)}, error.status

    def once(operation, payload, callback):
        key = request.headers.get('Idempotency-Key', '')
        if not KEY.fullmatch(key):
            raise WorkspaceError('Передайте дійсний Idempotency-Key.')
        digest = _digest(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')))
        with store.connect(write=True) as db:
            result = replay(db, g.account, operation, key, digest)
            if result is None:
                result = callback(db)
                save_response(db, g.account, operation, key, digest, result)
            return result

    @app.get('/')
    def home():
        return app.send_static_file('index.html')

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': 'local_sandbox', 'external_payments_enabled': False,
                'live_publish_enabled': False}

    @app.get('/legal')
    def legal():
        return {
            'version': TERMS_VERSION, 'status': 'тестові умови',
            'terms': 'Кабінет призначений для синтетичних даних. Тарифи, платежі та витрати змодельовано локально.',
            'privacy': 'Локальне сховище містить email, хеш пароля, магазини, чернетки, тестові платежі та журнал дій. '
                       'Кабінет надає експорт і видалення тестового акаунта. Уникайте приватних і платіжних даних.',
            'rights': {'export': 'GET /account/export', 'delete': 'DELETE /account', 'support': 'POST /support'},
        }

    @app.get('/plans')
    def plan_list():
        return billing.plans()

    @app.post('/register')
    def registration():
        _rate(store, 'registration', request.remote_addr, 12)
        data = _json_fields('email', 'password', 'invite', 'terms_version')
        address = _email(data.get('email'))
        password = _password(data.get('password'))
        if data.get('terms_version') != TERMS_VERSION:
            raise WorkspaceError('Підтвердьте поточну версію тестових умов.', 409)
        provided = data.get('invite')
        if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), invite.encode()):
            raise WorkspaceError('Потрібне дійсне запрошення власника.', 403)
        account = uuid4().hex
        password_hash = generate_password_hash(password)
        with store.connect(write=True) as db:
            created = db.execute(
                'INSERT INTO accounts VALUES (?, ?, ?, ?, ?) ON CONFLICT(email) DO NOTHING',
                (account, address, password_hash, TERMS_VERSION, g.now),
            )
            if not created.rowcount:
                raise WorkspaceError('Цю адресу вже зареєстровано.', 409)
            db.execute('INSERT INTO trial VALUES (?, ?)', (account, g.now + billing.TRIAL_SECONDS))
            audit(db, account, 'account.register', None, g.now)
        return {'registered': True}, 201

    @app.post('/login')
    def login():
        _rate(store, 'login-ip', request.remote_addr, 30)
        data = _json_fields('email', 'password')
        address = _email(data.get('email'))
        password = _password(data.get('password'))
        _rate(store, 'login-account', address, 10)
        with store.connect() as db:
            account = db.execute('SELECT id, password_hash FROM accounts WHERE email=?', (address,)).fetchone()
        valid = check_password_hash(account['password_hash'] if account else dummy_password, password)
        if not valid or account is None:
            raise WorkspaceError('Не вдалося увійти з цими даними.', 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.connect(write=True) as db:
            db.execute('DELETE FROM sessions WHERE account_id=? OR expires_at<=?', (account['id'], g.now))
            created = db.execute(
                'INSERT INTO sessions SELECT ?, id, ?, ? FROM accounts WHERE id=?',
                (_digest(token), csrf, g.now + SESSION_SECONDS, account['id']),
            )
            if not created.rowcount:
                raise WorkspaceError('Не вдалося увійти з цими даними.', 401)
        response = jsonify({'csrf': csrf})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @app.get('/session')
    def session_info():
        return {'account': g.account, 'csrf': g.csrf}

    def end_session():
        response = jsonify({'signed_out': True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @app.post('/logout')
    def logout():
        _json_fields()
        with store.connect(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (g.token_hash,))
        return end_session()

    @app.route('/stores', methods=['GET', 'POST'])
    def stores():
        if request.method == 'GET':
            with store.connect() as db:
                return {'stores': _rowset(db, 'SELECT id, marketplace, name FROM stores WHERE account_id=? ORDER BY name',
                                          (g.account,))}
        data = _json_fields('name', 'marketplace')
        name = _text(data.get('name'), 100)
        try:
            marketplace = marketplace_code(data.get('marketplace'))
        except ValueError:
            raise WorkspaceError('Некоректний маркетплейс eBay.') from None
        if marketplace not in MARKETPLACES:
            raise WorkspaceError('Маркетплейс ще не підготовлено до тестування.')
        store_id = uuid4().hex
        with store.connect(write=True) as db:
            added = db.execute(
                'INSERT INTO stores VALUES (?, ?, ?, ?) ON CONFLICT(account_id, marketplace, name) DO NOTHING',
                (store_id, g.account, marketplace, name),
            )
            if not added.rowcount:
                raise WorkspaceError('Магазин із такою назвою вже є.', 409)
            audit(db, g.account, 'store.create', store_id, g.now)
        return {'id': store_id, 'name': name, 'marketplace': marketplace}, 201

    @app.route('/stores/<store_id>/ebay', methods=['GET', 'POST', 'DELETE'])
    def ebay_connection(store_id):
        if request.method == 'GET':
            with store.connect() as db:
                _store(db, g.account, store_id)
                linked = db.execute('SELECT seller_id, expires_at FROM ebay_connections WHERE account_id=? AND store_id=?',
                                    (g.account, store_id)).fetchone()
            return {'environment': 'sandbox', 'enabled': ebay is not None,
                    'connection': dict(linked) if linked else None}
        _json_fields()
        with store.connect(write=True) as db:
            _store(db, g.account, store_id)
            db.execute('DELETE FROM ebay_states WHERE account_id=? AND store_id=?', (g.account, store_id))
            if request.method == 'DELETE':
                removed = db.execute('DELETE FROM ebay_connections WHERE account_id=? AND store_id=?', (g.account, store_id))
                if removed.rowcount:
                    audit(db, g.account, 'ebay.sandbox.disconnect', store_id, g.now)
                return {'disconnected': True}
            if ebay is None:
                raise WorkspaceError('eBay Sandbox ще не налаштовано.', 503)
            state = secrets.token_urlsafe(32)
            db.execute('INSERT INTO ebay_states(account_id, store_id, state_hash, session_hash, expires_at) '
                       'VALUES (?, ?, ?, ?, ?)', (g.account, store_id, _digest(state), g.token_hash, g.now + 600))
        return {'authorization_url': ebay.authorization_url(state)}

    @app.get('/ebay/sandbox/callback')
    def ebay_callback():
        if ebay is None:
            raise WorkspaceError('eBay Sandbox ще не налаштовано.', 503)
        received = request.args.getlist('state')
        if len(received) != 1 or not KEY.fullmatch(received[0]):
            raise WorkspaceError('Підключення eBay не підтверджено.', 409)
        state_hash = _digest(received[0])
        with store.connect(write=True) as db:
            row = db.execute(
                'UPDATE ebay_states SET consumed=1 WHERE state_hash=? AND account_id=? AND session_hash=? '
                'AND expires_at>? AND consumed=0 RETURNING store_id', (state_hash, g.account, g.token_hash, g.now),
            ).fetchone()
        if row is None:
            raise WorkspaceError('Підключення eBay не підтверджено.', 409)
        code = request.args.getlist('code')
        if len(code) != 1 or not code[0] or len(code[0]) > 8192 or request.args.get('error'):
            raise WorkspaceError('Вхід через eBay Sandbox скасовано або не завершено.', 409)
        seller, refresh, expires = ebay.exchange(code[0])
        store_id = row['store_id']
        sealed = ebay.seal(g.account, store_id, refresh)
        completed_at = int(app.config['WORKSPACE_CLOCK']())
        try:
            with store.connect(write=True) as db:
                removed = db.execute(
                    'DELETE FROM ebay_states WHERE state_hash=? AND account_id=? AND store_id=? AND consumed=1 '
                    'AND expires_at>? AND EXISTS (SELECT 1 FROM sessions '
                    'WHERE token_hash=? AND account_id=? AND expires_at>?)',
                    (state_hash, g.account, store_id, completed_at, g.token_hash, g.account, completed_at),
                )
                if not removed.rowcount:
                    raise WorkspaceError('Запит підключення eBay замінено або скасовано.', 409)
                db.execute('INSERT INTO ebay_connections VALUES (?, ?, ?, ?, ?) '
                           'ON CONFLICT(account_id, store_id) DO UPDATE SET seller_id=excluded.seller_id, '
                           'encrypted_refresh=excluded.encrypted_refresh, expires_at=excluded.expires_at',
                           (g.account, store_id, seller, sealed, completed_at + expires))
                audit(db, g.account, 'ebay.sandbox.connect', store_id, completed_at)
        except sqlite3.IntegrityError:
            raise WorkspaceError('Магазин eBay Sandbox уже підключено до іншого профілю.', 409) from None
        return redirect(url_for('home', shop=store_id), code=303)

    @app.route('/stores/<store_id>/drafts', methods=['GET', 'POST'])
    def drafts(store_id):
        if request.method == 'GET':
            with store.connect() as db:
                _store(db, g.account, store_id)
                return {'drafts': _rowset(
                    db, 'SELECT * FROM drafts WHERE account_id=? AND store_id=? ORDER BY sku', (g.account, store_id),
                )}
        data = _json_fields('sku', 'title', 'description', 'condition')
        sku = _text(data.get('sku'), 50).upper()
        if not SKU.fullmatch(sku):
            raise WorkspaceError('SKU містить неприпустимі символи.')
        values = _draft_values(data)

        def insert(db):
            _store(db, g.account, store_id)
            lot_id = uuid4().hex
            added = db.execute(
                'INSERT INTO drafts(id, account_id, store_id, sku, title, description, condition) '
                'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, store_id, sku) DO NOTHING',
                (lot_id, g.account, store_id, sku, *values),
            )
            if not added.rowcount:
                raise WorkspaceError('Цей SKU уже є в магазині.', 409)
            audit(db, g.account, 'draft.create', lot_id, g.now)
            return _draft(db, g.account, store_id, lot_id)

        return once(f'draft.create:{store_id}', data, insert), 201

    @app.route('/stores/<store_id>/drafts/<lot_id>', methods=['GET', 'PATCH'])
    def draft(store_id, lot_id):
        if request.method == 'GET':
            with store.connect() as db:
                data = _draft(db, g.account, store_id, lot_id)
            response = jsonify(data)
            response.set_etag(str(data['revision']))
            return response
        values = _draft_values(_json_fields('title', 'description', 'condition'))
        with store.connect(write=True) as db:
            current = _draft(db, g.account, store_id, lot_id)
            expected = f'"{current["revision"]}"'
            if request.headers.get('If-Match') != expected:
                raise WorkspaceError('Чернетка змінилася. Відкрийте актуальну версію.', 409)
            db.execute(
                'UPDATE drafts SET title=?, description=?, condition=?, revision=revision+1 '
                'WHERE account_id=? AND store_id=? AND id=?', (*values, g.account, store_id, lot_id),
            )
            audit(db, g.account, 'draft.update', lot_id, g.now)
            return _draft(db, g.account, store_id, lot_id)

    @app.route('/stores/<store_id>/drafts/<lot_id>/photos', methods=['GET', 'POST'])
    def draft_photos(store_id, lot_id):
        scope = (g.account, store_id, lot_id)
        with store.connect() as db:
            _draft(db, *scope)
            if request.method == 'GET':
                return album(db, scope)
        inputs = request.files.getlist('photo')
        if len(inputs) != 1 or len(request.files) != 1:
            raise WorkspaceError('Виберіть один кадр для цього запиту.')
        photo = prepare_photo(inputs[0])
        with store.connect(write=True) as db:
            _draft(db, *scope)
            slot, created = store_photo(db, scope, photo)
            if created:
                audit(db, g.account, 'draft.photo', lot_id, g.now)
            return {'slot': slot, **album(db, scope)}, 201 if created else 200

    @app.get('/stores/<store_id>/drafts/<lot_id>/photos/<int:slot>/<kind>')
    def draft_photo(store_id, lot_id, slot, kind):
        if kind == 'preview':
            with store.connect() as db:
                row = db.execute('SELECT preview FROM media WHERE account_id=? AND store_id=? AND draft_id=? AND slot=?',
                                 (g.account, store_id, lot_id, slot)).fetchone()
            if row:
                return Response(row['preview'], mimetype='image/jpeg')
        elif kind == 'original':
            with store.connect() as db:
                row = db.execute('SELECT original, mime FROM media WHERE account_id=? AND store_id=? AND draft_id=? AND slot=?',
                                 (g.account, store_id, lot_id, slot)).fetchone()
            if row:
                extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[row['mime']]
                return Response(row['original'], mimetype=row['mime'],
                                headers={'Content-Disposition': f'attachment; filename="photo-{slot:02d}.{extension}"'})
        raise WorkspaceError('Фото не знайдено.', 404)

    @app.get('/billing')
    def bill():
        with store.connect() as db:
            return billing.overview(db, g.account, g.now)

    @app.post('/billing/test/<action>')
    def bill_action(action):
        if action in {'checkout', 'renew'}:
            data = _json_fields('plan')
            operation = lambda db: billing.checkout(db, g.account, data.get('plan'), g.now,
                                                    renewal=action == 'renew')
        elif action == 'cancel':
            data = _json_fields()
            operation = lambda db: billing.cancel(db, g.account, g.now)
        elif action == 'refund':
            data = _json_fields('payment_id')
            payment = _text(data.get('payment_id'), 32)
            operation = lambda db: billing.refund(db, g.account, payment, g.now)
        else:
            raise WorkspaceError('Невідома тестова операція.', 404)
        return once('billing.' + action, data, operation)

    @app.post('/stores/<store_id>/test-preparation')
    def meter(store_id):
        data = _json_fields()

        def record(db):
            _store(db, g.account, store_id)
            return billing.meter(db, g.account, store_id, g.now)

        return once(f'usage:{store_id}', data, record)

    @app.route('/support', methods=['GET', 'POST'])
    def support():
        if request.method == 'GET':
            with store.connect() as db:
                return {'tickets': _rowset(db, 'SELECT id, detail, created_at FROM tickets WHERE account_id=? '
                                          'ORDER BY created_at', (g.account,))}
        detail = _text(_json_fields('detail').get('detail'), 1500)
        ticket = uuid4().hex
        with store.connect(write=True) as db:
            db.execute('INSERT INTO tickets VALUES (?, ?, ?, ?)', (ticket, g.account, detail, g.now))
            audit(db, g.account, 'support.create', ticket, g.now)
        return {'id': ticket, 'status': 'stored_for_test'}, 201

    @app.get('/account/export')
    def export_account():
        with store.connect(write=True) as db:
            audit(db, g.account, 'account.export', None, g.now)
            output = _account_snapshot(db, g.account)
        return Response(json.dumps(output, ensure_ascii=False), mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename=vintage-test-account.json'})

    @app.get('/account/export/photos')
    def export_account_photos():
        store_id = request.args.get('store')
        criteria, params = 'account_id=?', (g.account,)
        if store_id is not None:
            criteria += ' AND store_id=?'
            params += (store_id,)
        archive = SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        try:
            with store.connect() as db:
                db.execute('BEGIN')
                if store_id is not None:
                    _store(db, g.account, store_id)
                count, size = db.execute(
                    'SELECT count(*), coalesce(sum(length(original)), 0) FROM media WHERE ' + criteria, params,
                ).fetchone()
                if size > 100 * 1024 * 1024 or count > 3000:
                    raise WorkspaceError('Пакет завеликий. Виберіть магазин або завантажте потрібні оригінали з лота.', 413)
                files = []
                with ZipFile(archive, 'w', compression=ZIP_STORED) as package:
                    rows = db.execute('SELECT store_id, draft_id, slot, digest, mime, original '
                                      'FROM media WHERE ' + criteria + ' ORDER BY store_id, draft_id, slot', params)
                    for row in rows:
                        extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[row['mime']]
                        name = f"photos/{row['store_id']}/{row['draft_id']}/{row['slot']:02d}.{extension}"
                        package.writestr(name, row['original'])
                        files.append({'file': name, 'sha256': row['digest']})
                    package.writestr('photos.json', json.dumps(files, ensure_ascii=False))
            with store.connect(write=True) as db:
                audit(db, g.account, 'account.export.photos', store_id, g.now)
            archive.seek(0)
            response = send_file(archive, mimetype='application/zip', as_attachment=True,
                                 download_name='vintage-test-photos.zip', max_age=0)
        except Exception:
            archive.close()
            raise
        return response

    @app.delete('/account')
    def delete_account():
        _rate(store, 'delete', g.account, 5)
        data = _json_fields('password', 'confirm')
        password = _password(data.get('password'))
        with store.connect() as db:
            account = db.execute('SELECT password_hash FROM accounts WHERE id=?', (g.account,)).fetchone()
        if data.get('confirm') is not True or not account or not check_password_hash(account['password_hash'], password):
            raise WorkspaceError('Для видалення потрібне підтвердження і чинний пароль.', 403)
        store.erase_account(g.account, g.now)
        return end_session()

    return app
