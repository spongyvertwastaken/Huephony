"""The playback coordinator: Spotify polling, the published snapshot, and the lights."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from copy import deepcopy
from typing import Any

import requests

from lights import LightManager
from lights.planning import plan_light_colors
from palette.analysis import LIGHT_ROTATION_COLORS
from palette.colors import FALLBACK_PALETTE
from palette.service import extract_smart_palette, smart_palette_cache_key
from settings import load_settings
from spotify_client import (
    NOW_PLAYING_CACHE_TTL_SECONDS,
    auth_ready,
    get_access_token,
    get_spotify_player_state_read,
    player_epoch,
    read_spotify_up_next,
    spotify_player_cache_lock,
)

logger = logging.getLogger("spotify-lights")

PLAYBACK_POLL_PLAYING_SECONDS = 6.0
PLAYBACK_POLL_IDLE_SECONDS = 15.0
# A song's end is predictable from Spotify's progress, so the read after a
# natural track change is timed for just past it instead of waiting out the
# regular poll. If Spotify still reports the old track, a couple of short
# follow-ups run before the regular cadence takes over again.
PLAYBACK_TRACK_END_BUFFER_SECONDS = 0.6
PLAYBACK_TRACK_END_MIN_DELAY_SECONDS = 1.0
PLAYBACK_TRACK_END_MAX_PROBES = 2
PLAYBACK_ARTWORK_RETRY_SECONDS = 2.0
PLAYBACK_RECONCILE_DELAYS_SECONDS = (0.25, 0.5, 1.0, 1.5, 2.5)
PLAYBACK_RECONCILE_TIMEOUT_SECONDS = 45.0

light_manager = LightManager()


def lights_idle_state(settings: dict[str, Any], message: str | None = None) -> dict[str, Any]:
    enabled = bool(settings.get("lights_enabled"))
    devices = settings.get("devices") if isinstance(settings.get("devices"), list) else []
    configured = len([device for device in devices if isinstance(device, dict) and device.get("enabled", True)])
    return {
        "enabled": enabled,
        "connected": False,
        "message": message
        or ("Lights ready." if enabled and configured else "Lights are disabled." if not enabled else "No enabled light devices configured."),
    }


def idle_response(
    message: str,
    authenticated: bool = False,
    mode: str = "Needs Login",
    *,
    settings: dict[str, Any] | None = None,
    sequence: int = 0,
) -> dict[str, Any]:
    """Build an idle API payload without touching Spotify or the lights."""
    resolved_settings = settings or load_settings()
    return {
        "ok": True,
        "authenticated": authenticated,
        "is_playing": False,
        "mode": mode,
        "message": message,
        "track": None,
        "palette": FALLBACK_PALETTE,
        "light_plan": [],
        "smart_color_boost": {
            "source": "off",
            "quality_score": 0,
            "palette": [],
            "analysis": {},
        },
        "lights": lights_idle_state(resolved_settings),
        "sequence": sequence,
        "updated_at": int(time.time()),
    }


class PlaybackCoordinator:
    """Own Spotify polling and publish one authoritative playback snapshot.

    The UI can poll the cached snapshot as frequently as it likes without
    multiplying Spotify, artwork, or physical-light work. Only this daemon
    accepts player reads and every accepted read receives a monotonic sequence.
    """

    def __init__(self) -> None:
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence = 0
        initial_settings = load_settings()
        self._snapshot = idle_response(
            "Playback sync is starting.",
            mode="Starting",
            settings=initial_settings,
        )
        self._last_palette_key: str | None = None
        self._last_animation_key: str | None = None
        self._artwork_retry_track: str | None = None
        self._artwork_retry_count = 0
        self._reconcile_active = False
        self._reconcile_from_track: str | None = None
        self._reconcile_started_at = 0.0
        self._reconcile_attempt = 0
        self._end_probe_track: str | None = None
        self._end_probes = 0
        # The queued next track, prepared while the current one plays.
        self._prefetched_after: str | None = None
        self._up_next: dict[str, str] | None = None

    def start(self) -> None:
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(
                target=self._run,
                name="spotify-playback-coordinator",
                daemon=True,
            )
            self._thread.start()
        logger.info("Playback coordinator started.")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._thread_lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        with self._thread_lock:
            if self._thread is thread and (not thread or not thread.is_alive()):
                self._thread = None
        if thread and thread.is_alive():
            logger.warning("Playback coordinator is still finishing an in-flight request during shutdown.")
        else:
            logger.info("Playback coordinator stopped.")

    def wake(self) -> None:
        """Request a prompt refresh after settings or a playback command."""
        self._wake.set()

    def reconcile_after_control(self, previous_track_id: str | None) -> None:
        """Poll promptly after next/previous until Spotify reports a new ID."""
        with self._state_lock:
            self._reconcile_active = True
            self._reconcile_from_track = previous_track_id
            self._reconcile_started_at = time.monotonic()
            self._reconcile_attempt = 0
        logger.info(
            "Playback command awaiting track reconciliation from %s.",
            previous_track_id or "<none>",
        )
        self._wake.set()

    def cached_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = deepcopy(self._snapshot)
            up_next = self._up_next
        track = snapshot.get("track")
        if isinstance(track, dict) and up_next and up_next.get("after") == track.get("id"):
            # Lets the window load the next cover before the track changes.
            snapshot["up_next_cover_url"] = up_next.get("cover_url")
        return snapshot

    def forget_listening(self) -> None:
        """Drop everything learned from the Spotify account (on disconnect).

        Callers remove the token first, which also invalidates any in-flight
        read, so a late response cannot bring the forgotten track back.
        """
        settings = load_settings()
        with self._state_lock:
            self._sequence += 1
            self._snapshot = idle_response(
                "Connect Spotify to start cover sync.",
                authenticated=False,
                mode="Needs Login",
                settings=settings,
                sequence=self._sequence,
            )
            self._last_palette_key = None
            self._last_animation_key = None
            self._artwork_retry_track = None
            self._artwork_retry_count = 0
            self._reconcile_active = False
            self._end_probe_track = None
            self._end_probes = 0
            self._prefetched_after = None
            self._up_next = None
        try:
            light_manager.set_idle(settings)
        except Exception:
            logger.exception("Failed to put lights into idle mode after disconnecting Spotify.")

    def _maybe_prepare_up_next(self, outcome: dict[str, Any]) -> None:
        """Once per playing track, prepare the queued next track's colors.

        Runs off the polling thread. With the palette cached, the next track's
        colors are committed as soon as Spotify reports it, without the
        artwork download, analysis and revalidation read.
        """
        track_id = outcome.get("track_id")
        if not (outcome.get("authoritative") and outcome.get("is_playing") and track_id):
            return
        with self._state_lock:
            if self._prefetched_after == track_id:
                return
            self._prefetched_after = str(track_id)
        threading.Thread(
            target=self._prepare_up_next,
            args=(str(track_id),),
            name="spotify-up-next",
            daemon=True,
        ).start()

    def _prepare_up_next(self, track_id: str) -> None:
        try:
            access_token = get_access_token()
            item = read_spotify_up_next(access_token) if access_token else None
            if not item:
                return
            _, cover_url = self._item_album_and_cover(item)
            if not cover_url:
                return
            result = extract_smart_palette(
                cover_url,
                load_settings(),
                track_id=self._item_track_identity(item, cover_url),
            )
            if bool((result.get("analysis") or {}).get("fallback")):
                return
            with self._state_lock:
                if self._prefetched_after == track_id:
                    self._up_next = {"after": track_id, "cover_url": cover_url}
            logger.info("Prepared colors for the next queued track after %s.", track_id)
        except Exception as exc:
            logger.info("Could not prepare the next queued track: %s", exc)

    def replan_lights(self, settings: dict[str, Any]) -> None:
        """Bring the lights up to date right after a settings change.

        Re-plans the current track's light colors from the snapshot's artwork
        analysis, so a new room layout reaches the lights and the settings
        preview without waiting for the next Spotify read. A new effect or
        brightness reaches the lights the same way; unchanged light settings
        leave the running animation alone.
        """
        with self._state_lock:
            snapshot = self._snapshot
            boost_result = snapshot.get("smart_color_boost")
            if not snapshot.get("light_plan") or not isinstance(boost_result, dict):
                return
            plan = plan_light_colors(boost_result, settings)
            if plan:
                snapshot["light_plan"] = plan
            track = snapshot.get("track")
            try:
                light_manager.set_animation(
                    settings,
                    snapshot["light_plan"],
                    mode="playing" if snapshot.get("is_playing") else "idle",
                    track_id=str(track.get("id")) if isinstance(track, dict) else None,
                    sequence=snapshot.get("sequence"),
                )
            except Exception:
                logger.exception("Light re-plan after a settings change failed.")

    def current_track_id(self) -> str | None:
        with self._state_lock:
            track = self._snapshot.get("track")
            return str(track.get("id")) if isinstance(track, dict) and track.get("id") else None

    def _has_active_reconcile(self) -> bool:
        with self._state_lock:
            return self._reconcile_active

    def _run(self) -> None:
        delay = 0.0
        while not self._stop.is_set():
            if delay > 0 and self._wake.wait(timeout=delay):
                self._wake.clear()
            else:
                self._wake.clear()
            if self._stop.is_set():
                break
            try:
                reconcile_priority = self._has_active_reconcile()
                outcome = self.refresh_once(
                    force_refresh=reconcile_priority,
                    priority_read=reconcile_priority,
                )
                self._maybe_prepare_up_next(outcome)
            except Exception:
                logger.exception("Playback coordinator refresh failed; preserving the last authoritative snapshot.")
                outcome = {
                    "authoritative": False,
                    "is_playing": bool(self.cached_snapshot().get("is_playing")),
                }
            delay = self._next_delay(outcome)

    def _next_delay(self, outcome: dict[str, Any]) -> float:
        retry_after = max(0.0, float(outcome.get("retry_after") or 0.0))
        with self._state_lock:
            if self._reconcile_active:
                current_id = outcome.get("track_id")
                changed = bool(outcome.get("authoritative")) and current_id != self._reconcile_from_track
                elapsed = time.monotonic() - self._reconcile_started_at
                if changed:
                    logger.info(
                        "Playback reconciliation complete: %s -> %s.",
                        self._reconcile_from_track or "<none>",
                        current_id or "<none>",
                    )
                    self._reconcile_active = False
                elif elapsed >= PLAYBACK_RECONCILE_TIMEOUT_SECONDS:
                    logger.warning(
                        "Playback reconciliation timed out after %.1fs; normal monitoring will continue.",
                        elapsed,
                    )
                    self._reconcile_active = False
                else:
                    if retry_after:
                        return retry_after
                    attempt = self._reconcile_attempt
                    self._reconcile_attempt += 1
                    if attempt < len(PLAYBACK_RECONCILE_DELAYS_SECONDS):
                        return PLAYBACK_RECONCILE_DELAYS_SECONDS[attempt]
                    # The bounded local Spotify budget may postpone additional
                    # confirmation reads. Keep the expectation alive and retry
                    # at the normal playing cadence instead of dropping it.
                    return PLAYBACK_POLL_PLAYING_SECONDS

        if retry_after:
            return retry_after
        if outcome.get("artwork_retry"):
            retry_count = int(outcome.get("artwork_retry_count") or 1)
            return PLAYBACK_ARTWORK_RETRY_SECONDS if retry_count <= 1 else PLAYBACK_POLL_PLAYING_SECONDS
        if not outcome.get("is_playing"):
            return PLAYBACK_POLL_IDLE_SECONDS
        return self._track_end_delay(outcome) or PLAYBACK_POLL_PLAYING_SECONDS

    def _track_end_delay(self, outcome: dict[str, Any]) -> float | None:
        """A shorter delay that lands just after the playing track ends, if due."""
        ends_at = outcome.get("track_ends_at")
        track_id = outcome.get("track_id")
        if not isinstance(ends_at, (int, float)) or not track_id:
            return None
        until_end = ends_at - time.monotonic() + PLAYBACK_TRACK_END_BUFFER_SECONDS
        with self._state_lock:
            if until_end >= PLAYBACK_POLL_PLAYING_SECONDS:
                self._end_probe_track = None
                self._end_probes = 0
                return None
            probes = self._end_probes if self._end_probe_track == track_id else 0
            if probes >= PLAYBACK_TRACK_END_MAX_PROBES:
                return None
            self._end_probe_track = str(track_id)
            self._end_probes = probes + 1
        return max(PLAYBACK_TRACK_END_MIN_DELAY_SECONDS, until_end)


    def refresh_once(
        self,
        *,
        force_refresh: bool = False,
        priority_read: bool = False,
    ) -> dict[str, Any]:
        """Perform one serialized refresh and return scheduling metadata."""
        with self._refresh_lock:
            if not auth_ready():
                return self._commit_idle(
                    "Add Spotify credentials to backend .env.",
                    authenticated=False,
                    mode="Setup Needed",
                )

            try:
                access_token = get_access_token()
            except requests.RequestException as exc:
                logger.warning("Spotify token refresh failed; preserving playback snapshot: %s", exc)
                return {
                    "authoritative": False,
                    "is_playing": bool(self.cached_snapshot().get("is_playing")),
                }
            if not access_token:
                return self._commit_idle(
                    "Connect Spotify to start cover sync.",
                    authenticated=False,
                    mode="Needs Login",
                )

            try:
                read = get_spotify_player_state_read(
                    access_token,
                    force_refresh=force_refresh,
                    priority=priority_read,
                )
            except requests.RequestException as exc:
                logger.warning("Spotify player read failed; preserving playback snapshot: %s", exc)
                return {
                    "authoritative": False,
                    "is_playing": bool(self.cached_snapshot().get("is_playing")),
                }

            if not read.get("authoritative"):
                source = str(read.get("source") or "unknown")
                if source == "spotify-error":
                    logger.warning(
                        "Spotify player read returned %s; preserving authoritative snapshot: %.240s",
                        read.get("status_code"),
                        str(read.get("text") or ""),
                    )
                else:
                    logger.debug("Ignoring non-authoritative Spotify player read from %s.", source)
                return {
                    "authoritative": False,
                    "is_playing": bool(self.cached_snapshot().get("is_playing")),
                    "retry_after": float(read.get("retry_after") or 0.0),
                    "source": source,
                }

            status_code = int(read.get("status_code") or 0)
            read_epoch = int(read.get("epoch") or 0)
            if status_code == 401:
                return self._commit_idle(
                    "Spotify session expired. Connect again.",
                    authenticated=False,
                    mode="Needs Login",
                    read_epoch=read_epoch,
                )
            if status_code == 204:
                return self._commit_idle(
                    "No song is currently playing.",
                    authenticated=True,
                    mode="Idle",
                    read_epoch=read_epoch,
                )
            if status_code != 200:
                logger.warning(
                    "Unexpected authoritative Spotify player status %s; preserving snapshot.",
                    status_code,
                )
                return {
                    "authoritative": False,
                    "is_playing": bool(self.cached_snapshot().get("is_playing")),
                }

            payload = read.get("payload")
            item = payload.get("item") if isinstance(payload, dict) else None
            if not isinstance(item, dict):
                return self._commit_idle(
                    "No playable Spotify track found.",
                    authenticated=True,
                    mode="Idle",
                    read_epoch=read_epoch,
                )
            return self._commit_track(payload, item, read_epoch, access_token)

    def _commit_idle(
        self,
        message: str,
        *,
        authenticated: bool,
        mode: str,
        read_epoch: int | None = None,
    ) -> dict[str, Any]:
        settings = load_settings()
        next_snapshot = idle_response(
            message,
            authenticated=authenticated,
            mode=mode,
            settings=settings,
        )
        with spotify_player_cache_lock:
            if read_epoch is not None and player_epoch() != read_epoch:
                return {
                    "authoritative": False,
                    "is_playing": False,
                    "retry_after": 0.05,
                    "source": "command-invalidated-before-idle-commit",
                }
            with self._state_lock:
                previous_track = self._snapshot.get("track")
                previous_id = (
                    str(previous_track.get("id"))
                    if isinstance(previous_track, dict) and previous_track.get("id")
                    else None
                )
                self._sequence += 1
                sequence = self._sequence
                next_snapshot["sequence"] = sequence
                try:
                    light_manager.set_idle(settings)
                except Exception:
                    logger.exception("Failed to put lights into idle mode at playback sequence %s.", sequence)
                self._snapshot = next_snapshot
                self._last_palette_key = None
                self._last_animation_key = None
                self._artwork_retry_track = None
                self._artwork_retry_count = 0
        if previous_id:
            logger.info("Track transition seq=%s: %s -> <idle>.", sequence, previous_id)
        return {
            "authoritative": True,
            "track_id": None,
            "is_playing": False,
            "artwork_retry": False,
        }

    @staticmethod
    def _animation_settings_key(settings: dict[str, Any]) -> str:
        relevant = {
            key: settings.get(key)
            for key in (
                "lights_enabled",
                "default_light_adapter",
                "brightness_min",
                "brightness_max",
                "update_speed_ms",
                "transition_duration_ms",
                "animation_enabled",
                "animation_speed",
                "idle_animation_enabled",
                "light_count",
                "light_stands",
                "light_effect",
                "wave_direction",
                "progress_style",
                "devices",
            )
        }
        return hashlib.sha256(
            json.dumps(relevant, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _item_album_and_cover(item: dict[str, Any]) -> tuple[dict[str, Any], str]:
        album_value = item.get("album") or item.get("show") or {}
        album = album_value if isinstance(album_value, dict) else {}
        images = album.get("images")
        first_image = images[0] if isinstance(images, list) and images else {}
        cover_url = str(first_image.get("url") or "") if isinstance(first_image, dict) else ""
        return album, cover_url

    @staticmethod
    def _item_track_identity(item: dict[str, Any], cover_url: str) -> str:
        raw_track_id = item.get("id") or item.get("uri")
        return str(
            raw_track_id
            or f"{item.get('name') or 'unknown'}|{item.get('duration_ms') or 0}|{cover_url}"
        )

    def _commit_track(
        self,
        payload: dict[str, Any],
        item: dict[str, Any],
        read_epoch: int,
        access_token: str,
    ) -> dict[str, Any]:
        # When this read's progress was true, for the progress bar and for
        # timing the read after the track ends.
        read_at, read_at_ms = time.monotonic(), int(time.time() * 1000)
        settings = load_settings()
        album, cover_url = self._item_album_and_cover(item)
        track_id = self._item_track_identity(item, cover_url)
        artist_items = item.get("artists") if isinstance(item.get("artists"), list) else []
        artists = ", ".join(
            str(artist.get("name") or "Unknown Artist")
            for artist in artist_items
            if isinstance(artist, dict)
        )
        if not artists and isinstance(album, dict):
            artists = str(album.get("publisher") or "Unknown Artist")
        is_playing = bool(payload.get("is_playing"))
        palette_key = smart_palette_cache_key(cover_url, None, settings)
        animation_key = self._animation_settings_key(settings)

        with self._state_lock:
            previous = deepcopy(self._snapshot)
            previous_track = previous.get("track")
            previous_id = (
                str(previous_track.get("id"))
                if isinstance(previous_track, dict) and previous_track.get("id")
                else None
            )
            previous_cover = (
                str(previous_track.get("cover_url") or "")
                if isinstance(previous_track, dict)
                else ""
            )
            track_changed = track_id != previous_id
            playback_changed = is_playing != bool(previous.get("is_playing"))
            animation_settings_changed = animation_key != self._last_animation_key
            palette_needed = (
                track_changed
                or cover_url != previous_cover
                or palette_key != self._last_palette_key
                or self._artwork_retry_track == track_id
                or not previous.get("smart_color_boost")
            )

        palette_refreshed = False
        if palette_needed:
            palette_refreshed = True
            if cover_url:
                boost_result = extract_smart_palette(cover_url, settings, track_id=track_id)
            else:
                boost_result = {
                    "source": "local",
                    "cache_hit": False,
                    "quality_score": 0,
                    "palette": [],
                    "analysis": {"fallback": True, "reason": "missing_artwork"},
                }
            boost_result = deepcopy(boost_result)
            palette = [
                str(entry["hex"])
                for entry in boost_result.get("palette", [])
                if isinstance(entry, dict) and entry.get("hex")
            ]
            analysis = boost_result.get("analysis")
            analysis = dict(analysis) if isinstance(analysis, dict) else {}
            extraction_failed = bool(analysis.get("fallback"))
            if not palette:
                palette = list(FALLBACK_PALETTE)
                extraction_failed = True
                analysis["fallback"] = True
            boost_result["analysis"] = analysis
        else:
            palette = list(previous.get("palette") or FALLBACK_PALETTE)
            boost_result = deepcopy(previous.get("smart_color_boost") or {})
            extraction_failed = False

        # Artwork downloads and analysis are intentionally outside the commit
        # locks, but they can take long enough for Spotify to advance on its
        # own. Re-read the player after uncached artwork work and only dispatch
        # the palette if the same track/artwork is still current. In-app
        # commands are also caught by the cheaper epoch check before this call.
        if palette_refreshed and cover_url and not bool(boost_result.get("cache_hit")):
            if player_epoch() != read_epoch:
                return {
                    "authoritative": False,
                    "is_playing": is_playing,
                    "retry_after": 0.05,
                    "source": "command-invalidated-after-palette",
                }
            try:
                # Once artwork work has started, this validation is required
                # to safely commit colors. Its bounded lane is separate from
                # routine polls and reconciliation probes, so neither can
                # consume the slot needed to finish the new palette.
                validation = get_spotify_player_state_read(
                    access_token,
                    force_refresh=True,
                    validation=True,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Could not revalidate track %s after artwork work; preserving current lights: %s",
                    track_id,
                    exc,
                )
                return {
                    "authoritative": False,
                    "is_playing": is_playing,
                    "retry_after": 1.0,
                    "source": "artwork-revalidation-error",
                }

            if not validation.get("authoritative"):
                retry_after = max(0.05, float(validation.get("retry_after") or 0.0))
                logger.info(
                    "Deferring palette for %s until playback can be revalidated (source=%s, retry=%.2fs).",
                    track_id,
                    validation.get("source") or "unknown",
                    retry_after,
                )
                return {
                    "authoritative": False,
                    "is_playing": is_playing,
                    "retry_after": retry_after,
                    "source": "artwork-revalidation-deferred",
                }

            validation_payload = validation.get("payload")
            validation_item = (
                validation_payload.get("item")
                if isinstance(validation_payload, dict)
                else None
            )
            validation_status = int(validation.get("status_code") or 0)
            validation_track_id: str | None = None
            validation_cover_url = ""
            if validation_status == 200 and isinstance(validation_item, dict):
                _, validation_cover_url = self._item_album_and_cover(validation_item)
                validation_track_id = self._item_track_identity(
                    validation_item,
                    validation_cover_url,
                )
            if validation_track_id != track_id or validation_cover_url != cover_url:
                logger.info(
                    "Discarding palette for %s because Spotify advanced to %s during artwork work.",
                    track_id,
                    validation_track_id or "<idle>",
                )
                return {
                    "authoritative": False,
                    "is_playing": bool(
                        validation_payload.get("is_playing")
                        if isinstance(validation_payload, dict)
                        else False
                    ),
                    "retry_after": NOW_PLAYING_CACHE_TTL_SECONDS,
                    "source": "track-changed-during-artwork",
                }
            read_epoch = int(validation.get("epoch") or read_epoch)
            payload = validation_payload
            is_playing = bool(validation_payload.get("is_playing"))
            read_at, read_at_ms = time.monotonic(), int(time.time() * 1000)

        # The plan is cheap and depends on the room layout, so it is rebuilt on
        # every commit from the (cached) artwork analysis.
        light_plan = [] if extraction_failed else plan_light_colors(boost_result, settings)
        light_palette = light_plan or palette[:LIGHT_ROTATION_COLORS]
        lights = (
            light_manager.cached_summary(settings)
            if is_playing
            else lights_idle_state(settings, "Spotify playback is paused.")
        )
        next_snapshot = {
            "ok": True,
            "authenticated": True,
            "is_playing": is_playing,
            "mode": "Cover Sync" if is_playing else "Paused",
            "message": "Playing from Spotify." if is_playing else "Spotify playback is paused.",
            "track": {
                "id": track_id,
                "title": item.get("name", "Unknown Track"),
                "artist": artists or "Unknown Artist",
                "album": album.get("name", "Unknown Album") if isinstance(album, dict) else "Unknown Album",
                "cover_url": cover_url,
                "external_url": (item.get("external_urls") or {}).get("spotify"),
                "progress_ms": payload.get("progress_ms") or 0,
                "progress_at_ms": read_at_ms,
                "duration_ms": item.get("duration_ms") or 0,
                "volume_percent": (payload.get("device") or {}).get("volume_percent"),
            },
            "palette": palette,
            "light_plan": light_plan,
            "smart_color_boost": boost_result,
            "lights": lights,
            "updated_at": int(time.time()),
        }

        with spotify_player_cache_lock:
            if player_epoch() != read_epoch:
                logger.info("Discarding playback work for %s because a newer command invalidated it.", track_id)
                return {
                    "authoritative": False,
                    "is_playing": is_playing,
                    "retry_after": 0.05,
                    "source": "command-invalidated-after-palette",
                }
            with self._state_lock:
                self._sequence += 1
                sequence = self._sequence
                next_snapshot["sequence"] = sequence
                animation_needed = (
                    track_changed
                    or playback_changed
                    or palette_refreshed
                    or animation_settings_changed
                )
                try:
                    # Before the state change, so the Progress effect starts
                    # a new or resumed song from where it really is.
                    light_manager.set_progress(
                        track_id,
                        int(payload.get("progress_ms") or 0),
                        int(item.get("duration_ms") or 0),
                        read_at,
                    )
                except Exception:
                    logger.exception("Recording song progress failed for track %s.", track_id)
                try:
                    if extraction_failed:
                        if not is_playing and (track_changed or playback_changed):
                            light_manager.set_idle(settings)
                    elif animation_needed:
                        light_manager.set_animation(
                            settings,
                            light_palette,
                            mode="playing" if is_playing else "idle",
                            track_id=track_id,
                            sequence=sequence,
                        )
                except Exception:
                    logger.exception(
                        "Light animation update failed for track %s at sequence %s.",
                        track_id,
                        sequence,
                    )
                self._snapshot = next_snapshot
                self._last_palette_key = palette_key
                self._last_animation_key = animation_key
                if extraction_failed and cover_url:
                    if self._artwork_retry_track == track_id:
                        self._artwork_retry_count += 1
                    else:
                        self._artwork_retry_count = 1
                    self._artwork_retry_track = track_id
                else:
                    self._artwork_retry_track = None
                    self._artwork_retry_count = 0
                artwork_retry_count = self._artwork_retry_count

        if track_changed:
            logger.info(
                "Track transition seq=%s: %s -> %s (%s - %s).",
                sequence,
                previous_id or "<none>",
                track_id,
                artists or "Unknown Artist",
                item.get("name", "Unknown Track"),
            )
        if palette_refreshed:
            if extraction_failed:
                logger.warning(
                    "Artwork palette unavailable for track %s at seq=%s; keeping current lights and scheduling retry %s.",
                    track_id,
                    sequence,
                    artwork_retry_count,
                )
            else:
                logger.info(
                    "Palette ready seq=%s track=%s source=%s cache=%s colors=%s.",
                    sequence,
                    track_id,
                    boost_result.get("source") or "unknown",
                    bool(boost_result.get("cache_hit")),
                    ",".join(light_palette),
                )
        duration_ms = int(item.get("duration_ms") or 0)
        progress_ms = int(payload.get("progress_ms") or 0)
        return {
            "authoritative": True,
            "track_id": track_id,
            "is_playing": is_playing,
            "track_ends_at": (
                read_at + max(0, duration_ms - progress_ms) / 1000 if duration_ms > 0 else None
            ),
            "artwork_retry": extraction_failed and bool(cover_url),
            "artwork_retry_count": artwork_retry_count,
        }


playback_coordinator = PlaybackCoordinator()
