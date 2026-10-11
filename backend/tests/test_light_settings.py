from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from routers import lights as light_routes


class FakeLightManager:
    def __init__(self) -> None:
        self.discover_calls: list[Any] = []
        self.identify_calls: list[dict[str, Any]] = []

    def discover_lights(self, ips: list[str] | None = None) -> dict[str, Any]:
        self.discover_calls.append(ips)
        return {"ok": True, "error": None, "message": "No Govee lights answered.", "devices": [], "tips": ["tip"]}

    def identify_light(self, device: dict[str, Any]) -> dict[str, Any]:
        self.identify_calls.append(device)
        return {"ok": True, "reachable": True, "message": "ok"}


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> FakeLightManager:
    fake = FakeLightManager()
    monkeypatch.setattr(light_routes, "light_manager", fake)
    return fake


def test_discover_endpoint_scans_network_or_given_ips(fake_manager: FakeLightManager) -> None:
    assert light_routes.lights_discover(None)["tips"] == ["tip"]
    light_routes.lights_discover({})
    light_routes.lights_discover({"ips": [" 192.168.1.50 "]})

    assert fake_manager.discover_calls == [None, None, ["192.168.1.50"]]


@pytest.mark.parametrize(
    "payload",
    [
        {"ips": "192.168.1.50"},
        {"ips": []},
        {"ips": ["not-an-ip"]},
        {"ips": [5]},
        {"ips": ["239.255.255.250"]},
        {"ips": ["192.168.1.1"] * (light_routes.MAX_DISCOVERY_IPS + 1)},
    ],
)
def test_discover_endpoint_rejects_bad_ips(fake_manager: FakeLightManager, payload: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as error:
        light_routes.lights_discover(payload)
    assert error.value.status_code == 400
    assert fake_manager.discover_calls == []


def test_identify_endpoint_builds_device_from_body(fake_manager: FakeLightManager) -> None:
    result = light_routes.lights_identify({"ip": "192.168.1.50 ", "name": "Lamp", "device_id": "AA:01"})

    assert result["reachable"]
    assert fake_manager.identify_calls == [{"name": "Lamp", "ip": "192.168.1.50", "device_id": "AA:01"}]


@pytest.mark.parametrize("payload", [{}, {"ip": "nope"}, {"ip": 12}, {"ip": "0.0.0.0"}])
def test_identify_endpoint_rejects_bad_ip(fake_manager: FakeLightManager, payload: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as error:
        light_routes.lights_identify(payload)
    assert error.value.status_code == 400
    assert fake_manager.identify_calls == []
