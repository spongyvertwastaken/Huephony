"""Assign artwork colors to bulbs and spread them across the room layout."""

from __future__ import annotations

import colorsys
import math
from typing import Any

from palette.analysis import CHROMATIC_MIN_VALUE
from palette.colors import clamp_color, correct_red_pink_rgb, hex_from_rgb, hex_to_rgb_value

from .base import bulb_white_kelvin, rgb_to_oklab
from .layout import room_layout, slot_positions

# --- light plan ---------------------------------------------------------------
# One color per bulb slot (lights/layout.py). Colors share the slots roughly in
# proportion to how much of the artwork they cover, adjusted for how a room of
# lights reads:
# - A bulb cannot show black, so dark neutral areas are skipped; greys,
#   creams and beiges count as white (shown with the bulb's white LEDs).
# - A dark cover whose only color is in its shadows (a maroon snake on black)
#   uses those dark colors rather than going plain white. A cover with no hue
#   at all lights white.
# - Colors that would look the same on a bulb (two shades of one red) count
#   as one.
# - Coverage is compressed, so smaller color areas still claim bulbs. White
#   gets no such lift: next to a real color it needs a large part of the
#   cover to claim a bulb at all, and then takes at most a third of them.
# The slots are then arranged so the bulbs on one stand contrast with each
# other and repeated colors land on different stands.

LIGHT_BLACK_LIGHTNESS = 0.45  # neutral areas darker than this are skipped
LIGHT_GREY_LIGHTNESS = 0.7  # neutral areas darker than this count as half-weight white
LIGHT_DARK_COLOR_CHROMA = 0.045  # very dark areas need this much chroma to count as a color
LIGHT_MIN_COLOR_SHARE = 0.004  # smaller color areas are artwork noise
# A muted color at full brightness reads as a washed-out pastel from a lamp
# (skin -> peach, navy -> periwinkle). Saturation s becomes 1 - (1 - s)^curve,
# which deepens muted colors and leaves vivid ones as they are.
LIGHT_SATURATION_CURVE = 1.5
# Dark colors are used only when a cover has nothing brighter. A dusty maroon
# lifted to full brightness turns coral, so they are deepened harder.
LIGHT_DARK_SATURATION_CURVE = 3.0
LIGHT_DARK_MIN_SATURATION = 0.3  # HSV saturation: a clear tint, not a warm or cool black
LIGHT_DARK_MIN_SHARE = 0.02
LIGHT_SAME_COLOR_DISTANCE = 0.09  # Oklab distance between colors at full brightness
LIGHT_SHARE_EXPONENT = 0.6  # < 1 lifts smaller color areas against the dominant one
LIGHT_WHITE_MAX_FRACTION = 1 / 3
# White (with greys at half weight) must cover this much of the artwork to
# claim a bulb next to real colors; paper texture and highlights do not count.
LIGHT_WHITE_MIN_SHARE = 0.25
LIGHT_MIN_SLOT_FRACTION = 0.4  # a color must earn this much of one bulb to be used
LIGHT_STAND_SPACING = 3.0  # stands are this much further apart than bulbs on a stand
LIGHT_MIRROR_PENALTY = 0.05  # slight preference for mirror-symmetric stands
LIGHT_ARRANGE_NODE_LIMIT = 15000


def light_form(rgb: tuple[int, int, int], curve: float = LIGHT_SATURATION_CURVE) -> tuple[int, int, int]:
    """How an artwork color should look as light: full brightness, muted tones deepened."""
    hue, saturation, _ = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
    saturation = 1 - (1 - saturation) ** curve
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, 1.0)
    return (clamp_color(red * 255), clamp_color(green * 255), clamp_color(blue * 255))


def max_light_colors(slots: int) -> int:
    """Distinct colors for a room: every bulb its own up to 3, then fewer per bulb."""
    return slots if slots <= 3 else min(5, math.ceil(slots * 0.6))


def _light_candidates(boost_result: dict[str, Any]) -> list[tuple[float, tuple[int, int, int]]]:
    swatches = boost_result.get("swatches")
    if isinstance(swatches, list) and swatches:
        pairs = [(entry.get("share"), entry.get("hex")) for entry in swatches if isinstance(entry, dict)]
    else:
        palette = boost_result.get("palette")
        entries = [entry for entry in palette if isinstance(entry, dict)] if isinstance(palette, list) else []
        total = sum(float(entry.get("weight") or 0) for entry in entries) or 1.0
        pairs = [(float(entry.get("weight") or 0) / total, entry.get("hex")) for entry in entries]
    candidates = []
    for share, color in pairs:
        rgb = hex_to_rgb_value(str(color or ""))
        if rgb and isinstance(share, (int, float)) and share > 0:
            candidates.append((float(share), rgb))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def _apportion(weights: list[float], seats: int) -> list[int]:
    """Largest-remainder seats, at least one each (len(weights) <= seats)."""
    total = sum(weights) or 1.0
    quotas = [seats * weight / total for weight in weights]
    seats_won = [max(1, int(quota)) for quota in quotas]
    while sum(seats_won) > seats:
        index = max(
            (index for index in range(len(weights)) if seats_won[index] > 1),
            key=lambda index: seats_won[index] - quotas[index],
        )
        seats_won[index] -= 1
    while sum(seats_won) < seats:
        index = max(range(len(weights)), key=lambda index: quotas[index] - seats_won[index])
        seats_won[index] += 1
    return seats_won


def arrange_light_slots(counts: list[int], sizes: list[int]) -> list[int]:
    """Assign color indexes to slots so equal colors sit far apart.

    Equal colors on two slots cost 1/distance^2, with stands LIGHT_STAND_SPACING
    apart, so a stand showing one color twice costs far more than the same color
    on neighbouring stands. A greedy layout improved by pairwise swaps is the
    starting point; branch and bound then searches for anything cheaper
    (exhaustively for a normal room, node-limited for very large ones).
    """
    positions = slot_positions(sizes)
    count = len(positions)
    penalty = [[0.0] * count for _ in range(count)]
    for first, (stand_a, position_a) in enumerate(positions):
        for second, (stand_b, position_b) in enumerate(positions):
            if first != second:
                dx = (stand_a - stand_b) * LIGHT_STAND_SPACING
                dy = position_a - position_b
                penalty[first][second] = 1.0 / (dx * dx + dy * dy)
    slot_of = {position: slot for slot, position in enumerate(positions)}
    stands = len(sizes)
    mirror = [
        slot_of.get((stands - 1 - stand, position), -1)
        if sizes[stands - 1 - stand] == sizes[stand]
        else -1
        for stand, position in positions
    ]

    def slot_cost(slot: int, color: int, layout: list[int], skip: int = -1) -> float:
        """Cost slot adds with this color against every other placed slot."""
        cost = sum(
            penalty[slot][other]
            for other in range(count)
            if other != slot and other != skip and layout[other] == color
        )
        partner = mirror[slot]
        if 0 <= partner != slot and layout[partner] not in (-1, color):
            cost += LIGHT_MIRROR_PENALTY
        return cost

    def total_cost(layout: list[int]) -> float:
        return sum(slot_cost(slot, layout[slot], layout) for slot in range(count)) / 2

    greedy = [-1] * count
    left = list(counts)
    for slot in range(count):
        color = min(
            (color for color in range(len(left)) if left[color]),
            key=lambda color: slot_cost(slot, color, greedy),
        )
        greedy[slot] = color
        left[color] -= 1
    improved = True
    while improved:
        improved = False
        for first in range(count):
            for second in range(first + 1, count):
                color_a, color_b = greedy[first], greedy[second]
                if color_a == color_b:
                    continue
                before = slot_cost(first, color_a, greedy, second) + slot_cost(second, color_b, greedy, first)
                greedy[first], greedy[second] = color_b, color_a
                after = slot_cost(first, color_b, greedy, second) + slot_cost(second, color_a, greedy, first)
                if after < before - 1e-9:
                    improved = True
                else:
                    greedy[first], greedy[second] = color_a, color_b

    remaining = list(counts)
    assignment = [-1] * count
    best_cost = total_cost(greedy) + 1e-9
    best: list[int] = greedy
    nodes = 0

    def place(slot: int, cost: float) -> None:
        nonlocal best_cost, best, nodes
        if cost >= best_cost:
            return
        if slot == count:
            best_cost, best = cost, list(assignment)
            return
        nodes += 1
        if nodes > LIGHT_ARRANGE_NODE_LIMIT:
            return
        for color, left in enumerate(remaining):
            if not left:
                continue
            added = sum(penalty[slot][other] for other in range(slot) if assignment[other] == color)
            partner = mirror[slot]
            if 0 <= partner < slot and assignment[partner] != color:
                added += LIGHT_MIRROR_PENALTY
            assignment[slot] = color
            remaining[color] -= 1
            place(slot + 1, cost + added)
            remaining[color] += 1
            assignment[slot] = -1

    place(0, 0.0)
    return best


def plan_light_colors(boost_result: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    """One hex color per light slot for this artwork (see the notes above)."""
    candidates = _light_candidates(boost_result)
    if not candidates:
        return []
    sizes = room_layout(settings)
    slots = sum(sizes)
    red_correction = bool(settings.get("red_pink_correction_enabled", True))

    white_share = 0.0
    white_rgb: tuple[int, int, int] | None = None
    white_score = 0.0
    colors: list[dict[str, Any]] = []  # {"rgb", "share", "chroma", "lab"} merged by look
    dark: list[tuple[float, tuple[int, int, int], float]] = []  # skipped as too dark

    def add_color(share: float, rgb: tuple[int, int, int], chroma: float, curve: float) -> None:
        if red_correction:
            rgb = correct_red_pink_rgb(rgb)
        rgb = light_form(rgb, curve)
        lab = rgb_to_oklab(rgb)
        twin = next(
            (
                color
                for color in colors
                if math.dist(color["lab"], lab) < LIGHT_SAME_COLOR_DISTANCE
            ),
            None,
        )
        if twin:
            twin["share"] += share
        else:
            colors.append({"rgb": rgb, "share": share, "chroma": chroma, "lab": lab})

    for share, rgb in candidates:
        lightness, green_red, blue_yellow = rgb_to_oklab(rgb)
        chroma = math.hypot(green_red, blue_yellow)
        if bulb_white_kelvin(rgb) is not None:
            if lightness < LIGHT_BLACK_LIGHTNESS:
                dark.append((share, rgb, chroma))
                continue
            weight = share if lightness >= LIGHT_GREY_LIGHTNESS else share * 0.5
            white_share += weight
            # The white shown is the cover's main light neutral, not a beige or grey.
            if weight * lightness > white_score:
                white_rgb, white_score = rgb, weight * lightness
            continue
        if share < LIGHT_MIN_COLOR_SHARE:
            continue
        if lightness < 0.3 and chroma < LIGHT_DARK_COLOR_CHROMA:
            dark.append((share, rgb, chroma))
            continue
        add_color(share, rgb, chroma, LIGHT_SATURATION_CURVE)

    if not colors and white_share < LIGHT_WHITE_MIN_SHARE:
        for share, rgb, chroma in dark:
            _, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
            if (
                share >= LIGHT_DARK_MIN_SHARE
                and saturation >= LIGHT_DARK_MIN_SATURATION
                and value >= CHROMATIC_MIN_VALUE
            ):
                add_color(share, rgb, chroma, LIGHT_DARK_SATURATION_CURVE)

    entries = [
        {
            "hex": hex_from_rgb(color["rgb"]),
            "white": False,
            "weight": color["share"] ** LIGHT_SHARE_EXPONENT
            * (0.75 + 0.25 * min(1.0, color["chroma"] / 0.15)),
        }
        for color in colors
    ]
    if white_rgb is not None and white_share > 0:
        entries.append({"hex": hex_from_rgb(white_rgb), "white": True, "weight": white_share})
    if not entries:
        # No hue anywhere (an all-black cover). A bulb cannot show black.
        return ["#ffffff"] * slots
    entries.sort(key=lambda entry: entry["weight"], reverse=True)

    chromatic = [entry for entry in entries if not entry["white"]]
    white = next((entry for entry in entries if entry["white"]), None)
    if slots == 1:
        return [(chromatic[0] if chromatic else entries[0])["hex"]]

    total = sum(entry["weight"] for entry in entries)
    limit = max_light_colors(slots)
    chosen = [
        entry
        for index, entry in enumerate(chromatic)
        if index == 0 or entry["weight"] / total * slots >= LIGHT_MIN_SLOT_FRACTION
    ]
    use_white = white is not None and (not chosen or white_share >= LIGHT_WHITE_MIN_SHARE)
    chosen = chosen[: limit - 1 if use_white else limit]

    white_slots = 0
    if use_white:
        if chosen:
            selected_total = white["weight"] + sum(entry["weight"] for entry in chosen)
            cap = max(1, int(slots * LIGHT_WHITE_MAX_FRACTION))
            white_slots = max(1, min(cap, round(slots * white["weight"] / selected_total)))
        else:
            white_slots = slots
    color_slots = slots - white_slots
    chosen = chosen[:color_slots]

    final = chosen + ([white] if use_white else [])
    counts = (_apportion([entry["weight"] for entry in chosen], color_slots) if chosen else []) + (
        [white_slots] if use_white else []
    )
    # Most-used color first, so ties in the arrangement favour it.
    order = sorted(range(len(final)), key=lambda index: counts[index], reverse=True)
    final = [final[index] for index in order]
    counts = [counts[index] for index in order]
    return [final[index]["hex"] for index in arrange_light_slots(counts, sizes)]
