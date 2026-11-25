"""Conservative local scene clues for selecting and improving listing photographs."""
from collections import deque
from math import isfinite
from statistics import median

from PIL import Image, ImageFilter, ImageStat


EDIT_FIELDS = {
    'b': ('brightness', .75, 1.30), 'c': ('contrast', .75, 1.30),
    's': ('color', .70, 1.35), 'h': ('sharpness', .70, 1.50),
}
VISUAL_VERSION = 2
SCENE_TYPES = {'GENERAL_PRODUCT', 'FULL_SET', 'DETAIL', 'MARKINGS', 'DEFECT',
               'SCALE', 'RULER', 'BARCODE', 'PACKAGE', 'OTHER', 'UNCERTAIN'}
EVIDENCE_SCENES = {'SCALE', 'RULER', 'PACKAGE', 'OTHER'}


def edit_settings(values):
    result = {}
    for name, low, high in EDIT_FIELDS.values():
        try:
            value = float(values.get(name, 1))
        except (ValueError, TypeError):
            value = 1
        result[name] = round(max(low, min(high, value)), 3) if isfinite(value) else 1
    if values.get('balanced') is True:
        result['balanced'] = True
    return result


def _cutoff(histogram, fraction):
    target = sum(histogram) * fraction
    count = 0
    for value, number in enumerate(histogram):
        count += number
        if count >= target:
            return value
    return 255


def _corner_patches(image):
    width, height = image.size
    side = max(3, min(width, height) // 15)
    stats = [ImageStat.Stat(image.crop((x, y, x + side, y + side)))
             for x in (0, width - side) for y in (0, height - side)]
    return [patch.median for patch in stats], [max(patch.stddev) for patch in stats]


def _flat_background(corners, deviations):
    background = tuple(median(values) for values in zip(*corners))
    matching = sum(max(abs(a - b) for a, b in zip(rgb, background)) <= 22 and deviation < 17
                   for rgb, deviation in zip(corners, deviations))
    return background if matching >= 3 else None


def _gradient_background(image, corners, deviations):
    if max(deviations) >= 19:
        return None
    for band in zip(*corners):
        if max(band) - min(band) > 160 or abs(band[0] + band[3] - band[1] - band[2]) > 30:
            return None
    width, height = image.size
    side = max(3, min(width, height) // 15)
    for (x, y), (first, last) in (
        ((width // 2, side // 2), (0, 2)), ((width // 2, height - 1 - side // 2), (1, 3)),
        ((side // 2, height // 2), (0, 1)), ((width - 1 - side // 2, height // 2), (2, 3)),
    ):
        radius = side // 2
        patch = ImageStat.Stat(image.crop((x - radius, y - radius, x + radius + 1, y + radius + 1)))
        expected = [(a + b) / 2 for a, b in zip(corners[first], corners[last])]
        if max(patch.stddev) > 24 or max(abs(a - b) for a, b in zip(patch.median, expected)) > 22:
            return None
    return corners


def _foreground(image, flat, gradient):
    width, height = image.size
    intensity = bytearray(width * height)
    pixels = image.load()
    for y in range(height):
        if gradient:
            row_mix = y / (height - 1)
            left = [a + (b - a) * row_mix for a, b in zip(gradient[0], gradient[1])]
            right = [a + (b - a) * row_mix for a, b in zip(gradient[2], gradient[3])]
        for x in range(width):
            basis = flat
            if gradient:
                col_mix = x / (width - 1)
                basis = [a + (b - a) * col_mix for a, b in zip(left, right)]
            if max(abs(value - base) for value, base in zip(pixels[x, y], basis)) >= 35:
                intensity[y * width + x] = 255
    return Image.frombytes('L', image.size, bytes(intensity)).filter(ImageFilter.MedianFilter(3))


def _significant_regions(mask, minimum):
    width, height = mask.size
    data = bytearray(mask.tobytes())
    sizes = []
    for index, value in enumerate(data):
        if not value:
            continue
        size = 0
        todo = deque([index])
        data[index] = 0
        while todo:
            position = todo.popleft()
            y, x = divmod(position, width)
            size += 1
            for next_index in (position - 1 if x else position, position + 1 if x < width - 1 else position,
                               position - width if y else position, position + width if y < height - 1 else position):
                if data[next_index]:
                    data[next_index] = 0
                    todo.append(next_index)
        if size >= minimum:
            sizes.append(size)
    return sizes


def composition_and_tone(image, sharpness):
    sample = image.convert('RGB')
    sample.thumbnail((128, 128), Image.Resampling.LANCZOS)
    width, height = sample.size
    if min(width, height) < 24:
        return {'kind': 'UNCERTAIN', 'score': -999.0, 'tone': {}}
    corners, deviations = _corner_patches(sample)
    background = _flat_background(corners, deviations)
    gradient = _gradient_background(sample, corners, deviations) if background is None else None
    gray = sample.convert('L')
    hist = gray.histogram()
    low, middle, high = (_cutoff(hist, fraction) for fraction in (.02, .5, .98))
    tone = {'brightness': 1.06 if middle < 108 and high < 218 else 1,
            'contrast': 1.035 if 20 < high - low < 100 and low > 13 and high < 228 else 1,
            'color': 1, 'sharpness': 1.08 if .04 < sharpness < .23 else 1,
            'balanced': background is not None and 120 <= min(background) and max(background) - min(background) <= 16}
    if background is None and gradient is None:
        return {'kind': 'UNCERTAIN', 'score': -3 + min(sharpness, .65), 'tone': tone}
    mask = _foreground(sample, background, gradient)
    box = mask.getbbox()
    area = sum(mask.histogram()[1:]) / (width * height)
    if not box or area < .035 or area > .87:
        return {'kind': 'UNCERTAIN', 'score': -6 + min(sharpness, .65), 'tone': tone}
    left, top, right, bottom = box
    clipped = min(left / width, top / height, (width - right) / width, (height - bottom) / height) < .018
    regions = _significant_regions(mask, max(20, int(width * height * .035)))
    distributed = not clipped and len(regions) >= 2
    occupancy = (right - left) * (bottom - top) / (width * height)
    center = abs((left + right) / width - 1) + abs((top + bottom) / height - 1)
    exposure = (hist[0] + hist[255]) / (width * height)
    score = (1.4 * min(occupancy, .78) + .9 * min(sharpness, .5)
             + (1.3 if distributed else 0) - 2.4 * clipped - center - .3 * exposure)
    kind = 'CROP' if clipped else 'GROUP_VIEW' if distributed else 'GENERAL_VIEW'
    return {'kind': kind, 'score': round(score, 4), 'tone': tone}


def mild_white_balance(image):
    sample = image.copy()
    sample.thumbnail((128, 128), Image.Resampling.LANCZOS)
    if min(sample.size) < 24:
        return image
    background = _flat_background(*_corner_patches(sample))
    if background is None or min(background) < 120 or max(background) - min(background) > 16:
        return image
    target = median(background)
    bands = [channel.point([round(min(255, level * max(.97, min(1.03, target / observed))))
                            for level in range(256)])
             for channel, observed in zip(image.split(), background)]
    return Image.merge('RGB', bands)
