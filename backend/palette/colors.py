"""Color conversions and corrections shared by artwork analysis and lights."""

from __future__ import annotations

import colorsys

FALLBACK_PALETTE = [
    "#e74c3c",
    "#8e44ad",
    "#1a0a2e",
    "#f39c12",
    "#1a5276",
    "#08080a",
]


def hex_from_rgb(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def clamp_color(value: float) -> int:
    return max(0, min(255, round(value)))


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def color_luminance_rgb(rgb: tuple[int, int, int]) -> float:
    red, green, blue = rgb
    return (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)


def color_saturation_rgb(rgb: tuple[int, int, int]) -> float:
    red, green, blue = rgb
    _, saturation, _ = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    return saturation


def color_hue_rgb(rgb: tuple[int, int, int]) -> float:
    red, green, blue = rgb
    hue, _, _ = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    return hue


def is_red_rgb(rgb: tuple[int, int, int]) -> bool:
    hue = color_hue_rgb(rgb)
    return hue <= 0.055 or hue >= 0.94


def is_olive_or_green_rgb(rgb: tuple[int, int, int]) -> bool:
    hue = color_hue_rgb(rgb)
    saturation = color_saturation_rgb(rgb)
    return 0.12 <= hue <= 0.42 and saturation >= 0.12


def is_paper_or_neutral_rgb(rgb: tuple[int, int, int]) -> bool:
    luminance = color_luminance_rgb(rgb)
    saturation = color_saturation_rgb(rgb)
    red, green, blue = rgb
    channel_spread = max(rgb) - min(rgb)
    return luminance >= 218 and (saturation < 0.24 or channel_spread < 42 or min(red, green, blue) > 188)


def displayable_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    hue, saturation, value = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    luminance = color_luminance_rgb(rgb)

    if is_red_rgb(rgb) and saturation >= 0.6 and value >= 0.2:
        # Snap vivid rose/scarlet tints to pure red and deepen them so reds read
        # as red on the lights instead of washing out to pink. Softer tints
        # (saturation < 0.6) are genuine pinks, skin or browns now that swatches
        # come from hue clustering, so they pass through untouched. Near-black
        # is left alone too: a black area averaging (1, 0, 0) is "fully
        # saturated red" in HSV and used to become a dark red.
        hue = 0.0 if hue <= 0.06 else 0.99
        saturation = max(saturation, 0.92)
        value = min(max(value, 0.42), 0.78)
    elif is_olive_or_green_rgb(rgb):
        saturation = max(saturation, 0.38)
        if luminance < 52:
            value = min(0.48, max(value * 1.45, 0.24))
        else:
            value = min(max(value, 0.32), 0.72)
    elif is_paper_or_neutral_rgb(rgb):
        saturation = min(0.18, saturation)
        value = min(value, 0.72)

    next_red, next_green, next_blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return (clamp_color(next_red * 255), clamp_color(next_green * 255), clamp_color(next_blue * 255))


def adjust_color(rgb: tuple[int, int, int], saturation_scale: float = 1.0, value_scale: float = 1.0) -> tuple[int, int, int]:
    red, green, blue = rgb
    hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    saturation = max(0.0, min(1.0, saturation * saturation_scale))
    value = max(0.08, min(1.0, value * value_scale))
    next_red, next_green, next_blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return (clamp_color(next_red * 255), clamp_color(next_green * 255), clamp_color(next_blue * 255))


def hex_to_rgb_value(color: str) -> tuple[int, int, int] | None:
    text = color.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def red_pink_uncertain_rgb(rgb: tuple[int, int, int]) -> bool:
    # Only vivid rose tones are "uncertain" (LEDs render them pink even when the
    # artwork means red). Soft pinks below the saturation bar are real pinks --
    # hue clustering does not fabricate them -- and must not be pushed to red.
    hue = color_hue_rgb(rgb)
    saturation = color_saturation_rgb(rgb)
    red, green, blue = rgb
    return saturation >= 0.6 and (0.90 <= hue <= 0.985 or hue <= 0.025) and red > 120 and blue > green + 12


def correct_red_pink_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    if not red_pink_uncertain_rgb(rgb):
        return rgb
    hue = 0.0 if color_hue_rgb(rgb) <= 0.05 else 0.985
    _, saturation, value = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    red, green, blue = colorsys.hsv_to_rgb(hue, max(saturation, 0.82), min(max(value, 0.48), 0.92))
    return (clamp_color(red * 255), clamp_color(green * 255), clamp_color(blue * 255))
