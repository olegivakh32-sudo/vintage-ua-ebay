"""A signed archive of one held mobile lot and its exact local assets."""
from hashlib import sha256
import json
from pathlib import Path
import re
from zipfile import ZIP_STORED, ZipFile


FILE_LIMIT = 10 * 1024 * 1024
PACKAGE_LIMIT = 240 * 1024 * 1024
MANIFEST_LIMIT = 10 * 1024 * 1024
PID = re.compile(r'mobile-[a-f0-9]{32}')
PHOTO = re.compile(r'[a-f0-9]{32}\.jpg')
APPROVED = re.compile(r'[a-f0-9]{64}\.jpg')


def lot_photo_names(record):
    data = record['snapshot']
    pid = data['picker_session_id']
    photos = data['photos']
    if not PID.fullmatch(pid) or not 1 <= len(photos) <= 24:
        raise ValueError('Expected a held lot with phone photos')
    folder = sha256(pid.encode()).hexdigest()
    names = {}
    for photo in photos:
        prefix, basename = photo['mobile_file'].split('/')
        digest = photo['mobile_original_sha256']
        if prefix != folder or not PHOTO.fullmatch(basename) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Invalid photo reference')
        names[basename] = photo.get('photo_fingerprint')
        names[basename[:-4] + '.source'] = digest
    if len(names) != 2 * len(photos):
        raise ValueError('Duplicate photo reference')
    return folder, names


def _approval_names(raw):
    approvals = json.loads(raw)
    if not isinstance(approvals, dict) or not 1 <= len(approvals) <= 24:
        raise ValueError('Invalid approved photo list')
    names = {'approved.json': sha256(raw).hexdigest()}
    for index, item in approvals.items():
        filename = item['file']
        if not re.fullmatch(r'([1-9]|1\d|2[0-4])', index) or not APPROVED.fullmatch(filename):
            raise ValueError('Invalid approved photo')
        if filename != item['sha256'] + '.jpg':
            raise ValueError('Invalid approved photo checksum')
        names[filename] = item['sha256']
    return names


def _read(stream, limit):
    raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError('Invalid archive entry size')
    return raw


def export_package(stream, asset_root, record, signer):
    folder, names = lot_photo_names(record)
    root = Path(asset_root) / folder
    if record['snapshot'].get('photo_editor_approved'):
        with (root / 'approved.json').open('rb') as source:
            names.update(_approval_names(_read(source, FILE_LIMIT)))
    total = 0
    manifest = {}
    with ZipFile(stream, 'w', compression=ZIP_STORED) as archive:
        for name, expected in names.items():
            with (root / name).open('rb') as source:
                raw = _read(source, FILE_LIMIT)
            total += len(raw)
            digest = sha256(raw).hexdigest()
            if total > PACKAGE_LIMIT or (expected and digest != expected):
                raise ValueError('Photo is missing or its checksum has changed')
            manifest[name] = digest
            archive.writestr(name, raw)
        signed = signer.dumps({'record': record, 'assets': manifest})
        if len(signed.encode()) > MANIFEST_LIMIT:
            raise ValueError('Manifest exceeds size limit')
        archive.writestr('hold.json', signed)


def staged_import(stream, staging, signer):
    """Validate the envelope before copying individual allowed ZIP entries."""
    with ZipFile(stream) as archive:
        entries = archive.infolist()
        if not 3 <= len(entries) <= 74 or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError('Unexpected number of archive entries')
        total = sum(entry.file_size for entry in entries if entry.filename != 'hold.json')
        if total > PACKAGE_LIMIT:
            raise ValueError('Archive exceeds size limit')
        with archive.open('hold.json') as source:
            signed = _read(source, MANIFEST_LIMIT).decode()
        envelope = signer.loads(signed)
        record, assets = envelope['record'], envelope['assets']
        folder, names = lot_photo_names(record)
        if record['snapshot'].get('photo_editor_approved'):
            with archive.open('approved.json') as source:
                names.update(_approval_names(_read(source, FILE_LIMIT)))
        if set(names) != set(assets) or {entry.filename for entry in entries} != set(names) | {'hold.json'}:
            raise ValueError('Archive entries do not match the held lot')
        for name, expected in names.items():
            with archive.open(name) as source:
                raw = _read(source, FILE_LIMIT)
            digest = sha256(raw).hexdigest()
            if digest != assets[name] or (expected and digest != expected):
                raise ValueError('Archive photo checksum mismatch')
            with (Path(staging) / name).open('xb') as output:
                output.write(raw)
        return folder, record
