"""Conservative, local measurement hints. A missing hint never admits a photo."""

import math
import re

from PIL import Image, ImageFilter, ImageOps, ImageStat


_DARK_EDGE = re.compile(rb"[\xac-\xff][\x00-\x92]")


def measurement_hint(image, filename):
    words = re.sub(r"[_-]+", " ", str(filename or "")).casefold()
    if re.search(r"\b(?:ruler|caliper|measuring tape|лінійк\w*|рулетк\w*|штангенциркул\w*)\b", words):
        return "ruler"
    if re.search(r"\b(?:digital scale|kitchen scale|weighing|ваг[иах]|зважуван\w*)\b", words):
        return "scale"

    mono = image.convert("L")
    mono.thumbnail((420, 420))
    mono = ImageOps.autocontrast(mono, cutoff=1)
    for angle in (0, 90, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75):
        view = mono if angle == 0 else mono.rotate(angle, Image.Resampling.NEAREST, expand=True, fillcolor=128)
        if _has_ruler_ticks(view):
            return "ruler"
    if _has_digit_cluster(mono) or _has_digit_cluster(ImageOps.invert(mono)):
        return "display"
    return None


def _has_ruler_ticks(image):
    """Match a run of parallel short and long graduated ticks on a common baseline."""
    width, height = image.size
    if width < 70 or height < 35:
        return False
    data = image.tobytes()
    for y in range(17, height - 17):
        row_start = y * width
        edges = [row_start + edge.start() + 1
                 for edge in _DARK_EDGE.finditer(data[row_start:row_start + width])]
        if len(edges) < 7:
            continue
        for direction in (-width, width):
            ticks = []
            for index in edges:
                if data[index - direction * 3] < 157:
                    continue
                length = 0
                while length < 17 and data[index + direction * length] < 155:
                    length += 1
                if 3 <= length < 17:
                    ticks.append((index, length))
            for start in range(len(ticks) - 6):
                run = ticks[start:start + 7]
                gaps = [right[0] - left[0] for left, right in zip(run, run[1:])]
                pitch = sorted(gaps)[3]
                lengths = [length for _, length in run]
                if not (5 <= pitch <= 28 and max(lengths) - min(lengths) >= 3):
                    continue
                if all(abs(gap - pitch) <= 2 for gap in gaps):
                    return True
                # Oblique views can change spacing gradually. Require a mostly
                # monotone run with two recurring graduation lengths.
                changes = [b - a for a, b in zip(gaps, gaps[1:])]
                shortest, longest = min(lengths), max(lengths)
                if (max(abs(gap - pitch) for gap in gaps) <= max(2, pitch * .28)
                        and max(map(abs, changes)) <= 2
                        and (sum(change < 0 for change in changes) <= 1 or sum(change > 0 for change in changes) <= 1)
                        and sum(length <= shortest + 1 for length in lengths) >= 2
                        and sum(length >= longest - 1 for length in lengths) >= 2):
                    return True
    return False


def _has_digit_cluster(image):
    """Suggest a display when several similarly sized narrow glyphs share a baseline."""
    width, height = image.size
    if width < 60 or height < 40:
        return False
    # Bridge small gaps inside segmented digits before measuring connected components.
    mask = bytearray(image.point(lambda value: 255 if value < 68 else 0).filter(ImageFilter.MaxFilter(3)).tobytes())
    if mask.count(255) > len(mask) * .82:
        return False
    glyphs = []
    for index, marked in enumerate(mask):
        if not marked:
            continue
        mask[index] = 0
        pending = [index]
        min_x = max_x = index % width
        min_y = max_y = index // width
        while pending:
            position = pending.pop()
            y, x = divmod(position, width)
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            neighbors = (
                position - 1 if x else position,
                position + 1 if x < width - 1 else position,
                position - width if y else position,
                position + width if y < height - 1 else position,
            )
            for adjacent in neighbors:
                if mask[adjacent]:
                    mask[adjacent] = 0
                    pending.append(adjacent)
        glyph_w, glyph_h = max_x - min_x + 1, max_y - min_y + 1
        if 12 <= glyph_h <= min(80, height / 3) and .28 <= glyph_w / glyph_h <= .85:
            glyphs.append((min_x, max_x, min_y, max_y, glyph_h))
    glyphs.sort()
    for index, first in enumerate(glyphs):
        same_row = [glyph for glyph in glyphs[index + 1:]
                    if first[1] < glyph[0] <= first[0] + 3 * first[4]
                    and abs(glyph[2] - first[2]) <= max(2, math.ceil(first[4] / 8))
                    and abs(glyph[3] - first[3]) <= max(2, math.ceil(first[4] / 8))]
        if len(same_row) >= 2:
            earliest_right_edge = min(glyph[1] for glyph in same_row)
            latest_left_edge = max(glyph[0] for glyph in same_row)
            if earliest_right_edge < latest_left_edge:
                return True
    return False


def image_sharpness(image):
    """Relative edge strength at a common sample size for review and cover sorting."""
    sample = image.convert('L')
    sample.thumbnail((256, 256), Image.Resampling.LANCZOS)
    width, height = sample.size
    if width < 8 or height < 8:
        return 0.0
    inner = (2, 2, width - 2, height - 2)
    contrast = ImageStat.Stat(sample.crop(inner)).stddev[0]
    if contrast < 4:
        return 0.0
    edges = sample.filter(ImageFilter.FIND_EDGES).crop(inner)
    return round(ImageStat.Stat(edges).rms[0] / contrast, 4)


def duplicate_photo_indices(fingerprints):
    """Return later exact matches, leaving the first occurrence available."""
    first = {}
    duplicates = {}
    for index, fingerprint in fingerprints.items():
        if fingerprint:
            original = first.setdefault(fingerprint, index)
            if index != original:
                duplicates[index] = original
    return duplicates
