"""Expiring verification for the Render acceptance owner's Basic Auth pass."""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error
from pathlib import Path
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUDIENCE = 'vintage-ua-ebay-p094-test.onrender.com'
PUBLIC_KEY_PATH = Path(__file__).resolve().with_name('render_owner.pub')
PASS_PATTERN = re.compile(r'v1\.([0-9]{10})\.([A-Za-z0-9_-]{86})', re.ASCII)


def _message(expires_at):
    return f'vintage-ua-owner:v1:{AUDIENCE}:{expires_at}'.encode('ascii')


def load_verifier(path=PUBLIC_KEY_PATH):
    try:
        public_bytes = urlsafe_b64decode(path.read_bytes().strip())
        return Ed25519PublicKey.from_public_bytes(public_bytes)
    except (OSError, ValueError, Error):
        return None


def issue_pass(private_key, expires_at):
    signature = urlsafe_b64encode(private_key.sign(_message(expires_at))).decode('ascii').rstrip('=')
    return f'v1.{expires_at}.{signature}'


def valid_pass(password, verifier, now):
    match = PASS_PATTERN.fullmatch(password or '')
    if not match or verifier is None:
        return False
    expiry = int(match[1])
    if now >= expiry:
        return False
    try:
        verifier.verify(urlsafe_b64decode(match[2] + '=='), _message(expiry))
    except (InvalidSignature, ValueError, Error):
        return False
    return True
