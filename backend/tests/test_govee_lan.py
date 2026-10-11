"""Govee LAN adapter tests against fake bulbs (no real hardware).

Each FakeBulb is a pair of UDP sockets on its own loopback address
(127.0.0.2, 127.0.0.3, ...) playing the bulb's side of the Govee LAN API:
scans arrive on the scan port, commands on the control port, and replies go to
the adapter's reply port. RecordingSocket stands in for the adapter's socket
where exact send order, timing or multicast options matter.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from lights import govee
from lights.govee import (
    GoveeLanAdapter,
    decode_message,
    describe_status,
    bulb_color,
    full_scale_rgb,
    is_light_ip,
    led_rgb,
    scan_entry,
)
from lights.manager import MAX_FADE_STEPS_LOCAL, LightManager

SILENT_IP = "127.0.0.250"  # holds the fake ports; never answers


def _can_bind(ip: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((ip, 0))
        return True
    except OSError:
        return False


def turn(value: int) -> tuple[str, dict[str, Any]]:
    return ("turn", {"value": value})


def colorwc(red: int, green: int, blue: int, kelvin: int = 0) -> tuple[str, dict[str, Any]]:
    return ("colorwc", {"color": {"r": red, "g": green, "b": blue}, "colorTemInKelvin": kelvin})


def brightness(value: int) -> tuple[str, dict[str, Any]]:
    return ("brightness", {"value": value})


def parse(raw: bytes) -> tuple[str, dict[str, Any]]:
    # Strict, independent of the adapter's own decoder.
    message = json.loads(raw)["msg"]
    return message["cmd"], message["data"]


class FakeBulb:
    def __init__(
        self,
        ip: str,
        control_port: int,
        scan_port: int,
        reply_port: Callable[[], int | None],
        device_id: str,
        sku: str = "H6008",
        *,
        answer_scan: bool = True,
        answer_status: bool = True,
        report_ip: bool = True,
    ) -> None:
        self.ip = ip
        self.device_id = device_id
        self.sku = sku
        self.answer_scan = answer_scan
        self.answer_status = answer_status
        self.report_ip = report_ip
        self.junk_status_replies: list[bytes] = []
        self.status = {
            "onOff": 1,
            "brightness": 80,
            "color": {"r": 255, "g": 0, "b": 0},
            "colorTemInKelvin": 0,
        }
        self.scans = 0
        self._reply_port = reply_port
        self._condition = threading.Condition()
        self._received: list[tuple[float, bytes]] = []
        self._stop = threading.Event()
        self.control = self._bind(control_port)
        self.scan = self._bind(scan_port)
        self._threads = [
            threading.Thread(target=self._serve, args=(self.control, self._on_control), daemon=True),
            threading.Thread(target=self._serve, args=(self.scan, self._on_scan), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _bind(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.ip, port))
        sock.settimeout(0.05)
        return sock

    def _serve(self, sock: socket.socket, handle: Callable[[bytes, str], None]) -> None:
        while not self._stop.is_set():
            try:
                raw, (source_ip, _port) = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            handle(raw, source_ip)

    def _reply(self, sock: socket.socket, raw: bytes, source_ip: str) -> None:
        port = self._reply_port()
        if port:
            sock.sendto(raw, (source_ip, port))

    def _on_control(self, raw: bytes, source_ip: str) -> None:
        with self._condition:
            self._received.append((time.monotonic(), raw))
            self._condition.notify_all()
        try:
            cmd, _data = parse(raw)
        except (ValueError, KeyError, TypeError):
            return
        if cmd == "devStatus":
            for junk in self.junk_status_replies:
                self._reply(self.control, junk, source_ip)
            if self.answer_status:
                reply = json.dumps({"msg": {"cmd": "devStatus", "data": self.status}}).encode()
                self._reply(self.control, reply, source_ip)

    def _on_scan(self, raw: bytes, source_ip: str) -> None:
        with self._condition:
            self.scans += 1
            self._condition.notify_all()
        if parse(raw) != ("scan", {"account_topic": "reserve"}) or not self.answer_scan:
            return
        data: dict[str, Any] = {
            "device": self.device_id,
            "sku": self.sku,
            "bleVersionHard": "3.01.01",
            "bleVersionSoft": "1.03.01",
            "wifiVersionHard": "1.00.10",
            "wifiVersionSoft": "1.02.03",
        }
        if self.report_ip:
            data["ip"] = self.ip
        self._reply(self.scan, json.dumps({"msg": {"cmd": "scan", "data": data}}).encode(), source_ip)

    def raw_commands(self) -> list[bytes]:
        with self._condition:
            return [raw for _at, raw in self._received]

    def commands(self) -> list[tuple[str, dict[str, Any]]]:
        return [parse(raw) for raw in self.raw_commands()]

    def control_commands(self) -> list[tuple[str, dict[str, Any]]]:
        return [command for command in self.commands() if command[0] != "devStatus"]

    def wait_for_commands(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._received) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def settle(self, quiet: float = 0.1) -> None:
        """Wait until no new datagram has arrived for `quiet` seconds."""
        while True:
            before = len(self.raw_commands())
            time.sleep(quiet)
            if len(self.raw_commands()) == before:
                return

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1)
        self.control.close()
        self.scan.close()


class FakeNetwork:
    def __init__(self) -> None:
        # Reserve one control and one scan port number for every fake bulb.
        self._reserved = []
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((SILENT_IP, 0))
            self._reserved.append(sock)
        self.control_port = self._reserved[0].getsockname()[1]
        self.scan_port = self._reserved[1].getsockname()[1]
        self.bulbs: list[FakeBulb] = []
        self.adapters: list[GoveeLanAdapter] = []
        self.adapter: GoveeLanAdapter | None = None

    def add(self, device_id: str | None = None, **kwargs: Any) -> FakeBulb:
        index = len(self.bulbs) + 1
        bulb = FakeBulb(
            f"127.0.0.{index + 1}",
            self.control_port,
            self.scan_port,
            lambda: self.adapter.listening_port if self.adapter else None,
            device_id if device_id is not None else f"AA:BB:CC:DD:EE:FF:00:{index:02X}",
            **kwargs,
        )
        self.bulbs.append(bulb)
        return bulb

    def make_adapter(self, **kwargs: Any) -> GoveeLanAdapter:
        options = {
            "bind_host": "127.0.0.1",
            "reply_port": 0,
            "control_port": self.control_port,
            "scan_port": self.scan_port,
            "ip_refresh_seconds": None,
        }
        options.update(kwargs)
        self.adapter = GoveeLanAdapter(**options)
        self.adapters.append(self.adapter)
        return self.adapter

    def close(self) -> None:
        for adapter in self.adapters:
            adapter.close()
        for bulb in self.bulbs:
            bulb.close()
        for sock in self._reserved:
            sock.close()


class RecordingSocket:
    def __init__(self, responder: Callable[[bytes, tuple[str, int]], None] | None = None) -> None:
        self.responder = responder
        self.fail_with: OSError | None = None
        self.options: list[tuple[Any, ...]] = []
        self._lock = threading.Lock()
        self._sent: list[tuple[float, bytes, tuple[str, int]]] = []

    def sendto(self, payload: bytes, address: tuple[str, int]) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        with self._lock:
            self._sent.append((time.monotonic(), bytes(payload), address))
        if self.responder:
            self.responder(payload, address)
        return len(payload)

    def setsockopt(self, *args: Any) -> None:
        self.options.append(args)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 4002)

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        pass

    def sent(self, address: tuple[str, int] | None = None) -> list[tuple[float, bytes, tuple[str, int]]]:
        with self._lock:
            return [entry for entry in self._sent if address is None or entry[2] == address]

    def commands(self, ip: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return [
            parse(raw)
            for _at, raw, address in self.sent()
            if ip is None or address == (ip, govee.CONTROL_PORT)
        ]

    def clear(self) -> None:
        with self._lock:
            self._sent.clear()


def install(adapter: GoveeLanAdapter, fake: RecordingSocket) -> RecordingSocket:
    adapter._sock = fake  # type: ignore[assignment]
    adapter._listening = True
    return fake


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove deliberate delays; tests that check them set their own."""
    monkeypatch.setattr(govee, "DEFAULT_COMMAND_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(govee, "SEND_REPEAT_GAP_SECONDS", 0.0)
    monkeypatch.setattr(govee, "STATUS_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(govee, "IDENTIFY_BLINK_SECONDS", 0.01)


@pytest.fixture
def network(fast: None) -> Iterator[FakeNetwork]:
    if not _can_bind("127.0.0.2") or not _can_bind(SILENT_IP):
        pytest.skip("needs 127.0.0.x loopback aliases (available on Linux and Windows)")
    instance = FakeNetwork()
    yield instance
    instance.close()


@pytest.fixture
def recording(fast: None) -> Iterator[tuple[GoveeLanAdapter, RecordingSocket]]:
    adapter = GoveeLanAdapter(bind_host="127.0.0.1", reply_port=0, ip_refresh_seconds=None)
    fake = install(adapter, RecordingSocket())
    yield adapter, fake
    adapter._sock = None
    adapter.close()


# --- protocol helpers ---------------------------------------------------------


def test_decode_message_accepts_only_well_formed_messages() -> None:
    assert decode_message(b'{"msg":{"cmd":"devStatus","data":{"onOff":1}}}') == (
        "devStatus",
        {"onOff": 1},
    )
    for raw in (
        b"",
        b"not json",
        b"\xff\xfe\x00garbage",
        b"[]",
        b'{"msg": "nope"}',
        b'{"msg": {"cmd": 42, "data": {}}}',
        b'{"msg": {"cmd": "scan", "data": [1, 2]}}',
        b'{"msg": {"cmd": "scan"}}',
        b"[" * 100_000,
    ):
        assert decode_message(raw) is None, raw[:40]


def test_full_scale_rgb_keeps_channel_ratios() -> None:
    assert full_scale_rgb((127, 48, 0)) == (255, 96, 0)
    assert full_scale_rgb((10, 30, 50)) == (51, 153, 255)
    assert full_scale_rgb((255, 10, 0)) == (255, 10, 0)
    # No hue to keep: like the old HSV path, black becomes white.
    assert full_scale_rgb((0, 0, 0)) == (255, 255, 255)


def test_scan_entry_parsing() -> None:
    entry = scan_entry(
        {"ip": "192.168.1.23", "device": "1F:80:C5:32:32:36:72:4E", "sku": "H6008", "wifiVersionSoft": "1.02.03"},
        "192.168.1.23",
    )
    assert entry == {
        "device_id": "1F:80:C5:32:32:36:72:4E",
        "ip": "192.168.1.23",
        "sku": "H6008",
        "name": "Govee H6008 (724E)",
        "wifi_version": "1.02.03",
        "ble_version": "",
    }
    # Missing or bogus "ip": the reply's source address is used.
    assert scan_entry({"device": "AB", "sku": ""}, "10.0.0.7")["ip"] == "10.0.0.7"
    assert scan_entry({"device": "AB", "ip": "not-an-ip"}, "10.0.0.7")["ip"] == "10.0.0.7"
    assert scan_entry({"device": "AB"}, "10.0.0.7")["name"] == "Govee light (AB)"
    # No usable device id: cannot be deduped or saved.
    assert scan_entry({"sku": "H6008", "ip": "10.0.0.7"}, "10.0.0.7") is None
    assert scan_entry({"device": 1234}, "10.0.0.7") is None
    assert scan_entry({"device": "  "}, "10.0.0.7") is None


def test_ip_validation_and_status_text() -> None:
    assert is_light_ip("192.168.1.50")
    for bad in ("", "192.168.1", "192.168.1.256", "0.0.0.0", "239.255.255.250", "255.255.255.255", "host"):
        assert not is_light_ip(bad), bad
    assert describe_status({"onOff": 1, "brightness": 80}) == "Online (on, 80%)."
    assert describe_status({"onOff": 0, "brightness": 80}) == "Online (off)."
    assert describe_status({"onOff": 1, "brightness": "x"}) == "Online (on)."
    assert describe_status({}) == "Online."


# --- control commands ----------------------------------------------------------


def test_set_color_sends_exact_turn_colorwc_brightness_payloads(network: FakeNetwork) -> None:
    bulb = network.add()
    adapter = network.make_adapter()

    result = adapter.set_color({"name": "Desk", "ip": bulb.ip}, "#7f3000", 40, 3000)

    assert result.ok
    # Full scale (255, 96, 0), then decoded to linear LED drive levels.
    assert result.message == "Set to #ff1e00 at 40%."
    expected_burst = [turn(1), colorwc(255, 30, 0), brightness(40)]
    # By default every unacknowledged burst is sent twice to survive one drop.
    assert bulb.wait_for_commands(len(expected_burst) * 2)
    bulb.settle()
    assert bulb.commands() == expected_burst * 2
    assert bulb.raw_commands()[0] == b'{"msg":{"cmd":"turn","data":{"value":1}}}'
    assert bulb.raw_commands()[1] == (
        b'{"msg":{"cmd":"colorwc","data":{"color":{"r":255,"g":30,"b":0},"colorTemInKelvin":0}}}'
    )
    assert bulb.raw_commands()[2] == b'{"msg":{"cmd":"brightness","data":{"value":40}}}'


def test_turn_off_payload_and_next_color_turns_back_on(recording) -> None:
    adapter, sock = recording
    device = {"name": "Desk", "ip": "192.168.1.20"}
    adapter.set_color(device, "#ff0000", 50, 0)
    sock.clear()

    result = adapter.turn_off(device)
    assert result.ok and result.message == "Turned off."
    assert sock.commands() == [turn(0)] * govee.SEND_REPEAT_COUNT

    sock.clear()
    adapter.set_color(device, "#ff0000", 50, 0)
    assert sock.commands() == [turn(1), colorwc(255, 0, 0), brightness(50)] * govee.SEND_REPEAT_COUNT


def test_led_rgb_decodes_screen_gamma() -> None:
    # Pure channels are unchanged; mixed channels drop to their linear light.
    assert led_rgb((255, 0, 0)) == (255, 0, 0)
    assert led_rgb((255, 255, 255)) == (255, 255, 255)
    assert led_rgb((255, 128, 0)) == (255, 55, 0)


def test_bulb_color_sends_whites_as_color_temperature() -> None:
    assert bulb_color("#ffffff") == ((255, 255, 255), 5000)
    # Black and grey have no hue: neutral white, like the old full-scale path.
    assert bulb_color("#000000")[1] == 5000
    assert bulb_color("#808080")[1] == 5000
    _, cream = bulb_color("#faf0d7")
    _, cool = bulb_color("#ebf2ff")
    assert 2700 <= cream < 5000 < cool <= 6500
    # Real colors, including pastels, stay on the RGB LEDs.
    for color in ("#ffc8d2", "#aac8ff", "#ff8c28"):
        assert bulb_color(color)[1] == 0, color


def test_white_is_sent_with_kelvin_and_not_resent(recording) -> None:
    adapter, sock = recording
    device = {"name": "Desk", "ip": "192.168.1.20"}
    result = adapter.set_color(device, "#ffffff", 50, 0)
    # White LEDs outshine the RGB ones, so white runs at half brightness.
    assert result.message == "Set to 5000 K white at 25%."
    assert sock.commands() == [turn(1), colorwc(255, 255, 255, 5000), brightness(25)] * govee.SEND_REPEAT_COUNT

    sock.clear()
    assert adapter.set_color(device, "#ffffff", 50, 0).message == "Already 5000 K white at 25%."
    assert sock.commands() == []

    # Leaving white for a color switches the bulb back to its RGB LEDs.
    adapter.set_color(device, "#ff0000", 50, 0)
    assert sock.commands() == [colorwc(255, 0, 0), brightness(50)] * govee.SEND_REPEAT_COUNT


def test_brightness_is_clamped_to_govee_range(recording) -> None:
    adapter, sock = recording
    adapter.set_color({"ip": "192.168.1.20"}, "#00ff00", 0, 0)
    assert brightness(1) in sock.commands()
    sock.clear()
    adapter.set_color({"ip": "192.168.1.20"}, "#00ff00", 150, 0)
    assert sock.commands() == [brightness(100)] * govee.SEND_REPEAT_COUNT


def test_unchanged_values_are_not_resent(recording) -> None:
    adapter, sock = recording
    device = {"name": "Desk", "ip": "192.168.1.20"}
    adapter.set_color(device, "#ff0000", 50, 0)
    sock.clear()

    # A darker shade of the same hue is the same full-scale color.
    again = adapter.set_color(device, "#800000", 50, 0)
    assert again.ok
    assert again.message == "Already #ff0000 at 50%."
    assert sock.commands() == []

    adapter.set_color(device, "#ff0000", 70, 0)
    assert sock.commands() == [brightness(70)] * govee.SEND_REPEAT_COUNT

    sock.clear()
    adapter.set_color(device, "#00ff00", 70, 0)
    assert sock.commands() == [colorwc(0, 255, 0)] * govee.SEND_REPEAT_COUNT


def test_full_state_is_reasserted_after_refresh_window(recording, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, sock = recording
    device = {"ip": "192.168.1.20"}
    adapter.set_color(device, "#ff0000", 50, 0)
    monkeypatch.setattr(govee, "STATE_REFRESH_SECONDS", 0.0)
    sock.clear()

    adapter.set_color(device, "#ff0000", 50, 0)

    assert sock.commands() == [turn(1), colorwc(255, 0, 0), brightness(50)] * govee.SEND_REPEAT_COUNT


def test_resend_count_constant_controls_duplicates(recording, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, sock = recording
    monkeypatch.setattr(govee, "SEND_REPEAT_COUNT", 1)
    adapter.set_color({"ip": "192.168.1.20"}, "#0000ff", 30, 0)
    assert sock.commands() == [turn(1), colorwc(0, 0, 255), brightness(30)]


def test_bursts_to_one_bulb_respect_update_speed(recording) -> None:
    adapter, sock = recording
    first = {"name": "A", "ip": "192.168.1.20", "_update_speed_ms": 300}
    second = {"name": "B", "ip": "192.168.1.21", "_update_speed_ms": 300}

    adapter.set_color(first, "#ff0000", 50, 0)
    adapter.set_color(second, "#ff0000", 50, 0)
    adapter.set_color(first, "#00ff00", 50, 0)

    first_times = [at for at, _raw, address in sock.sent() if address[0] == first["ip"]]
    second_times = [at for at, _raw, address in sock.sent() if address[0] == second["ip"]]
    burst = 3 * govee.SEND_REPEAT_COUNT
    # Same bulb: the next burst waits out the update interval.
    assert first_times[burst] - first_times[0] >= 0.29
    # Different bulbs are independent and are not delayed by each other.
    assert second_times[0] - first_times[0] < 0.2


def test_send_failure_is_reported_and_forces_full_resend(recording) -> None:
    adapter, sock = recording
    device = {"name": "Desk", "ip": "192.168.1.20"}
    adapter.set_color(device, "#ff0000", 50, 0)

    sock.fail_with = OSError(101, "Network is unreachable")
    result = adapter.set_color(device, "#00ff00", 50, 0)
    assert not result.ok
    assert "unreachable" in result.message
    assert adapter._sock is None, "a failing socket is dropped and recreated on next use"

    sock.fail_with = None
    sock.clear()
    install(adapter, sock)
    adapter.set_color(device, "#00ff00", 50, 0)
    assert sock.commands() == [turn(1), colorwc(0, 255, 0), brightness(50)] * govee.SEND_REPEAT_COUNT


def test_missing_or_invalid_ip_fails_without_raising(recording) -> None:
    adapter, sock = recording
    for device, expected in (
        ({"name": "No IP"}, "No IP address"),
        ({"name": "Bad IP", "ip": "192.168.1.300"}, "Invalid IP address"),
    ):
        for result in (
            adapter.set_color(device, "#ff0000", 50, 0),
            adapter.turn_off(device),
            adapter.status(device),
        ):
            assert not result.ok
            assert expected in result.message
    assert not adapter.set_color({"ip": "192.168.1.20"}, "not-a-color", 50, 0).ok
    assert sock.sent() == []


def test_concurrent_commands_never_interleave_one_bulbs_bursts(recording) -> None:
    adapter, sock = recording
    ips = [f"192.168.1.{20 + index}" for index in range(6)]
    calls_per_thread = 25
    expected: dict[str, int] = {ip: 0 for ip in ips}
    expected_lock = threading.Lock()
    failures: list[str] = []

    def worker(thread_index: int) -> None:
        for call in range(calls_per_thread):
            ip = ips[(thread_index + call) % len(ips)]
            # Full-scale and still unique per call after LED gamma decoding
            # (tiny channel values would collapse to 0/1), so every call
            # changes color.
            color = f"#ff{40 + call * 8:02x}{thread_index * 30:02x}"
            result = adapter.set_color({"name": ip, "ip": ip}, color, 60, 0)
            if not result.ok:
                failures.append(result.message)
            with expected_lock:
                expected[ip] += 1

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    for ip in ips:
        colors = [data["color"] for cmd, data in sock.commands(ip) if cmd == "colorwc"]
        assert len(colors) == expected[ip] * govee.SEND_REPEAT_COUNT
        # Each burst's resend directly follows it: c1, c1, c2, c2, ...
        for index in range(0, len(colors), govee.SEND_REPEAT_COUNT):
            assert all(color == colors[index] for color in colors[index : index + govee.SEND_REPEAT_COUNT])


# --- status ----------------------------------------------------------------------


def test_status_reports_online_from_devstatus(network: FakeNetwork) -> None:
    bulb = network.add()
    adapter = network.make_adapter()

    result = adapter.status({"name": "Desk", "ip": bulb.ip})
    assert result.ok
    assert result.message == "Online (on, 80%)."
    assert bulb.commands()[0] == ("devStatus", {})

    bulb.status = {"onOff": 0, "brightness": 80}
    assert adapter.status({"name": "Desk", "ip": bulb.ip}).message == "Online (off)."


def test_status_times_out_when_bulb_is_silent(network: FakeNetwork) -> None:
    bulb = network.add(answer_status=False)
    adapter = network.make_adapter()

    started = time.monotonic()
    result = adapter.status({"name": "Desk", "ip": bulb.ip})
    elapsed = time.monotonic() - started

    assert not result.ok
    assert f"No reply from {bulb.ip}" in result.message
    assert 0.35 <= elapsed < 1.5
    # The query is re-sent once inside the timeout window.
    assert bulb.commands() == [("devStatus", {}), ("devStatus", {})]


def test_malformed_replies_are_ignored_and_receiver_survives(network: FakeNetwork) -> None:
    bulb = network.add(answer_status=False)
    bulb.junk_status_replies = [
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'{"msg": "nope"}',
        b'{"msg": {"cmd": "devStatus", "data": [1, 2]}}',
        b'{"msg": {"cmd": 7, "data": {}}}',
        b'{"msg": {"cmd": "scan", "data": {"sku": "H6008"}}}',
    ]
    adapter = network.make_adapter()

    assert not adapter.status({"ip": bulb.ip}).ok
    assert adapter._receiver is not None and adapter._receiver.is_alive()

    bulb.answer_status = True
    assert adapter.status({"ip": bulb.ip}).ok


def test_status_replies_are_matched_by_source_ip(network: FakeNetwork) -> None:
    silent = network.add(answer_status=False)
    answering = network.add()
    adapter = network.make_adapter()
    results: dict[str, Any] = {}

    def check(bulb: FakeBulb) -> None:
        results[bulb.ip] = adapter.status({"name": bulb.ip, "ip": bulb.ip})

    threads = [threading.Thread(target=check, args=(bulb,)) for bulb in (silent, answering)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results[answering.ip].ok
    assert not results[silent.ip].ok


def test_manager_drives_govee_bulbs_end_to_end(network: FakeNetwork) -> None:
    top_1 = network.add()
    top_2 = network.add()
    manager = LightManager()
    adapter = network.make_adapter()
    manager.adapters["govee"] = manager.govee = adapter
    settings = {
        "lights_enabled": True,
        "brightness_min": 60,
        "brightness_max": 60,
        "update_speed_ms": 350,
        "devices": [
            {"name": "top 1", "adapter": "govee", "device_id": top_1.device_id, "ip": top_1.ip},
            # No adapter: falls back to the default (govee).
            {"name": "top 2", "device_id": top_2.device_id, "ip": top_2.ip},
        ],
    }
    try:
        summary = manager.apply_palette(settings, ["#ff0000", "#0000ff"], force=True)
        assert summary["connected"]
        for bulb, rgb in ((top_1, (255, 0, 0)), (top_2, (0, 0, 255))):
            bulb.settle()
            # Checkerboard: top 1 takes the dominant color, top 2 the runner-up.
            assert bulb.commands() == [turn(1), colorwc(*rgb), brightness(60)] * govee.SEND_REPEAT_COUNT

        status = manager.summary(settings)
        assert [device["ok"] for device in status["devices"]] == [True, True]
    finally:
        manager._executor.shutdown(wait=True)


def test_manager_defaults_to_govee_and_local_fade_steps() -> None:
    manager = LightManager()
    try:
        devices = manager.enabled_devices(
            {"update_speed_ms": 500, "devices": [{"name": "a", "ip": "192.168.1.2"}, {"enabled": False}]}
        )
        assert [device["adapter"] for device in devices] == ["govee"]
        assert devices[0]["_update_speed_ms"] == 500
        assert manager._adapter_for(devices[0]) is manager.govee
        assert manager.adapters["govee-lan"] is manager.govee
        assert manager._fade_steps(0) == 1
        assert manager._fade_steps(1000) == 2
        assert manager._fade_steps(60_000) == MAX_FADE_STEPS_LOCAL
    finally:
        manager._executor.shutdown(wait=True)


# --- discovery -------------------------------------------------------------------


def test_discover_parses_and_dedupes_scan_replies(network: FakeNetwork) -> None:
    first = network.add("1F:80:C5:32:32:36:72:4E")
    no_ip_field = network.add("1F:80:C5:32:32:36:99:01", report_ip=False)
    nameless = network.add("", answer_status=False)  # reply without a device id
    adapter = network.make_adapter()

    result = adapter.discover([first.ip, no_ip_field.ip, nameless.ip], duration=1.0)

    assert result["ok"] and result["error"] is None
    assert result["message"] == "Found 2 Govee lights."
    assert result["tips"] == []
    assert [(entry["device_id"], entry["ip"], entry["sku"], entry["name"]) for entry in result["devices"]] == [
        (first.device_id, first.ip, "H6008", "Govee H6008 (724E)"),
        (no_ip_field.device_id, no_ip_field.ip, "H6008", "Govee H6008 (9901)"),
    ]
    # The scan was re-sent, so each bulb answered more than once.
    assert first.scans >= 2 and no_ip_field.scans >= 2


def test_discover_multicasts_on_every_interface(recording, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, sock = recording
    monkeypatch.setattr(govee, "local_ipv4_addresses", lambda: ["192.168.1.10", "10.0.0.5"])

    def reply(device: str, ip: str) -> bytes:
        return json.dumps({"msg": {"cmd": "scan", "data": {"device": device, "sku": "H6008", "ip": ip}}}).encode()

    def respond(payload: bytes, address: tuple[str, int]) -> None:
        if address == (govee.MULTICAST_GROUP, govee.SCAN_PORT):
            adapter._handle_datagram(reply("AA:01", "192.168.1.31"), "192.168.1.31")
            adapter._handle_datagram(reply("AA:02", "192.168.1.32"), "192.168.1.32")
            adapter._handle_datagram(b"garbage", "192.168.1.33")

    sock.responder = respond
    result = adapter.discover(duration=0.2)

    assert [(entry["device_id"], entry["ip"]) for entry in result["devices"]] == [
        ("AA:01", "192.168.1.31"),
        ("AA:02", "192.168.1.32"),
    ]
    sends = sock.sent()
    assert sends and all(address == (govee.MULTICAST_GROUP, govee.SCAN_PORT) for _at, _raw, address in sends)
    assert all(parse(raw) == ("scan", {"account_topic": "reserve"}) for _at, raw, _address in sends)
    # OS default route, then each LAN interface explicitly.
    assert len(sends) == 3
    interfaces = [args[2] for args in sock.options if args[1] == socket.IP_MULTICAST_IF]
    assert interfaces == [
        socket.inet_aton("192.168.1.10"),
        socket.inet_aton("10.0.0.5"),
        socket.inet_aton("0.0.0.0"),
    ]


def test_discover_with_no_replies_returns_tips(recording) -> None:
    adapter, _sock = recording
    result = adapter.discover(duration=0.2)
    assert result == {
        "ok": True,
        "error": None,
        "message": "No Govee lights answered.",
        "devices": [],
        "tips": govee.DISCOVERY_TIPS,
    }


def test_discover_by_ip_falls_back_to_devstatus(network: FakeNetwork) -> None:
    bulb = network.add(answer_scan=False)
    silent = network.add(answer_scan=False, answer_status=False)
    adapter = network.make_adapter()

    result = adapter.discover([bulb.ip, silent.ip], duration=0.3)

    assert [(entry["device_id"], entry["ip"]) for entry in result["devices"]] == [("", bulb.ip)]


def test_discover_rejects_only_invalid_ips(recording) -> None:
    adapter, sock = recording
    result = adapter.discover(["999.1.1.1", "nope"], duration=0.2)
    assert not result["ok"] and result["error"] == "invalid_ip"
    assert sock.sent() == []


def test_busy_reply_port_is_reported_and_control_still_works(network: FakeNetwork) -> None:
    bulb = network.add()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))
    busy_port = blocker.getsockname()[1]
    adapter = network.make_adapter(reply_port=busy_port)
    try:
        result = adapter.discover([bulb.ip], duration=0.2)
        assert not result["ok"]
        assert result["error"] == "listen_failed"
        assert str(busy_port) in result["message"]
        assert str(busy_port) in result["tips"][0]

        status = adapter.status({"ip": bulb.ip})
        assert not status.ok and str(busy_port) in status.message

        assert adapter.set_color({"ip": bulb.ip}, "#ff0000", 50, 0).ok
        assert bulb.wait_for_commands(1)
        assert ("colorwc", {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0}) in bulb.commands()
    finally:
        blocker.close()

    # Once the port is free again the adapter starts listening on its own.
    assert adapter.discover([bulb.ip], duration=0.5)["devices"][0]["device_id"] == bulb.device_id


def test_scan_learned_ip_replaces_stale_saved_ip(network: FakeNetwork) -> None:
    bulb = network.add()
    adapter = network.make_adapter()
    assert adapter.discover([bulb.ip], duration=0.5)["devices"]

    stale = {"name": "Desk", "device_id": bulb.device_id, "ip": SILENT_IP}
    assert adapter.set_color(stale, "#00ff00", 50, 0).ok
    assert bulb.wait_for_commands(3)
    assert colorwc(0, 255, 0) in bulb.commands()


def test_background_ip_refresh_scan_is_rate_limited(fast: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(govee, "local_ipv4_addresses", lambda: [])
    adapter = GoveeLanAdapter(bind_host="127.0.0.1", reply_port=0, ip_refresh_seconds=60)
    sock = install(adapter, RecordingSocket())
    group = (govee.MULTICAST_GROUP, govee.SCAN_PORT)
    try:
        device = {"ip": "192.168.1.20", "device_id": "AA:01"}
        adapter.set_color(device, "#ff0000", 50, 0)
        adapter.set_color(device, "#00ff00", 50, 0)
        deadline = time.monotonic() + 2
        while not sock.sent(group) and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        assert len(sock.sent(group)) == 1

        # Devices without a device id cannot be re-found, so they never trigger it.
        adapter._last_refresh_scan_at = None
        sock.clear()
        adapter.set_color({"ip": "192.168.1.21"}, "#ff0000", 50, 0)
        time.sleep(0.1)
        assert sock.sent(group) == []
    finally:
        adapter._sock = None
        adapter.close()


# --- identify --------------------------------------------------------------------


def test_identify_blinks_and_reports_reachability(network: FakeNetwork) -> None:
    bulb = network.add()
    adapter = network.make_adapter()

    result = adapter.identify({"name": "Desk", "ip": bulb.ip})

    assert result["ok"] and result["reachable"]
    bulb.settle()
    repeat = govee.SEND_REPEAT_COUNT
    on_off = ([turn(0)] * repeat + [turn(1)] * repeat) * govee.IDENTIFY_BLINKS
    assert bulb.commands()[0] == ("devStatus", {})
    assert bulb.control_commands() == [turn(1), colorwc(*govee.IDENTIFY_RGB), brightness(100)] * repeat + on_off

    # Afterwards the next real color re-sends the full state.
    before = len(bulb.commands())
    assert adapter.set_color({"ip": bulb.ip}, "#ff0000", 40, 0).ok
    bulb.settle()
    assert bulb.commands()[before:] == [turn(1), colorwc(255, 0, 0), brightness(40)] * repeat


def test_identify_still_blinks_when_bulb_does_not_answer(network: FakeNetwork) -> None:
    bulb = network.add(answer_status=False)
    adapter = network.make_adapter()

    result = adapter.identify({"name": "Desk", "ip": bulb.ip})

    assert result["ok"] and not result["reachable"]
    assert "didn't answer" in result["message"]
    bulb.settle()
    assert turn(0) in bulb.control_commands()


def test_identify_rejects_missing_ip(recording) -> None:
    adapter, sock = recording
    result = adapter.identify({"name": "Nowhere"})
    assert result == {"ok": False, "reachable": False, "message": result["message"]}
    assert "No IP address" in result["message"]
    assert sock.sent() == []
