"""Lightweight local hints for measurement photos before the optional visual AI pass."""

import re

from PIL import Image


def measurement_hint(image, filename):
    """Recognize explicit labels and a straight ruler with repeated graduated ticks."""
    words = re.sub(r"[_-]+", " ", str(filename or "")).casefold()
    if re.search(r"\b(?:ruler|caliper|measuring tape|лінійк\w*|рулетк\w*|штангенциркул\w*)\b", words):
        return "ruler"
    if re.search(r"\b(?:digital scale|kitchen scale|weighing|ваг[иах]|зважуван\w*)\b", words):
        return "scale"

    mono = image.convert("L")
    mono.thumbnail((480, 480))
    if _has_ruler_ticks(mono) or _has_ruler_ticks(mono.transpose(Image.Transpose.ROTATE_90)):
        return "ruler"
    return None


def _has_ruler_ticks(image):
    """Find repeated dark ticks anchored to a common bright edge, in either direction."""
    width, height = image.size
    if width < 90 or height < 30:
        return False
    pixels = image.load()
    for y in range(16, height - 16, 2):
        for direction in (-1, 1):
            ticks = []
            last_x = -3
            for x in range(2, width - 2):
                if pixels[x, y] >= 95 or pixels[x - 1, y] < 160:
                    continue
                if pixels[x, y - direction * 3] < 165 or x - last_x < 3:
                    continue
                length = 0
                while length < 16 and pixels[x, y + direction * length] < 110:
                    length += 1
                if 4 <= length < 16:
                    ticks.append((x, length))
                    last_x = x
            for start in range(len(ticks) - 8):
                run = ticks[start:start + 9]
                gaps = [right[0] - left[0] for left, right in zip(run, run[1:])]
                pitch = sorted(gaps)[4]
                lengths = [length for _, length in run]
                if 5 <= pitch <= 28 and all(abs(gap - pitch) <= 1 for gap in gaps) and max(lengths) - min(lengths) >= 3:
                    return True
    return False
