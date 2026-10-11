from __future__ import annotations

import json
from collections import OrderedDict, deque
import time
from pathlib import Path
from typing import Any

import pytest

import playback
import secrets_store
import settings as app_settings
import spotify_client
from palette import service
from routers import auth as auth_routes
from routers import settings as settings_routes
from runtime_paths import app_config_dir


def test_config_override_is_shared_by_server_and_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import desktop_backend

    monkeypatch.setenv("SPOTIFY_LIGHTING_CONFIG_DIR", str(tmp_path))

    assert app_config_dir() == tmp_path
    assert app_settings.app_config_dir() == tmp_path
    assert desktop_backend.app_config_dir() == tmp_path


def test_retired_keys_and_tuya_bulbs_are_dropped_on_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "default_light_adapter": "tuya-local",
                "brightness_max": 70,
                "tuya_access_id": "old-id",
                "tuya_access_secret": "old-secret",
                "gemini_api_key": "old-key",
                "ai_provider": "gemini",
                "devices": [
                    {"name": "old default", "ip": "192.168.1.9"},
                    {"name": "old tuya", "adapter": "tuya-cloud"},
                    {"name": "lamp", "adapter": "govee", "ip": "192.168.1.50"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(app_settings, "LIGHTS_CONFIG_PATH", tmp_path / "missing.json")

    settings = app_settings.load_settings()

    assert settings["default_light_adapter"] == "govee"
    assert settings["brightness_max"] == 70
    assert [device["name"] for device in settings["devices"]] == ["lamp"]
    assert not any(key.startswith(("tuya_", "gemini_", "ai_")) for key in settings)

    app_settings.save_settings({"brightness_max": 60})
    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(stored) <= set(app_settings.DEFAULT_SETTINGS)
    assert [device["name"] for device in stored["devices"]] == ["lamp"]


def test_devices_without_an_adapter_stay_under_a_govee_default() -> None:
    clean = app_settings.current_settings_only({"devices": [{"name": "lamp", "ip": "192.168.1.50"}]})

    assert clean["devices"] == [{"name": "lamp", "ip": "192.168.1.50"}]


def test_glass_defaults_to_frosted() -> None:
    assert app_settings.DEFAULT_SETTINGS["glass_style"] == "frosted"


def test_artwork_size_is_a_saved_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(app_settings, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "LIGHTS_CONFIG_PATH", tmp_path / "lights.config.json")

    assert app_settings.load_settings()["artwork_size"] == 70
    app_settings.save_settings({"artwork_size": 85})
    assert app_settings.load_settings()["artwork_size"] == 85


def test_spotify_token_is_encrypted_at_rest_and_old_plain_tokens_still_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "spotify_token.json"
    monkeypatch.setattr(spotify_client, "TOKEN_PATH", token_path)
    monkeypatch.setattr(spotify_client, "spotify_credentials", lambda: ("client", "secret", "http://127.0.0.1:8000/callback", ""))

    token_path.write_text(
        json.dumps({"access_token": "plain", "_client_id": "client", "_redirect_uri": "http://127.0.0.1:8000/callback"}),
        encoding="utf-8",
    )
    assert spotify_client.load_token()["access_token"] == "plain"

    spotify_client.save_token({"access_token": "fresh", "refresh_token": "refresh"})
    stored = token_path.read_text(encoding="utf-8")
    if secrets_store.can_encrypt():
        assert "fresh" not in stored and "refresh" not in stored
    assert spotify_client.load_token()["access_token"] == "fresh"


def test_changing_spotify_credentials_drops_the_saved_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app_settings, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "LIGHTS_CONFIG_PATH", tmp_path / "lights.config.json")
    for name in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    removed: list[str] = []
    monkeypatch.setattr(settings_routes, "remove_spotify_token", removed.append)

    settings_routes.put_settings({"spotify_client_id": "client", "spotify_client_secret": "secret"})
    assert len(removed) == 1

    # The settings page sends the secret back masked; that is not a change.
    settings_routes.put_settings({"spotify_client_id": "client", "spotify_client_secret": "********", "brightness_max": 60})
    assert len(removed) == 1


def test_disconnect_deletes_sign_in_and_listening_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_path = tmp_path / "spotify_token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spotify_client, "TOKEN_PATH", token_path)
    monkeypatch.setattr(spotify_client, "spotify_player_cache", dict(spotify_client.spotify_player_cache))
    monkeypatch.setattr(service, "palette_cache", OrderedDict({"cover": {"palette": []}}))
    monkeypatch.setattr(playback, "load_settings", lambda: dict(app_settings.DEFAULT_SETTINGS))
    idle_calls: list[Any] = []
    monkeypatch.setattr(playback.light_manager, "set_idle", lambda settings: idle_calls.append(settings))
    coordinator = playback.PlaybackCoordinator()
    coordinator._snapshot = {**coordinator._snapshot, "authenticated": True, "track": {"id": "track-a"}}
    coordinator._up_next = {"after": "track-a", "cover_url": "https://images.test/next.jpg"}
    monkeypatch.setattr(auth_routes, "playback_coordinator", coordinator)

    result = auth_routes.spotify_disconnect()

    assert result["ok"] is True
    assert not token_path.exists()
    assert len(service.palette_cache) == 0
    snapshot = coordinator.cached_snapshot()
    assert snapshot["authenticated"] is False
    assert snapshot["track"] is None
    assert "up_next_cover_url" not in snapshot
    assert idle_calls


def test_poll_after_a_natural_track_end_is_timed_for_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playback, "load_settings", lambda: dict(app_settings.DEFAULT_SETTINGS))
    coordinator = playback.PlaybackCoordinator()
    ending = {"authoritative": True, "is_playing": True, "track_id": "track-a", "track_ends_at": time.monotonic() + 2.0}

    first = coordinator._next_delay(ending)
    assert 2.0 < first <= 2.0 + playback.PLAYBACK_TRACK_END_BUFFER_SECONDS
    assert first < playback.PLAYBACK_POLL_PLAYING_SECONDS

    # Spotify still reports the ended track: a bounded number of short
    # follow-ups, then the regular cadence.
    ended = {**ending, "track_ends_at": time.monotonic()}
    assert coordinator._next_delay(ended) == playback.PLAYBACK_TRACK_END_MIN_DELAY_SECONDS
    assert coordinator._next_delay(ended) == playback.PLAYBACK_POLL_PLAYING_SECONDS

    # A new track starts its own budget.
    next_track = {**ended, "track_id": "track-b"}
    assert coordinator._next_delay(next_track) == playback.PLAYBACK_TRACK_END_MIN_DELAY_SECONDS


def test_poll_mid_track_keeps_the_regular_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playback, "load_settings", lambda: dict(app_settings.DEFAULT_SETTINGS))
    coordinator = playback.PlaybackCoordinator()
    playing = {"authoritative": True, "is_playing": True, "track_id": "track-a", "track_ends_at": time.monotonic() + 90}

    assert coordinator._next_delay(playing) == playback.PLAYBACK_POLL_PLAYING_SECONDS
    assert coordinator._next_delay({**playing, "track_ends_at": None}) == playback.PLAYBACK_POLL_PLAYING_SECONDS


def test_queue_reads_have_their_own_small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spotify_client, "spotify_queue_read_request_times", deque())
    monkeypatch.setattr(spotify_client, "spotify_player_read_request_times", deque())
    now = 1000.0

    for _ in range(spotify_client.SPOTIFY_MAX_QUEUE_READS_PER_WINDOW):
        assert spotify_client.reserve_spotify_player_read(now, queue=True) == 0
    assert spotify_client.reserve_spotify_player_read(now, queue=True) > 0
    assert len(spotify_client.spotify_player_read_request_times) == 0
    with pytest.raises(ValueError):
        spotify_client.reserve_spotify_player_read(now, queue=True, priority=True)


def test_up_next_cover_is_only_shown_for_the_track_it_follows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playback, "load_settings", lambda: dict(app_settings.DEFAULT_SETTINGS))
    coordinator = playback.PlaybackCoordinator()
    coordinator._snapshot = {**coordinator._snapshot, "track": {"id": "track-a"}}
    coordinator._up_next = {"after": "track-a", "cover_url": "https://images.test/next.jpg"}

    assert coordinator.cached_snapshot()["up_next_cover_url"] == "https://images.test/next.jpg"

    coordinator._snapshot = {**coordinator._snapshot, "track": {"id": "track-b"}}
    assert "up_next_cover_url" not in coordinator.cached_snapshot()
