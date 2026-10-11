from __future__ import annotations

from typing import Any

import playback
import spotify_client
from palette.service import smart_palette_cache_key
from routers import player as player_routes
from settings import DEFAULT_SETTINGS


SETTINGS: dict[str, Any] = {
    **DEFAULT_SETTINGS,
    "lights_enabled": True,
    "devices": [{"device_id": "test", "enabled": True}],
}

PALETTE_RESULT = {
    "source": "local",
    "cache_hit": False,
    "quality_score": 1.0,
    "palette": [
        {"hex": "#d81717", "weight": 70, "role": "dominant"},
        {"hex": "#570808", "weight": 30, "role": "accent"},
    ],
    "analysis": {},
}


class FakeLightManager:
    def __init__(self) -> None:
        self.animations: list[dict[str, Any]] = []
        self.idle_calls = 0
        self.progress: list[dict[str, Any]] = []

    def cached_summary(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "enabled": True, "connected": True, "devices": []}

    def set_animation(
        self,
        settings: dict[str, Any],
        palette: list[str],
        mode: str,
        track_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        self.animations.append(
            {
                "palette": list(palette),
                "mode": mode,
                "track_id": track_id,
                "sequence": sequence,
                "effect": settings.get("light_effect"),
            }
        )

    def set_idle(self, settings: dict[str, Any]) -> None:
        self.idle_calls += 1

    def set_progress(
        self,
        track_id: str | None,
        progress_ms: int,
        duration_ms: int,
        read_at: float | None = None,
    ) -> None:
        self.progress.append({"track_id": track_id, "progress_ms": progress_ms, "duration_ms": duration_ms})


def player_payload(track_id: str, *, cover_url: str = "https://images.test/cover.jpg") -> dict[str, Any]:
    return {
        "is_playing": True,
        "progress_ms": 1234,
        "device": {"volume_percent": 72},
        "item": {
            "id": track_id,
            "uri": f"spotify:track:{track_id}",
            "name": f"Track {track_id}",
            "duration_ms": 180_000,
            "artists": [{"name": "Artist"}],
            "album": {
                "name": "Album",
                "images": [{"url": cover_url}],
            },
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        },
    }


def authoritative_read(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status_code": 200,
        "payload": payload,
        "text": "",
        "authoritative": True,
        "source": "spotify",
        "epoch": spotify_client.player_epoch(),
        "retry_after": 0.0,
    }


def configure_coordinator(monkeypatch, reads: list[dict[str, Any]]) -> tuple[playback.PlaybackCoordinator, FakeLightManager]:
    fake_lights = FakeLightManager()
    monkeypatch.setattr(playback, "load_settings", lambda: dict(SETTINGS))
    monkeypatch.setattr(playback, "auth_ready", lambda: True)
    monkeypatch.setattr(playback, "get_access_token", lambda: "token")
    monkeypatch.setattr(playback, "light_manager", fake_lights)
    monkeypatch.setattr(playback, "get_spotify_player_state_read", lambda *args, **kwargs: reads.pop(0))
    coordinator = playback.PlaybackCoordinator()
    return coordinator, fake_lights


def test_now_playing_returns_only_a_deep_copied_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(playback, "load_settings", lambda: dict(SETTINGS))
    coordinator = playback.PlaybackCoordinator()
    monkeypatch.setattr(player_routes, "playback_coordinator", coordinator)

    def unexpected_call(*args, **kwargs):
        raise AssertionError("cached endpoint performed playback work")

    monkeypatch.setattr(playback, "auth_ready", unexpected_call)
    monkeypatch.setattr(playback, "get_access_token", unexpected_call)
    monkeypatch.setattr(playback, "get_spotify_player_state_read", unexpected_call)
    monkeypatch.setattr(playback, "extract_smart_palette", unexpected_call)

    response = player_routes.now_playing()
    response["mode"] = "mutated"

    assert coordinator.cached_snapshot()["mode"] == "Starting"


def test_non_authoritative_cached_read_cannot_replace_snapshot_or_touch_lights(monkeypatch) -> None:
    stale = authoritative_read(player_payload("old"))
    stale.update({"authoritative": False, "source": "local-guard-cache", "retry_after": 8.0})
    coordinator, fake_lights = configure_coordinator(monkeypatch, [stale])
    before = coordinator.cached_snapshot()

    outcome = coordinator.refresh_once(force_refresh=True)

    assert outcome["authoritative"] is False
    assert outcome["source"] == "local-guard-cache"
    assert coordinator.cached_snapshot() == before
    assert fake_lights.animations == []
    assert fake_lights.idle_calls == 0


def test_track_change_dispatches_new_identity_and_sequence_even_for_same_palette(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-b")),
        authoritative_read(player_payload("track-b")),
    ]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))

    first = coordinator.refresh_once(force_refresh=True)
    second = coordinator.refresh_once(force_refresh=True)

    assert first["track_id"] == "track-a"
    assert second["track_id"] == "track-b"
    assert [(call["track_id"], call["sequence"]) for call in fake_lights.animations] == [
        ("track-a", 1),
        ("track-b", 2),
    ]
    assert fake_lights.animations[0]["palette"] == fake_lights.animations[1]["palette"]
    assert coordinator.cached_snapshot()["sequence"] == 2


def test_next_reconciliation_keeps_polling_a_a_until_b_is_authoritative(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-b")),
        authoritative_read(player_payload("track-b")),
    ]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))

    coordinator.refresh_once(force_refresh=True)
    coordinator.reconcile_after_control("track-a")

    still_a = coordinator.refresh_once(force_refresh=True)
    assert coordinator._next_delay(still_a) == playback.PLAYBACK_RECONCILE_DELAYS_SECONDS[0]
    assert coordinator._has_active_reconcile() is True

    changed_to_b = coordinator.refresh_once(force_refresh=True)
    assert coordinator._next_delay(changed_to_b) == playback.PLAYBACK_POLL_PLAYING_SECONDS
    assert coordinator._has_active_reconcile() is False
    assert coordinator.cached_snapshot()["track"]["id"] == "track-b"
    assert [(call["track_id"], call["sequence"]) for call in fake_lights.animations] == [
        ("track-a", 1),
        ("track-b", 3),
    ]


def test_command_invalidation_during_palette_work_cannot_commit_old_track(monkeypatch) -> None:
    # Keep this test's epoch/cache mutation isolated from the module singleton.
    monkeypatch.setattr(spotify_client, "spotify_player_cache", dict(spotify_client.spotify_player_cache))
    reads = [authoritative_read(player_payload("old-track"))]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    before = coordinator.cached_snapshot()

    def invalidate_while_extracting(*args, **kwargs):
        spotify_client.clear_spotify_player_cache()
        return dict(PALETTE_RESULT)

    monkeypatch.setattr(playback, "extract_smart_palette", invalidate_while_extracting)

    outcome = coordinator.refresh_once(force_refresh=True)

    assert outcome["authoritative"] is False
    assert outcome["source"] == "command-invalidated-after-palette"
    assert coordinator.cached_snapshot() == before
    assert fake_lights.animations == []


def test_artwork_failure_keeps_lights_unchanged_then_retries(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
    ]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    failed = {
        "source": "local",
        "cache_hit": False,
        "quality_score": 0.0,
        "palette": [],
        "analysis": {"fallback": True},
    }
    palette_results = [failed, PALETTE_RESULT]
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(palette_results.pop(0)))

    first = coordinator.refresh_once(force_refresh=True)
    assert first["artwork_retry"] is True
    assert coordinator._next_delay(first) == playback.PLAYBACK_ARTWORK_RETRY_SECONDS
    assert fake_lights.animations == []

    second = coordinator.refresh_once(force_refresh=True)
    assert second["artwork_retry"] is False
    assert [(call["track_id"], call["sequence"]) for call in fake_lights.animations] == [
        ("track-a", 2)
    ]


def test_external_track_change_during_artwork_discards_stale_palette(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-b")),
        authoritative_read(player_payload("track-b")),
        authoritative_read(player_payload("track-b")),
    ]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    extracted_tracks: list[str | None] = []

    def extract_for_track(*args, **kwargs):
        extracted_tracks.append(kwargs.get("track_id"))
        return dict(PALETTE_RESULT)

    monkeypatch.setattr(playback, "extract_smart_palette", extract_for_track)

    stale = coordinator.refresh_once(force_refresh=True)

    assert stale["authoritative"] is False
    assert stale["source"] == "track-changed-during-artwork"
    assert coordinator.cached_snapshot()["track"] is None
    assert fake_lights.animations == []

    current = coordinator.refresh_once(force_refresh=True)

    assert current["track_id"] == "track-b"
    assert extracted_tracks == ["track-a", "track-b"]
    assert [call["track_id"] for call in fake_lights.animations] == ["track-b"]


def test_reconciliation_and_artwork_validation_use_separate_reserved_reads(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
    ]
    coordinator, _ = configure_coordinator(monkeypatch, reads)
    calls: list[dict[str, Any]] = []

    def recording_read(*args, **kwargs):
        calls.append(dict(kwargs))
        return reads.pop(0)

    monkeypatch.setattr(playback, "get_spotify_player_state_read", recording_read)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))

    coordinator.refresh_once(force_refresh=True, priority_read=True)

    assert calls[0]["priority"] is True
    assert not calls[0].get("validation", False)
    assert calls[1]["validation"] is True
    assert not calls[1].get("priority", False)


def test_automatic_artwork_validation_uses_priority_reserve(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
    ]
    coordinator, _ = configure_coordinator(monkeypatch, reads)
    calls: list[dict[str, Any]] = []

    def recording_read(*args, **kwargs):
        calls.append(dict(kwargs))
        return reads.pop(0)

    monkeypatch.setattr(playback, "get_spotify_player_state_read", recording_read)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))

    coordinator.refresh_once(force_refresh=True)

    assert calls[0]["priority"] is False
    assert not calls[0].get("validation", False)
    assert calls[1]["validation"] is True
    assert not calls[1].get("priority", False)


def test_palette_cache_key_is_artwork_and_settings_based_not_track_based() -> None:
    first = smart_palette_cache_key("https://images.test/shared.jpg", "track-a", SETTINGS)
    second = smart_palette_cache_key("https://images.test/shared.jpg", "track-b", SETTINGS)

    assert first == second


def test_snapshot_light_plan_reaches_lights_and_replans_for_a_new_room(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
    ]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))

    coordinator.refresh_once(force_refresh=True)
    plan = coordinator.cached_snapshot()["light_plan"]

    # One enabled light and no layout set: one slot.
    assert len(plan) == 1
    assert fake_lights.animations[-1]["palette"] == plan

    coordinator.replan_lights({**SETTINGS, "light_count": 6, "light_stands": 3})

    replanned = coordinator.cached_snapshot()["light_plan"]
    assert len(replanned) == 6
    assert fake_lights.animations[-1]["palette"] == replanned
    assert fake_lights.animations[-1]["track_id"] == "track-a"
    assert reads == [], "re-planning must not read Spotify again"


def test_effect_change_reaches_lights_without_waiting_for_spotify(monkeypatch) -> None:
    reads = [
        authoritative_read(player_payload("track-a")),
        authoritative_read(player_payload("track-a")),
    ]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))
    coordinator.refresh_once(force_refresh=True)
    plan = coordinator.cached_snapshot()["light_plan"]
    calls = len(fake_lights.animations)

    # Same room, so the plan is unchanged; only the effect differs.
    coordinator.replan_lights({**SETTINGS, "light_effect": "wave"})

    assert len(fake_lights.animations) == calls + 1
    assert fake_lights.animations[-1]["effect"] == "wave"
    assert fake_lights.animations[-1]["palette"] == plan
    assert fake_lights.animations[-1]["mode"] == "playing"
    assert reads == []


def test_every_commit_records_song_progress_for_the_progress_effect(monkeypatch) -> None:
    payload = player_payload("track-a")
    reads = [authoritative_read(payload), authoritative_read(payload)]
    coordinator, fake_lights = configure_coordinator(monkeypatch, reads)
    monkeypatch.setattr(playback, "extract_smart_palette", lambda *args, **kwargs: dict(PALETTE_RESULT))

    coordinator.refresh_once(force_refresh=True)

    assert fake_lights.progress == [{"track_id": "track-a", "progress_ms": 1234, "duration_ms": 180_000}]
