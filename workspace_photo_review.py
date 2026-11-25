"""Durable, offline photo review for the isolated workspace."""
import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
import time
from uuid import uuid4

from PIL import Image

from catalogue_labels import Catalogue, scan_labels
from mobile_photos import MEASUREMENT_HINT_VERSION, measurement_hint
from photo_assessment import VISUAL_VERSION, composition_and_tone, suggested_photo_role
from product_codes import BARCODE_HINT_VERSION, catalogue_hints
from product_evidence import barcode_evidence
from workspace_db import WorkspaceDB

LEASE_SECONDS = 90
MAX_ATTEMPTS = 3


def queue_review(db, scope, now, catalogue=None):
    rows = db.execute(
        'SELECT slot, digest FROM media WHERE account_id=? AND store_id=? AND draft_id=? ORDER BY slot',
        scope,
    ).fetchall()
    if not rows:
        return
    album_key = sha256(json.dumps([VISUAL_VERSION, MEASUREMENT_HINT_VERSION, BARCODE_HINT_VERSION,
                                   catalogue.digest if catalogue else None, [tuple(row) for row in rows]]).encode()).hexdigest()
    db.execute(
        'INSERT INTO photo_reviews (account_id, store_id, draft_id, album_key, queued_at) VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT(account_id, store_id, draft_id) DO UPDATE SET album_key=excluded.album_key, '
        "queued_at=excluded.queued_at, state='PENDING', attempts=0, lease_token=NULL, lease_until=NULL, result=NULL "
        'WHERE photo_reviews.album_key<>excluded.album_key',
        (*scope, album_key, now),
    )


def review_status(db, scope):
    row = db.execute(
        'SELECT state, result FROM photo_reviews WHERE account_id=? AND store_id=? AND draft_id=?', scope,
    ).fetchone()
    if row is None:
        return {'state': 'UNREQUESTED'}
    return {'state': row['state'], **(json.loads(row['result']) if row['result'] else {})}


def _claim(store, now):
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE photo_reviews SET state='FAILED', lease_token=NULL, lease_until=NULL "
            "WHERE state='RUNNING' AND lease_until<=? AND attempts>=?", (now, MAX_ATTEMPTS),
        )
        job = db.execute(
            'SELECT account_id, store_id, draft_id, attempts FROM photo_reviews '
            "WHERE state='PENDING' OR (state='RUNNING' AND lease_until<=?) ORDER BY queued_at, attempts, draft_id LIMIT 1",
            (now,),
        ).fetchone()
        if job is None:
            return None
        scope = tuple(job)[:3]
        token = uuid4().hex
        db.execute(
            "UPDATE photo_reviews SET state='RUNNING', attempts=attempts+1, lease_token=?, lease_until=? "
            'WHERE account_id=? AND store_id=? AND draft_id=?', (token, now + LEASE_SECONDS, *scope),
        )
        return scope, token, job['attempts'] + 1


def _review(rows, catalogue=None):
    photos = []
    for row in rows:
        with Image.open(BytesIO(row['preview'])) as image:
            visual = composition_and_tone(image, row['sharpness'])
            hint = measurement_hint(image, '') or row['hint']
        role = suggested_photo_role(visual['kind'], hint, row['sharpness'])
        photos.append({'slot': row['slot'], **visual, 'hint': hint, 'role': role})
    candidates = [photo for photo in photos if photo['role'] == 'EBAY_PRODUCT']
    main = max(candidates, key=lambda photo: photo['score'], default=None)
    codes = catalogue_hints((row['slot'], json.loads(row['codes'])) for row in rows)
    evidence = {'barcode': barcode_evidence(codes)} if codes['candidates'] else {}
    if catalogue:
        optional = sorted((row for row in rows if row['hint'] not in {'scale', 'ruler'}),
                          key=lambda row: -row['sharpness'])
        labels = scan_labels([(row['slot'], row['preview']) for row in optional], catalogue)
        if labels:
            evidence['catalogue_matches'] = labels
    return {'photos': photos, 'suggested_main': main['slot'] if main else None, 'product_evidence': evidence}


def run_one(store, clock=time.time, *, catalogue=None):
    """Process one attempt and commit only if its lease still owns the current album."""
    claim = _claim(store, int(clock()))
    if claim is None:
        return None
    scope, token, attempt = claim
    with store.connect() as db:
        rows = db.execute(
            'SELECT slot, preview, hint, sharpness, codes FROM media WHERE account_id=? AND store_id=? AND draft_id=? '
            'ORDER BY slot', scope,
        ).fetchall()
    result = None
    try:
        result = json.dumps(_review(rows, catalogue), ensure_ascii=False, allow_nan=False)
        state = 'READY'
    except (OSError, ValueError, RuntimeError, Image.DecompressionBombError):
        state = 'FAILED' if attempt >= MAX_ATTEMPTS else 'PENDING'
    now = int(clock())
    with store.connect(write=True) as db:
        changed = db.execute(
            'UPDATE photo_reviews SET state=?, result=?, lease_token=NULL, lease_until=NULL '
            'WHERE account_id=? AND store_id=? AND draft_id=? AND lease_token=? AND lease_until>?',
            (state, result, *scope, token, now),
        ).rowcount
    return state if changed else 'SUPERSEDED'


def main():
    parser = argparse.ArgumentParser(description='Run a bounded local workspace photo-review batch.')
    parser.add_argument('--limit', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')
    path = os.environ.get('VINTAGE_WORKSPACE_DB')
    if not path:
        parser.error('VINTAGE_WORKSPACE_DB is required')
    store = WorkspaceDB(path, os.environ.get('VINTAGE_WORKSPACE_ERASURES'))
    filename = os.environ.get('VINTAGE_WORKSPACE_CATALOGUE')
    catalogue = Catalogue.read(filename) if filename else None
    counts = dict.fromkeys(('READY', 'PENDING', 'FAILED', 'SUPERSEDED'), 0)
    for _ in range(args.limit):
        result = run_one(store, catalogue=catalogue)
        if result is None:
            break
        counts[result] += 1
    print(json.dumps(counts))
    return int(bool(counts['FAILED']))


if __name__ == '__main__':
    raise SystemExit(main())
