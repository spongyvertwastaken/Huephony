from __future__ import annotations

import time
from collections import deque
from typing import Any

import spotify_client


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""
        self.content = b""

    def json(self) -> dict[str, Any]:
        return {}


def configure_control_auth(monkeypatch) -> None:
    monkeypatch.setattr(spotify_client, "auth_ready", lambda: True)
    monkeypatch.setattr(spotify_client, "get_access_token", lambda: "token")
    monkeypatch.setattr(spotify_client, "spotify_server_rate_limited_until", 0.0)


def test_player_reads_keep_their_own_bounded_rolling_budget(monkeypatch) -> None:
    monkeypatch.setattr(spotify_client, "spotify_player_read_request_times", deque())

    for _ in range(spotify_client.SPOTIFY_MAX_PLAYER_READS_PER_WINDOW):
        assert spotify_client.reserve_spotify_player_read(now=100.0) == 0

    assert spotify_client.reserve_spotify_player_read(now=100.0) == spotify_client.SPOTIFY_REQUEST_WINDOW_SECONDS
    assert len(spotify_client.spotify_player_read_request_times) == spotify_client.SPOTIFY_MAX_PLAYER_READS_PER_WINDOW

    # Once the rolling window expires, automatic/reconciliation reads resume.
    assert spotify_client.reserve_spotify_player_read(
        now=100.0 + spotify_client.SPOTIFY_REQUEST_WINDOW_SECONDS
    ) == 0
    assert len(spotify_client.spotify_player_read_request_times) == 1


def test_priority_reconciliation_reads_have_a_separate_bounded_reserve(monkeypatch) -> None:
    monkeypatch.setattr(
        spotify_client,
        "spotify_player_read_request_times",
        deque([100.0] * spotify_client.SPOTIFY_MAX_PLAYER_READS_PER_WINDOW),
    )
    monkeypatch.setattr(spotify_client, "spotify_priority_player_read_request_times", deque())
    monkeypatch.setattr(spotify_client, "spotify_validation_player_read_request_times", deque())

    assert spotify_client.reserve_spotify_player_read(now=100.0) == spotify_client.SPOTIFY_REQUEST_WINDOW_SECONDS
    for _ in range(spotify_client.SPOTIFY_MAX_PRIORITY_PLAYER_READS_PER_WINDOW):
        assert spotify_client.reserve_spotify_player_read(now=100.0, priority=True) == 0

    assert (
        spotify_client.reserve_spotify_player_read(now=100.0, priority=True)
        == spotify_client.SPOTIFY_REQUEST_WINDOW_SECONDS
    )
    assert len(spotify_client.spotify_player_read_request_times) == spotify_client.SPOTIFY_MAX_PLAYER_READS_PER_WINDOW
    assert (
        len(spotify_client.spotify_priority_player_read_request_times)
        == spotify_client.SPOTIFY_MAX_PRIORITY_PLAYER_READS_PER_WINDOW
    )

    # Reconciliation probes cannot consume the separate slot required after
    # artwork processing, and validation has its own finite cap.
    for _ in range(spotify_client.SPOTIFY_MAX_VALIDATION_PLAYER_READS_PER_WINDOW):
        assert spotify_client.reserve_spotify_player_read(now=100.0, validation=True) == 0
    assert (
        spotify_client.reserve_spotify_player_read(now=100.0, validation=True)
        == spotify_client.SPOTIFY_REQUEST_WINDOW_SECONDS
    )


def test_rapid_controls_are_not_rejected_by_a_full_player_read_budget(monkeypatch) -> None:
    configure_control_auth(monkeypatch)
    now = time.monotonic()
    full_read_budget = deque(
        [now] * spotify_client.SPOTIFY_MAX_PLAYER_READS_PER_WINDOW
    )
    monkeypatch.setattr(spotify_client, "spotify_player_read_request_times", full_read_budget)
    monkeypatch.setattr(spotify_client, "clear_spotify_player_cache", lambda: None)

    calls: list[tuple[str, str]] = []

    def successful_control(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((method, url))
        return FakeResponse(204)

    monkeypatch.setattr(spotify_client.requests, "request", successful_control)

    results = [spotify_client.spotify_control_request("POST", "/next") for _ in range(10)]

    assert all(result["ok"] for result in results)
    assert len(calls) == 10
    assert len(spotify_client.spotify_player_read_request_times) == spotify_client.SPOTIFY_MAX_PLAYER_READS_PER_WINDOW


def test_real_spotify_429_pauses_both_control_and_read_lanes(monkeypatch) -> None:
    configure_control_auth(monkeypatch)
    monkeypatch.setattr(spotify_client, "spotify_player_read_request_times", deque())
    monkeypatch.setattr(spotify_client, "spotify_player_cache", dict(spotify_client.spotify_player_cache))
    monkeypatch.setattr(spotify_client.time, "monotonic", lambda: 100.0)

    calls = 0

    def rate_limited_control(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(429, headers={"Retry-After": "7"})

    monkeypatch.setattr(spotify_client.requests, "request", rate_limited_control)

    first = spotify_client.spotify_control_request("POST", "/next")
    second = spotify_client.spotify_control_request("POST", "/next")

    assert first["ok"] is False
    assert second["ok"] is False
    assert "9s" in first["message"]
    assert "9s" in second["message"]
    assert calls == 1

    def unexpected_read(*args: Any, **kwargs: Any) -> FakeResponse:
        raise AssertionError("player read ignored the real Spotify cooldown")

    monkeypatch.setattr(spotify_client.requests, "get", unexpected_read)
    read = spotify_client.get_spotify_player_state_read("token", force_refresh=True)

    assert read["authoritative"] is False
    assert read["source"] == "spotify-429"
    assert read["retry_after"] == 9.0
    assert len(spotify_client.spotify_player_read_request_times) == 0
