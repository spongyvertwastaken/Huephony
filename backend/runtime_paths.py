"""Per-user storage shared by the HTTP server and desktop entry point."""

from __future__ import annotations

import os
from pathlib import Path


def app_config_dir() -> Path:
    override = os.getenv("SPOTIFY_LIGHTING_CONFIG_DIR")
    if override:
        return Path(override)
    # Keep the original folder names so existing installations retain settings.
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "SpotifyRoomLighting"
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "spotify-room-lighting"
