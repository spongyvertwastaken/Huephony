"""Now playing and the playback controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from playback import playback_coordinator
from spotify_client import spotify_control_request

router = APIRouter()


@router.get("/api/now-playing")
def now_playing() -> dict[str, Any]:
    # This endpoint is intentionally side-effect and network free. The UI may
    # call it frequently; Spotify, artwork, and lights are owned by the daemon.
    return playback_coordinator.cached_snapshot()


def _player_control(
    method: str,
    path: str = "",
    *,
    reconcile_track: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    previous_track_id = playback_coordinator.current_track_id()
    result = spotify_control_request(method, path, **kwargs)
    if result.get("ok"):
        if reconcile_track:
            playback_coordinator.reconcile_after_control(previous_track_id)
        else:
            playback_coordinator.wake()
    return result


@router.put("/api/player/play")
def player_play() -> dict[str, Any]:
    return _player_control("PUT", "/play")


@router.put("/api/player/pause")
def player_pause() -> dict[str, Any]:
    return _player_control("PUT", "/pause")


@router.post("/api/player/next")
def player_next() -> dict[str, Any]:
    return _player_control("POST", "/next", reconcile_track=True)


@router.post("/api/player/previous")
def player_previous() -> dict[str, Any]:
    return _player_control("POST", "/previous", reconcile_track=True)


@router.put("/api/player/volume")
def player_volume(volume_percent: int = Query(ge=0, le=100)) -> dict[str, Any]:
    return _player_control(
        "PUT",
        "/volume",
        params={"volume_percent": volume_percent},
    )


@router.put("/api/player/seek")
def player_seek(position_ms: int = Query(ge=0)) -> dict[str, Any]:
    return _player_control(
        "PUT",
        "/seek",
        params={"position_ms": position_ms},
    )
