"""Tests for artwork colors, palette analysis, and extraction recovery.

These guard the palette pipeline against regressions when thresholds are tuned.
"""

import colorsys
import math
from io import BytesIO

from PIL import Image

from lights.base import rgb_to_oklab
from lights.planning import LIGHT_SAME_COLOR_DISTANCE
from palette import analysis, colors, service


# --- color space helpers ----------------------------------------------------


def test_hex_round_trip():
    for rgb in [(0, 0, 0), (255, 255, 255), (231, 76, 60), (30, 80, 200)]:
        assert colors.hex_to_rgb_value(colors.hex_from_rgb(rgb)) == rgb


def test_hex_to_rgb_value_rejects_bad_input():
    assert colors.hex_to_rgb_value("not-a-color") is None
    assert colors.hex_to_rgb_value("#12345") is None


def test_oklab_identity_distance_is_zero():
    assert math.dist(rgb_to_oklab((120, 200, 90)), rgb_to_oklab((120, 200, 90))) == 0.0


def test_oklab_black_white_far_apart():
    assert math.dist(rgb_to_oklab((0, 0, 0)), rgb_to_oklab((255, 255, 255))) > 0.9


def test_oklab_near_duplicates_are_close():
    # The room planner should treat these slightly different reds alike.
    assert math.dist(rgb_to_oklab((200, 30, 30)), rgb_to_oklab((205, 35, 28))) < LIGHT_SAME_COLOR_DISTANCE


def test_oklab_distinct_hues_stay_apart():
    # Red and blue should stay distinct in the room plan.
    assert math.dist(rgb_to_oklab((220, 30, 30)), rgb_to_oklab((30, 60, 220))) > LIGHT_SAME_COLOR_DISTANCE


# --- displayable_color / red-pink correction --------------------------------


def test_displayable_color_keeps_red_hue_and_saturates():
    out = colors.displayable_color((180, 40, 40))
    hue, sat, _ = colorsys.rgb_to_hsv(out[0] / 255, out[1] / 255, out[2] / 255)
    assert colors.is_red_rgb(out)
    assert sat >= 0.7


def test_displayable_color_leaves_near_black_and_skin_alone():
    # A black area averaging (1, 0, 0) is "fully saturated red" in HSV; it
    # must stay black, not become a dark red.
    assert max(colors.displayable_color((1, 0, 0))) < 10
    # Skin/brown (HSV saturation ~0.53) is not a red to snap.
    skin = colors.displayable_color((0x98, 0x5A, 0x48))
    assert colors.color_saturation_rgb(skin) < 0.6


def test_displayable_color_desaturates_paper_white():
    out = colors.displayable_color((250, 248, 244))
    assert colors.color_saturation_rgb(out) <= 0.2


def test_correct_red_pink_pushes_uncertain_pink_to_red():
    pink = (210, 90, 120)  # high red, blue > green: reads pink-ish
    if colors.red_pink_uncertain_rgb(pink):
        corrected = colors.correct_red_pink_rgb(pink)
        assert colors.is_red_rgb(corrected)


def test_vivid_rose_is_corrected_but_soft_pink_is_not():
    vivid_rose = (220, 30, 95)  # saturation ~0.86: LEDs would render this pink
    assert colors.red_pink_uncertain_rgb(vivid_rose)
    soft_pink = (238, 150, 180)  # saturation ~0.37: a real pink cover color
    assert not colors.red_pink_uncertain_rgb(soft_pink)
    assert colors.correct_red_pink_rgb(soft_pink) == soft_pink


def test_displayable_color_leaves_true_pink_alone():
    out = colors.displayable_color((238, 150, 180))
    assert colors.color_saturation_rgb(out) < 0.5, "soft pink must not be force-saturated to red"


# --- weighted palette normalization -----------------------------------------


def test_normalize_weighted_palette_sums_to_100():
    entries = [
        {"hex": "#1e50c8", "weight": 40, "role": "accent"},
        {"hex": "#e68c28", "weight": 30, "role": "accent"},
        {"hex": "#14aaa0", "weight": 20, "role": "accent"},
    ]
    result = analysis.normalize_weighted_palette(entries, count=6)
    assert sum(entry["weight"] for entry in result) == 100
    assert all(entry["weight"] >= 1 for entry in result)


def test_normalize_weighted_palette_dedups_near_duplicates():
    entries = [
        {"hex": "#c81e1e", "weight": 50, "role": "accent"},
        {"hex": "#cd2320", "weight": 50, "role": "accent"},  # perceptually identical
    ]
    result = analysis.normalize_weighted_palette(entries, count=6)
    assert len(result) == 1


def test_normalize_weighted_palette_empty_uses_fallback():
    result = analysis.normalize_weighted_palette([], count=4)
    assert result
    assert sum(entry["weight"] for entry in result) == 100


def test_normalize_weighted_palette_tiny_tail_weights_still_sum_to_100():
    # A huge leader with several minimum-weight tails used to round to 101.
    entries = [
        {"hex": "#c81414", "weight": 99.0, "role": "accent"},
        {"hex": "#1450c8", "weight": 0.6, "role": "base"},
        {"hex": "#14c850", "weight": 0.6, "role": "base"},
    ]
    result = analysis.normalize_weighted_palette(entries, count=6)
    assert sum(entry["weight"] for entry in result) == 100
    assert all(entry["weight"] >= 1 for entry in result)


# --- build_local_smart_palette: the white + small red case ------------------


def _white_with_red_swatches():
    # 90% white, 5% saturated red, 5% mid grey.
    return [(900, (245, 245, 245)), (50, (220, 30, 30)), (50, (120, 120, 120))]


def test_neutral_dominated_cover_includes_red_accent():
    # A white-dominated cover with a red accent should still surface the red
    # somewhere in the palette (the rotation filter then keeps it dominant).
    result = analysis.build_local_smart_palette(_white_with_red_swatches(), {}, count=6)
    reds = [
        hexv
        for hexv in (entry["hex"] for entry in result["palette"])
        if (rgb := colors.hex_to_rgb_value(hexv)) and colors.is_red_rgb(rgb)
    ]
    assert reds, "expected a red accent in the palette"


def test_palette_weights_sum_to_100():
    result = analysis.build_local_smart_palette(_white_with_red_swatches(), {}, count=6)
    assert sum(entry["weight"] for entry in result["palette"]) == 100


def test_red_swatch_not_rejected_to_white():
    # A medium/dark red must survive the light-safe filter, not fall back to white.
    from lights.base import light_safe_palette_color

    chosen = light_safe_palette_color(["#9e1b1b", "#7a0f0f"], 0)
    assert colors.is_red_rgb(colors.hex_to_rgb_value(chosen))


def test_balanced_cover_keeps_dominant_color_first():
    swatches = [
        (400, (30, 80, 200)),   # blue, dominant
        (300, (230, 140, 40)),  # orange
        (200, (20, 170, 160)),  # teal
        (100, (240, 240, 240)),  # white, minority
    ]
    result = analysis.build_local_smart_palette(swatches, {}, count=6)
    assert result["analysis"]["neutral_ratio"] < 0.5
    leading = colors.hex_to_rgb_value(result["palette"][0]["hex"])
    # Blue channel should dominate the leading color (not force-promoted neutral).
    assert leading[2] > leading[0] and leading[2] > leading[1]


# --- pixel hue clustering ----------------------------------------------------


def _cover(spec, size=(60, 60)):
    """Build a test cover: spec is a list of (fraction, rgb) row bands."""
    image = Image.new("RGB", size)
    pixels = image.load()
    total_rows = size[1]
    row = 0
    for fraction, rgb in spec:
        rows = round(fraction * total_rows)
        for y in range(row, min(total_rows, row + rows)):
            for x in range(size[0]):
                pixels[x, y] = rgb
        row += rows
    return image


def test_pixel_swatches_white_and_red_never_average_to_pink():
    # 90% white + 10% pure red: every chromatic swatch must be a true red,
    # never an averaged pink (the median-cut failure mode).
    image = _cover([(0.9, (250, 250, 250)), (0.1, (200, 20, 20))])
    swatches = analysis.extract_pixel_swatches(image)
    assert swatches, "expected swatches"
    chromatic = [rgb for _, rgb in swatches if colors.color_saturation_rgb(rgb) >= 0.15]
    assert chromatic, "expected the red cluster to survive"
    for rgb in chromatic:
        assert colors.is_red_rgb(rgb)
        assert colors.color_saturation_rgb(rgb) >= 0.7, f"washed-out red (pink) fabricated: {rgb}"


def test_pixel_swatches_counts_reflect_coverage():
    image = _cover([(0.9, (250, 250, 250)), (0.1, (200, 20, 20))])
    swatches = analysis.extract_pixel_swatches(image)
    total = sum(count for count, _ in swatches)
    red_share = sum(count for count, rgb in swatches if colors.is_red_rgb(rgb) and colors.color_saturation_rgb(rgb) > 0.5) / total
    assert 0.05 <= red_share <= 0.2


def test_pixel_swatches_separate_hue_families():
    image = _cover([(0.5, (200, 30, 30)), (0.5, (30, 60, 200))])
    swatches = analysis.extract_pixel_swatches(image)
    hues = sorted(colors.color_hue_rgb(rgb) for _, rgb in swatches)
    assert len(hues) == 2
    # One red-family hue, one blue-family hue; nothing merged in between.
    assert min(hues) <= 0.06 or min(hues) >= 0.9
    assert 0.55 <= max(hues) <= 0.72


# --- minority color floor ----------------------------------------------------


def test_minority_floor_bumps_small_red_on_white_cover():
    # 90% white / 10% red: the red accent must end up clearly visible
    # (combined chromatic share in the 30-50 band by strength).
    result = analysis.build_local_smart_palette(_white_with_red_swatches(), {}, count=6)
    chromatic_weight = sum(
        entry["weight"]
        for entry in result["palette"]
        if analysis.entry_is_chromatic(entry)
    )
    assert chromatic_weight >= 30
    red_positions = [
        index
        for index, entry in enumerate(result["palette"])
        if (rgb := colors.hex_to_rgb_value(entry["hex"])) and colors.is_red_rgb(rgb)
    ]
    # The accent must sit inside the light rotation window.
    assert red_positions and min(red_positions) < analysis.LIGHT_ROTATION_COLORS


def test_minority_floor_strengths_scale():
    weights = {}
    for strength in ("low", "medium", "high"):
        result = analysis.build_local_smart_palette(
            _white_with_red_swatches(), {"accent_boost_strength": strength}, count=6
        )
        weights[strength] = sum(
            entry["weight"] for entry in result["palette"] if analysis.entry_is_chromatic(entry)
        )
    assert weights["low"] <= weights["medium"] <= weights["high"]
    assert weights["low"] >= 30 - 2  # rounding slack


def test_minority_floor_not_applied_to_balanced_covers():
    swatches = [
        (400, (30, 80, 200)),
        (300, (230, 140, 40)),
        (200, (20, 170, 160)),
        (100, (240, 240, 240)),
    ]
    result = analysis.build_local_smart_palette(swatches, {}, count=6)
    assert not result["analysis"]["minority_floor_applied"]


# --- HSV fade -----------------------------------------------------------------


def test_lerp_hex_chromatic_midpoint_stays_saturated():
    from lights.base import lerp_hex

    mid = colors.hex_to_rgb_value(lerp_hex("#c81414", "#1414c8", 0.5))
    assert colors.color_saturation_rgb(mid) >= 0.8, f"fade washed out: {mid}"


def test_lerp_hex_endpoints_exact():
    from lights.base import lerp_hex

    assert lerp_hex("#c81414", "#1414c8", 0.0) == "#c81414"
    assert lerp_hex("#c81414", "#1414c8", 1.0) == "#1414c8"


def test_lerp_hex_neutral_pairs_blend_in_rgb():
    from lights.base import lerp_hex

    mid = colors.hex_to_rgb_value(lerp_hex("#f0f0f0", "#c81414", 0.5))
    # Halfway from white to red should be a soft desaturated red, not a hue spin.
    assert mid[0] > mid[1] == mid[2]


def test_smart_palette_reuses_downloaded_bytes_for_legacy_recovery(monkeypatch):
    cover_url = "https://images.test/legacy-recovery.jpg"
    with service.palette_cache_lock:
        service.palette_cache.clear()

    image_bytes = BytesIO()
    Image.new("RGB", (24, 24), (180, 20, 20)).save(image_bytes, format="PNG")
    fetch_calls = 0

    def fetch_once(_cover_url):
        nonlocal fetch_calls
        fetch_calls += 1
        return image_bytes.getvalue()

    def fail_smart_analysis(_image):
        raise ValueError("force legacy analysis")

    monkeypatch.setattr(service, "_fetch_cover_bytes", fetch_once)
    monkeypatch.setattr(service, "extract_pixel_swatches", fail_smart_analysis)

    result = service.extract_smart_palette(cover_url, {}, track_id="legacy-track")

    assert fetch_calls == 1
    assert result["analysis"]["fallback"] is False
    assert result["analysis"]["legacy_recovery"] is True
    assert result["palette"][0]["hex"] not in service.FALLBACK_PALETTE


def test_smart_palette_fetch_failure_does_not_start_a_third_download(monkeypatch):
    cover_url = "https://images.test/no-third-download.jpg"
    with service.palette_cache_lock:
        service.palette_cache.clear()
    fetch_calls = 0

    def fail_bounded_fetch(_cover_url):
        nonlocal fetch_calls
        fetch_calls += 1
        raise OSError("artwork unavailable")

    def unexpected_legacy_fetch(*args, **kwargs):
        raise AssertionError("legacy extraction tried a separate artwork download")

    monkeypatch.setattr(service, "_fetch_cover_bytes", fail_bounded_fetch)
    monkeypatch.setattr(service, "extract_legacy_palette", unexpected_legacy_fetch)

    result = service.extract_smart_palette(cover_url, {}, track_id="failed-track")

    assert fetch_calls == 1
    assert result["analysis"]["fallback"] is True
    assert result["analysis"]["legacy_recovery"] is False
