"""Extract artwork swatches and build the screen's weighted color palette."""

from __future__ import annotations

import colorsys
import math
from typing import Any

from PIL import Image

from .colors import (
    FALLBACK_PALETTE,
    adjust_color,
    clamp_color,
    color_distance,
    color_luminance_rgb,
    color_saturation_rgb,
    correct_red_pink_rgb,
    displayable_color,
    hex_from_rgb,
    hex_to_rgb_value,
    is_olive_or_green_rgb,
    is_red_rgb,
    red_pink_uncertain_rgb,
)

# --- pixel hue clustering -----------------------------------------------------
# Median-cut quantization averages pixels across hue boundaries, which fabricates
# colors that are not in the artwork (red + white boxes average to pink). These
# helpers instead pool pixels into hue families and rebuild each swatch from that
# family's own pixels, so every swatch is a color that really appears on the cover.

HUE_BIN_COUNT = 36  # 10-degree bins
HUE_MERGE_RADIUS = 1  # bins within +/-10 degrees of a peak fold into its cluster
CHROMATIC_MIN_SATURATION = 0.15
CHROMATIC_MIN_VALUE = 0.10
WHITE_POOL_MIN_VALUE = 0.82
MIN_CLUSTER_FRACTION = 0.002


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _cluster_color(members: list[tuple[float, float, float]]) -> tuple[int, int, int]:
    """Representative color for a hue cluster, from its own pixels only.

    Hue is the saturation-weighted circular mean (vivid pixels anchor it);
    saturation/value come from upper-mid percentiles so the swatch reads as the
    vivid form of the family rather than its washed-out fringe.
    """
    x = 0.0
    y = 0.0
    for hue, saturation, _ in members:
        angle = hue * math.tau
        x += math.cos(angle) * saturation
        y += math.sin(angle) * saturation
    mean_hue = (math.atan2(y, x) / math.tau) % 1.0
    saturation = _percentile(sorted(item[1] for item in members), 0.7)
    value = _percentile(sorted(item[2] for item in members), 0.6)
    red, green, blue = colorsys.hsv_to_rgb(mean_hue, saturation, value)
    return (clamp_color(red * 255), clamp_color(green * 255), clamp_color(blue * 255))


def extract_pixel_swatches(image: Image.Image) -> list[tuple[int, tuple[int, int, int]]]:
    """Cluster cover pixels into hue families plus white/grey/black pools.

    Returns (pixel_count, rgb) swatches sorted by count. Chromatic pixels never
    mix with neutrals or with other hue families, so a 90% white / 10% red cover
    yields a true red swatch and a white swatch -- never an averaged pink.
    """
    small = image.convert("RGB")
    small.thumbnail((128, 128))
    pixels = list(small.getdata())
    if not pixels:
        return []

    bins: list[list[tuple[float, float, float]]] = [[] for _ in range(HUE_BIN_COUNT)]
    # count, red sum, green sum, blue sum
    pools = {"white": [0, 0, 0, 0], "grey": [0, 0, 0, 0], "black": [0, 0, 0, 0]}

    for red, green, blue in pixels:
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if value < CHROMATIC_MIN_VALUE:
            pool = pools["black"]
        elif saturation < CHROMATIC_MIN_SATURATION:
            pool = pools["white"] if value >= WHITE_POOL_MIN_VALUE else pools["grey"]
        else:
            bins[min(HUE_BIN_COUNT - 1, int(hue * HUE_BIN_COUNT))].append((hue, saturation, value))
            continue
        pool[0] += 1
        pool[1] += red
        pool[2] += green
        pool[3] += blue

    min_cluster = max(8, int(len(pixels) * MIN_CLUSTER_FRACTION))
    consumed = [False] * HUE_BIN_COUNT
    swatches: list[tuple[int, tuple[int, int, int]]] = []
    while True:
        peak = max(
            (index for index in range(HUE_BIN_COUNT) if not consumed[index]),
            key=lambda index: len(bins[index]),
            default=None,
        )
        if peak is None or not bins[peak]:
            break
        members: list[tuple[float, float, float]] = []
        for offset in range(-HUE_MERGE_RADIUS, HUE_MERGE_RADIUS + 1):
            index = (peak + offset) % HUE_BIN_COUNT
            if not consumed[index]:
                consumed[index] = True
                members.extend(bins[index])
        if len(members) >= min_cluster:
            swatches.append((len(members), _cluster_color(members)))

    for pool in pools.values():
        if pool[0] >= min_cluster:
            swatches.append(
                (pool[0], (round(pool[1] / pool[0]), round(pool[2] / pool[0]), round(pool[3] / pool[0])))
            )

    swatches.sort(key=lambda item: item[0], reverse=True)
    return swatches


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or fallback).lower()
    return text if text in allowed else fallback


def normalized_color_similarity(colors: list[tuple[int, int, int]]) -> float:
    if len(colors) < 2:
        return 0.0
    similarities = []
    for index, first in enumerate(colors):
        for second in colors[index + 1 :]:
            distance = color_distance(first, second) ** 0.5
            similarities.append(max(0.0, 1.0 - distance / 441.7))
    return sum(similarities) / len(similarities) if similarities else 0.0


def swatch_is_neutral(rgb: tuple[int, int, int]) -> bool:
    saturation = color_saturation_rgb(rgb)
    luminance = color_luminance_rgb(rgb)
    spread = max(rgb) - min(rgb)
    return saturation < 0.18 or spread < 24 or luminance > 224 or luminance < 22


def swatch_is_saturated_accent(rgb: tuple[int, int, int], percentage: float) -> bool:
    return percentage >= 0.003 and color_saturation_rgb(rgb) >= 0.34 and 28 <= color_luminance_rgb(rgb) <= 238


def palette_role(rgb: tuple[int, int, int], percentage: float, neutral: bool) -> str:
    luminance = color_luminance_rgb(rgb)
    saturation = color_saturation_rgb(rgb)
    if luminance < 42:
        return "shadow"
    if neutral and luminance > 185:
        return "highlight" if percentage < 0.18 else "base"
    if saturation >= 0.34:
        return "accent"
    if luminance < 90:
        return "contrast"
    return "base"


def normalize_weighted_palette(entries: list[dict[str, Any]], count: int = 8) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for entry in entries:
        rgb = hex_to_rgb_value(str(entry.get("hex") or ""))
        if not rgb:
            continue
        if any(color_distance(rgb, hex_to_rgb_value(item["hex"]) or rgb) < 700 for item in unique):
            continue
        unique.append(
            {
                "hex": hex_from_rgb(rgb),
                "weight": max(1.0, float(entry.get("weight") or 1.0)),
                "role": str(entry.get("role") or "accent"),
            }
        )
        if len(unique) >= count:
            break

    if not unique:
        unique = [{"hex": color, "weight": 1.0, "role": "accent"} for color in FALLBACK_PALETTE[:count]]

    total = sum(float(entry["weight"]) for entry in unique) or 1.0
    rounded = []
    running = 0
    for index, entry in enumerate(unique):
        if index == len(unique) - 1:
            weight = max(1, 100 - running)
        else:
            weight = max(1, round(float(entry["weight"]) / total * 100))
            running += weight
        rounded.append({"hex": entry["hex"], "weight": weight, "role": entry["role"]})

    # Rounding can overshoot 100; take the excess back from the tail without
    # pushing any entry below 1.
    overflow = sum(entry["weight"] for entry in rounded) - 100
    index = len(rounded) - 1
    while overflow > 0 and index >= 0:
        take = min(overflow, rounded[index]["weight"] - 1)
        rounded[index]["weight"] -= take
        overflow -= take
        index -= 1
    return rounded


# Number of dominant colors the lights rotate through. The palette is already
# ranked by dominance, so the top few are the cover's real colors; more than this
# just pulls in extraction noise and muddies the room.
LIGHT_ROTATION_COLORS = 3

# Combined palette share guaranteed to chromatic accents when a single color (or
# neutrals overall) own >= 60% of the artwork, keyed by accent_boost_strength.
# This is what lifts the small red logo on a 90%-white cover into clear view.
MINORITY_FLOOR_BY_STRENGTH = {"low": 30, "medium": 40, "high": 50}
MINORITY_FLOOR_TRIGGER_RATIO = 0.6


def entry_is_chromatic(entry: dict[str, Any]) -> bool:
    rgb = hex_to_rgb_value(str(entry.get("hex") or ""))
    if not rgb:
        return False
    return color_saturation_rgb(rgb) >= 0.25 and 20 <= color_luminance_rgb(rgb) <= 240


def apply_minority_color_floor(
    entries: list[dict[str, Any]],
    floor: int,
    dominance_ratio: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Guarantee minority accents a visible share when one color owns the cover.

    When the artwork is dominated by a single color or by neutrals overall
    (dominance_ratio >= trigger), chromatic minority entries are rescaled to a
    combined `floor` percent and moved directly behind the leader so they always
    make the light rotation.
    """
    if floor <= 0 or len(entries) < 2 or dominance_ratio < MINORITY_FLOOR_TRIGGER_RATIO:
        return entries, False

    chromatic_ids = {id(entry) for entry in entries[1:] if entry_is_chromatic(entry)}
    if not chromatic_ids:
        return entries, False

    total = sum(float(entry["weight"]) for entry in entries) or 1.0
    chromatic_total = sum(float(entry["weight"]) for entry in entries if id(entry) in chromatic_ids)
    if chromatic_total / total * 100 >= floor:
        return entries, False

    rest_total = total - chromatic_total or 1.0
    chromatic_total = chromatic_total or 1.0
    for entry in entries:
        if id(entry) in chromatic_ids:
            entry["weight"] = float(entry["weight"]) / chromatic_total * floor
        else:
            entry["weight"] = float(entry["weight"]) / rest_total * (100 - floor)
    reordered = [entries[0]] + sorted(entries[1:], key=lambda entry: float(entry["weight"]), reverse=True)
    return reordered, True


def analyze_smart_color_swatches(swatches: list[tuple[int, tuple[int, int, int]]]) -> dict[str, Any]:
    total = max(1, sum(count for count, _ in swatches))
    candidates = []
    neutral_total = 0.0
    saturation_total = 0.0
    saturated_accents = []
    red_uncertain = False

    for count, rgb in swatches:
        percentage = count / total
        saturation = color_saturation_rgb(rgb)
        luminance = color_luminance_rgb(rgb)
        neutral = swatch_is_neutral(rgb)
        saturation_total += saturation * percentage
        if neutral:
            neutral_total += percentage
        if swatch_is_saturated_accent(rgb, percentage):
            saturated_accents.append({"hex": hex_from_rgb(rgb), "percentage": percentage, "saturation": saturation})
        if red_pink_uncertain_rgb(rgb):
            red_uncertain = True
        candidates.append(
            {
                "hex": hex_from_rgb(rgb),
                "percentage": percentage,
                "saturation": saturation,
                "luminance": luminance,
                "neutral": neutral,
            }
        )

    best_accent = max(
        (candidate["percentage"] * candidate["saturation"] for candidate in saturated_accents),
        default=0.0,
    )
    missed_accent_score = min(1.0, best_accent * 18 + max(0.0, neutral_total - 0.45))
    colors = [hex_to_rgb_value(candidate["hex"]) for candidate in candidates[:8]]
    palette_similarity = normalized_color_similarity([color for color in colors if color])
    quality_score = 0.92
    quality_score -= max(0.0, neutral_total - 0.48) * 0.42
    quality_score -= max(0.0, palette_similarity - 0.68) * 0.5
    quality_score -= missed_accent_score * 0.22
    quality_score -= 0.1 if red_uncertain else 0.0
    quality_score -= max(0.0, 0.22 - saturation_total) * 0.55

    return {
        "neutral_ratio": round(neutral_total, 4),
        "average_saturation": round(saturation_total, 4),
        "palette_similarity": round(palette_similarity, 4),
        "saturated_accent_candidates": saturated_accents[:8],
        "missed_accent_score": round(missed_accent_score, 4),
        "red_pink_uncertainty": red_uncertain,
        "quality_score": round(max(0.0, min(1.0, quality_score)), 4),
        "candidate_colors": candidates[:16],
    }


def build_local_smart_palette(
    swatches: list[tuple[int, tuple[int, int, int]]],
    settings: dict[str, Any],
    count: int = 8,
) -> dict[str, Any]:
    analysis = analyze_smart_color_swatches(swatches)
    strength = normalize_choice(settings.get("accent_boost_strength"), {"low", "medium", "high"}, "medium")
    neutral_reduction = normalize_choice(settings.get("neutral_reduction"), {"off", "balanced", "strong"}, "balanced")
    accent_multiplier = {"low": 1.35, "medium": 2.1, "high": 3.1}[strength]
    neutral_multiplier = {"off": 1.0, "balanced": 0.38, "strong": 0.18}[neutral_reduction]
    red_correction = bool(settings.get("red_pink_correction_enabled", True))
    total = max(1, sum(value for value, _ in swatches))

    scored = []
    for swatch_count, rgb in swatches:
        percentage = swatch_count / total
        neutral = swatch_is_neutral(rgb)
        saturation = color_saturation_rgb(rgb)
        luminance = color_luminance_rgb(rgb)
        corrected = correct_red_pink_rgb(rgb) if red_correction else rgb
        score = percentage * (0.55 + saturation)
        if neutral:
            score *= neutral_multiplier
            if percentage > 0.28 and luminance > 120:
                score *= 1.35
        if swatch_is_saturated_accent(rgb, percentage):
            score *= accent_multiplier
        if is_red_rgb(corrected) and saturation >= 0.2:
            score *= 1.45
        if is_olive_or_green_rgb(corrected):
            score *= 1.25
        scored.append((score, percentage, corrected, neutral))

    scored.sort(key=lambda item: item[0], reverse=True)
    entries = []
    for score, percentage, rgb, neutral in scored:
        display_rgb = displayable_color(rgb)
        if any(color_distance(display_rgb, hex_to_rgb_value(entry["hex"]) or display_rgb) < 850 for entry in entries):
            continue
        entries.append(
            {
                "hex": hex_from_rgb(display_rgb),
                "weight": max(1.0, score * 100),
                "role": palette_role(rgb, percentage, neutral),
            }
        )
        if len(entries) >= count:
            break

    if not any(entry["role"] == "accent" for entry in entries):
        accents = [
            (candidate["percentage"], hex_to_rgb_value(candidate["hex"]))
            for candidate in analysis["saturated_accent_candidates"]
        ]
        accents = [(percentage, rgb) for percentage, rgb in accents if rgb]
        if accents:
            _, accent_rgb = max(accents, key=lambda item: item[0] * color_saturation_rgb(item[1]))
            entries.insert(1, {"hex": hex_from_rgb(displayable_color(accent_rgb)), "weight": 25, "role": "accent"})

    floor = MINORITY_FLOOR_BY_STRENGTH[strength]
    dominant_ratio = max((swatch_count / total for swatch_count, _ in swatches), default=0.0)
    dominance = max(dominant_ratio, float(analysis.get("neutral_ratio") or 0.0))
    entries, floor_applied = apply_minority_color_floor(entries, floor, dominance)

    # Hue clustering produces few, clean swatches; pad with variants of the
    # leader so the screen background always has enough distinct layers.
    while 0 < len(entries) < 4:
        source = hex_to_rgb_value(entries[0]["hex"])
        if not source:
            break
        variant = adjust_color(
            source,
            saturation_scale=0.85 if len(entries) % 2 else 1.1,
            value_scale=1.25 if len(entries) % 2 else 0.8,
        )
        if any(color_distance(variant, hex_to_rgb_value(entry["hex"]) or variant) < 700 for entry in entries):
            break
        entries.append({"hex": hex_from_rgb(variant), "weight": 2.0, "role": "base"})

    weighted = normalize_weighted_palette(entries, count=count)
    analysis["palette_similarity"] = normalized_color_similarity(
        [hex_to_rgb_value(entry["hex"]) for entry in weighted if hex_to_rgb_value(entry["hex"])]
    )
    analysis["minority_floor_applied"] = floor_applied
    analysis["minority_floor"] = floor if floor_applied else 0
    return {
        "source": "local",
        "quality_score": analysis["quality_score"],
        "palette": weighted,
        # The artwork's real colors and coverage, for planning the lights.
        "swatches": [
            {"hex": hex_from_rgb(rgb), "share": round(swatch_count / total, 5)}
            for swatch_count, rgb in swatches[:32]
        ],
        "analysis": analysis,
    }
