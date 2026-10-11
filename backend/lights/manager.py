from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from collections.abc import Callable

from .audio import AudioMeter
from .base import LightAdapter, LightResult, UnsupportedLightAdapter, lerp_hex, light_safe_palette_color
from .effects import (
    EFFECT_BRIGHTNESS,
    PROGRESS_STYLES,
    WAVE_DIRECTIONS,
    choice,
    light_effect,
    progress_frame,
    wave_frame,
)
from .govee import GoveeLanAdapter
from .layout import device_slot, room_layout, slot_place

logger = logging.getLogger("spotify-lights")

DEFAULT_LIGHT_ADAPTER = "govee"

# Step cadence (ms) for the animation loop, per animation_speed setting. This is
# how often each bulb advances to the next palette color. Idle drift runs slower.
ANIMATION_STEP_MS = {"slow": 6000, "medium": 4000, "fast": 2500}
IDLE_STEP_MULTIPLIER = 1.6
# Cap on interpolation sub-steps per fade. Bulbs are controlled locally and
# update in parallel, so fades can afford this many steps.
MAX_FADE_STEPS_LOCAL = 6
# Device commands fan out concurrently; this bounds the worker pool.
MAX_PARALLEL_DEVICE_COMMANDS = 12
# A single adapter failure is usually a transient local network error. Retry
# only the failed bulbs, and cap the burst so a disconnected device cannot
# stall the animator indefinitely.
MAX_DEVICE_COMMAND_ATTEMPTS = 3
DEVICE_RETRY_DELAY_SECONDS = 0.12
# Effects (lights/effects.py) are redrawn this often, but never faster than
# the gap between commands to one bulb (update_speed_ms).
EFFECT_MIN_FRAME_SECONDS = 0.15
# Pulse ignores brightness changes smaller than this (percent), so the bulbs
# are not sent a new level for every flicker of the music.
PULSE_DEADBAND = 3
# An effect resends every frame anyway, so a failing bulb is logged at most
# this often instead of on every frame.
EFFECT_FAILURE_LOG_SECONDS = 30.0

# Only settings that can alter device selection or an animation frame belong in
# the desired-state identity. Other app settings may change between Spotify
# observations and must not restart an otherwise identical light animation.
ANIMATION_SETTING_KEYS = (
    "lights_enabled",
    "devices",
    "default_light_adapter",
    "update_speed_ms",
    "brightness_min",
    "brightness_max",
    "transition_duration_ms",
    "animation_speed",
    "animation_enabled",
    "idle_animation_enabled",
    "light_count",
    "light_stands",
    "light_effect",
    "wave_direction",
    "progress_style",
)


class LightManager:
    def __init__(self) -> None:
        # One instance for both names: it owns the shared UDP socket and the
        # per-bulb send state.
        self.govee = GoveeLanAdapter()
        self.adapters = {
            "govee": self.govee,
            "govee-lan": self.govee,
        }
        self.last_update_at = 0.0
        self.last_track_id: str | None = None
        self.last_summary: dict[str, Any] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_PARALLEL_DEVICE_COMMANDS, thread_name_prefix="light-io"
        )
        # The Pulse effect's ears; only listens while something asks for a level.
        self.audio = AudioMeter()
        self._effect_failures_logged: dict[str, float] = {}

        # Animation engine state. Guarded by _anim_lock; the loop runs on a daemon
        # thread started lazily on the first set_animation/set_idle call.
        self._anim_lock = threading.Lock()
        self._anim_stop = threading.Event()
        self._anim_wake = threading.Event()  # interrupts the loop's sleep on a new palette/mode
        self._anim_thread: threading.Thread | None = None
        self._anim: dict[str, Any] = {
            "mode": "off",          # off | playing | idle
            "palette": [],          # last real (playing) palette
            "settings": {},
            "settings_key": {},
            "track_id": None,
            "sequence": None,
            "phase": 0,
            "generation": 0,
        }
        # Where the playing song was at a known moment, for the Progress
        # effect. Updated on every Spotify read without restarting effects.
        self._progress: dict[str, Any] = {"track_id": None, "progress_ms": 0, "duration_ms": 0, "at": 0.0}

    def _animation_settings_key(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {key: settings.get(key) for key in ANIMATION_SETTING_KEYS}

    def _select_color(
        self,
        device: dict[str, Any],
        colors: list[str],
        phase: int,
        index: int,
        sizes: list[int],
    ) -> str:
        """The color for a bulb's slot; animation rotates colors through the slots."""
        slot = device_slot(device, index, sizes)
        return light_safe_palette_color(colors, (slot + phase) % len(colors))

    def enabled_devices(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        devices = settings.get("devices") or []
        if not isinstance(devices, list):
            return []
        default_adapter = str(settings.get("default_light_adapter") or DEFAULT_LIGHT_ADAPTER)
        normalized = []
        for device in devices:
            if not isinstance(device, dict) or not bool(device.get("enabled", True)):
                continue
            next_device = dict(device)
            next_device["adapter"] = str(next_device.get("adapter") or default_adapter)
            # Adapters space their commands to each bulb by this interval.
            next_device["_update_speed_ms"] = settings.get("update_speed_ms") or 350
            normalized.append(next_device)
        return normalized

    def discover_lights(self, ips: list[str] | None = None) -> dict[str, Any]:
        """Search the local network for Govee bulbs (see GoveeLanAdapter.discover)."""
        return self.govee.discover(ips)

    def identify_light(self, device: dict[str, Any]) -> dict[str, Any]:
        """Blink one bulb so the user can tell which physical bulb it is."""
        return self.govee.identify(device)

    def _fan_out(self, calls: list[Callable[[], LightResult]]) -> list[LightResult]:
        """Run one command per device concurrently, preserving device order.

        All bulbs change together instead of stepping through the room one by
        one; adapters serialize internally where a shared rate limit requires it.
        """
        if len(calls) <= 1:
            return [call() for call in calls]
        futures = [self._executor.submit(call) for call in calls]
        return [future.result() for future in futures]

    def summary(self, settings: dict[str, Any]) -> dict[str, Any]:
        devices = self.enabled_devices(settings)
        if not settings.get("lights_enabled"):
            return {
                "ok": True,
                "enabled": False,
                "connected": False,
                "message": "Lights are disabled.",
                "devices": [],
            }
        if not devices:
            return {
                "ok": True,
                "enabled": True,
                "connected": False,
                "message": "No enabled light devices configured.",
                "devices": [],
            }

        results = self._fan_out(
            [lambda device=device: self._adapter_for(device).status(device) for device in devices]
        )
        return self._summary_from_results(True, results)

    def cached_summary(self, settings: dict[str, Any]) -> dict[str, Any]:
        devices = self.enabled_devices(settings)
        if not settings.get("lights_enabled"):
            return {
                "ok": True,
                "enabled": False,
                "connected": False,
                "message": "Lights are disabled.",
                "devices": [],
            }
        if not devices:
            return {
                "ok": True,
                "enabled": True,
                "connected": False,
                "message": "No enabled light devices configured.",
                "devices": [],
            }
        return self.last_summary or {
            "ok": True,
            "enabled": True,
            "connected": False,
            "message": "Lights ready.",
            "devices": [],
        }

    def apply_palette(
        self,
        settings: dict[str, Any],
        palette: list[str],
        track_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if not settings.get("lights_enabled"):
            return {
                "ok": True,
                "enabled": False,
                "connected": False,
                "message": "Lights are disabled.",
                "devices": [],
            }

        devices = self.enabled_devices(settings)
        if not devices:
            return {
                "ok": True,
                "enabled": True,
                "connected": False,
                "message": "No enabled light devices configured.",
                "devices": [],
            }

        now = time.monotonic()
        update_speed_ms = max(250, int(settings.get("update_speed_ms") or 350))
        if not force and now - self.last_update_at < update_speed_ms / 1000:
            return self.last_summary or {
                "ok": True,
                "enabled": True,
                "connected": False,
                "message": "Light update skipped by rate limit.",
                "devices": [],
            }
        if track_id and not force and track_id == self.last_track_id:
            return self.last_summary or {
                "ok": True,
                "enabled": True,
                "connected": False,
                "message": "Palette already applied for this track.",
                "devices": [],
            }

        brightness_min = max(1, min(100, int(settings.get("brightness_min") or 20)))
        brightness_max = max(brightness_min, min(100, int(settings.get("brightness_max") or 85)))
        transition_ms = int(settings.get("transition_duration_ms") or 3000)
        colors = palette or ["#ffffff"]

        # Same deterministic brightness curve as animation phase 0, so the
        # instant apply and the first animation frame render identically.
        sizes = room_layout(settings)
        calls: list[Callable[[], LightResult]] = []
        for index, device in enumerate(devices):
            color = self._select_color(device, colors, 0, index, sizes)
            brightness = self._breath_brightness(brightness_min, brightness_max, 0, index, idle=False)
            calls.append(
                lambda device=device, color=color, brightness=brightness: self._adapter_for(device).set_color(
                    device, color, brightness, transition_ms
                )
            )
        results = self._fan_out(calls)

        self.last_update_at = now
        if track_id:
            self.last_track_id = track_id
        self.last_summary = self._summary_from_results(True, results, ok=True)
        return self.last_summary

    def turn_off(self, settings: dict[str, Any]) -> dict[str, Any]:
        # Stop the animation loop from driving lights, then send the off command.
        self.stop_lights(settings)
        devices = self.enabled_devices(settings)
        results = self._fan_out(
            [lambda device=device: self._adapter_for(device).turn_off(device) for device in devices]
        )
        self.last_summary = self._summary_from_results(bool(settings.get("lights_enabled")), results, ok=True)
        return self.last_summary

    # --- animation engine ---------------------------------------------------

    def set_animation(
        self,
        settings: dict[str, Any],
        palette: list[str] | None,
        mode: str,
        track_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        """Point the animation loop at the newest desired playback state."""
        try:
            incoming_sequence = int(sequence) if sequence is not None else None
        except (TypeError, ValueError):
            logger.warning("Ignoring light state with invalid sequence %r", sequence)
            return

        next_settings = dict(settings)
        next_settings_key = self._animation_settings_key(next_settings)
        next_track_id = str(track_id) if track_id is not None else None
        cleaned = [
            color for color in (palette or []) if isinstance(color, str) and color
        ]

        with self._anim_lock:
            current_sequence = self._anim["sequence"]
            if (
                incoming_sequence is not None
                and current_sequence is not None
                and incoming_sequence < current_sequence
            ):
                logger.info(
                    "Ignoring stale light state track=%s sequence=%s; latest=%s",
                    next_track_id,
                    incoming_sequence,
                    current_sequence,
                )
                return

            next_palette = cleaned or list(self._anim["palette"])
            same_state = (
                mode == self._anim["mode"]
                and next_palette == self._anim["palette"]
                and next_settings_key == self._anim["settings_key"]
                and next_track_id == self._anim["track_id"]
            )

            # Sequence orders observations; it is not part of the light state.
            # Recording a newer duplicate prevents a late older response from
            # being accepted without needlessly interrupting the current frame.
            if incoming_sequence is not None:
                self._anim["sequence"] = incoming_sequence
            self._anim["settings"] = next_settings
            self._anim["settings_key"] = next_settings_key

            if same_state:
                generation = int(self._anim["generation"])
                duplicate = True
                track_changed = False
            else:
                track_changed = next_track_id != self._anim["track_id"]
                palette_changed = next_palette != self._anim["palette"]
                if track_changed or palette_changed:
                    self._anim["phase"] = 0
                self._anim["palette"] = next_palette
                self._anim["track_id"] = next_track_id
                self._anim["mode"] = mode
                self._anim["generation"] += 1
                generation = int(self._anim["generation"])
                duplicate = False

        if duplicate:
            logger.debug(
                "Light state unchanged track=%s sequence=%s generation=%s",
                next_track_id,
                incoming_sequence,
                generation,
            )
            self._ensure_thread()
            return

        self._ensure_thread()
        logger.info(
            "Queued light state track=%s sequence=%s generation=%s track_changed=%s palette=%s",
            next_track_id,
            incoming_sequence,
            generation,
            track_changed,
            next_palette,
        )
        self._anim_wake.set()

    def set_idle(self, settings: dict[str, Any]) -> None:
        """Switch to idle drift without changing the stored palette."""
        next_settings = dict(settings)
        next_settings_key = self._animation_settings_key(next_settings)
        with self._anim_lock:
            if (
                self._anim["mode"] == "idle"
                and self._anim["settings_key"] == next_settings_key
            ):
                self._anim["settings"] = next_settings
                return
            self._anim["settings"] = next_settings
            self._anim["settings_key"] = next_settings_key
            self._anim["mode"] = "idle"
            self._anim["generation"] += 1
        self._ensure_thread()
        self._anim_wake.set()

    def stop_lights(self, settings: dict[str, Any]) -> None:
        next_settings = dict(settings)
        next_settings_key = self._animation_settings_key(next_settings)
        with self._anim_lock:
            if (
                self._anim["mode"] == "off"
                and self._anim["settings_key"] == next_settings_key
            ):
                self._anim["settings"] = next_settings
                return
            self._anim["settings"] = next_settings
            self._anim["settings_key"] = next_settings_key
            self._anim["mode"] = "off"
            self._anim["generation"] += 1
        self._anim_wake.set()

    def set_progress(
        self,
        track_id: str | None,
        progress_ms: int,
        duration_ms: int,
        read_at: float | None = None,
    ) -> None:
        """Record where the song was at read_at (time.monotonic), for Progress."""
        with self._anim_lock:
            self._progress = {
                "track_id": str(track_id) if track_id is not None else None,
                "progress_ms": max(0, int(progress_ms or 0)),
                "duration_ms": max(0, int(duration_ms or 0)),
                "at": time.monotonic() if read_at is None else float(read_at),
            }

    def song_progress(self, track_id: str | None, now: float | None = None) -> float:
        """How far into the song the playing track is now (0-1), counting on from the last read."""
        with self._anim_lock:
            progress = dict(self._progress)
        if track_id is None or progress["track_id"] != str(track_id) or progress["duration_ms"] <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        played_ms = progress["progress_ms"] + max(0.0, now - progress["at"]) * 1000
        return max(0.0, min(1.0, played_ms / progress["duration_ms"]))

    def pulse_reading(self) -> dict[str, Any]:
        """The Pulse effect's live loudness (see audio.AudioMeter.reading)."""
        return self.audio.reading()

    def _ensure_thread(self) -> None:
        if self._anim_thread and self._anim_thread.is_alive():
            return
        with self._anim_lock:
            if self._anim_thread and self._anim_thread.is_alive():
                return
            self._anim_stop.clear()
            self._anim_thread = threading.Thread(target=self._run_animation, name="light-animator", daemon=True)
            self._anim_thread.start()

    def _current_generation(self) -> int:
        with self._anim_lock:
            return int(self._anim["generation"])

    def _run_animation(self) -> None:
        prev_colors: list[str] | None = None
        prev_brightness: list[int] | None = None
        prev_device_ids: list[Any] | None = None
        prev_palette: list[str] | None = None
        prev_track_id: str | None = None
        applied_generation: int | None = None
        # Keep per-device fade progress across outer-loop retries. Without this,
        # one offline bulb makes every healthy bulb receive the same completed
        # target again each time the animator retries the failed generation.
        fade_progress_key: tuple[Any, ...] | None = None
        fade_completed_steps: list[int] = []
        fade_device_results: list[LightResult | None] = []
        # A running effect: what restarts it, when it started, and each
        # bulb's (color, level) at that moment (where a wave sweeps from).
        effect_key: tuple[Any, ...] | None = None
        effect_started = 0.0
        effect_start: list[tuple[str, float]] = []
        while not self._anim_stop.is_set():
            self._anim_wake.clear()
            with self._anim_lock:
                mode = self._anim["mode"]
                palette = list(self._anim["palette"])
                settings = dict(self._anim["settings"])
                track_id = self._anim["track_id"]
                phase = int(self._anim["phase"])
                generation = int(self._anim["generation"])

            devices = self.enabled_devices(settings)
            if not settings.get("lights_enabled") or mode == "off" or not devices:
                prev_colors = None
                prev_brightness = None
                effect_key = None
                prev_palette = None
                prev_track_id = None
                applied_generation = None
                fade_progress_key = None
                fade_completed_steps = []
                fade_device_results = []
                self._anim_wake.wait(timeout=0.4)
                continue

            idle = mode == "idle"
            animation_enabled = bool(settings.get("animation_enabled", True))
            idle_animation = bool(settings.get("idle_animation_enabled", True))

            if idle and not idle_animation:
                # Hold the last applied color while idle.
                self._anim_wake.wait(timeout=0.5)
                continue

            frame_palette = palette if palette else ["#f2eee8"]
            if not frame_palette:
                self._anim_wake.wait(timeout=0.4)
                continue

            device_ids = [device.get("device_id") for device in devices]
            if device_ids != prev_device_ids:
                prev_colors = None
                prev_brightness = None
                applied_generation = None
                fade_progress_key = None
                fade_completed_steps = []
                fade_device_results = []
                prev_device_ids = device_ids

            brightness_min = max(1, min(100, int(settings.get("brightness_min") or 20)))
            brightness_max = max(brightness_min, min(100, int(settings.get("brightness_max") or 85)))
            transition_ms = max(0, int(settings.get("transition_duration_ms") or 0))
            speed = str(settings.get("animation_speed") or "slow").lower()
            step_interval = ANIMATION_STEP_MS.get(speed, ANIMATION_STEP_MS["slow"]) / 1000
            if idle:
                step_interval *= IDLE_STEP_MULTIPLIER

            # Wave, Pulse and Progress redraw the room continuously while a
            # song plays; paused, the room holds its last frame like Still.
            effect = light_effect(settings)
            if not idle and effect != "still":
                now = time.monotonic()
                next_effect_key = (
                    effect,
                    track_id,
                    tuple(frame_palette),
                    tuple(str(device_id) for device_id in device_ids),
                    settings.get("wave_direction"),
                    speed,
                )
                if next_effect_key != effect_key:
                    # A new song, palette or effect: a wave sweeps in from
                    # whatever the bulbs show right now.
                    effect_key = next_effect_key
                    effect_started = now
                    low, high = EFFECT_BRIGHTNESS
                    effect_start = [
                        (
                            prev_colors[index],
                            max(0.0, min(1.0, (prev_brightness[index] - low) / (high - low))),
                        )
                        for index in range(len(devices))
                    ] if prev_colors and prev_brightness and len(prev_colors) == len(devices) else []
                colors, brightness = self._effect_frame(
                    effect,
                    devices,
                    frame_palette,
                    settings,
                    track_id,
                    now - effect_started,
                    now,
                    effect_start,
                    brightness_min,
                    brightness_max,
                    prev_brightness,
                )
                if self._send_effect_frame(devices, colors, brightness, generation, track_id):
                    if applied_generation != generation:
                        logger.info(
                            "Applied %s light effect track=%s generation=%s palette=%s",
                            effect,
                            track_id,
                            generation,
                            frame_palette,
                        )
                    prev_colors = colors
                    prev_brightness = brightness
                    prev_palette = frame_palette
                    prev_track_id = track_id
                    applied_generation = generation
                    fade_progress_key = None
                    fade_completed_steps = []
                    fade_device_results = []
                    self.last_track_id = track_id
                frame_seconds = max(
                    EFFECT_MIN_FRAME_SECONDS, int(settings.get("update_speed_ms") or 350) / 1000
                )
                self._anim_wake.wait(timeout=max(0.02, frame_seconds - (time.monotonic() - now)))
                continue
            effect_key = None

            # Static playing mode holds one color per device. A new accepted
            # generation must still write once even when its colors match the
            # previous generation (for example, two tracks from one album).
            if not animation_enabled and not idle:
                targets = self._frame_colors(devices, frame_palette, 0, settings)
                brightness = [
                    self._breath_brightness(brightness_min, brightness_max, 0, index, idle=False)
                    for index in range(len(devices))
                ]
                if targets != prev_colors or applied_generation != generation:
                    next_progress_key = (
                        generation,
                        tuple(str(device_id) for device_id in device_ids),
                        tuple(prev_colors or []),
                        tuple(targets),
                        tuple(brightness),
                        transition_ms,
                        1,
                    )
                    if fade_progress_key != next_progress_key:
                        fade_progress_key = next_progress_key
                        fade_completed_steps = [0] * len(devices)
                        fade_device_results = [None] * len(devices)
                    completed = self._apply_fade(
                        devices,
                        prev_colors,
                        targets,
                        brightness,
                        transition_ms,
                        generation,
                        track_id=track_id,
                        completed_steps=fade_completed_steps,
                        device_results=fade_device_results,
                    )
                    if completed:
                        with self._anim_lock:
                            still_current = self._anim["generation"] == generation
                    if completed and still_current:
                        prev_colors = targets
                        prev_brightness = brightness
                        prev_palette = frame_palette
                        prev_track_id = track_id
                        applied_generation = generation
                        fade_progress_key = None
                        fade_completed_steps = []
                        fade_device_results = []
                        self.last_track_id = track_id
                        logger.info(
                            "Applied static light state track=%s generation=%s palette=%s",
                            track_id,
                            generation,
                            frame_palette,
                        )
                self._anim_wake.wait(timeout=0.5)
                continue

            targets = self._frame_colors(devices, frame_palette, phase, settings)
            brightness = [
                self._breath_brightness(brightness_min, brightness_max, phase, index, idle)
                for index in range(len(devices))
            ]
            # Snap instantly on a new palette or track identity so the room hits
            # every new song immediately, including songs whose palettes match.
            # Only ongoing rotation within one song fades smoothly.
            palette_changed = frame_palette != prev_palette
            track_changed = track_id != prev_track_id
            new_state = applied_generation != generation
            steps = (
                1
                if prev_colors is None or palette_changed or track_changed
                else self._fade_steps(transition_ms)
            )
            next_progress_key = (
                generation,
                tuple(str(device_id) for device_id in device_ids),
                tuple(prev_colors or []),
                tuple(targets),
                tuple(brightness),
                transition_ms,
                steps,
            )
            if fade_progress_key != next_progress_key:
                fade_progress_key = next_progress_key
                fade_completed_steps = [0] * len(devices)
                fade_device_results = [None] * len(devices)
            start = time.monotonic()
            completed = self._apply_fade(
                devices,
                prev_colors,
                targets,
                brightness,
                transition_ms,
                generation,
                steps,
                track_id=track_id,
                completed_steps=fade_completed_steps,
                device_results=fade_device_results,
            )
            if completed:
                with self._anim_lock:
                    still_current = self._anim["generation"] == generation
                    if still_current:
                        self._anim["phase"] = phase + 1
                if still_current:
                    prev_colors = targets
                    prev_brightness = brightness
                    prev_palette = frame_palette
                    prev_track_id = track_id
                    applied_generation = generation
                    fade_progress_key = None
                    fade_completed_steps = []
                    fade_device_results = []
                    self.last_track_id = track_id
                if still_current and new_state:
                    logger.info(
                        "Applied animated light state track=%s generation=%s palette=%s",
                        track_id,
                        generation,
                        frame_palette,
                    )

            # Sleep until the next step, but wake immediately on a new palette/mode
            # (e.g. a track change) so the lights respond fast instead of waiting
            # out the full step interval.
            remaining = step_interval - (time.monotonic() - start)
            self._anim_wake.wait(timeout=max(0.05, remaining))

    def _frame_colors(
        self,
        devices: list[dict[str, Any]],
        palette: list[str],
        phase: int,
        settings: dict[str, Any],
    ) -> list[str]:
        sizes = room_layout(settings)
        return [
            self._select_color(device, palette, phase, index, sizes)
            for index, device in enumerate(devices)
        ]

    def _effect_frame(
        self,
        effect: str,
        devices: list[dict[str, Any]],
        palette: list[str],
        settings: dict[str, Any],
        track_id: str | None,
        elapsed: float,
        now: float,
        start: list[tuple[str, float]],
        brightness_min: int,
        brightness_max: int,
        last_brightness: list[int] | None,
    ) -> tuple[list[str], list[int]]:
        """One frame of a Wave, Pulse or Progress effect: (colors, brightness) per device.

        Levels span the bulbs' whole range (EFFECT_BRIGHTNESS);
        brightness_min/max is the user's Still range, used only when Pulse
        has nothing to follow.
        """
        sizes = room_layout(settings)
        places = [slot_place(device_slot(device, index, sizes), sizes) for index, device in enumerate(devices)]
        plan_colors = self._frame_colors(devices, palette, 0, settings)
        low, high = EFFECT_BRIGHTNESS
        span = high - low

        if effect == "wave":
            frame = wave_frame(
                places,
                sizes,
                palette,
                elapsed,
                start,
                choice(settings, "wave_direction", WAVE_DIRECTIONS),
                str(settings.get("animation_speed") or "slow").lower(),
            )
        elif effect == "progress":
            frame = progress_frame(
                places,
                sizes,
                plan_colors,
                palette,
                self.song_progress(track_id, now),
                choice(settings, "progress_style", PROGRESS_STYLES),
            )
        else:
            reading = self.audio.reading()
            if reading["available"] and reading["hearing"]:
                frame = [(color, float(reading["level"])) for color in plan_colors]
            else:
                # Nothing to follow (paused on another device, say): look like Still.
                frame = [
                    (color, (self._breath_brightness(brightness_min, brightness_max, 0, index, idle=False) - low) / span)
                    for index, color in enumerate(plan_colors)
                ]

        colors = [color for color, _ in frame]
        brightness = [max(1, int(round(low + span * level))) for _, level in frame]
        if effect == "pulse" and last_brightness and len(last_brightness) == len(brightness):
            brightness = [
                previous if abs(level - previous) < PULSE_DEADBAND else level
                for level, previous in zip(brightness, last_brightness)
            ]
        return colors, brightness

    def _send_effect_frame(
        self,
        devices: list[dict[str, Any]],
        colors: list[str],
        brightness: list[int],
        generation: int,
        track_id: str | None,
    ) -> bool:
        """Send one effect frame; False if a newer light state replaced it meanwhile.

        Every bulb gets the frame each time: adapters skip values a bulb
        already has, and a bulb whose command failed is simply retried with
        the next frame.
        """
        results = self._fan_out(
            [
                lambda device=device, color=color, level=level: self._adapter_for(device).set_color(
                    device, color, level, 0
                )
                for device, color, level in zip(devices, colors, brightness)
            ]
        )
        if self._anim_stop.is_set() or self._current_generation() != generation:
            return False
        now = time.monotonic()
        for device, result in zip(devices, results):
            if result.ok:
                continue
            name = str(device.get("name") or device.get("device_id") or "Unnamed light")
            if now - self._effect_failures_logged.get(name, -math.inf) >= EFFECT_FAILURE_LOG_SECONDS:
                self._effect_failures_logged[name] = now
                logger.warning(
                    "Light command failed track=%s generation=%s device=%s: %s",
                    track_id,
                    generation,
                    name,
                    result.message,
                )
        self.last_update_at = now
        self.last_summary = self._summary_from_results(True, results)
        return True

    def _breath_brightness(self, brightness_min: int, brightness_max: int, phase: int, device_index: int, idle: bool) -> int:
        if brightness_max <= brightness_min:
            return brightness_max
        wave = 0.5 + 0.5 * math.sin(phase * 0.7 + device_index * 0.9)
        span = brightness_max - brightness_min
        if idle:
            # Dimmer, gentler breathing while idle.
            return int(round(brightness_min + span * 0.4 * wave))
        return int(round(brightness_min + span * wave))

    def _fade_steps(self, transition_ms: int) -> int:
        if transition_ms <= 0:
            return 1
        return max(2, min(MAX_FADE_STEPS_LOCAL, transition_ms // 500))

    def _apply_fade(
        self,
        devices: list[dict[str, Any]],
        prev_colors: list[str] | None,
        targets: list[str],
        brightness: list[int],
        transition_ms: int,
        generation: int,
        steps: int = 1,
        track_id: str | None = None,
        completed_steps: list[int] | None = None,
        device_results: list[LightResult | None] | None = None,
    ) -> bool:
        steps = max(1, steps)
        progress = (
            completed_steps
            if completed_steps is not None and len(completed_steps) == len(devices)
            else [0] * len(devices)
        )
        result_state = (
            device_results
            if device_results is not None and len(device_results) == len(devices)
            else [None] * len(devices)
        )
        for step in range(1, steps + 1):
            if self._anim_stop.is_set() or self._current_generation() != generation:
                logger.debug(
                    "Interrupted light fade before step track=%s generation=%s",
                    track_id,
                    generation,
                )
                return False
            fraction = step / steps
            step_colors: list[str] = []
            for index in range(len(devices)):
                if prev_colors and index < len(prev_colors):
                    color = lerp_hex(prev_colors[index], targets[index], fraction)
                else:
                    color = targets[index]
                step_colors.append(color)

            pending = [
                index for index in range(len(devices)) if progress[index] < step
            ]
            if not pending:
                continue
            for attempt in range(1, MAX_DEVICE_COMMAND_ATTEMPTS + 1):
                if self._anim_stop.is_set() or self._current_generation() != generation:
                    logger.debug(
                        "Interrupted light fade during retry track=%s generation=%s",
                        track_id,
                        generation,
                    )
                    return False

                calls: list[Callable[[], LightResult]] = []
                for index in pending:
                    device = devices[index]
                    color = step_colors[index]
                    level = brightness[index]
                    calls.append(
                        lambda device=device, color=color, level=level: self._adapter_for(
                            device
                        ).set_color(device, color, level, transition_ms)
                    )

                retry_results = self._fan_out(calls)
                if self._current_generation() != generation:
                    logger.debug(
                        "Discarding completed stale light commands track=%s generation=%s",
                        track_id,
                        generation,
                    )
                    return False

                for index, result in zip(pending, retry_results):
                    result_state[index] = result
                    if result.ok:
                        progress[index] = step
                pending = [
                    index for index in pending if progress[index] < step
                ]
                if not pending:
                    break

                for index in pending:
                    result = result_state[index]
                    logger.warning(
                        "Light command failed track=%s generation=%s device=%s "
                        "step=%s/%s attempt=%s/%s: %s",
                        track_id,
                        generation,
                        devices[index].get("name") or devices[index].get("device_id"),
                        step,
                        steps,
                        attempt,
                        MAX_DEVICE_COMMAND_ATTEMPTS,
                        result.message if result else "No command result.",
                    )

                if attempt < MAX_DEVICE_COMMAND_ATTEMPTS:
                    self._anim_wake.wait(timeout=DEVICE_RETRY_DELAY_SECONDS)

            results = [
                result
                if result is not None
                else LightResult(
                    str(device.get("name") or device.get("device_id") or "Unnamed light"),
                    False,
                    "No command result.",
                )
                for device, result in zip(devices, result_state)
            ]
            if pending:
                self.last_summary = self._summary_from_results(True, results)
                logger.error(
                    "Light target not applied track=%s generation=%s failed_devices=%s",
                    track_id,
                    generation,
                    [results[index].name for index in pending],
                )
                return False

            if step < steps:
                self._anim_wake.wait(timeout=transition_ms / 1000 / steps)

        results = [
            result
            if result is not None
            else LightResult(
                str(device.get("name") or device.get("device_id") or "Unnamed light"),
                False,
                "No command result.",
            )
            for device, result in zip(devices, result_state)
        ]
        if (
            not results
            or self._anim_stop.is_set()
            or self._current_generation() != generation
        ):
            return False

        self.last_update_at = time.monotonic()
        self.last_summary = self._summary_from_results(True, results)
        return True

    def _adapter_for(self, device: dict[str, Any]) -> LightAdapter:
        adapter_name = str(device.get("adapter") or DEFAULT_LIGHT_ADAPTER).lower()
        return self.adapters.get(adapter_name) or UnsupportedLightAdapter(adapter_name)

    def _summary_from_results(
        self,
        enabled: bool,
        results: list[LightResult],
        ok: bool | None = None,
    ) -> dict[str, Any]:
        connected = any(result.ok for result in results)
        failed = [result for result in results if not result.ok]
        message = "Lights updated." if connected else "No lights responded."
        if failed and connected:
            message = f"{len(failed)} light(s) did not respond."
        if not results:
            message = "No enabled light devices configured."

        return {
            "ok": connected if ok is None else ok,
            "enabled": enabled,
            "connected": connected,
            "message": message,
            "devices": [
                {"name": result.name, "ok": result.ok, "message": result.message}
                for result in results
            ],
        }
