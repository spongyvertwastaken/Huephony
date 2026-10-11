from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest

from lights.base import LightResult
from lights.manager import LightManager


PALETTE = ["#2457a6", "#28a36a"]


class RecordingAdapter:
    """Thread-safe light adapter with optional scripted command outcomes."""

    def __init__(
        self,
        outcomes: Iterable[bool] = (),
        always_fail_device_ids: Iterable[str] = (),
    ) -> None:
        self._outcomes = deque(outcomes)
        self._always_fail_device_ids = set(always_fail_device_ids)
        self._condition = threading.Condition()
        self.calls: list[dict[str, Any]] = []

    def set_color(
        self,
        device: dict[str, Any],
        color: str,
        brightness: int,
        transition_ms: int,
    ) -> LightResult:
        with self._condition:
            self.calls.append(
                {
                    "device_id": device.get("device_id"),
                    "color": color,
                    "brightness": brightness,
                    "transition_ms": transition_ms,
                }
            )
            device_id = str(device.get("device_id") or "")
            ok = (
                False
                if device_id in self._always_fail_device_ids
                else self._outcomes.popleft() if self._outcomes else True
            )
            self._condition.notify_all()
        return LightResult("Test bulb", ok, "ok" if ok else "temporary failure")

    def wait_for_calls(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.calls) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def call_count(self, device_id: str) -> int:
        with self._condition:
            return sum(call["device_id"] == device_id for call in self.calls)

    def wait_for_device_calls(
        self, device_id: str, count: int, timeout: float = 2.0
    ) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while sum(call["device_id"] == device_id for call in self.calls) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


@pytest.fixture
def settings() -> dict[str, Any]:
    return {
        "lights_enabled": True,
        # Static mode makes every write in these tests a state change or retry,
        # rather than a normal palette-rotation frame.
        "animation_enabled": False,
        "idle_animation_enabled": False,
        "brightness_min": 60,
        "brightness_max": 60,
        "transition_duration_ms": 0,
        "update_speed_ms": 250,
        "devices": [
            {
                "name": "Test bulb",
                "device_id": "test-bulb",
                "adapter": "recording",
                "enabled": True,
            }
        ],
    }


@pytest.fixture
def manager() -> Iterator[LightManager]:
    instance = LightManager()
    yield instance
    instance._anim_stop.set()
    instance._anim_wake.set()
    if instance._anim_thread:
        instance._anim_thread.join(timeout=2)
    instance._executor.shutdown(wait=True, cancel_futures=True)


def install_adapter(manager: LightManager, adapter: RecordingAdapter) -> None:
    manager.adapters["recording"] = adapter


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_track_change_reapplies_even_when_palette_is_identical(
    manager: LightManager, settings: dict[str, Any]
) -> None:
    adapter = RecordingAdapter()
    install_adapter(manager, adapter)

    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-a", sequence=1
    )
    assert adapter.wait_for_calls(1), "the first track was not applied"

    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-b", sequence=2
    )
    assert adapter.wait_for_calls(2), (
        "a new track must be applied even when its extracted palette matches "
        "the previous track"
    )
    assert adapter.calls[0]["color"] == adapter.calls[1]["color"]


def test_identical_animation_state_is_deduplicated(
    manager: LightManager, settings: dict[str, Any]
) -> None:
    adapter = RecordingAdapter()
    install_adapter(manager, adapter)

    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-a", sequence=7
    )
    assert adapter.wait_for_calls(1)
    generation = manager._current_generation()

    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-a", sequence=8
    )

    assert manager._current_generation() == generation
    assert manager._anim["sequence"] == 8
    assert not adapter.wait_for_calls(2, timeout=0.7), (
        "an identical playback snapshot should not wake the animator or write "
        "the same light state again"
    )


def test_older_sequence_cannot_replace_a_newer_track(
    manager: LightManager, settings: dict[str, Any]
) -> None:
    adapter = RecordingAdapter()
    install_adapter(manager, adapter)

    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="new-track", sequence=10
    )
    assert adapter.wait_for_calls(1)
    generation = manager._current_generation()

    manager.set_animation(
        settings,
        ["#7646a8"],
        mode="playing",
        track_id="stale-track",
        sequence=9,
    )

    assert manager._current_generation() == generation
    assert manager._anim["track_id"] == "new-track"
    assert manager._anim["sequence"] == 10
    assert not adapter.wait_for_calls(2, timeout=0.7)


def test_transient_device_failure_is_retried_before_state_is_committed(
    manager: LightManager, settings: dict[str, Any]
) -> None:
    adapter = RecordingAdapter(outcomes=[False, True])
    install_adapter(manager, adapter)

    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-a", sequence=1
    )

    assert adapter.wait_for_calls(2), (
        "a failed device write was treated as applied instead of being retried"
    )
    assert wait_until(
        lambda: bool(manager.last_summary and manager.last_summary["connected"])
    )
    assert not adapter.wait_for_calls(3, timeout=0.7), (
        "the manager should stop retrying once the target is successfully applied"
    )


def test_permanent_partial_failure_only_retries_failed_device_and_allows_new_track(
    manager: LightManager, settings: dict[str, Any]
) -> None:
    adapter = RecordingAdapter(always_fail_device_ids={"failed-bulb"})
    install_adapter(manager, adapter)
    two_bulb_settings = {
        **settings,
        "devices": [
            {
                "name": "Healthy bulb",
                "device_id": "healthy-bulb",
                "adapter": "recording",
                "enabled": True,
            },
            {
                "name": "Failed bulb",
                "device_id": "failed-bulb",
                "adapter": "recording",
                "enabled": True,
            },
        ],
    }

    manager.set_animation(
        two_bulb_settings,
        PALETTE,
        mode="playing",
        track_id="track-a",
        sequence=1,
    )

    assert adapter.wait_for_device_calls("failed-bulb", 6, timeout=3), (
        "the disconnected bulb did not continue retrying"
    )
    assert adapter.call_count("healthy-bulb") == 1, (
        "an already-successful bulb was rewritten while another bulb retried"
    )

    failed_calls_before_track_change = adapter.call_count("failed-bulb")
    next_palette = ["#d92b2b", "#2b4fd9"]
    manager.set_animation(
        two_bulb_settings,
        next_palette,
        mode="playing",
        track_id="track-b",
        sequence=2,
    )

    assert adapter.wait_for_device_calls("healthy-bulb", 2, timeout=2), (
        "persisted retry progress blocked the healthy bulb on a new track"
    )
    assert adapter.wait_for_device_calls(
        "failed-bulb", failed_calls_before_track_change + 6, timeout=3
    )
    assert adapter.call_count("healthy-bulb") == 2, (
        "the new track's successful write was repeated by partial retries"
    )
    healthy_colors = [
        call["color"]
        for call in adapter.calls
        if call["device_id"] == "healthy-bulb"
    ]
    assert healthy_colors == [PALETTE[0], next_palette[0]]


def test_interrupted_generation_does_not_advance_the_applied_target(
    manager: LightManager, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = RecordingAdapter()
    install_adapter(manager, adapter)

    old_apply_fade = manager._apply_fade
    interrupted_fade_entered = threading.Event()
    allow_interrupted_fade_to_continue = threading.Event()
    intercept_next_fade = threading.Event()

    def controlled_apply_fade(*args: Any, **kwargs: Any) -> Any:
        if intercept_next_fade.is_set():
            intercept_next_fade.clear()
            interrupted_fade_entered.set()
            assert allow_interrupted_fade_to_continue.wait(2)
        return old_apply_fade(*args, **kwargs)

    monkeypatch.setattr(manager, "_apply_fade", controlled_apply_fade)

    baseline = ["#123456"]
    manager.set_animation(
        settings, baseline, mode="playing", track_id="baseline", sequence=1
    )
    assert adapter.wait_for_calls(1)

    # Hold track A immediately before its device command. Track B supersedes it
    # with the same target colors, so A's stale generation must not claim that
    # those colors reached the bulb and suppress B's real write.
    intercept_next_fade.set()
    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-a", sequence=2
    )
    assert interrupted_fade_entered.wait(2)
    manager.set_animation(
        settings, PALETTE, mode="playing", track_id="track-b", sequence=3
    )
    allow_interrupted_fade_to_continue.set()

    assert adapter.wait_for_calls(2), (
        "the interrupted track incorrectly advanced the applied target and "
        "caused its replacement to be skipped"
    )
    assert adapter.calls[0]["color"] == baseline[0]
    assert adapter.calls[1]["color"] != adapter.calls[0]["color"]
