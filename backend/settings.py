"""Saved settings: defaults, legacy migrations, and the settings files."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import secrets_store
from lights import DEFAULT_LIGHT_ADAPTER
from runtime_paths import app_config_dir

# Before CONFIG_DIR: .env can move it (SPOTIFY_LIGHTING_CONFIG_DIR).
ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")
load_dotenv()

logger = logging.getLogger("spotify-lights")

CONFIG_DIR = app_config_dir()
SETTINGS_PATH = CONFIG_DIR / "settings.json"
LIGHTS_CONFIG_PATH = CONFIG_DIR / "lights.config.json"

SECRET_SETTING_KEYS = ("spotify_client_secret",)
# Adapters from older builds (Tuya, placeholders). Their bulbs cannot be driven
# by this build, so saved entries using them are dropped on load.
RETIRED_LIGHT_ADAPTERS = frozenset(
    {
        "tuya-cloud",
        "cloud",
        "tuya",
        "tuya-local",
        "smartlife",
        "lepro-placeholder",
        "daybetter-placeholder",
    }
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_redirect_uri": "http://127.0.0.1:8000/callback",
    "frontend_url": "http://127.0.0.1:5173",
    "lights_enabled": False,
    "default_light_adapter": DEFAULT_LIGHT_ADAPTER,
    "brightness_min": 20,
    "brightness_max": 85,
    "update_speed_ms": 350,
    "transition_duration_ms": 3000,
    # Color rotation under the Still effect has no switch in the app, so it
    # stays off unless set in settings.json.
    "animation_enabled": False,
    # Pace of the Wave effect (and of that rotation).
    "animation_speed": "slow",
    "idle_animation_enabled": False,
    # How the room moves while a song plays: still | wave | pulse | progress
    # (see lights/effects.py).
    "light_effect": "still",
    "wave_direction": "across",  # across (stand to stand) | down (each stand)
    "progress_style": "fill",  # fill (stands light up) | blend (main color to accent)
    "smart_color_boost_enabled": True,
    "smart_color_boost_mode": "auto",
    "accent_boost_strength": "medium",
    "neutral_reduction": "balanced",
    "red_pink_correction_enabled": True,
    # Room layout for color planning; 0 = one slot per enabled light.
    "light_count": 0,
    "light_stands": 0,
    "glass_style": "frosted",
    # Cover size with the player details hidden: percent of the screen's short side.
    "artwork_size": 70,
    "devices": [],
}

def env_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def load_json_file(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return fallback


def _retired_light(device: Any, default_adapter: str) -> bool:
    if not isinstance(device, dict):
        return False
    adapter = str(device.get("adapter") or default_adapter).strip().lower()
    return adapter in RETIRED_LIGHT_ADAPTERS


def current_settings_only(saved: Any) -> dict[str, Any]:
    """Keep only the settings and lights this build uses.

    Files from older builds can still hold retired keys (Tuya and Gemini
    credentials among them) and Tuya bulbs. Leaving them out on load also
    removes them from disk on the next save, and keeps them out of the UI.
    """
    if not isinstance(saved, dict):
        return {}
    clean = {key: value for key, value in saved.items() if key in DEFAULT_SETTINGS}
    default_adapter = str(clean.get("default_light_adapter") or DEFAULT_LIGHT_ADAPTER).strip().lower()
    if default_adapter in RETIRED_LIGHT_ADAPTERS:
        clean["default_light_adapter"] = DEFAULT_LIGHT_ADAPTER
    if isinstance(clean.get("devices"), list):
        # Entries without an adapter used the saved default, so they are
        # retired along with it.
        clean["devices"] = [
            device for device in clean["devices"] if not _retired_light(device, default_adapter)
        ]
    return clean


def load_settings() -> dict[str, Any]:
    settings = DEFAULT_SETTINGS | current_settings_only(load_json_file(SETTINGS_PATH, {}))
    for key in SECRET_SETTING_KEYS:
        if settings.get(key):
            settings[key] = secrets_store.unprotect(str(settings[key]))
    lights_config = load_json_file(LIGHTS_CONFIG_PATH, {})
    if not isinstance(lights_config, dict):
        lights_config = {}
    config_devices = current_settings_only(
        {"devices": lights_config.get("devices"), "default_light_adapter": settings["default_light_adapter"]}
    ).get("devices")
    if config_devices and not settings.get("devices"):
        settings["devices"] = config_devices
    if lights_config.get("settings"):
        settings |= current_settings_only(lights_config["settings"])

    env_overrides = {
        "spotify_client_id": os.getenv("SPOTIFY_CLIENT_ID"),
        "spotify_client_secret": os.getenv("SPOTIFY_CLIENT_SECRET"),
        "spotify_redirect_uri": os.getenv("SPOTIFY_REDIRECT_URI"),
        "frontend_url": os.getenv("FRONTEND_URL"),
    }
    for key, value in env_overrides.items():
        if value:
            settings[key] = value

    lights_enabled = env_bool("LIGHTS_ENABLED")
    if lights_enabled is not None:
        settings["lights_enabled"] = lights_enabled

    return settings


def public_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    public = dict(settings)
    public["spotify_client_secret"] = "********" if settings.get("spotify_client_secret") else ""
    public["config_dir"] = str(CONFIG_DIR)
    return public


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    allowed = set(DEFAULT_SETTINGS.keys())
    next_settings = {key: payload[key] for key in payload if key in allowed}
    if next_settings.get("spotify_client_secret") == "********":
        next_settings["spotify_client_secret"] = current.get("spotify_client_secret", "")
    merged = current | next_settings
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    to_store = dict(merged)
    for key in SECRET_SETTING_KEYS:
        if to_store.get(key):
            to_store[key] = secrets_store.protect(str(to_store[key]))
    SETTINGS_PATH.write_text(json.dumps(to_store, indent=2), encoding="utf-8")
    return merged
