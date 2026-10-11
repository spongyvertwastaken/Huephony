"""The lights: status, test colors, discovery, and identify."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from lights.govee import is_light_ip
from palette.analysis import LIGHT_ROTATION_COLORS
from palette.colors import FALLBACK_PALETTE
from playback import light_manager, playback_coordinator
from settings import load_settings

MAX_DISCOVERY_IPS = 16

router = APIRouter()


@router.get("/api/lights/status")
def lights_status() -> dict[str, Any]:
    settings = load_settings()
    return light_manager.summary(settings)


@router.post("/api/lights/test")
def lights_test() -> dict[str, Any]:
    settings = load_settings()
    return light_manager.apply_palette(settings, FALLBACK_PALETTE, track_id="test", force=True)


@router.post("/api/lights/apply-palette")
def lights_apply_palette(payload: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    palette = payload.get("palette") if isinstance(payload, dict) else None
    if not isinstance(palette, list) or not all(isinstance(color, str) for color in palette):
        raise HTTPException(status_code=400, detail="Body must include palette: string[].")
    # Apply immediately for instant feedback, then hand the palette to the
    # animation loop so it keeps breathing/cycling. The current track's room
    # plan wins over the screen palette the frontend sends.
    snapshot = playback_coordinator.cached_snapshot()
    plan = snapshot.get("light_plan") if snapshot.get("track") else None
    light_palette = plan or palette[:LIGHT_ROTATION_COLORS]
    summary = light_manager.apply_palette(settings, light_palette, force=True)
    light_manager.set_animation(settings, light_palette, mode="playing")
    return summary


@router.get("/api/lights/pulse")
def lights_pulse() -> dict[str, Any]:
    # The Pulse effect's live loudness, for the settings preview. Asking
    # starts the audio meter; it stops shortly after the requests do.
    return light_manager.pulse_reading()


@router.post("/api/lights/off")
def lights_off() -> dict[str, Any]:
    settings = load_settings()
    return light_manager.turn_off(settings)


@router.post("/api/lights/discover")
def lights_discover(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    # Runs a Govee LAN scan for a couple of seconds. An empty body searches the
    # whole network; {"ips": [...]} asks specific addresses (Add by IP). Finding
    # nothing is a normal result with troubleshooting tips, not an error.
    ips = payload.get("ips") if isinstance(payload, dict) else None
    if ips is not None:
        if (
            not isinstance(ips, list)
            or not 0 < len(ips) <= MAX_DISCOVERY_IPS
            or not all(isinstance(ip, str) and is_light_ip(ip) for ip in ips)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"ips must be a list of 1-{MAX_DISCOVERY_IPS} IPv4 addresses like 192.168.1.50.",
            )
        ips = [ip.strip() for ip in ips]
    return light_manager.discover_lights(ips)


@router.post("/api/lights/identify")
def lights_identify(payload: dict[str, Any]) -> dict[str, Any]:
    ip = payload.get("ip") if isinstance(payload, dict) else None
    if not isinstance(ip, str) or not is_light_ip(ip):
        raise HTTPException(status_code=400, detail="Body must include ip: an IPv4 address like 192.168.1.50.")
    device = {
        "name": str(payload.get("name") or ip),
        "ip": ip.strip(),
        "device_id": str(payload.get("device_id") or ""),
    }
    return light_manager.identify_light(device)
