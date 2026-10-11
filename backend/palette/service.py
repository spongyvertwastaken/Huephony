"""Fetch artwork, cache extracted palettes, and recover from analysis failures."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from io import BytesIO
from typing import Any

import requests
from PIL import Image

from .analysis import (
    build_local_smart_palette,
    extract_pixel_swatches,
    normalize_choice,
    normalize_weighted_palette,
)
from .colors import (
    FALLBACK_PALETTE,
    adjust_color,
    color_distance,
    color_luminance_rgb,
    color_saturation_rgb,
    displayable_color,
    hex_from_rgb,
    is_olive_or_green_rgb,
    is_paper_or_neutral_rgb,
    is_red_rgb,
)

logger = logging.getLogger("spotify-lights")

palette_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
PALETTE_CACHE_MAX_ENTRIES = 256
palette_cache_lock = threading.Lock()


def extract_legacy_palette(
    cover_url: str,
    count: int = 8,
    *,
    cover_bytes: bytes | None = None,
) -> list[str]:
    """Build the legacy palette, optionally reusing already-fetched artwork."""
    if not cover_url and cover_bytes is None:
        return FALLBACK_PALETTE

    try:
        content = cover_bytes if cover_bytes is not None else _fetch_cover_bytes(cover_url)
        image = Image.open(BytesIO(content)).convert("RGB")
        image.thumbnail((160, 160))
        quantized = image.quantize(colors=24, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        ranked = sorted(quantized.getcolors(160 * 160) or [], reverse=True)

        swatches: list[tuple[int, tuple[int, int, int]]] = []
        for swatch_count, index in ranked:
            offset = index * 3
            rgb = tuple(palette[offset : offset + 3])
            if len(rgb) != 3:
                continue
            rgb_tuple = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            if color_luminance_rgb(rgb_tuple) < 24:
                continue
            swatches.append((int(swatch_count), rgb_tuple))

        if not swatches:
            return FALLBACK_PALETTE

        def dominance_score(item: tuple[int, tuple[int, int, int]]) -> float:
            swatch_count, rgb = item
            saturation = color_saturation_rgb(rgb)
            luminance = color_luminance_rgb(rgb)
            if is_paper_or_neutral_rgb(rgb):
                return swatch_count * 0.03
            if luminance > 238:
                return swatch_count * 0.06
            saturation_weight = 0.42 + saturation
            light_weight = 0.55 + min(luminance, 190) / 255
            hue_weight = 1.0
            if is_red_rgb(rgb) and saturation >= 0.22:
                hue_weight = 2.9
            elif is_olive_or_green_rgb(rgb):
                hue_weight = 1.55
            return swatch_count * saturation_weight * light_weight * hue_weight

        ranked_swatches = sorted(swatches, key=dominance_score, reverse=True)
        dominant = ranked_swatches[0][1]
        accents: list[tuple[int, int, int]] = []

        for _, rgb in ranked_swatches[1:]:
            if color_luminance_rgb(rgb) > 245 or is_paper_or_neutral_rgb(rgb):
                continue
            if color_distance(rgb, dominant) < 520:
                continue
            if all(color_distance(rgb, existing) > 900 for existing in accents):
                accents.append(rgb)
            if len(accents) >= 4:
                break

        colors: list[tuple[int, int, int]] = [
            dominant,
            adjust_color(dominant, saturation_scale=0.82, value_scale=1.28),
            adjust_color(dominant, saturation_scale=1.12, value_scale=0.82),
            *accents,
        ]

        while len(colors) < 6:
            source = colors[len(colors) % len(colors)] if colors else dominant
            if len(colors) % 3 == 1:
                colors.append(adjust_color(source, saturation_scale=0.82, value_scale=1.28))
            elif len(colors) % 3 == 2:
                colors.append(adjust_color(source, saturation_scale=1.12, value_scale=0.82))
            else:
                colors.append(adjust_color(dominant, saturation_scale=0.72, value_scale=1.45))

        hex_colors = [hex_from_rgb(displayable_color(color)) for color in colors]
        return hex_colors[:count]
    except Exception as exc:
        logger.warning("Palette extraction failed: %s", exc)
        return FALLBACK_PALETTE


def smart_palette_cache_key(cover_url: str, track_id: str | None, settings: dict[str, Any]) -> str:
    # Track IDs are deliberately excluded. The artwork pixels plus the color
    # settings fully determine the palette and allow that work to be reused.
    relevant = {
        "cover_url": cover_url,
        "smart_color_boost_enabled": settings.get("smart_color_boost_enabled"),
        "smart_color_boost_mode": settings.get("smart_color_boost_mode"),
        "accent_boost_strength": settings.get("accent_boost_strength"),
        "neutral_reduction": settings.get("neutral_reduction"),
        "red_pink_correction_enabled": settings.get("red_pink_correction_enabled"),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode("utf-8")).hexdigest()


def store_palette_cache(cache_key: str, result: dict[str, Any]) -> None:
    """Insert into the palette cache with bounded LRU eviction."""
    with palette_cache_lock:
        palette_cache[cache_key] = result
        palette_cache.move_to_end(cache_key)
        while len(palette_cache) > PALETTE_CACHE_MAX_ENTRIES:
            palette_cache.popitem(last=False)


def _fetch_cover_bytes(cover_url: str) -> bytes:
    """Fetch cover art, retrying once. Cover CDN hiccups (SSL resets, timeouts)
    are usually transient; one quick retry avoids falling back to an unrelated
    hardcoded palette for what would otherwise be a single flaky request."""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(cover_url, timeout=8)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.4)
    assert last_exc is not None
    raise last_exc


def extract_smart_palette(
    cover_url: str,
    settings: dict[str, Any],
    track_id: str | None = None,
    count: int = 8,
) -> dict[str, Any]:
    mode = normalize_choice(settings.get("smart_color_boost_mode"), {"off", "auto", "always"}, "auto")
    if not settings.get("smart_color_boost_enabled", True) or mode == "off":
        legacy_colors = extract_legacy_palette(cover_url, count=count)
        extraction_failed = not cover_url or legacy_colors == FALLBACK_PALETTE
        legacy_entries = normalize_weighted_palette(
            [{"hex": color, "weight": 1, "role": "accent"} for color in legacy_colors],
            count=count,
        )
        return {
            "source": "off",
            "cache_hit": False,
            "quality_score": 1.0,
            "palette": legacy_entries,
            "analysis": {"disabled": True, "fallback": extraction_failed},
        }

    cache_key = smart_palette_cache_key(cover_url, track_id, settings)
    with palette_cache_lock:
        cached = palette_cache.get(cache_key)
        if cached is not None:
            palette_cache.move_to_end(cache_key)
    if cached:
        cached_result = deepcopy(cached)
        cached_result["cache_hit"] = True
        return cached_result

    cover_bytes: bytes | None = None
    try:
        cover_bytes = _fetch_cover_bytes(cover_url)
        image = Image.open(BytesIO(cover_bytes))
        swatches = extract_pixel_swatches(image)
        if not swatches:
            raise ValueError("No swatches extracted.")
        result = build_local_smart_palette(swatches, settings, count=count)
        result["cache_hit"] = False
        store_palette_cache(cache_key, result)
        return result
    except Exception as exc:
        logger.warning("Smart Color Boost failed; using legacy palette: %s", exc)
        # If the download succeeded but smart analysis failed, reuse those
        # exact bytes for the legacy algorithm. If the download itself failed,
        # _fetch_cover_bytes already made its bounded retry; starting a third,
        # longer request here would block playback monitoring with stale state.
        legacy_colors = (
            extract_legacy_palette(cover_url, count=count, cover_bytes=cover_bytes)
            if cover_bytes is not None
            else list(FALLBACK_PALETTE)
        )
        extraction_failed = not cover_url or legacy_colors == FALLBACK_PALETTE
        legacy_entries = normalize_weighted_palette(
            [{"hex": color, "weight": 1, "role": "accent"} for color in legacy_colors],
            count=count,
        )
        return {
            "source": "local",
            "cache_hit": False,
            "quality_score": 0.5,
            "palette": legacy_entries,
            # A successful legacy pass is still artwork-derived and is safe to
            # apply. Only the hardcoded fallback keeps the old lights and asks
            # the coordinator to retry the cover.
            "analysis": {
                "fallback": extraction_failed,
                "legacy_recovery": not extraction_failed,
            },
        }




def clear_palette_cache() -> None:
    """Forget artwork-derived state when the user disconnects Spotify."""
    with palette_cache_lock:
        palette_cache.clear()
