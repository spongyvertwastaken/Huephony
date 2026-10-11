"""Where the bulbs sit: a row of stands, each holding one or more bulbs.

The room is described by two numbers, light_count and light_stands, with the
bulbs split as evenly as possible across the stands. Every bulb position is a
"slot", numbered stand by stand from the top: with 6 lights on 3 stands, slot 0
is the top of stand 1, slot 1 its bottom, slot 2 the top of stand 2, and so on.
The color planner fills the slots; the light manager maps each bulb to one.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LIGHTS = 32
# Slots planned before any bulb is configured (for the settings preview).
DEFAULT_LIGHT_COUNT = 3

# "top 2" / "middle 1" / "bottom 3": a position on a numbered stand.
POSITION_NAME_RE = re.compile(r"^\s*(top|middle|bottom)\s*-?\s*(\d+)\s*$", re.IGNORECASE)


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def stand_sizes(count: int, stands: int) -> list[int]:
    """Split count bulbs over stands, earlier stands taking any remainder."""
    count = max(1, min(MAX_LIGHTS, count))
    stands = max(1, min(count, stands))
    base, extra = divmod(count, stands)
    return [base + (1 if index < extra else 0) for index in range(stands)]


def enabled_light_count(settings: dict[str, Any]) -> int:
    devices = settings.get("devices")
    if not isinstance(devices, list):
        return 0
    return sum(1 for device in devices if isinstance(device, dict) and device.get("enabled", True))


def room_layout(settings: dict[str, Any]) -> list[int]:
    """Bulbs per stand. 0 means automatic: one slot per enabled bulb, each on its own stand."""
    count = (
        _positive_int(settings.get("light_count"))
        or enabled_light_count(settings)
        or DEFAULT_LIGHT_COUNT
    )
    stands = _positive_int(settings.get("light_stands")) or count
    return stand_sizes(count, stands)


def slot_positions(sizes: list[int]) -> list[tuple[int, int]]:
    """(stand, position-on-stand) for every slot, in slot order."""
    return [(stand, position) for stand, size in enumerate(sizes) for position in range(size)]


def slot_place(slot: int, sizes: list[int]) -> tuple[int, int]:
    """(stand, position-on-stand) of one slot; 0 is the top of a stand."""
    positions = slot_positions(sizes)
    return positions[slot % len(positions)] if positions else (0, 0)


def device_slot(device: dict[str, Any], index: int, sizes: list[int]) -> int:
    """The slot a bulb shows: its saved spot, else its "top 2"-style name, else list order.

    A saved spot outside the current room (after the room shrank) is ignored.
    """
    total = max(1, sum(sizes))
    saved = device.get("slot")
    if isinstance(saved, int) and not isinstance(saved, bool) and 0 <= saved < total:
        return saved

    match = POSITION_NAME_RE.match(str(device.get("name") or ""))
    if match and sizes:
        stand = (int(match.group(2)) - 1) % len(sizes)
        size = sizes[stand]
        word = match.group(1).lower()
        position = 0 if word == "top" else size - 1 if word == "bottom" else size // 2
        return sum(sizes[:stand]) + position

    return index % total
