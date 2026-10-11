"""Light effects: how the room moves while a song plays.

still     Each bulb keeps its planned color for the whole song.
wave      The song's colors sweep across the room one after another, left
          to right across the stands or top to bottom down each stand.
pulse     Each bulb keeps its color; brightness follows the music's
          loudness (see audio.py).
progress  The room shows how far into the song it is: stands brighten one
          by one ("fill"), or the room blends from the song's main color to
          its accent ("blend"). Both start over on the next track.

Effects are pure functions of time and place. The light manager evaluates
them a few times a second and sends each bulb what changed. Brightness is
returned as a level from 0 (dimmest) to 1 (full), over EFFECT_BRIGHTNESS.
"""

from __future__ import annotations

from typing import Any

from .base import bulb_white_kelvin, hex_to_rgb, lerp_hex

EFFECTS = ("still", "wave", "pulse", "progress")
WAVE_DIRECTIONS = ("across", "down")
PROGRESS_STYLES = ("fill", "blend")

# Brightness (percent) that effect levels 0 and 1 map to. Effects always use
# the bulbs' whole range: the brightness_min/max setting only shapes Still
# (and paused playback), since a narrow or pinned range, say 100-100, would
# leave Pulse, Wave and Progress fill nothing to vary.
EFFECT_BRIGHTNESS = (0, 100)

# Wave pace per animation_speed: (seconds between sweeps, seconds a sweep
# takes to cross the room). Each color holds the whole room for the rest.
WAVE_TIMING = {"slow": (9.0, 3.0), "medium": (6.0, 2.0), "fast": (3.5, 1.2)}
# Each bulb fades to the arriving color over this share of the sweep time,
# short enough that the edge of the wave stays visible.
WAVE_FADE_SHARE = 0.6
# A single-color song has nothing to sweep, so the wave alternates the color
# at full and at the lowest brightness instead.
WAVE_SINGLE_COLOR_LEVELS = (1.0, 0.0)
# Blend is stepped so a bulb gets a new color about every second, not on
# every frame.
PROGRESS_BLEND_STEPS = 200


def choice(settings: dict[str, Any], key: str, options: tuple[str, ...]) -> str:
    value = str(settings.get(key) or "").strip().lower()
    return value if value in options else options[0]


def light_effect(settings: dict[str, Any]) -> str:
    return choice(settings, "light_effect", EFFECTS)


def colors_by_use(plan: list[str]) -> list[str]:
    """The plan's distinct colors, most bulbs first (ties keep plan order)."""
    counts: dict[str, int] = {}
    for color in plan:
        counts[color] = counts.get(color, 0) + 1
    return sorted(counts, key=lambda color: -counts[color])


def _mix(start: str, end: str, fraction: float) -> str:
    """lerp_hex, landing exactly on the end colors (no resend for rounding)."""
    if fraction <= 0:
        return start
    if fraction >= 1:
        return end
    return lerp_hex(start, end, fraction)


def _is_white(color: str) -> bool:
    try:
        return bulb_white_kelvin(hex_to_rgb(color)) is not None
    except ValueError:
        return False


def main_and_accent(plan: list[str]) -> tuple[str, str | None]:
    """The song's main color and its accent: the next real (non-white) color."""
    ordered = colors_by_use(plan) or ["#f2eee8"]
    others = ordered[1:]
    accent = next((color for color in others if not _is_white(color)), others[0] if others else None)
    return ordered[0], accent


# --- wave ---------------------------------------------------------------------


def wave_axis(direction: str, sizes: list[int]) -> str:
    """The direction a wave really travels: the requested one when the room has it."""
    across = len(sizes) > 1
    down = bool(sizes) and max(sizes) > 1
    if direction == "down":
        return "down" if down or not across else "across"
    return "across" if across or not down else "down"


def wave_offset(stand: int, position: int, sizes: list[int], axis: str) -> float:
    """How far along the wave's path a bulb sits, 0 (first reached) to 1 (last)."""
    if axis == "across":
        return stand / (len(sizes) - 1) if len(sizes) > 1 else 0.0
    size = sizes[stand] if 0 <= stand < len(sizes) else 1
    # A lone bulb on its stand is reached halfway down.
    return position / (size - 1) if size > 1 else 0.5


def wave_sequence(plan: list[str]) -> list[tuple[str, float]]:
    """(color, level) steps the wave carries through the room, main color first."""
    colors = colors_by_use(plan) or ["#f2eee8"]
    if len(colors) == 1:
        return [(colors[0], level) for level in WAVE_SINGLE_COLOR_LEVELS]
    return [(color, 1.0) for color in colors]


def wave_step(offset: float, elapsed: float, steps: int, speed: str) -> tuple[int, int, float]:
    """(from step, to step, fade fraction) for a bulb at this offset.

    Step -1 is whatever the bulb showed when the wave started: before the
    first sweep reaches a bulb it keeps that, so a new song's colors visibly
    travel in.
    """
    hold, sweep = WAVE_TIMING.get(speed, WAVE_TIMING["slow"])
    fade = sweep * WAVE_FADE_SHARE
    local = elapsed - offset * sweep
    if local < 0 or steps <= 0:
        return -1, -1, 1.0
    sweeps = int(local // hold)
    fraction = min(1.0, (local - sweeps * hold) / fade) if fade > 0 else 1.0
    previous = (sweeps - 1) % steps if sweeps > 0 else -1
    return previous, sweeps % steps, fraction


def wave_frame(
    places: list[tuple[int, int]],
    sizes: list[int],
    plan: list[str],
    elapsed: float,
    start: list[tuple[str, float]],
    direction: str,
    speed: str,
) -> list[tuple[str, float]]:
    """(color, level) per bulb. start is each bulb's (color, level) when the wave began."""
    sequence = wave_sequence(plan)
    axis = wave_axis(direction, sizes)
    frame = []
    for index, (stand, position) in enumerate(places):
        before = start[index] if index < len(start) else sequence[0]
        offset = wave_offset(stand, position, sizes, axis)
        source, target, fraction = wave_step(offset, elapsed, len(sequence), speed)
        from_color, from_level = before if source < 0 else sequence[source]
        to_color, to_level = before if target < 0 else sequence[target]
        frame.append(
            (
                _mix(from_color, to_color, fraction),
                from_level + (to_level - from_level) * fraction,
            )
        )
    return frame


# --- progress -------------------------------------------------------------------


def fill_level(stand: int, position: int, sizes: list[int], progress: float) -> float:
    """How lit a bulb is when the song is this far along (0-1).

    Stands light up one by one, left to right, each over its share of the
    song. With a single stand its bulbs light from the bottom up instead.
    """
    if len(sizes) > 1:
        units, unit = len(sizes), stand
    else:
        size = sizes[0] if sizes else 1
        units, unit = size, size - 1 - position
    return max(0.0, min(1.0, progress * units - unit))


def progress_frame(
    places: list[tuple[int, int]],
    sizes: list[int],
    plan_colors: list[str],
    plan: list[str],
    progress: float,
    style: str,
) -> list[tuple[str, float]]:
    """(color, level) per bulb. plan_colors is each bulb's own planned color."""
    progress = max(0.0, min(1.0, progress))
    main, accent = main_and_accent(plan)
    if style == "blend" and accent is not None:
        stepped = round(progress * PROGRESS_BLEND_STEPS) / PROGRESS_BLEND_STEPS
        color = _mix(main, accent, stepped)
        return [(color, 1.0) for _ in places]
    # Fill, and blend for a song with a single color (nothing to blend to).
    return [
        (plan_colors[index] if index < len(plan_colors) else main, fill_level(stand, position, sizes, progress))
        for index, (stand, position) in enumerate(places)
    ]
