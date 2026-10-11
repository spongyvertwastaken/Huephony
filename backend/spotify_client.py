"""Spotify Web API access: sign-in tokens, request budgets, player reads, and controls."""

from __future__ import annotations

import base64
import json
import logging
import math
import threading
import time
from collections import deque
from typing import Any

import requests

import secrets_store
from settings import CONFIG_DIR, DEFAULT_SETTINGS, load_settings

logger = logging.getLogger("spotify-lights")

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_PLAYER_URL = "https://api.spotify.com/v1/me/player"
SPOTIFY_QUEUE_URL = "https://api.spotify.com/v1/me/player/queue"
SCOPES = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
NOW_PLAYING_CACHE_TTL_SECONDS = 0.75
SPOTIFY_REQUEST_WINDOW_SECONDS = 30
# Routine player reads have a conservative budget. Command reconciliation and
# the identity check required before committing fresh artwork each use a
# smaller, separately bounded reserve, so probes cannot consume the palette's
# mandatory validation slot. Playback controls themselves never fail just
# because background reads used their allowance, while a real Spotify 429
# pauses every lane until its Retry-After cooldown expires. The routine budget
# covers the regular poll plus the reads timed for the end of a track.
SPOTIFY_MAX_PLAYER_READS_PER_WINDOW = 8
SPOTIFY_MAX_PRIORITY_PLAYER_READS_PER_WINDOW = 4
SPOTIFY_MAX_VALIDATION_PLAYER_READS_PER_WINDOW = 4
# One queue read per track is enough to prepare the next track's colors.
SPOTIFY_MAX_QUEUE_READS_PER_WINDOW = 2

TOKEN_PATH = CONFIG_DIR / "spotify_token.json"

spotify_player_read_request_times: deque[float] = deque()
spotify_priority_player_read_request_times: deque[float] = deque()
spotify_validation_player_read_request_times: deque[float] = deque()
spotify_queue_read_request_times: deque[float] = deque()
spotify_api_request_lock = threading.Lock()
spotify_server_rate_limited_until = 0.0
spotify_player_cache_lock = threading.RLock()
spotify_player_cache: dict[str, Any] = {
    "fetched_at": 0.0,
    "status_code": 0,
    "payload": None,
    "text": "",
    "rate_limited_until": 0.0,
    "rate_limit_logged_at": 0.0,
    "rate_limit_count": 0,
    "cooldown_status": 0,
    # Incremented whenever a successful playback command invalidates an
    # in-flight player read. A response from an older epoch is never allowed to
    # become authoritative after a skip/pause/play command.
    "invalidation_epoch": 0,
}


def spotify_credentials() -> tuple[str, str, str, str]:
    settings = load_settings()
    return (
        settings.get("spotify_client_id", ""),
        settings.get("spotify_client_secret", ""),
        settings.get("spotify_redirect_uri", DEFAULT_SETTINGS["spotify_redirect_uri"]),
        settings.get("frontend_url", DEFAULT_SETTINGS["frontend_url"]),
    )


def auth_ready() -> bool:
    client_id, client_secret, redirect_uri, _ = spotify_credentials()
    return bool(client_id and client_secret and redirect_uri)


def token_headers() -> dict[str, str]:
    client_id, client_secret, _, _ = spotify_credentials()
    raw = f"{client_id}:{client_secret}".encode()
    return {
        "Authorization": f"Basic {base64.b64encode(raw).decode('utf-8')}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def load_token() -> dict[str, Any] | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        token = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        # Tokens are stored encrypted (see save_token); files from older
        # builds are plain JSON and are encrypted on the next refresh.
        if isinstance(token, dict) and "protected" in token:
            token = json.loads(secrets_store.unprotect(str(token["protected"])) or "null")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read Spotify token cache: %s", exc)
        return None
    if not isinstance(token, dict):
        return None
    client_id, _, redirect_uri, _ = spotify_credentials()
    token_client_id = token.get("_client_id")
    token_redirect_uri = token.get("_redirect_uri")
    if not token_client_id or token_client_id != client_id or token_redirect_uri != redirect_uri:
        logger.info("Spotify token cache does not match current app credentials; reconnect required.")
        remove_spotify_token("Spotify credential change")
        return None
    return token


def save_token(token: dict[str, Any]) -> None:
    client_id, _, redirect_uri, _ = spotify_credentials()
    token["_client_id"] = client_id
    token["_redirect_uri"] = redirect_uri
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    stored: dict[str, Any] = token
    if secrets_store.can_encrypt():
        stored = {"protected": secrets_store.protect(json.dumps(token))}
    TOKEN_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")


def remove_spotify_token(reason: str) -> bool:
    """Delete the saved sign-in and reset player reads.

    Returns False if the sign-in is still on disk afterwards.
    """
    global spotify_server_rate_limited_until
    try:
        TOKEN_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove Spotify token cache after %s: %s", reason, exc)
    clear_spotify_player_cache()
    with spotify_api_request_lock:
        spotify_player_read_request_times.clear()
        spotify_priority_player_read_request_times.clear()
        spotify_validation_player_read_request_times.clear()
        spotify_queue_read_request_times.clear()
        spotify_server_rate_limited_until = 0.0
    return not TOKEN_PATH.exists()


def exchange_code(code: str) -> dict[str, Any]:
    _, _, redirect_uri, _ = spotify_credentials()
    response = requests.post(
        SPOTIFY_TOKEN_URL,
        headers=token_headers(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=12,
    )
    if response.status_code >= 400:
        logger.warning("Spotify code exchange failed: %s %s", response.status_code, response.text)
    response.raise_for_status()
    token = response.json()
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600)) - 60
    save_token(token)
    return token


def refresh_token(token: dict[str, Any]) -> dict[str, Any] | None:
    refresh = token.get("refresh_token")
    if not refresh:
        return None

    response = requests.post(
        SPOTIFY_TOKEN_URL,
        headers=token_headers(),
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        timeout=12,
    )
    if response.status_code >= 400:
        logger.warning("Spotify refresh failed: %s %s", response.status_code, response.text)
        return None

    fresh = response.json()
    fresh["refresh_token"] = fresh.get("refresh_token", refresh)
    fresh["expires_at"] = int(time.time()) + int(fresh.get("expires_in", 3600)) - 60
    save_token(fresh)
    return fresh


def get_access_token() -> str | None:
    token = load_token()
    if not token:
        return None
    if int(token.get("expires_at", 0)) <= int(time.time()):
        token = refresh_token(token)
    return token.get("access_token") if token else None


def clear_spotify_player_cache(*, invalidate_inflight: bool = True) -> None:
    """Clear only the raw Spotify response cache.

    The public now-playing snapshot deliberately survives this invalidation. Its
    last authoritative track remains visible until the coordinator observes a
    genuinely fresh replacement from Spotify.
    """
    with spotify_player_cache_lock:
        epoch = int(spotify_player_cache.get("invalidation_epoch") or 0)
        if invalidate_inflight:
            epoch += 1
        spotify_player_cache.update(
            {
                "fetched_at": 0.0,
                "status_code": 0,
                "payload": None,
                "text": "",
                "rate_limited_until": 0.0,
                "rate_limit_logged_at": 0.0,
                "rate_limit_count": 0,
                "cooldown_status": 0,
                "invalidation_epoch": epoch,
            }
        )


def player_epoch() -> int:
    """The current command epoch; each successful playback command advances it.

    Hold spotify_player_cache_lock across the check and the commit that
    depends on it, so a command cannot land in between.
    """
    with spotify_player_cache_lock:
        return int(spotify_player_cache.get("invalidation_epoch") or 0)


def reserve_spotify_player_read(
    now: float | None = None,
    *,
    priority: bool = False,
    validation: bool = False,
    queue: bool = False,
) -> int:
    """Reserve one bounded routine, reconciliation, validation, or queue read.

    User-initiated controls do not consume either read budget. Their bounded
    reconciliation reads use the priority lane so routine polling cannot starve
    the color update that follows a successful Next/Previous command.
    """
    current_time = now if now is not None else time.monotonic()
    if priority + validation + queue > 1:
        raise ValueError("A Spotify player read cannot use two local lanes.")
    if queue:
        request_times = spotify_queue_read_request_times
        request_limit = SPOTIFY_MAX_QUEUE_READS_PER_WINDOW
    elif validation:
        request_times = spotify_validation_player_read_request_times
        request_limit = SPOTIFY_MAX_VALIDATION_PLAYER_READS_PER_WINDOW
    elif priority:
        request_times = spotify_priority_player_read_request_times
        request_limit = SPOTIFY_MAX_PRIORITY_PLAYER_READS_PER_WINDOW
    else:
        request_times = spotify_player_read_request_times
        request_limit = SPOTIFY_MAX_PLAYER_READS_PER_WINDOW
    with spotify_api_request_lock:
        while (
            request_times
            and current_time - request_times[0]
            >= SPOTIFY_REQUEST_WINDOW_SECONDS
        ):
            request_times.popleft()

        if len(request_times) >= request_limit:
            return max(
                1,
                int(
                    SPOTIFY_REQUEST_WINDOW_SECONDS
                    - (current_time - request_times[0])
                    + 0.999
                ),
            )

        request_times.append(current_time)
    return 0


def spotify_server_rate_limit_remaining(now: float | None = None) -> float:
    """Return only Spotify's real server-issued 429 cooldown."""
    current_time = now if now is not None else time.monotonic()
    with spotify_api_request_lock:
        return max(0.0, spotify_server_rate_limited_until - current_time)


def spotify_rate_limit_cooldown_seconds(response: requests.Response) -> int:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return 120
    try:
        requested_seconds = math.ceil(float(retry_after))
    except (TypeError, ValueError):
        requested_seconds = 30
    return min(max(5, requested_seconds + 2), 900)


def note_spotify_server_rate_limit(cooldown_seconds: int, now: float | None = None) -> None:
    """Share a real Spotify 429 deadline across reads and controls."""
    global spotify_server_rate_limited_until
    current_time = now if now is not None else time.monotonic()
    with spotify_api_request_lock:
        spotify_server_rate_limited_until = max(
            spotify_server_rate_limited_until,
            current_time + cooldown_seconds,
        )


def get_spotify_player_state_read(
    access_token: str,
    *,
    force_refresh: bool = False,
    priority: bool = False,
    validation: bool = False,
) -> dict[str, Any]:
    """Read Spotify player state and label whether it came from the network.

    Cached, locally guarded, rate-limited, and command-invalidated reads are
    useful diagnostics, but they are never authoritative. The playback
    coordinator is the only routine caller and refuses to publish or apply
    colors from any non-authoritative result.
    """
    now = time.monotonic()
    with spotify_player_cache_lock:
        cached_payload = spotify_player_cache.get("payload")
        cached_status = int(spotify_player_cache.get("status_code") or 0)
        cached_text = str(spotify_player_cache.get("text") or "")
        cached_at = float(spotify_player_cache.get("fetched_at") or 0.0)
        rate_limited_until = float(spotify_player_cache.get("rate_limited_until") or 0.0)
        cooldown_status = int(spotify_player_cache.get("cooldown_status") or 429)
        request_epoch = int(spotify_player_cache.get("invalidation_epoch") or 0)
    cached_is_usable = cached_status in {200, 204}
    normalized_cached_payload = cached_payload if isinstance(cached_payload, dict) else None

    server_rate_limit_remaining = spotify_server_rate_limit_remaining(now)
    if server_rate_limit_remaining:
        return {
            "status_code": cached_status if cached_is_usable else 429,
            "payload": normalized_cached_payload if cached_is_usable else None,
            "text": cached_text if cached_is_usable else "Spotify rate-limit cooldown is active.",
            "authoritative": False,
            "source": "spotify-429-cache" if cached_is_usable else "spotify-429",
            "epoch": request_epoch,
            "retry_after": max(0.05, server_rate_limit_remaining),
        }

    if now < rate_limited_until:
        return {
            "status_code": cached_status if cached_is_usable else cooldown_status,
            "payload": normalized_cached_payload if cached_is_usable else None,
            "text": cached_text or "Spotify cooldown is active.",
            "authoritative": False,
            "source": "cooldown-cache" if cached_is_usable else "cooldown",
            "epoch": request_epoch,
            "retry_after": max(0.05, rate_limited_until - now),
        }

    if (
        not force_refresh
        and cached_is_usable
        and now - cached_at < NOW_PLAYING_CACHE_TTL_SECONDS
    ):
        return {
            "status_code": cached_status,
            "payload": normalized_cached_payload,
            "text": cached_text,
            "authoritative": False,
            "source": "ttl-cache",
            "epoch": request_epoch,
            "retry_after": max(0.05, NOW_PLAYING_CACHE_TTL_SECONDS - (now - cached_at)),
        }

    local_wait_seconds = reserve_spotify_player_read(
        now,
        priority=priority,
        validation=validation,
    )
    if local_wait_seconds:
        return {
            "status_code": cached_status if cached_is_usable else 429,
            "payload": normalized_cached_payload if cached_is_usable else None,
            "text": cached_text if cached_is_usable else f"Local Spotify request guard active for {local_wait_seconds}s.",
            "authoritative": False,
            "source": "local-guard-cache" if cached_is_usable else "local-guard",
            "epoch": request_epoch,
            "retry_after": float(local_wait_seconds),
        }

    response = requests.get(
        SPOTIFY_PLAYER_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=12,
    )
    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = None
    normalized_payload = payload if isinstance(payload, dict) else None
    server_cooldown_seconds = 0
    if response.status_code == 429:
        server_cooldown_seconds = spotify_rate_limit_cooldown_seconds(response)
        note_spotify_server_rate_limit(server_cooldown_seconds, now)

    with spotify_player_cache_lock:
        current_epoch = int(spotify_player_cache.get("invalidation_epoch") or 0)
        if current_epoch != request_epoch:
            return {
                "status_code": response.status_code,
                "payload": normalized_payload,
                "text": response.text,
                "authoritative": False,
                "source": "command-invalidated",
                "epoch": request_epoch,
                "retry_after": float(server_cooldown_seconds) if server_cooldown_seconds else 0.05,
            }

        if response.status_code == 429:
            spotify_player_cache["rate_limit_count"] = int(
                spotify_player_cache.get("rate_limit_count") or 0
            ) + 1
            cooldown_seconds = server_cooldown_seconds
            spotify_player_cache["cooldown_status"] = 429
            spotify_player_cache["rate_limited_until"] = now + cooldown_seconds
            last_logged = float(spotify_player_cache.get("rate_limit_logged_at") or 0.0)
            if now - last_logged > 10:
                logger.warning(
                    "Spotify rate limit reached; preserving the last authoritative track and pausing player reads for %ss.",
                    cooldown_seconds,
                )
                spotify_player_cache["rate_limit_logged_at"] = now
            return {
                "status_code": 429,
                "payload": None,
                "text": response.text,
                "authoritative": False,
                "source": "spotify-429",
                "epoch": request_epoch,
                "retry_after": float(cooldown_seconds),
            }

        if response.status_code in {200, 204}:
            spotify_player_cache.update(
                {
                    "fetched_at": now,
                    "status_code": response.status_code,
                    "payload": normalized_payload,
                    "text": response.text,
                    "rate_limited_until": 0.0,
                    "rate_limit_logged_at": 0.0,
                    "rate_limit_count": 0,
                    "cooldown_status": 0,
                }
            )
            return {
                "status_code": response.status_code,
                "payload": normalized_payload,
                "text": response.text,
                "authoritative": True,
                "source": "spotify",
                "epoch": request_epoch,
                "retry_after": 0.0,
            }

        if response.status_code == 403:
            spotify_player_cache["rate_limited_until"] = now + 300
            spotify_player_cache["cooldown_status"] = 403
        elif response.status_code in {500, 502, 503, 504}:
            spotify_player_cache["rate_limited_until"] = now + 60
            spotify_player_cache["cooldown_status"] = response.status_code

    return {
        "status_code": response.status_code,
        "payload": normalized_payload,
        "text": response.text,
        "authoritative": response.status_code == 401,
        "source": "spotify-error",
        "epoch": request_epoch,
        "retry_after": 0.0,
    }


def read_spotify_up_next(access_token: str) -> dict[str, Any] | None:
    """Return the next item in the user's Spotify queue, or None.

    Used only to prepare the next track's colors ahead of time, so any limit,
    error or empty queue simply skips that preparation.
    """
    if spotify_server_rate_limit_remaining() or reserve_spotify_player_read(queue=True):
        return None
    response = requests.get(
        SPOTIFY_QUEUE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=8,
    )
    if response.status_code == 429:
        note_spotify_server_rate_limit(spotify_rate_limit_cooldown_seconds(response))
        return None
    if response.status_code != 200:
        return None
    try:
        queue = response.json().get("queue")
    except (ValueError, AttributeError):
        return None
    first = queue[0] if isinstance(queue, list) and queue else None
    return first if isinstance(first, dict) else None


def spotify_control_request(method: str, path: str = "", **kwargs: Any) -> dict[str, Any]:
    if not auth_ready():
        return {"ok": False, "message": "Add Spotify credentials first."}

    access_token = get_access_token()
    if not access_token:
        return {"ok": False, "message": "Connect Spotify first."}

    url = f"{SPOTIFY_PLAYER_URL}{path}"
    server_wait_seconds = spotify_server_rate_limit_remaining()
    if server_wait_seconds:
        return {
            "ok": False,
            "message": (
                "Spotify rate limit reached. Try again in about "
                f"{max(1, math.ceil(server_wait_seconds))}s."
            ),
        }

    try:
        response = requests.request(
            method,
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=12,
            **kwargs,
        )
    except requests.RequestException as exc:
        logger.warning("Spotify control request failed: %s", exc)
        return {"ok": False, "message": "Spotify is unreachable right now."}

    if response.status_code in {200, 202, 204}:
        clear_spotify_player_cache()
        return {"ok": True, "message": "Playback command sent."}
    if response.status_code == 401:
        return {"ok": False, "message": "Spotify session expired. Connect again."}
    if response.status_code == 403:
        return {
            "ok": False,
            "message": "Spotify rejected this command. Reconnect Spotify and make sure playback is active.",
        }
    if response.status_code == 404:
        return {"ok": False, "message": "No active Spotify device found."}
    if response.status_code == 429:
        now = time.monotonic()
        cooldown_seconds = spotify_rate_limit_cooldown_seconds(response)
        note_spotify_server_rate_limit(cooldown_seconds, now)
        with spotify_player_cache_lock:
            spotify_player_cache["rate_limit_count"] = int(
                spotify_player_cache.get("rate_limit_count") or 0
            ) + 1
            spotify_player_cache["cooldown_status"] = 429
            spotify_player_cache["rate_limited_until"] = max(
                float(spotify_player_cache.get("rate_limited_until") or 0.0),
                now + cooldown_seconds,
            )
        logger.warning(
            "Spotify rate limit reached by playback control; pausing API work for %ss.",
            cooldown_seconds,
        )
        return {
            "ok": False,
            "message": f"Spotify rate limit reached. Try again in about {cooldown_seconds}s.",
        }

    logger.warning("Spotify control error: %s %s", response.status_code, response.text)
    return {"ok": False, "message": "Spotify returned an error for this command."}
