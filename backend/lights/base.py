from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LightResult:
    name: str
    ok: bool
    message: str


class LightAdapter(Protocol):
    def status(self, device: dict[str, Any]) -> LightResult:
        ...

    def set_color(
        self,
        device: dict[str, Any],
        color: str,
        brightness: int,
        transition_ms: int,
    ) -> LightResult:
        ...

    def turn_off(self, device: dict[str, Any]) -> LightResult:
        ...


@dataclass
class UnsupportedLightAdapter:
    """Report an unknown saved adapter without sending any device commands."""

    name: str

    def status(self, device: dict[str, Any]) -> LightResult:
        return LightResult(
            name=device_name(device),
            ok=False,
            message=f"Unsupported light adapter: {self.name}.",
        )

    def set_color(
        self,
        device: dict[str, Any],
        color: str,
        brightness: int,
        transition_ms: int,
    ) -> LightResult:
        return self.status(device)

    def turn_off(self, device: dict[str, Any]) -> LightResult:
        return self.status(device)


def device_name(device: dict[str, Any]) -> str:
    return str(device.get("name") or device.get("device_id") or "Unnamed light")


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    cleaned = color.strip().lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"Invalid hex color: {color}")
    return tuple(int(cleaned[index : index + 2], 16) for index in (0, 2, 4))


def hex_from_rgb(rgb: tuple[int, int, int]) -> str:
    red, green, blue = (max(0, min(255, int(round(channel)))) for channel in rgb)
    return f"#{red:02x}{green:02x}{blue:02x}"


# Below this saturation a color reads as neutral; hue interpolation with it
# would swing through arbitrary hues, so those pairs blend in RGB instead.
LERP_NEUTRAL_SATURATION = 0.15


def lerp_hex(start: str, end: str, fraction: float) -> str:
    """Interpolate between two hex colors. fraction 0 -> start, 1 -> end.

    Chromatic pairs blend in HSV along the shortest hue arc so mid-fade colors
    stay saturated (red -> blue passes through vivid purple, never washed-out
    grey/pink). Pairs involving a neutral fall back to straight RGB blending.
    """
    fraction = max(0.0, min(1.0, fraction))
    try:
        start_rgb = hex_to_rgb(start)
        end_rgb = hex_to_rgb(end)
    except ValueError:
        return end

    start_h, start_s, start_v = colorsys.rgb_to_hsv(*(channel / 255 for channel in start_rgb))
    end_h, end_s, end_v = colorsys.rgb_to_hsv(*(channel / 255 for channel in end_rgb))
    if start_s < LERP_NEUTRAL_SATURATION or end_s < LERP_NEUTRAL_SATURATION:
        blended = tuple(start_rgb[i] + (end_rgb[i] - start_rgb[i]) * fraction for i in range(3))
        return hex_from_rgb(blended)

    hue_delta = end_h - start_h
    if hue_delta > 0.5:
        hue_delta -= 1.0
    elif hue_delta < -0.5:
        hue_delta += 1.0
    hue = (start_h + hue_delta * fraction) % 1.0
    saturation = start_s + (end_s - start_s) * fraction
    value = start_v + (end_v - start_v) * fraction
    blended = colorsys.hsv_to_rgb(hue, saturation, value)
    return hex_from_rgb(tuple(channel * 255 for channel in blended))


def srgb_to_linear(channel: float) -> float:
    """sRGB-encoded 0-1 value to linear light (what an LED's PWM duty produces)."""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def rgb_to_oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to the Oklab perceptual color space."""
    red, green, blue = (srgb_to_linear(channel / 255) for channel in rgb)

    long_cone = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium_cone = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short_cone = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue

    long_root = long_cone ** (1 / 3)
    medium_root = medium_cone ** (1 / 3)
    short_root = short_cone ** (1 / 3)

    lightness = 0.2104542553 * long_root + 0.7936177850 * medium_root - 0.0040720468 * short_root
    green_red = 1.9779984951 * long_root - 2.4285922050 * medium_root + 0.4505937099 * short_root
    blue_yellow = 0.0259040371 * long_root + 0.7827717662 * medium_root - 0.8086757660 * short_root
    return (lightness, green_red, blue_yellow)


# Whitish colors a bulb's white LEDs can show: chromaticity within this distance
# (CIE 1960 uv) of the blackbody curve, between these color temperatures, and
# muted enough (Oklab chroma) that they read as a white rather than a pastel.
WHITE_LIGHT_MAX_DUV = 0.012
WHITE_LIGHT_CCT_RANGE = (2400.0, 8000.0)
WHITE_LIGHT_MAX_CHROMA = 0.08
# Below this Oklab chroma a color has no real hue (grey, black, off-white).
NEUTRAL_CHROMA = 0.035
# Bulb white range (Govee H6008-class bulbs: 2700-6500 K).
BULB_KELVIN_RANGE = (2700, 6500)


def _planckian_xy(kelvin: float) -> tuple[float, float]:
    """Blackbody chromaticity (Kim et al. cubic spline, 1667-25000 K)."""
    t = max(1667.0, min(25000.0, kelvin))
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def _uv1960(x: float, y: float) -> tuple[float, float]:
    denominator = -2 * x + 12 * y + 3
    return 4 * x / denominator, 6 * y / denominator


def white_light_cct(rgb: tuple[int, int, int]) -> float | None:
    """Color temperature of a whitish sRGB color, or None if it is a real color.

    Creams, beiges and cool whites sit on or near the blackbody curve and look
    best from a bulb's white LEDs at the matching temperature; anything further
    off the curve (pinks, greens, saturated oranges) needs the RGB LEDs.
    """
    red, green, blue = (srgb_to_linear(channel / 255) for channel in rgb)
    big_x = 0.4124 * red + 0.3576 * green + 0.1805 * blue
    big_y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    big_z = 0.0193 * red + 0.1192 * green + 0.9505 * blue
    total = big_x + big_y + big_z
    if total <= 1e-6:
        return None
    x, y = big_x / total, big_y / total
    # McCamy's approximation, accurate to a few kelvin over the white range.
    n = (x - 0.3320) / (0.1858 - y)
    cct = 449 * n**3 + 3525 * n**2 + 6823.3 * n + 5520.33
    if not WHITE_LIGHT_CCT_RANGE[0] <= cct <= WHITE_LIGHT_CCT_RANGE[1]:
        return None
    u, v = _uv1960(x, y)
    planck_u, planck_v = _uv1960(*_planckian_xy(cct))
    if math.hypot(u - planck_u, v - planck_v) > WHITE_LIGHT_MAX_DUV:
        return None
    return cct


def bulb_kelvin(cct: float) -> int:
    """Bulb white temperature for an artwork white.

    Screens show white at 6500 K, which reads as cold blue-white from a lamp in
    a room. Temperatures are compressed toward warm so plain white lands near a
    neutral 5000 K, creams go warm and only distinctly cool whites go cool.
    """
    low, high = BULB_KELVIN_RANGE
    return int(round(max(low, min(high, low + (cct - low) * 0.6)) / 50) * 50)


def bulb_white_kelvin(rgb: tuple[int, int, int]) -> int | None:
    """White temperature to show this color with, or None for an RGB color.

    Greys and blacks (no real hue) are neutral white; creams, beiges and cool
    whites get their own temperature.
    """
    _, green_red, blue_yellow = rgb_to_oklab(rgb)
    chroma = math.hypot(green_red, blue_yellow)
    if chroma >= WHITE_LIGHT_MAX_CHROMA:
        return None
    cct = white_light_cct(rgb)
    if cct is None:
        if chroma >= NEUTRAL_CHROMA:
            return None
        cct = 6504.0
    return bulb_kelvin(cct)


def color_luminance(color: str) -> float:
    red, green, blue = hex_to_rgb(color)
    return (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)


def light_safe_palette_color(colors: list[str], preferred_index: int) -> str:
    if not colors:
        return "#f2eee8"

    preferred = colors[preferred_index % len(colors)]
    # Skip only near-black swatches. The floor is low because saturated colors
    # like red are inherently low-luminance and must not be rejected to white.
    for offset in range(len(colors)):
        candidate = colors[(preferred_index + offset) % len(colors)]
        try:
            if color_luminance(candidate) >= 16:
                return candidate
        except ValueError:
            continue

    # Everything is near-black: keep the intended color rather than washing to white.
    return preferred
