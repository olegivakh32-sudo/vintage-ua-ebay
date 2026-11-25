"""Private photo preparation and selection for a sandbox account draft."""
from hashlib import sha256
from io import BytesIO
import json

from PIL import Image, ImageOps, UnidentifiedImageError

from mobile_photos import image_sharpness, measurement_hint
from product_codes import catalogue_hints, read_retail_codes
from workspace_db import WorkspaceError
from workspace_photo_review import review_status

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_ACCOUNT_BYTES = 256 * 1024 * 1024
MIMES = {'JPEG': 'image/jpeg', 'MPO': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}


def prepare_photo(upload):
    raw = upload.stream.read(MAX_PHOTO_BYTES + 1)
    if len(raw) > MAX_PHOTO_BYTES:
        raise WorkspaceError('Фото перевищує 10 МБ.', 413)
    try:
        with Image.open(BytesIO(raw)) as source:
            mime = MIMES.get(source.format)
            if not mime or source.width * source.height > 50_000_000:
                raise ValueError('Unsupported photo')
            if source.format in ('JPEG', 'MPO'):
                source.draft('RGB', (1800, 1800))
            picture = ImageOps.exif_transpose(source)
            picture.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            if 'A' in picture.getbands() or 'transparency' in picture.info:
                rgba = picture.convert('RGBA')
                canvas = Image.new('RGBA', rgba.size, 'white')
                canvas.alpha_composite(rgba)
                picture = canvas
            picture = picture.convert('RGB')
            rendered = BytesIO()
            picture.save(rendered, 'JPEG', quality=88, optimize=True)
            digest = sha256(raw).hexdigest()
            return (digest, mime, raw, rendered.getvalue(), measurement_hint(picture, ''),
                    json.dumps(read_retail_codes(picture)), image_sharpness(picture))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, RuntimeError):
        raise WorkspaceError('Відкрийте справжнє JPEG, PNG або WebP в допустимих розмірах.') from None


def album(db, scope):
    rows = db.execute(
        'SELECT slot, digest, hint, codes FROM media WHERE account_id=? AND store_id=? AND draft_id=? ORDER BY slot',
        scope,
    ).fetchall()
    review = review_status(db, scope)
    return {
        'photos': [{'slot': row['slot'], 'sha256': row['digest'], 'hint': row['hint']} for row in rows],
        'suggested_main': review.get('suggested_main'),
        'review': review,
        'retail_codes': catalogue_hints((row['slot'], json.loads(row['codes'])) for row in rows),
    }


def store_photo(db, scope, photo):
    digest, _, original, preview, _, _, _ = photo
    row = db.execute('SELECT slot FROM media WHERE account_id=? AND store_id=? AND draft_id=? AND digest=?',
                     (*scope, digest)).fetchone()
    if row:
        return row['slot'], False
    slot = db.execute('SELECT COALESCE(MAX(slot), 0)+1 FROM media WHERE account_id=? AND store_id=? AND draft_id=?',
                      scope).fetchone()[0]
    if slot > 24:
        raise WorkspaceError('Для чернетки доступно до 24 кадрів.', 409)
    total = db.execute('SELECT COALESCE(SUM(length(original)+length(preview)),0) FROM media WHERE account_id=?',
                       (scope[0],)).fetchone()[0]
    if total + len(original) + len(preview) > MAX_ACCOUNT_BYTES:
        raise WorkspaceError('Тестовий ліміт зберігання фото перевищено.', 409)
    db.execute('INSERT INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (*scope, slot, *photo))
    db.execute('UPDATE drafts SET revision=revision+1 WHERE account_id=? AND store_id=? AND id=?', scope)
    return slot, True
