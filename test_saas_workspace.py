from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from saas_workspace import COOKIE, SESSION_SECONDS, TERMS_VERSION, create_app
from sandbox_billing import TRIAL_LIMIT
from workspace_ebay import IDENTITY_API, IDENTITY_SCOPE, TOKEN_API


ORIGIN = 'https://vintage-workspace.test'
PASSWORD = 'test-long-password-94'
INVITE = 'invite-only-for-local-synthetic-data'
ALICE = 'alice@example.test'
BOB = 'bob@example.test'


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = 1772352000
        self.config = {'WORKSPACE_DB': Path(self.temp.name) / 'workspace.sqlite',
                       'WORKSPACE_INVITE': INVITE, 'WORKSPACE_CLOCK': lambda: self.clock}
        self.app = create_app(self.config)
        self.first = self.app.test_client()
        self.second = self.app.test_client()

    def send(self, client, path, method='GET', body=None, *, csrf=None, key=None, headers=None):
        values = dict(headers or {})
        if csrf:
            values['X-CSRF-Token'] = csrf
        if key:
            values['Idempotency-Key'] = key
        return client.open(path, method=method, json=body, headers=values, base_url=ORIGIN)

    def register(self, client, email, **changes):
        return self.send(client, '/register', 'POST', dict({
            'email': email, 'password': PASSWORD, 'invite': INVITE, 'terms_version': TERMS_VERSION,
        }, **changes))

    def login(self, client, email):
        response = self.send(client, '/login', 'POST', {'email': email, 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        return response

    def account(self, client, email):
        self.assertEqual(self.register(client, email).status_code, 201)
        response = self.login(client, email)
        return response.json['csrf']

    def store(self, client, csrf, name='My shop'):
        result = self.send(client, '/stores', 'POST', {'name': name, 'marketplace': 'EBAY_US'}, csrf=csrf)
        self.assertEqual(result.status_code, 201)
        return result.json['id']

    def draft(self, client, csrf, store, key='draft-create-001'):
        result = self.send(client, f'/stores/{store}/drafts', 'POST', {
            'sku': 'LOT-004', 'title': 'Replacement capacitor', 'condition': 'NEW',
        }, csrf=csrf, key=key)
        self.assertEqual(result.status_code, 201, result.get_data(as_text=True))
        return result.json

    def checkout(self, client, csrf, key='billing-start-01', plan='monthly'):
        return self.send(client, '/billing/test/checkout', 'POST', {'plan': plan}, csrf=csrf, key=key)

    def test_https_invite_terms_session_and_cross_site_checks(self):
        self.assertEqual(self.first.get('/health').status_code, 403)
        self.assertEqual(self.send(self.first, '/stores').status_code, 401)
        self.assertEqual(self.register(self.first, ALICE, invite='invalid').status_code, 403)
        self.assertEqual(self.register(self.first, ALICE, terms_version='old').status_code, 409)
        self.assertEqual(self.register(self.first, ALICE).status_code, 201)
        self.assertEqual(self.register(self.first, ALICE.upper()).status_code, 409)
        authentication = self.login(self.first, ALICE)
        cookie = authentication.headers['Set-Cookie']
        self.assertIn(COOKIE + '=', cookie)
        self.assertIn('Secure; HttpOnly; Path=/; SameSite=Lax', cookie)
        self.assertNotIn('Domain=', cookie)
        csrf = authentication.json['csrf']
        self.assertEqual(self.send(self.first, '/stores', 'POST', {}).status_code, 403)
        self.assertEqual(self.send(self.first, '/stores', 'POST', {}, csrf=csrf,
                                   headers={'Origin': 'https://elsewhere.example'}).status_code, 403)
        self.assertEqual(self.send(self.first, '/logout', 'POST', {}, csrf=csrf).status_code, 200)
        self.assertEqual(self.send(self.first, '/stores').status_code, 401)

    def test_cookie_rotation_expiry_and_rate_limit(self):
        csrf = self.account(self.first, ALICE)
        token = self.first.get_cookie(COOKIE, domain='vintage-workspace.test').value
        self.second.set_cookie(COOKIE, token, domain='vintage-workspace.test', secure=True)
        next_csrf = self.login(self.first, ALICE).json['csrf']
        self.assertNotEqual(next_csrf, csrf)
        self.assertEqual(self.send(self.second, '/session').status_code, 401)
        self.clock += SESSION_SECONDS
        self.assertEqual(self.send(self.first, '/session').status_code, 401)
        for attempt in range(11):
            result = self.send(self.first, '/login', 'POST', {'email': ALICE, 'password': 'not-the-password-0000'})
            self.assertEqual(result.status_code, 401 if attempt < 10 else 429)

    def test_drafts_and_shops_are_scoped_and_revisions_are_optimistic(self):
        alice, bob = self.account(self.first, ALICE), self.account(self.second, BOB)
        one = self.store(self.first, alice)
        two = self.store(self.first, alice, 'Another shop')
        foreign = self.store(self.second, bob)
        draft = self.draft(self.first, alice, one)
        self.assertEqual(draft, self.draft(self.first, alice, one))
        self.assertNotEqual(draft['id'], self.draft(self.first, alice, two)['id'])
        self.assertNotEqual(draft['id'], self.draft(self.second, bob, foreign)['id'])
        self.assertEqual(self.send(self.second, f'/stores/{one}/drafts').status_code, 404)
        url = f'/stores/{one}/drafts/{draft["id"]}'
        self.assertEqual(self.send(self.second, url).status_code, 404)
        self.assertEqual(self.send(self.first, f'/stores/{two}/drafts/{draft["id"]}').status_code, 404)
        self.assertEqual(self.send(self.second, url, 'PATCH', {'title': 'Other'}, csrf=bob).status_code, 404)
        self.assertEqual(self.send(self.first, url).headers['ETag'], '"1"')
        patch = {'title': 'Vintage capacitor', 'description': 'Stored draft', 'condition': 'USED'}
        changed = self.send(self.first, url, 'PATCH', patch, csrf=alice, headers={'If-Match': '"1"'})
        self.assertEqual(changed.json['revision'], 2)
        self.assertEqual(self.send(self.first, url, 'PATCH', patch, csrf=alice,
                                   headers={'If-Match': '"1"'}).status_code, 409)
        self.assertEqual(self.send(self.first, f'/stores/{one}/drafts', 'POST', {
            'sku': 'LOT-004', 'title': 'Different', 'condition': 'NEW',
        }, csrf=alice, key='draft-create-003').status_code, 409)

    def test_checkout_cancel_refund_and_cross_account_access(self):
        alice, bob = self.account(self.first, ALICE), self.account(self.second, BOB)
        self.assertEqual(self.checkout(self.first, alice, plan=['monthly']).status_code, 400)
        paid = self.checkout(self.first, alice).json
        self.assertEqual(paid, self.checkout(self.first, alice).json)
        self.assertEqual(self.checkout(self.first, alice, plan='annual').status_code, 409)
        self.assertEqual(self.checkout(self.first, alice, key='billing-start-02').status_code, 409)
        self.assertEqual(self.send(self.first, '/billing/test/checkout', 'POST',
                                   {'plan': 'annual', 'card_number': 'TEST-ONLY'}, csrf=alice,
                                   key='unknown-field-1').status_code, 400)
        self.assertEqual(self.send(self.second, '/billing').json['payments'], [])
        payment = {'payment_id': paid['id']}
        self.assertEqual(self.send(self.second, '/billing/test/refund', 'POST', payment,
                                   csrf=bob, key='billing-refund-01').status_code, 404)
        self.assertTrue(self.send(self.first, '/billing/test/cancel', 'POST', {}, csrf=alice,
                                  key='billing-cancel-01').json['cancel_at_end'])
        for i in range(2):
            self.assertTrue(self.send(self.first, '/billing/test/refund', 'POST', payment, csrf=alice,
                                      key=f'billing-refund-{i:02d}').json['refunded'])
        view = self.send(self.first, '/billing').json
        self.assertEqual(view['subscription']['state'], 'refunded')
        self.assertEqual(len(view['payments']), 1)
        self.assertIsNotNone(view['payments'][0]['refunded_at'])

    def test_parallel_charge_and_replay_produce_one_payment(self):
        csrf = self.account(self.first, ALICE)
        token = self.first.get_cookie(COOKIE, domain='vintage-workspace.test').value
        ready = Barrier(3)

        def pay(index):
            client = self.app.test_client()
            client.set_cookie(COOKIE, token, domain='vintage-workspace.test', secure=True)
            ready.wait(timeout=5)
            key = 'concurrent-same' if index < 2 else 'concurrent-other'
            return key, self.checkout(client, csrf, key=key).status_code

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(pay, range(3)))
        if results[2][1] == 200:
            self.assertEqual([status for _, status in results], [409, 409, 200])
        else:
            self.assertEqual([status for _, status in results], [200, 200, 409])
        self.assertEqual(len(self.send(self.first, '/billing').json['payments']), 1)

    def test_cancel_prevents_renewal_and_annual_period_can_begin_manually(self):
        csrf = self.account(self.first, ALICE)
        month = self.checkout(self.first, csrf).json
        self.send(self.first, '/billing/test/cancel', 'POST', {}, csrf=csrf, key='cancel-before-end')
        self.clock = month['end']
        csrf = self.login(self.first, ALICE).json['csrf']
        renew = self.send(self.first, '/billing/test/renew', 'POST', {'plan': 'monthly'},
                          csrf=csrf, key='renew-after-cancel')
        self.assertEqual(renew.status_code, 409)
        year = self.checkout(self.first, csrf, key='choose-annual-1', plan='annual').json
        self.assertGreater(year['end'] - year['start'], 364 * 86400)
        self.assertEqual(len(self.send(self.first, '/billing').json['payments']), 2)

    def test_ebay_sandbox_is_disabled_until_explicit_configuration(self):
        csrf = self.account(self.first, ALICE)
        store = self.store(self.first, csrf)
        path = f'/stores/{store}/ebay'
        self.assertEqual(self.send(self.first, path).json, {
            'environment': 'sandbox', 'enabled': False, 'connection': None,
        })
        self.assertEqual(self.send(self.first, path, 'POST', {}, csrf=csrf).status_code, 503)

    def test_ebay_state_is_bound_to_store_account_session_and_expires(self):
        self.app = create_app(dict(self.config, EBAY_SANDBOX_CLIENT_ID='test-app-id',
                                   EBAY_SANDBOX_CLIENT_SECRET='test-secret',
                                   EBAY_SANDBOX_RUNAME='test-runame',
                                   EBAY_SANDBOX_ENCRYPTION_KEY=urlsafe_b64encode(bytes(32)).decode()))
        self.first, self.second = self.app.test_client(), self.app.test_client()
        alice, bob = self.account(self.first, ALICE), self.account(self.second, BOB)
        store = self.store(self.first, alice)
        foreign = self.store(self.second, bob)
        secret = 'synthetic-refresh-token-with-private-content'
        token = Mock(status_code=200)
        token.json.return_value = {'access_token': 'synthetic-access', 'refresh_token': secret,
                                   'refresh_token_expires_in': 999}
        identity = Mock(status_code=200)
        identity.json.return_value = {'userId': 'SANDBOX-SELLER-ID', 'email': 'should-not-be-used@example.test'}
        path = f'/stores/{store}/ebay'
        with patch('workspace_ebay.requests.post', return_value=token) as exchange, \
                patch('workspace_ebay.requests.get', return_value=identity) as identify:
            self.assertEqual(self.send(self.second, path, 'POST', {}, csrf=bob).status_code, 404)
            opened = self.send(self.first, path, 'POST', {}, csrf=alice).json
            uri = urlsplit(opened['authorization_url'])
            self.assertEqual(uri.netloc, 'auth.sandbox.ebay.com')
            args = parse_qs(uri.query)
            self.assertEqual(args['scope'], [IDENTITY_SCOPE])
            url = f'/ebay/sandbox/callback?code=local-code&state={args["state"][0]}'
            self.assertEqual(self.send(self.second, url).status_code, 409)
            exchange.assert_not_called()
            self.assertEqual(self.send(self.first, url).status_code, 303)
            self.assertEqual(self.send(self.first, url).status_code, 409)
            exchange.assert_called_once()
            self.assertEqual(exchange.call_args.args[0], TOKEN_API)
            self.assertFalse(exchange.call_args.kwargs['allow_redirects'])
            self.assertEqual(identify.call_args.args[0], IDENTITY_API)
            self.assertEqual(self.send(self.first, path).json['connection']['seller_id'], 'SANDBOX-SELLER-ID')
            self.assertIsNone(self.send(self.second, f'/stores/{foreign}/ebay').json['connection'])
            exported = self.send(self.first, '/account/export').get_data(as_text=True)
            self.assertNotIn(secret, exported)
            self.assertNotIn('should-not-be-used', exported)
            self.assertNotIn(secret.encode(), Path(self.config['WORKSPACE_DB']).read_bytes())
            opened = self.send(self.first, path, 'POST', {}, csrf=alice).json
            replaced = parse_qs(urlsplit(opened['authorization_url']).query)['state'][0]
            self.clock += 601
            self.assertEqual(self.send(self.first, f'/ebay/sandbox/callback?code=x&state={replaced}').status_code, 409)
            exchange.assert_called_once()
            self.assertEqual(self.send(self.first, path, 'DELETE', {}, csrf=alice).status_code, 200)
            self.assertIsNone(self.send(self.first, path).json['connection'])

    def test_trial_quota_replay_and_per_store_cost(self):
        csrf = self.account(self.first, ALICE)
        store = self.store(self.first, csrf)
        other = self.store(self.first, csrf, 'Other')
        for i in range(TRIAL_LIMIT):
            path = f'/stores/{store if i % 2 else other}/test-preparation'
            params = {'csrf': csrf, 'key': f'preparation-{i:02d}'}
            result = self.send(self.first, path, 'POST', {}, **params)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.send(self.first, path, 'POST', {}, **params).json, result.json)
        self.assertEqual(self.send(self.first, f'/stores/{store}/test-preparation', 'POST', {},
                                   csrf=csrf, key='one-past-limit').status_code, 409)
        view = self.send(self.first, '/billing').json
        self.assertEqual(view['used'], TRIAL_LIMIT)
        self.assertEqual(view['estimated_minor'], 2 * TRIAL_LIMIT)

    def test_export_delete_and_consistent_backup_exclude_foreign_rows(self):
        alice, bob = self.account(self.first, ALICE), self.account(self.second, BOB)
        store = self.store(self.first, alice)
        self.draft(self.first, alice, store)
        self.draft(self.second, bob, self.store(self.second, bob))
        self.checkout(self.first, alice)
        self.send(self.first, f'/stores/{store}/test-preparation', 'POST', {}, csrf=alice, key='test-usage-001')
        self.send(self.first, '/support', 'POST', {'detail': 'Test ticket'}, csrf=alice)
        storage = self.app.extensions['vintage_workspace']
        early = storage.backup(Path(self.temp.name) / 'before-erasure.sqlite')
        export = self.send(self.first, '/account/export')
        self.assertNotIn(BOB, export.get_data(as_text=True))
        self.assertNotIn('password_hash', export.get_data(as_text=True))
        self.assertEqual(len(export.json['drafts']), 1)
        self.assertEqual(len(export.json['support']), 1)
        self.assertEqual(self.send(self.second, '/support').json['tickets'], [])
        result = self.send(self.first, '/account', 'DELETE', {'confirm': True, 'password': PASSWORD}, csrf=alice)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.send(self.first, '/account/export').status_code, 401)
        copy = storage.backup(Path(self.temp.name) / 'backup.sqlite')
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(copy) as db:
            self.assertEqual(db.execute('SELECT email FROM accounts').fetchall(), [(BOB,)])
            self.assertEqual(db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM payments').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'current erasure journal'):
            create_app(dict(self.config, WORKSPACE_DB=early))
        restarted = create_app(dict(self.config, WORKSPACE_DB=early, WORKSPACE_ERASURES=storage.erasures))
        restored = restarted.test_client()
        rejected = self.send(restored, '/login', 'POST', {'email': ALICE, 'password': PASSWORD})
        self.assertEqual(rejected.status_code, 401)
        self.login(restored, BOB)
        self.assertEqual(len(self.send(restored, '/account/export').json['drafts']), 1)
