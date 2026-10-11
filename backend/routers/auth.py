"""Spotify sign-in: connect, the OAuth callback, and disconnect."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from palette.service import clear_palette_cache
from playback import playback_coordinator
from settings import CONFIG_DIR
from spotify_client import (
    SCOPES,
    SPOTIFY_AUTHORIZE_URL,
    auth_ready,
    exchange_code,
    remove_spotify_token,
    spotify_credentials,
)

logger = logging.getLogger("spotify-lights")

router = APIRouter()


@router.get("/login")
def login(desktop: bool = Query(default=False)) -> RedirectResponse:
    if not auth_ready():
        raise HTTPException(
            status_code=503,
            detail="Spotify environment variables are not configured.",
        )

    client_id, _, redirect_uri, _ = spotify_credentials()
    params = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "show_dialog": "false",
            "state": "desktop" if desktop else "web",
        }
    )
    return RedirectResponse(f"{SPOTIFY_AUTHORIZE_URL}?{params}")


@router.get("/callback")
def callback(
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    state: str | None = Query(default=None),
):
    _, _, _, frontend_url = spotify_credentials()
    if error:
        logger.warning("Spotify OAuth error: %s", error)
        return RedirectResponse(f"{frontend_url}?spotify_error={error}")
    if not code:
        return RedirectResponse(f"{frontend_url}?spotify_error=missing_code")

    try:
        exchange_code(code)
    except requests.RequestException as exc:
        logger.exception("Spotify code exchange failed")
        return RedirectResponse(f"{frontend_url}?spotify_error={exc.__class__.__name__}")

    playback_coordinator.wake()

    if state == "desktop":
        return HTMLResponse(
            """
            <html>
              <head><title>Spotify Connected</title></head>
              <body style="margin: 0; font-family: system-ui, sans-serif; background: #08080a; color: white; display: grid; min-height: 100vh; place-items: center;">
                <main style="text-align: center;">
                  <h1 style="font-size: 28px; margin: 0 0 10px;">Spotify connected</h1>
                  <p style="margin: 0; color: rgba(255,255,255,0.68);">You can close this tab and return to Huephony.</p>
                </main>
              </body>
            </html>
            """
        )

    if frontend_url.startswith("http://") or frontend_url.startswith("https://"):
        return RedirectResponse(frontend_url)
    return HTMLResponse(
        """
        <html>
          <head><title>Spotify Connected</title></head>
          <body style="font-family: system-ui; background: #08080a; color: white; display: grid; min-height: 100vh; place-items: center;">
            <main style="text-align: center;">
              <h1>Spotify connected</h1>
              <p>You can return to Huephony.</p>
            </main>
          </body>
        </html>
        """
    )


def clear_log_files() -> None:
    """Empty this app's log files, which can name recently played tracks."""
    config_dir = CONFIG_DIR.resolve()
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        path = Path(handler.baseFilename).resolve()
        if path.parent != config_dir:
            continue
        handler.acquire()
        try:
            if handler.stream:
                handler.stream.seek(0)
                handler.stream.truncate()
        finally:
            handler.release()
        for backup in path.parent.glob(f"{path.name}.*"):
            try:
                backup.unlink()
            except OSError as exc:
                logger.warning("Could not delete old log %s: %s", backup.name, exc)


@router.post("/api/spotify/disconnect")
def spotify_disconnect() -> dict[str, Any]:
    # Spotify's developer policy: disconnecting must stop all access and
    # delete what the app holds about the account's listening.
    if not remove_spotify_token("Spotify disconnect"):
        return {"ok": False, "message": "Could not remove the saved Spotify sign-in. Close the app and try again."}
    clear_palette_cache()
    playback_coordinator.forget_listening()
    clear_log_files()
    logger.info("Spotify disconnected; sign-in, playback state and logs cleared.")
    return {"ok": True, "message": "Spotify disconnected. Your sign-in and listening data were deleted from this computer."}
