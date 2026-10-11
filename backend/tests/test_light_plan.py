"""Room layout and per-bulb color planning."""

from __future__ import annotations

from collections import Counter

from lights.planning import plan_light_colors
from palette.colors import color_saturation_rgb, hex_to_rgb_value, is_red_rgb
from lights.base import bulb_white_kelvin
from lights.layout import device_slot, room_layout, stand_sizes

ROOM = {"light_count": 6, "light_stands": 3}


def swatches(*pairs: tuple[float, str]) -> dict:
    return {"swatches": [{"share": share, "hex": color} for share, color in pairs]}


def is_white(color: str) -> bool:
    return bulb_white_kelvin(hex_to_rgb_value(color)) is not None


def stands(plan: list[str], sizes: list[int]) -> list[list[str]]:
    groups, start = [], 0
    for size in sizes:
        groups.append(plan[start : start + size])
        start += size
    return groups


# --- layout -----------------------------------------------------------------


def test_stand_sizes_split_evenly() -> None:
    assert stand_sizes(6, 3) == [2, 2, 2]
    assert stand_sizes(5, 2) == [3, 2]
    assert stand_sizes(3, 9) == [1, 1, 1]


def test_room_layout_defaults_to_one_slot_per_enabled_light() -> None:
    devices = [{"name": "a"}, {"name": "b", "enabled": False}, {"name": "c"}]
    assert room_layout({"devices": devices}) == [1, 1]
    assert room_layout({"devices": devices, "light_count": 6, "light_stands": 3}) == [2, 2, 2]
    assert room_layout({}) == [1, 1, 1]
    assert room_layout({"light_count": "junk", "light_stands": -2, "devices": devices}) == [1, 1]


def test_device_slot_prefers_saved_spot_then_name_then_order() -> None:
    sizes = [2, 2, 2]
    assert device_slot({"name": "top 1", "slot": 5}, 0, sizes) == 5
    assert device_slot({"name": "top 1", "slot": 9}, 0, sizes) == 0
    assert device_slot({"name": "top 2"}, 0, sizes) == 2
    assert device_slot({"name": "Bottom 3"}, 0, sizes) == 5
    assert device_slot({"name": "Desk lamp"}, 4, sizes) == 4
    assert device_slot({"name": "Desk lamp"}, 7, sizes) == 1


# --- planning -----------------------------------------------------------------


def test_white_cover_keeps_white_but_gives_most_bulbs_to_the_accent() -> None:
    plan = plan_light_colors(
        swatches((0.9, "#f5f5f5"), (0.05, "#dc1e1e"), (0.05, "#787878")), ROOM
    )
    assert len(plan) == 6
    whites = [color for color in plan if is_white(color)]
    assert 1 <= len(whites) <= 2
    assert all(is_red_rgb(hex_to_rgb_value(color)) for color in plan if not is_white(color))


def test_small_white_and_grey_areas_get_no_bulb_next_to_real_colors() -> None:
    # Teal cover with a lavender drawing, a little white and grey paper texture.
    plan = plan_light_colors(
        swatches((0.6, "#2a9d96"), (0.2, "#a9a6dc"), (0.04, "#f2f2f2"), (0.1, "#b4b4b4")), ROOM
    )
    assert len(plan) == 6
    assert not any(is_white(color) for color in plan)


def test_colors_share_bulbs_by_coverage_and_small_ones_still_show() -> None:
    plan = plan_light_colors(
        swatches((0.5, "#1e50c8"), (0.3, "#e68c28"), (0.15, "#14aaa0"), (0.05, "#d23c8c")), ROOM
    )
    counts = Counter(plan)
    assert len(counts) == 4, plan
    assert counts.most_common(1)[0][1] == 2


def test_bulbs_on_one_stand_contrast_when_possible() -> None:
    plan = plan_light_colors(
        swatches((0.34, "#1e50c8"), (0.33, "#e68c28"), (0.33, "#14aaa0")), ROOM
    )
    for stand in stands(plan, [2, 2, 2]):
        assert stand[0] != stand[1], plan


def test_black_areas_are_skipped() -> None:
    plan = plan_light_colors(swatches((0.85, "#050505"), (0.15, "#b41e1e")), ROOM)
    assert all(is_red_rgb(hex_to_rgb_value(color)) for color in plan)


def test_shades_of_one_color_count_as_one() -> None:
    plan = plan_light_colors(
        swatches((0.5, "#c81e1e"), (0.3, "#7a1010"), (0.2, "#1e50c8")), ROOM
    )
    assert len(set(plan)) == 2


def test_muted_colors_are_deepened_for_the_bulbs() -> None:
    (color,) = set(plan_light_colors(swatches((1.0, "#985a48")), ROOM))
    assert color_saturation_rgb(hex_to_rgb_value(color)) > color_saturation_rgb((0x98, 0x5A, 0x48))
    assert max(hex_to_rgb_value(color)) == 255


def test_dark_cover_uses_the_color_in_its_shadows() -> None:
    # A dusty maroon snake on black (KSHMR - Karam): nothing passes the usual
    # bars, but the bulbs should still be red, not black or plain white.
    for extra in ((), ((0.03, "#f0f0f0"),)):
        plan = plan_light_colors(swatches((0.75, "#090808"), (0.22, "#341e1e"), *extra), ROOM)
        assert len(plan) == 6
        for color in plan:
            rgb = hex_to_rgb_value(color)
            assert is_red_rgb(rgb) and not is_white(color), plan
            assert max(rgb) == 255 and color_saturation_rgb(rgb) > 0.7, plan


def test_black_cover_without_any_hue_lights_white() -> None:
    # Warm and navy-tinted blacks are still black, not a color.
    plan = plan_light_colors(swatches((0.7, "#090808"), (0.2, "#02050f"), (0.1, "#1c1c1c")), ROOM)
    assert plan == ["#ffffff"] * 6


def test_monochrome_cover_is_all_white() -> None:
    plan = plan_light_colors(swatches((0.6, "#101010"), (0.4, "#e6e6e6")), ROOM)
    assert all(is_white(color) for color in plan)


def test_plan_falls_back_to_weighted_palette_without_swatches() -> None:
    result = {"palette": [{"hex": "#d81717", "weight": 70}, {"hex": "#1e50c8", "weight": 30}]}
    plan = plan_light_colors(result, ROOM)
    assert len(plan) == 6 and len(set(plan)) == 2


def test_plan_size_follows_the_room() -> None:
    result = swatches((0.6, "#1e50c8"), (0.4, "#e68c28"))
    for count, stand_count in ((1, 1), (2, 1), (4, 2), (12, 4), (32, 8)):
        plan = plan_light_colors(result, {"light_count": count, "light_stands": stand_count})
        assert len(plan) == count


def test_large_rooms_spread_colors_across_stands() -> None:
    plan = plan_light_colors(
        swatches((0.5, "#1e50c8"), (0.5, "#e68c28")), {"light_count": 16, "light_stands": 8}
    )
    for stand in stands(plan, [2] * 8):
        assert stand[0] != stand[1], plan
