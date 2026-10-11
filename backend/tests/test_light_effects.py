from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from lights import audio, effects, manager as manager_module
from lights.audio import AudioMeter, LoudnessFollower
from lights.base import LightResult
from lights.effects import (
    colors_by_use,
    fill_level,
    main_and_accent,
    progress_frame,
    wave_axis,
    wave_frame,
    wave_offset,
    wave_step,
)
from lights.manager import LightManager

RED, BLUE, GREEN, WHITE = "#ff2020", "#2040ff", "#20ff40", "#f4f1ea"


# --- effect math ----------------------------------------------------------------


def test_colors_by_use_ranks_by_bulbs_then_plan_order() -> None:
    assert colors_by_use([BLUE, RED, RED, GREEN, BLUE, RED]) == [RED, BLUE, GREEN]
    assert colors_by_use([GREEN, BLUE]) == [GREEN, BLUE]


def test_accent_is_the_next_real_color_not_white() -> None:
    assert main_and_accent([RED, RED, WHITE, WHITE, BLUE]) == (RED, BLUE)
    assert main_and_accent([RED, WHITE]) == (RED, WHITE)
    assert main_and_accent([RED, RED]) == (RED, None)


@pytest.mark.parametrize(
    ("direction", "sizes", "expected"),
    [
        ("across", [2, 2, 2], "across"),
        ("down", [2, 2, 2], "down"),
        ("down", [1, 1, 1], "across"),  # nothing to travel down
        ("across", [3], "down"),  # a single stand
        ("across", [1], "across"),
    ],
)
def test_wave_takes_the_direction_the_room_has(direction: str, sizes: list[int], expected: str) -> None:
    assert wave_axis(direction, sizes) == expected


def test_wave_offsets_run_left_to_right_and_top_to_bottom() -> None:
    sizes = [3, 3, 1]
    assert [wave_offset(stand, 0, sizes, "across") for stand in range(3)] == [0.0, 0.5, 1.0]
    assert [wave_offset(0, position, sizes, "down") for position in range(3)] == [0.0, 0.5, 1.0]
    assert wave_offset(2, 0, sizes, "down") == 0.5


def test_a_wave_reaches_the_first_stand_before_the_last() -> None:
    hold, sweep = effects.WAVE_TIMING["medium"]
    moment = hold + sweep * 0.5  # the second sweep is halfway across the room
    first = wave_step(0.0, moment, 2, "medium")
    last = wave_step(1.0, moment, 2, "medium")
    assert first[1] == 1 and first[2] > 0  # already turning to color 1
    assert last[1] == 0  # still showing color 0


def test_wave_starts_from_what_the_bulbs_showed() -> None:
    places = [(0, 0), (1, 0), (2, 0)]
    start = [(GREEN, 0.5)] * 3
    # Before the first sweep reaches the last stand it keeps its old look.
    frame = wave_frame(places, [1, 1, 1], [RED, BLUE], 0.01, start, "across", "medium")
    assert frame[2] == (GREEN, 0.5)
    hold, sweep = effects.WAVE_TIMING["medium"]
    settled = wave_frame(places, [1, 1, 1], [RED, BLUE], sweep * 2, start, "across", "medium")
    assert settled == [(RED, 1.0)] * 3


def test_single_color_wave_sweeps_brightness_instead() -> None:
    hold, sweep = effects.WAVE_TIMING["slow"]
    places = [(0, 0)]
    bright = wave_frame(places, [1], [RED, RED], sweep * 2, [], "across", "slow")
    dim = wave_frame(places, [1], [RED, RED], hold + sweep * 2, [], "across", "slow")
    assert bright == [(RED, 1.0)]
    assert dim == [(RED, 0.0)]


def test_fill_lights_stands_one_by_one() -> None:
    sizes = [2, 2, 2]
    assert [fill_level(stand, 0, sizes, 0.0) for stand in range(3)] == [0.0, 0.0, 0.0]
    assert [fill_level(stand, 1, sizes, 0.5) for stand in range(3)] == pytest.approx([1.0, 0.5, 0.0])
    assert [fill_level(stand, 0, sizes, 1.0) for stand in range(3)] == [1.0, 1.0, 1.0]


def test_fill_on_one_stand_rises_from_the_bottom() -> None:
    sizes = [3]
    levels = [fill_level(0, position, sizes, 0.5) for position in range(3)]  # top, middle, bottom
    assert levels == pytest.approx([0.0, 0.5, 1.0])


def test_blend_runs_from_main_color_to_accent() -> None:
    places = [(0, 0), (1, 0)]
    plan = [RED, RED, BLUE]
    start = progress_frame(places, [1, 1], [RED, BLUE], plan, 0.0, "blend")
    end = progress_frame(places, [1, 1], [RED, BLUE], plan, 1.0, "blend")
    middle = progress_frame(places, [1, 1], [RED, BLUE], plan, 0.5, "blend")
    assert start == [(RED, 1.0), (RED, 1.0)]
    assert end == [(BLUE, 1.0), (BLUE, 1.0)]
    assert middle[0][0] not in (RED, BLUE) and middle[0] == middle[1]


def test_blend_with_one_color_falls_back_to_fill() -> None:
    frame = progress_frame([(0, 0), (1, 0)], [1, 1], [RED, RED], [RED, RED], 0.5, "blend")
    assert frame == [(RED, 1.0), (RED, 0.0)]


# --- loudness -------------------------------------------------------------------


def feed(follower: LoudnessFollower, seconds: float, db: float | None, beat_db: float = 4.0) -> list[float]:
    """Synthetic music: a section level plus a kick drum twice a second."""
    levels = []
    dt = 1 / 30
    for step in range(int(seconds / dt)):
        if db is None:
            power = 0.0
        else:
            beat = beat_db if (step * dt) % 0.5 < 0.1 else 0.0
            power = 10 ** ((db + beat) / 10)
        follower.update(power, dt)
        levels.append(follower.level)
    return levels


def settled(levels: list[float]) -> float:
    tail = levels[len(levels) // 2 :]
    return sum(tail) / len(tail)


def test_choruses_read_brighter_than_verses_at_any_volume() -> None:
    for volume in (0.0, -15.0):
        follower = LoudnessFollower()
        feed(follower, 20, -20 + volume)  # chorus
        verse = settled(feed(follower, 20, -26 + volume))
        chorus = settled(feed(follower, 20, -20 + volume))
        assert chorus > 0.8
        assert 0.1 < verse < 0.35
        assert follower.hearing


def test_loud_music_reaches_full_brightness_only_on_its_peaks() -> None:
    # A punchy chorus used to sit near the top the whole time.
    follower = LoudnessFollower()
    levels = feed(follower, 30, -26, beat_db=6.0)[450:]
    assert max(levels) > 0.95
    assert min(levels) < 0.75
    assert sum(level > 0.95 for level in levels) / len(levels) < 0.1


def test_a_short_breakdown_dims_without_forgetting_the_chorus() -> None:
    follower = LoudnessFollower()
    feed(follower, 30, -20)
    breakdown = feed(follower, 10, -34)
    chorus = feed(follower, 10, -20)
    assert breakdown[-1] < 0.1
    assert settled(chorus) > 0.8


def test_turning_the_volume_down_is_relearned() -> None:
    follower = LoudnessFollower()
    feed(follower, 40, -20)
    quieter = feed(follower, 40, -32)
    assert min(quieter[:150]) < 0.2  # at first everything seems quiet
    assert settled(quieter) > 0.8  # then the new volume is the loud level


def test_a_brief_gap_in_the_music_is_a_dip_not_a_blackout() -> None:
    # Live music showed single frames plunging to black on short rests.
    follower = LoudnessFollower()
    feed(follower, 20, -20)
    gap = feed(follower, 0.2, None)
    after = feed(follower, 0.3, -20)
    assert min(gap + after) > 0.45


def test_silence_is_not_hearing_and_does_not_reset_the_reference() -> None:
    follower = LoudnessFollower()
    feed(follower, 20, -20)
    reference = follower.reference_db
    feed(follower, 5, None)
    assert not follower.hearing
    assert follower.level == 0.0
    assert follower.reference_db == reference


class FakeSource:
    def __init__(self, power: float | None = 0.01, fail: bool = False, every: int = 1) -> None:
        self.power = power
        self.fail = fail
        self.every = every  # new data on every n-th read only
        self.reads = 0
        self.closed = threading.Event()

    def refresh(self) -> None:
        if self.fail:
            raise OSError("no audio")

    def read(self) -> tuple[float | None, str | None]:
        self.reads += 1
        return (self.power if self.reads % self.every == 0 else None), "spotify"

    def close(self) -> None:
        self.closed.set()


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_meter_listens_only_while_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "IDLE_STOP_SECONDS", 0.3)
    source = FakeSource()
    meter = AudioMeter(lambda: source)

    assert meter.reading()["available"]
    assert wait_until(lambda: meter.reading()["hearing"] and meter.reading()["source"] == "spotify")
    assert meter.reading()["level"] > 0.9  # steady sound is the loud level

    time.sleep(0.2)
    assert not source.closed.is_set()
    assert source.closed.wait(2.0), "the meter must stop once nobody asks for levels"


def test_reads_without_new_sound_are_not_silence_until_sound_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "IDLE_STOP_SECONDS", 0.5)
    monkeypatch.setattr(audio, "SILENT_AFTER_SECONDS", 0.2)
    source = FakeSource(every=3)  # data on every third read, about every 100 ms
    meter = AudioMeter(lambda: source)
    meter.reading()
    assert wait_until(lambda: meter.reading()["hearing"])
    time.sleep(0.5)
    assert meter.reading()["hearing"] and meter.reading()["level"] > 0.9

    source.power = None  # loopback stops sending data when nothing plays
    assert wait_until(lambda: not meter.reading()["hearing"])


def test_meter_reports_when_audio_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "IDLE_STOP_SECONDS", 0.3)
    meter = AudioMeter(lambda: FakeSource(fail=True))
    meter.reading()
    assert wait_until(lambda: not meter.reading()["available"])
    reading = meter.reading()
    assert reading["message"] and not reading["hearing"]


# --- the manager running effects ------------------------------------------------


class RecordingAdapter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []

    def set_color(self, device: dict[str, Any], color: str, brightness: int, transition_ms: int) -> LightResult:
        with self.lock:
            self.calls.append({"device_id": device.get("device_id"), "color": color, "brightness": brightness})
        return LightResult(str(device.get("name")), True, "ok")

    def latest(self) -> dict[str, tuple[str, int]]:
        with self.lock:
            return {call["device_id"]: (call["color"], call["brightness"]) for call in self.calls}

    def history(self, device_id: str) -> list[tuple[str, int]]:
        with self.lock:
            return [(call["color"], call["brightness"]) for call in self.calls if call["device_id"] == device_id]


class FakeMeter:
    def __init__(self) -> None:
        self.level = 0.5
        self.hearing = True

    def reading(self) -> dict[str, Any]:
        return {"available": True, "source": "spotify", "hearing": self.hearing, "level": self.level, "message": ""}


def room_settings(effect: str, **overrides: Any) -> dict[str, Any]:
    return {
        "lights_enabled": True,
        "animation_enabled": False,
        "idle_animation_enabled": False,
        "brightness_min": 10,
        "brightness_max": 90,
        "transition_duration_ms": 0,
        "update_speed_ms": 100,
        "light_count": 3,
        "light_stands": 3,
        "light_effect": effect,
        "animation_speed": "fast",
        "devices": [
            {"name": f"Stand {index + 1}", "device_id": f"bulb-{index}", "adapter": "recording", "slot": index}
            for index in range(3)
        ],
        **overrides,
    }


@pytest.fixture
def lights(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[LightManager, RecordingAdapter]]:
    monkeypatch.setattr(manager_module, "EFFECT_MIN_FRAME_SECONDS", 0.02)
    instance = LightManager()
    adapter = RecordingAdapter()
    instance.adapters["recording"] = adapter
    instance.audio = FakeMeter()  # type: ignore[assignment]
    yield instance, adapter
    instance._anim_stop.set()
    instance._anim_wake.set()
    if instance._anim_thread:
        instance._anim_thread.join(timeout=2)
    instance._executor.shutdown(wait=True, cancel_futures=True)


def test_wave_moves_a_color_across_the_stands(
    lights: tuple[LightManager, RecordingAdapter], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(effects.WAVE_TIMING, "fast", (0.8, 0.4))
    manager, adapter = lights
    manager.set_animation(room_settings("wave"), [RED, BLUE, RED], mode="playing", track_id="song", sequence=1)

    # Red holds the room first; then blue sweeps in from the left.
    assert wait_until(lambda: adapter.latest().get("bulb-0", ("", 0))[0] == BLUE, timeout=3)
    assert adapter.latest()["bulb-2"][0] != BLUE, "the far stand turns blue only after the first"
    assert wait_until(lambda: adapter.latest()["bulb-2"][0] == BLUE, timeout=3)
    assert all(level == 100 for _, level in adapter.latest().values())


def test_pulse_brightness_follows_the_music(lights: tuple[LightManager, RecordingAdapter]) -> None:
    manager, adapter = lights
    meter = manager.audio
    meter.level = 1.0
    manager.set_animation(room_settings("pulse"), [RED, BLUE, GREEN], mode="playing", track_id="song", sequence=1)
    assert wait_until(lambda: adapter.latest().get("bulb-0") == (RED, 100))
    assert adapter.latest()["bulb-1"] == (BLUE, 100)

    meter.level = 0.25
    assert wait_until(lambda: adapter.latest()["bulb-0"] == (RED, 25))

    meter.level = 0.26  # a change below the deadband is not sent
    time.sleep(0.2)
    assert adapter.latest()["bulb-0"] == (RED, 25)

    meter.hearing = False  # nothing to follow: Still's brightness
    still = manager._breath_brightness(10, 90, 0, 0, idle=False)
    assert wait_until(lambda: adapter.latest()["bulb-0"] == (RED, still))


def test_effects_use_the_full_range_whatever_the_brightness_setting(
    lights: tuple[LightManager, RecordingAdapter],
) -> None:
    manager, adapter = lights
    pinned = {"brightness_min": 100, "brightness_max": 100}
    manager.audio.level = 0.25
    manager.set_animation(
        room_settings("pulse", **pinned), [RED, BLUE, GREEN], mode="playing", track_id="song", sequence=1
    )
    assert wait_until(lambda: adapter.latest().get("bulb-0") == (RED, 25))

    # Still keeps the user's setting.
    manager.set_animation(
        room_settings("still", **pinned), [RED, BLUE, GREEN], mode="playing", track_id="song", sequence=2
    )
    assert wait_until(lambda: set(level for _, level in adapter.latest().values()) == {100})


def test_progress_fills_the_room_and_resets_on_the_next_track(
    lights: tuple[LightManager, RecordingAdapter],
) -> None:
    manager, adapter = lights
    settings = room_settings("progress")
    manager.set_progress("song-a", 120_000, 180_000)  # two thirds in
    manager.set_animation(settings, [RED, BLUE, GREEN], mode="playing", track_id="song-a", sequence=1)
    assert wait_until(lambda: len(adapter.latest()) == 3)
    levels = {device: level for device, (_, level) in adapter.latest().items()}
    assert levels["bulb-0"] == 100 and levels["bulb-1"] >= 99 and levels["bulb-2"] <= 3

    manager.set_progress("song-b", 0, 200_000)
    manager.set_animation(settings, [GREEN, RED, BLUE], mode="playing", track_id="song-b", sequence=2)
    assert wait_until(lambda: adapter.latest()["bulb-0"] == (GREEN, 1))
    assert adapter.latest()["bulb-1"] == (RED, 1)


def test_progress_blend_colors_the_whole_room(lights: tuple[LightManager, RecordingAdapter]) -> None:
    manager, adapter = lights
    manager.set_progress("song", 180_000, 180_000)
    manager.set_animation(
        room_settings("progress", progress_style="blend"), [RED, RED, BLUE], mode="playing", track_id="song", sequence=1
    )
    assert wait_until(lambda: set(adapter.latest().values()) == {(BLUE, 100)})


def test_song_progress_counts_on_from_the_last_read(lights: tuple[LightManager, RecordingAdapter]) -> None:
    manager, _ = lights
    now = time.monotonic()
    manager.set_progress("song", 30_000, 100_000, read_at=now - 10)
    assert manager.song_progress("song", now) == pytest.approx(0.4)
    assert manager.song_progress("other-song", now) == 0.0
    assert manager.song_progress("song", now + 1000) == 1.0


def test_switching_back_to_still_restores_the_plan(lights: tuple[LightManager, RecordingAdapter]) -> None:
    manager, adapter = lights
    manager.audio.level = 0.0
    manager.set_animation(room_settings("pulse"), [RED, BLUE, GREEN], mode="playing", track_id="song", sequence=1)
    assert wait_until(lambda: adapter.latest().get("bulb-0") == (RED, 1))

    manager.set_animation(room_settings("still"), [RED, BLUE, GREEN], mode="playing", track_id="song", sequence=2)
    still = manager._breath_brightness(10, 90, 0, 0, idle=False)
    assert wait_until(lambda: adapter.latest()["bulb-0"] == (RED, still))


def test_paused_effects_hold_the_last_frame(lights: tuple[LightManager, RecordingAdapter]) -> None:
    manager, adapter = lights
    settings = room_settings("pulse")
    manager.set_animation(settings, [RED, BLUE, GREEN], mode="playing", track_id="song", sequence=1)
    assert wait_until(lambda: len(adapter.latest()) == 3)

    manager.set_idle(settings)
    time.sleep(0.15)
    count = len(adapter.calls)
    manager.audio.level = 1.0
    time.sleep(0.3)
    assert len(adapter.calls) == count
