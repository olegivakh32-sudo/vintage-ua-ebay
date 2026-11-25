"""Storage for the isolated multi-account sandbox."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_meta (
    id INTEGER PRIMARY KEY CHECK (id=1), lineage TEXT NOT NULL,
    restore_required INTEGER NOT NULL DEFAULT 0 CHECK (restore_required IN (0, 1))
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    terms_version TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE(account_id, id), UNIQUE(account_id, marketplace, name)
);
CREATE TABLE IF NOT EXISTS ebay_states (
    account_id TEXT NOT NULL, store_id TEXT NOT NULL, state_hash TEXT UNIQUE NOT NULL,
    session_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1)),
    PRIMARY KEY (account_id, store_id),
    FOREIGN KEY (account_id, store_id) REFERENCES stores(account_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ebay_connections (
    account_id TEXT NOT NULL, store_id TEXT NOT NULL, seller_id TEXT UNIQUE NOT NULL,
    encrypted_refresh BLOB NOT NULL, expires_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, store_id),
    FOREIGN KEY (account_id, store_id) REFERENCES stores(account_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, store_id TEXT NOT NULL,
    sku TEXT NOT NULL, title TEXT NOT NULL, condition TEXT NOT NULL,
    description TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (account_id, store_id) REFERENCES stores(account_id, id) ON DELETE CASCADE,
    UNIQUE(account_id, store_id, sku)
);
CREATE UNIQUE INDEX IF NOT EXISTS draft_scope ON drafts(account_id, store_id, id);
CREATE TABLE IF NOT EXISTS media (
    account_id TEXT NOT NULL, store_id TEXT NOT NULL, draft_id TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 24),
    digest TEXT NOT NULL, mime TEXT NOT NULL, original BLOB NOT NULL, preview BLOB NOT NULL,
    hint TEXT, codes TEXT NOT NULL, sharpness REAL NOT NULL,
    PRIMARY KEY (account_id, store_id, draft_id, slot),
    UNIQUE(account_id, store_id, draft_id, digest),
    FOREIGN KEY (account_id, store_id, draft_id) REFERENCES drafts(account_id, store_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    detail TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    action TEXT NOT NULL, resource_id TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    operation TEXT NOT NULL, request_key TEXT NOT NULL, digest TEXT NOT NULL,
    response TEXT NOT NULL, PRIMARY KEY (account_id, operation, request_key)
);
CREATE TABLE IF NOT EXISTS trial (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE, ends_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    plan TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
    cancel_at_end INTEGER NOT NULL DEFAULT 0 CHECK (cancel_at_end IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('active', 'refunded')),
    UNIQUE(account_id, starts_at, ends_at)
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    plan TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
    minor INTEGER NOT NULL CHECK (minor > 0), created_at INTEGER NOT NULL,
    UNIQUE(account_id, starts_at, ends_at), UNIQUE(account_id, id)
);
CREATE TABLE IF NOT EXISTS refunds (
    payment_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at INTEGER NOT NULL,
    FOREIGN KEY (account_id, payment_id) REFERENCES payments(account_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL, feature TEXT NOT NULL, minor INTEGER NOT NULL CHECK (minor >= 0),
    period INTEGER NOT NULL, created_at INTEGER NOT NULL,
    FOREIGN KEY (account_id, store_id) REFERENCES stores(account_id, id)
);
"""


class WorkspaceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _private_db_file(path, *, create):
    if not path.exists():
        if not create:
            raise ValueError('The current erasure journal is required')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Workspace databases must be private regular files')


class WorkspaceDB:
    def __init__(self, path, erasures=None):
        self.path = Path(path).expanduser().resolve()
        self.erasures = (Path(erasures).expanduser().resolve() if erasures
                         else self.path.with_suffix(self.path.suffix + '.erasures'))
        if self.path == self.erasures:
            raise ValueError('The erasure journal must be separate from the workspace database')
        _private_db_file(self.path, create=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        with self.connect(write=True) as db:
            meta = db.execute('SELECT lineage, restore_required FROM workspace_meta WHERE id=1').fetchone()
            if meta and meta['restore_required'] and not erasures:
                raise ValueError('Restoring a snapshot requires the current erasure journal')
            _private_db_file(self.erasures, create=meta is None)
            self._attach_erasures(db)
            if meta is None:
                lineage = uuid4().hex
                db.execute('CREATE TABLE IF NOT EXISTS privacy.meta(id INTEGER PRIMARY KEY CHECK (id=1), lineage TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS privacy.erased(account_id TEXT PRIMARY KEY, erased_at INTEGER NOT NULL)')
                db.execute('INSERT INTO privacy.meta VALUES (1, ?)', (lineage,))
                db.execute('INSERT INTO workspace_meta VALUES (1, ?, 0)', (lineage,))
            elif self._journal_lineage(db) != meta['lineage']:
                raise ValueError('The erasure journal belongs to another workspace')
            elif meta['restore_required']:
                db.execute('DELETE FROM accounts WHERE id IN (SELECT account_id FROM privacy.erased)')
                db.execute('DELETE FROM sessions')
                db.execute('UPDATE workspace_meta SET restore_required=0 WHERE id=1')
            self.lineage = meta['lineage'] if meta else lineage

    def _attach_erasures(self, db):
        db.execute('ATTACH DATABASE ? AS privacy', (str(self.erasures),))
        if any(db.execute(f'PRAGMA {schema}.journal_mode').fetchone()[0] != 'delete' for schema in ('main', 'privacy')):
            raise ValueError('Workspace erasure transactions require SQLite DELETE journal mode')

    @staticmethod
    def _journal_lineage(db):
        if db.execute("SELECT 1 FROM privacy.sqlite_master WHERE type='table' AND name='meta'").fetchone():
            row = db.execute('SELECT lineage FROM privacy.meta WHERE id=1').fetchone()
            return row[0] if row else None
        return None

    @contextmanager
    def connect(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA secure_delete=ON')
            with connection:
                if write:
                    connection.execute('BEGIN IMMEDIATE')
                yield connection

    def backup(self, destination):
        """Create an exclusive, consistent private snapshot. Restore is an offline operation."""
        path = Path(destination)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with self.connect() as source, closing(sqlite3.connect(path)) as copy:
                source.backup(copy)
                with copy:
                    copy.execute('UPDATE workspace_meta SET restore_required=1 WHERE id=1')
                if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Workspace backup failed integrity check')
        except Exception:
            path.unlink()
            raise
        return path

    def erase_account(self, account_id, now):
        with self.connect(write=True) as db:
            self._attach_erasures(db)
            if self._journal_lineage(db) != self.lineage:
                raise ValueError('The erasure journal belongs to another workspace')
            db.execute('INSERT OR IGNORE INTO privacy.erased VALUES (?, ?)', (account_id, now))
            db.execute('DELETE FROM accounts WHERE id=?', (account_id,))


def audit(db, account_id, action, resource_id, now):
    db.execute('INSERT INTO audit(account_id, action, resource_id, created_at) VALUES (?, ?, ?, ?)',
               (account_id, action, resource_id, now))


def replay(db, account_id, operation, key, digest):
    saved = db.execute(
        'SELECT digest, response FROM operations WHERE account_id=? AND operation=? AND request_key=?',
        (account_id, operation, key),
    ).fetchone()
    if saved is None:
        return None
    if saved['digest'] != digest:
        raise WorkspaceError('Цей ключ уже застосовано до іншого запиту.', 409)
    return json.loads(saved['response'])


def save_response(db, account_id, operation, key, digest, result):
    db.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?)',
               (account_id, operation, key, digest, json.dumps(result, ensure_ascii=False)))
