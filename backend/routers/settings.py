"""Health, status, and the saved settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from playback import playback_coordinator
from settings import CONFIG_DIR, load_settings, public_settings, save_settings
from spotify_client import auth_ready, load_token, remove_spotify_token

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    settings = load_settings()
    return {
        "ok": True,
        "spotify_configured": auth_ready(),
        "lights_enabled": bool(settings.get("lights_enabled")),
        "config_dir": str(CONFIG_DIR),
    }


@router.get("/api/status")
def status() -> dict[str, Any]:
    settings = load_settings()
    return {
        "ok": True,
        "spotify_configured": auth_ready(),
        "authenticated": bool(load_token()),
        "lights_enabled": bool(settings.get("lights_enabled")),
    }


@router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {"ok": True, "settings": public_settings()}


@router.put("/api/settings")
def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
    previous = load_settings()
    settings = save_settings(payload)
    # A sign-in made with other Spotify app credentials no longer works.
    spotify_keys = ("spotify_client_id", "spotify_client_secret", "spotify_redirect_uri")
    if any(settings.get(key) != previous.get(key) for key in spotify_keys):
        remove_spotify_token("Spotify settings update")
    playback_coordinator.replan_lights(settings)
    playback_coordinator.wake()
    return {"ok": True, "settings": public_settings(settings)}
