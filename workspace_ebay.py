"""Store authorization for eBay Sandbox with no live credentials or write scopes."""
import base64
import binascii
import json
from os import urandom
from urllib.parse import urlencode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import requests

from workspace_db import WorkspaceError

AUTHORIZE = 'https://auth.sandbox.ebay.com/oauth2/authorize'
TOKEN_API = 'https://api.sandbox.ebay.com/identity/v1/oauth2/token'
IDENTITY_API = 'https://apiz.sandbox.ebay.com/commerce/identity/v1/user'
IDENTITY_SCOPE = 'https://api.ebay.com/oauth/api_scope/commerce.identity.readonly'


class SandboxEBay:
    def __init__(self, client_id, client_secret, ru_name, encryption_key, transport=requests):
        if not all(isinstance(value, str) and value.strip() for value in (client_id, client_secret, ru_name, encryption_key)):
            raise ValueError('Configure all four eBay Sandbox settings')
        try:
            key = base64.b64decode(encryption_key, altchars=b'-_', validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('Invalid encryption key for eBay Sandbox') from None
        if len(key) != 32:
            raise ValueError('eBay Sandbox requires a 32 byte encryption key')
        self._cipher = AESGCM(key)
        self._client = (client_id, client_secret)
        self._ru_name = ru_name
        self._transport = transport

    def authorization_url(self, state):
        return AUTHORIZE + '?' + urlencode({
            'client_id': self._client[0], 'response_type': 'code',
            'redirect_uri': self._ru_name, 'scope': IDENTITY_SCOPE, 'state': state,
        })

    def exchange(self, code):
        try:
            token = self._transport.post(
                TOKEN_API, auth=self._client, timeout=(5, 12), allow_redirects=False,
                data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self._ru_name},
            )
            if token.status_code != 200:
                raise ValueError('Token exchange failed')
            result = token.json()
            if not isinstance(result, dict):
                raise ValueError('Invalid token response')
            access = result.get('access_token')
            refresh = result.get('refresh_token')
            expiry = result.get('refresh_token_expires_in')
            if (not all(isinstance(value, str) and 0 < len(value) <= 8192 and '\n' not in value and '\r' not in value
                        for value in (access, refresh))
                    or type(expiry) is not int or not 0 < expiry <= 50_000_000):
                raise ValueError('Invalid token response')
            identity = self._transport.get(IDENTITY_API, timeout=(5, 12), allow_redirects=False,
                                           headers={'Authorization': 'Bearer ' + access})
            if identity.status_code != 200:
                raise ValueError('Identity lookup failed')
            user = identity.json()
            seller = user.get('userId') if isinstance(user, dict) else None
            if not isinstance(seller, str) or not 1 <= len(seller) <= 128:
                raise ValueError('Identity missing')
            return seller, refresh, expiry
        except (requests.RequestException, ValueError):
            raise WorkspaceError('eBay Sandbox не підтвердив підключення. Почніть нову спробу.', 502) from None

    def seal(self, account, store, refresh):
        nonce = urandom(12)
        associated_data = json.dumps(['ebay-sandbox-v1', account, store]).encode()
        return nonce + self._cipher.encrypt(nonce, refresh.encode(), associated_data)
