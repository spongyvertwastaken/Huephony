from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .base import (
    LightResult,
    bulb_white_kelvin,
    device_name,
    hex_from_rgb,
    hex_to_rgb,
    srgb_to_linear,
)

logger = logging.getLogger("spotify-lights")

# Govee LAN API ("LAN Control" toggle in the Govee Home app). Bulbs listen for
# scans on the multicast group, accept JSON commands on the control port, and
# send every reply (scan and devStatus) to the reply port on the asking host.
MULTICAST_GROUP = "239.255.255.250"
SCAN_PORT = 4001
REPLY_PORT = 4002
CONTROL_PORT = 4003

# devStatus is the only control command that answers. The request is resent
# once halfway through the window to cover a dropped request or reply.
STATUS_TIMEOUT_SECONDS = 1.2
# Discovery listens this long, re-sending the scan every resend interval.
DISCOVERY_SECONDS = 2.5
DISCOVERY_RESEND_SECONDS = 0.8
# turn / brightness / colorwc are never acknowledged, so one dropped datagram
# would silently leave a bulb on the wrong color. Each control burst goes out
# SEND_REPEAT_COUNT times, SEND_REPEAT_GAP_SECONDS apart. Every command sets
# absolute state, so a duplicate that also arrives is harmless. 1 disables.
SEND_REPEAT_COUNT = 2
SEND_REPEAT_GAP_SECONDS = 0.06
# Unchanged power/color/brightness values are not resent, but the full state is
# re-asserted at least this often. That repairs a bulb switched off from the
# Govee app, or a burst lost despite the resend.
STATE_REFRESH_SECONDS = 30.0
# Minimum spacing between control bursts to one bulb. Follows update_speed_ms
# when the manager supplies it as _update_speed_ms.
DEFAULT_COMMAND_INTERVAL_SECONDS = 0.35
MIN_COMMAND_INTERVAL_SECONDS = 0.1
# While lights are driven, a background scan refreshes device_id -> ip at most
# this often, so a bulb the router gave a new address keeps working.
IP_REFRESH_SCAN_SECONDS = 120.0
# A bulb's white LEDs are far brighter than its RGB LEDs at the same level, so
# a bulb showing white runs at half the configured brightness.
WHITE_BRIGHTNESS_SCALE = 0.5
# Identify: a bright, unmistakable color, then a few on/off blinks.
IDENTIFY_RGB = (0, 170, 255)
IDENTIFY_BLINKS = 3
IDENTIFY_BLINK_SECONDS = 0.4

RECEIVE_POLL_SECONDS = 0.25
RECEIVE_BUFFER_BYTES = 4096

DISCOVERY_TIPS = [
    "In the Govee Home app, tap each bulb, tap the gear icon (Settings), and turn on LAN Control.",
    "Make sure the bulbs are powered on and show as online in the Govee Home app.",
    "Connect this computer to the same home Wi-Fi network as the bulbs (not a guest network, and with any VPN turned off).",
    "If Windows asked whether to allow Huephony on the network, choose Allow. In Windows Wi-Fi settings, the network should be set to Private.",
    "Some routers block the search this app uses (settings named multicast, IGMP snooping, or client/AP isolation). You can still add a bulb by its IP address, found in your router's list of connected devices.",
]


def encode_message(cmd: str, data: dict[str, Any]) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode("utf-8")


def decode_message(raw: bytes) -> tuple[str, dict[str, Any]] | None:
    """Return (cmd, data) for a well-formed Govee message, else None."""
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    message = decoded.get("msg") if isinstance(decoded, dict) else None
    if not isinstance(message, dict):
        return None
    cmd = message.get("cmd")
    data = message.get("data")
    if not isinstance(cmd, str) or not isinstance(data, dict):
        return None
    return cmd, data


def is_light_ip(value: str) -> bool:
    """True for an IPv4 address a bulb could have (not 0.0.0.0/multicast/broadcast)."""
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        return False
    return not (
        address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
    )


def full_scale_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Scale a color so its brightest channel is 255, keeping channel ratios.

    Brightness is sent separately as the "brightness" command instead of being
    baked into the RGB values:
    - Baking dims by shrinking the 8-bit channels. At 20% a deep orange
      (255, 96, 0) becomes (51, 19, 0), and near the bottom of the breathing
      range small channels round to 0 and the hue shifts. The bulb's own
      dimmer keeps the full 0-255 resolution for the color itself.
    - RGBWW firmware picks its own LED mix for a color. A dimmed white sent as
      RGB (51, 51, 51) is a grey "color", not white at 20%, and may not be
      rendered the way the brightness command renders it.
    - It matches the previous adapters (hue/saturation from the color, level
      from the configured brightness), so brightness_min/max keep their
      meaning and dark album colors still light the room.
    Pure black has no hue; like the old HSV path it becomes white.
    """
    peak = max(rgb)
    if peak <= 0:
        return (255, 255, 255)
    scale = 255 / peak
    red, green, blue = (min(255, int(round(channel * scale))) for channel in rgb)
    return (red, green, blue)


def led_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Drive levels that make the bulb's RGB LEDs match an sRGB color.

    Screen values are gamma-encoded: sRGB 128 emits about 22% of full light.
    The bulb's channels are linear PWM, so sending 128 lights that LED at 50%
    and every mixed color drifts toward its brighter channel (orange turns
    yellow, deep red turns pink, everything looks pastel). Decoding to linear
    light first keeps the mix the artwork shows.
    """
    red, green, blue = (int(round(255 * srgb_to_linear(channel / 255))) for channel in rgb)
    return (red, green, blue)


def bulb_color(color: str) -> tuple[tuple[int, int, int], int]:
    """(rgb, colorTemInKelvin) for a colorwc command.

    Whites, creams and greys use the bulb's white LEDs at a matching
    temperature (colorwc with a non-zero colorTemInKelvin), which looks far
    better than mixing white from RGB; the RGB sent alongside is a plain
    full-scale version. Other colors go to the RGB LEDs at full scale (see
    full_scale_rgb), decoded to linear drive levels (see led_rgb).
    """
    rgb = hex_to_rgb(color)
    kelvin = bulb_white_kelvin(rgb)
    if kelvin is not None:
        return full_scale_rgb(rgb), kelvin
    return led_rgb(full_scale_rgb(rgb)), 0


def suggested_name(sku: str, device_id: str) -> str:
    tail = "".join(char for char in device_id if char.isalnum())[-4:].upper()
    label = f"Govee {sku}" if sku else "Govee light"
    return f"{label} ({tail})" if tail else label


def scan_entry(data: dict[str, Any], source_ip: str) -> dict[str, Any] | None:
    """Normalize one scan reply. The device id is required for dedupe."""
    device_id = data.get("device")
    if not isinstance(device_id, str) or not device_id.strip():
        return None
    device_id = device_id.strip()
    # Some models omit "ip"; the address the reply came from is then used.
    reported_ip = str(data.get("ip") or "").strip()
    ip = reported_ip if is_light_ip(reported_ip) else source_ip
    sku = str(data.get("sku") or "").strip()
    return {
        "device_id": device_id,
        "ip": ip,
        "sku": sku,
        "name": suggested_name(sku, device_id),
        "wifi_version": str(data.get("wifiVersionSoft") or ""),
        "ble_version": str(data.get("bleVersionSoft") or ""),
    }


def describe_status(data: dict[str, Any]) -> str:
    power = data.get("onOff")
    if power in (0, "0"):
        return "Online (off)."
    if power in (1, "1"):
        brightness = data.get("brightness")
        if isinstance(brightness, (int, float)) and not isinstance(brightness, bool):
            return f"Online (on, {int(brightness)}%)."
        return "Online (on)."
    return "Online."


def local_ipv4_addresses() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses."""
    candidates: list[str] = []
    try:
        candidates += [
            str(info[4][0])
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        ]
    except OSError:
        pass
    try:
        # Connecting a UDP socket only selects a route; nothing is sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((MULTICAST_GROUP, SCAN_PORT))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass

    found: list[str] = []
    for candidate in candidates:
        try:
            address = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


@dataclass
class _Bulb:
    """What was last sent to one bulb. Fields change only under lock."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    on: bool | None = None
    rgb: tuple[int, int, int] | None = None
    kelvin: int = 0
    brightness: int | None = None
    asserted_at: float | None = None  # last time power, color and brightness all went out
    last_burst_at: float | None = None


class _StatusWaiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.data: dict[str, Any] | None = None


class _ScanCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._found: dict[str, dict[str, Any]] = {}
        self.changed = threading.Event()

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._found[entry["device_id"]] = entry
        self.changed.set()

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._found.values())


class GoveeLanAdapter:
    """Local-network Govee adapter (Govee LAN API over UDP).

    One UDP socket, bound to the reply port, is shared by every bulb. A
    receiver thread routes scan replies to active discoveries and devStatus
    replies to waiters by source IP, so status checks for several bulbs run in
    parallel. If the reply port is taken, control still works from an
    ephemeral port; only discovery and status checks are unavailable.

    Control commands (turn / colorwc / brightness) have no reply, so set_color
    and turn_off are fire-and-forget: they succeed unless the socket send
    itself fails. To make that dependable each burst is sent twice, unchanged
    values are skipped, and the full state is periodically re-asserted.

    Thread safety: the manager fans commands out on a thread pool. Per-bulb
    state and send spacing are guarded by a per-bulb lock; the socket, the
    reply routing tables and the bulb table each have their own guard.
    """

    def __init__(
        self,
        *,
        bind_host: str = "",
        reply_port: int = REPLY_PORT,
        control_port: int = CONTROL_PORT,
        scan_port: int = SCAN_PORT,
        multicast_group: str = MULTICAST_GROUP,
        ip_refresh_seconds: float | None = IP_REFRESH_SCAN_SECONDS,
    ) -> None:
        self._bind_host = bind_host
        self._reply_port = reply_port
        self._control_port = control_port
        self._scan_port = scan_port
        self._multicast_group = multicast_group
        self._ip_refresh_seconds = ip_refresh_seconds

        self._socket_guard = threading.Lock()
        self._sock: socket.socket | None = None
        self._listening = False
        self._listen_error = ""
        self._receiver: threading.Thread | None = None
        self._closed = False
        self._scan_send_lock = threading.Lock()

        self._bulbs: dict[str, _Bulb] = {}
        self._bulbs_guard = threading.Lock()

        self._replies_guard = threading.Lock()
        self._status_waiters: dict[str, list[_StatusWaiter]] = {}
        self._scan_collectors: list[_ScanCollector] = []
        self._learned_ips: dict[str, str] = {}
        self._last_refresh_scan_at: float | None = None

    # --- LightAdapter ---------------------------------------------------

    def status(self, device: dict[str, Any]) -> LightResult:
        name = device_name(device)
        try:
            ip = self._device_ip(device)
            if not self._ensure_listener():
                return LightResult(name, False, self._listen_error)
            data = self._query_status(ip)
            if data is None:
                return LightResult(
                    name,
                    False,
                    f"No reply from {ip}. Check the bulb is powered on, LAN Control is on "
                    "in the Govee Home app, and this computer is on the same network.",
                )
            return LightResult(name, True, describe_status(data))
        except Exception as exc:
            logger.warning("Govee status failed for %s: %s", name, exc)
            return LightResult(name, False, self._error_message(exc))

    def set_color(
        self,
        device: dict[str, Any],
        color: str,
        brightness: int,
        transition_ms: int,
    ) -> LightResult:
        name = device_name(device)
        try:
            ip = self._device_ip(device)
            rgb, kelvin = bulb_color(color)
            level = max(1, min(100, int(brightness)))
            if kelvin:
                level = max(1, round(level * WHITE_BRIGHTNESS_SCALE))
            sent_color = f"{kelvin} K white" if kelvin else hex_from_rgb(rgb)
            bulb = self._bulb(ip)
            with bulb.lock:
                now = time.monotonic()
                fresh = bulb.asserted_at is not None and now - bulb.asserted_at < STATE_REFRESH_SECONDS
                payloads = []
                if not (fresh and bulb.on):
                    payloads.append(encode_message("turn", {"value": 1}))
                if not (fresh and bulb.rgb == rgb and bulb.kelvin == kelvin):
                    red, green, blue = rgb
                    payloads.append(
                        encode_message(
                            "colorwc",
                            {"color": {"r": red, "g": green, "b": blue}, "colorTemInKelvin": kelvin},
                        )
                    )
                if not (fresh and bulb.brightness == level):
                    payloads.append(encode_message("brightness", {"value": level}))
                if not payloads:
                    return LightResult(name, True, f"Already {sent_color} at {level}%.")

                self._send_burst(bulb, ip, payloads, self._command_interval(device))
                if len(payloads) == 3:
                    bulb.asserted_at = bulb.last_burst_at
                bulb.on, bulb.rgb, bulb.kelvin, bulb.brightness = True, rgb, kelvin, level
            if device.get("device_id"):
                self._maybe_refresh_ips()
            return LightResult(name, True, f"Set to {sent_color} at {level}%.")
        except Exception as exc:
            logger.warning("Govee color update failed for %s: %s", name, exc)
            return LightResult(name, False, self._error_message(exc))

    def turn_off(self, device: dict[str, Any]) -> LightResult:
        name = device_name(device)
        try:
            ip = self._device_ip(device)
            bulb = self._bulb(ip)
            with bulb.lock:
                self._send_burst(
                    bulb, ip, [encode_message("turn", {"value": 0})], self._command_interval(device)
                )
                bulb.on, bulb.rgb, bulb.brightness, bulb.asserted_at = False, None, None, None
            return LightResult(name, True, "Turned off.")
        except Exception as exc:
            logger.warning("Govee off failed for %s: %s", name, exc)
            return LightResult(name, False, self._error_message(exc))

    # --- setup helpers ----------------------------------------------------

    def discover(
        self, ips: list[str] | None = None, duration: float | None = None
    ) -> dict[str, Any]:
        """Scan for bulbs with LAN Control on; never raises.

        Without ips the scan is multicast on every local interface. With ips
        it is sent to each address directly (for networks that block
        multicast), and an address that ignores the scan but answers devStatus
        is still reported, without a device id.
        """
        targets = [str(ip).strip() for ip in ips or [] if is_light_ip(ip)]
        if ips and not targets:
            return self._discovery_result([], "invalid_ip", "Enter an IP address like 192.168.1.50.")
        try:
            if not self._ensure_listener():
                return self._discovery_result([], "listen_failed", self._listen_error)
            found = self._collect_scan_replies(
                targets, DISCOVERY_SECONDS if duration is None else duration
            )
            if targets:
                found = [entry for entry in found if entry["ip"] in targets]
                answered = {entry["ip"] for entry in found}
                for ip in targets:
                    if ip not in answered and self._query_status(ip) is not None:
                        found.append(
                            {
                                "device_id": "",
                                "ip": ip,
                                "sku": "",
                                "name": f"Govee light ({ip})",
                                "wifi_version": "",
                                "ble_version": "",
                            }
                        )
        except Exception as exc:
            logger.warning("Govee discovery failed: %s", exc)
            return self._discovery_result([], "network_error", f"Network error: {self._error_message(exc)}")
        found.sort(key=lambda entry: ipaddress.IPv4Address(entry["ip"]))
        return self._discovery_result(found, None, "")

    def identify(self, device: dict[str, Any]) -> dict[str, Any]:
        """Blink one bulb so the user can see which physical bulb it is.

        Checks reachability first (devStatus), then blinks regardless: control
        commands can work even where a firewall blocks replies.
        """
        name = device_name(device)
        try:
            ip = self._device_ip(device)
            reply = self._query_status(ip) if self._ensure_listener() else None
            on = encode_message("turn", {"value": 1})
            off = encode_message("turn", {"value": 0})
            red, green, blue = IDENTIFY_RGB
            color = encode_message(
                "colorwc", {"color": {"r": red, "g": green, "b": blue}, "colorTemInKelvin": 0}
            )
            bulb = self._bulb(ip)
            with bulb.lock:
                # Whatever happens below, the next set_color re-sends everything.
                bulb.asserted_at = None
                self._send_burst(
                    bulb, ip, [on, color, encode_message("brightness", {"value": 100})], IDENTIFY_BLINK_SECONDS
                )
                for _ in range(IDENTIFY_BLINKS):
                    self._send_burst(bulb, ip, [off], IDENTIFY_BLINK_SECONDS)
                    self._send_burst(bulb, ip, [on], IDENTIFY_BLINK_SECONDS)
                bulb.on, bulb.rgb, bulb.kelvin, bulb.brightness = True, IDENTIFY_RGB, 0, 100
        except Exception as exc:
            logger.warning("Govee identify failed for %s: %s", name, exc)
            return {"ok": False, "reachable": False, "message": self._error_message(exc)}

        if reply is not None:
            return {
                "ok": True,
                "reachable": True,
                "message": f"{name} answered. Look for the bulb that blinked and turned blue.",
            }
        if not self._listening:
            return {
                "ok": True,
                "reachable": False,
                "message": f"Sent a blink to {ip}, but replies can't be received here. {self._listen_error}",
            }
        return {
            "ok": True,
            "reachable": False,
            "message": f"Sent a blink to {ip}, but the light didn't answer. If nothing blinked, "
            "check that LAN Control is on and the address is right.",
        }

    def learned_ip(self, device_id: str) -> str | None:
        with self._replies_guard:
            return self._learned_ips.get(device_id)

    @property
    def listening_port(self) -> int | None:
        sock = self._sock
        if sock is None or not self._listening:
            return None
        return int(sock.getsockname()[1])

    def close(self) -> None:
        with self._socket_guard:
            self._closed = True
            sock = self._sock
        if sock is not None:
            self._reset_socket(sock)

    # --- internals --------------------------------------------------------

    def _collect_scan_replies(self, targets: list[str], duration: float) -> list[dict[str, Any]]:
        """Scan (re-sending periodically) and gather replies until done or timed out.

        Raises the send error if not a single scan could be sent.
        """
        collector = _ScanCollector()
        with self._replies_guard:
            self._scan_collectors.append(collector)
        send_error: OSError | None = None
        sent = False
        try:
            deadline = time.monotonic() + duration
            next_send = time.monotonic()
            while True:
                now = time.monotonic()
                if now >= next_send:
                    try:
                        self._send_scan(targets or None)
                        sent = True
                    except OSError as exc:
                        send_error = exc
                    next_send = now + DISCOVERY_RESEND_SECONDS
                if targets and {entry["ip"] for entry in collector.devices()} >= set(targets):
                    break
                remaining = deadline - now
                if remaining <= 0:
                    break
                collector.changed.wait(max(0.01, min(remaining, next_send - now)))
                collector.changed.clear()
        finally:
            with self._replies_guard:
                self._scan_collectors.remove(collector)
        if not sent:
            raise send_error or OSError("Could not send the search on this network.")
        return collector.devices()

    def _discovery_result(
        self, devices: list[dict[str, Any]], error: str | None, message: str
    ) -> dict[str, Any]:
        if error:
            tips = list(DISCOVERY_TIPS)
            if error == "listen_failed":
                tips.insert(
                    0,
                    "Close any other copy of this app, or another smart-home program on this "
                    f"computer that uses UDP port {self._reply_port}, then try again.",
                )
            return {"ok": False, "error": error, "message": message, "devices": [], "tips": tips}
        if not devices:
            return {
                "ok": True,
                "error": None,
                "message": "No Govee lights answered.",
                "devices": [],
                "tips": list(DISCOVERY_TIPS),
            }
        plural = "" if len(devices) == 1 else "s"
        return {
            "ok": True,
            "error": None,
            "message": f"Found {len(devices)} Govee light{plural}.",
            "devices": devices,
            "tips": [],
        }

    def _device_ip(self, device: dict[str, Any]) -> str:
        # An address learned from a scan this session beats the saved one: it
        # is how a bulb keeps working after the router hands out a new IP.
        device_id = str(device.get("device_id") or "").strip()
        ip = (self.learned_ip(device_id) if device_id else None) or str(device.get("ip") or "").strip()
        if not ip:
            raise ValueError("No IP address saved for this light. Use Find my lights to add it again.")
        if not is_light_ip(ip):
            raise ValueError(f"Invalid IP address: {ip}")
        return ip

    def _bulb(self, ip: str) -> _Bulb:
        with self._bulbs_guard:
            bulb = self._bulbs.get(ip)
            if bulb is None:
                bulb = self._bulbs[ip] = _Bulb()
            return bulb

    @staticmethod
    def _command_interval(device: dict[str, Any]) -> float:
        try:
            update_speed_ms = int(device.get("_update_speed_ms") or 0)
        except (TypeError, ValueError):
            update_speed_ms = 0
        if update_speed_ms <= 0:
            return DEFAULT_COMMAND_INTERVAL_SECONDS
        return max(MIN_COMMAND_INTERVAL_SECONDS, update_speed_ms / 1000)

    def _send_burst(self, bulb: _Bulb, ip: str, payloads: list[bytes], min_interval: float) -> None:
        # Caller holds bulb.lock.
        if bulb.last_burst_at is not None:
            wait_for = min_interval - (time.monotonic() - bulb.last_burst_at)
            if wait_for > 0:
                time.sleep(wait_for)
        bulb.last_burst_at = time.monotonic()
        try:
            for attempt in range(max(1, SEND_REPEAT_COUNT)):
                if attempt:
                    time.sleep(SEND_REPEAT_GAP_SECONDS)
                for payload in payloads:
                    self._send(payload, (ip, self._control_port))
        except OSError:
            bulb.asserted_at = None
            raise

    def _send(self, payload: bytes, address: tuple[str, int]) -> None:
        sock = self._socket()
        try:
            sock.sendto(payload, address)
        except ConnectionResetError:
            # Windows can surface an ICMP error from an earlier datagram (for
            # example a wrong IP) on a later call. The new send is unaffected.
            sock.sendto(payload, address)
        except TimeoutError:
            raise
        except OSError:
            self._reset_socket(sock)
            raise

    def _send_scan(self, targets: list[str] | None) -> None:
        payload = encode_message("scan", {"account_topic": "reserve"})
        if targets:
            for ip in targets:
                self._send(payload, (ip, self._scan_port))
            return

        sock = self._socket()
        group = (self._multicast_group, self._scan_port)
        errors: list[OSError] = []
        sent = False
        with self._scan_send_lock:
            try:
                sock.sendto(payload, group)
                sent = True
            except OSError as exc:
                errors.append(exc)
            # The OS default picks one interface. PCs with VPN or virtual
            # adapters may pick the wrong one, so also send on each LAN address.
            for interface in local_ipv4_addresses():
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
                    sock.sendto(payload, group)
                    sent = True
                except OSError as exc:
                    errors.append(exc)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("0.0.0.0"))
            except OSError:
                pass
        if not sent:
            raise errors[0] if errors else OSError("No network interface available.")

    def _maybe_refresh_ips(self) -> None:
        if self._ip_refresh_seconds is None or not self._listening:
            return
        now = time.monotonic()
        with self._replies_guard:
            last = self._last_refresh_scan_at
            if last is not None and now - last < self._ip_refresh_seconds:
                return
            self._last_refresh_scan_at = now
        # Replies are handled by the receiver; listing interfaces can be slow,
        # so keep it off the command thread.
        threading.Thread(target=self._background_scan, name="govee-lan-rescan", daemon=True).start()

    def _background_scan(self) -> None:
        try:
            self._send_scan(None)
        except Exception as exc:
            logger.debug("Govee background scan failed: %s", exc)

    def _query_status(self, ip: str, timeout: float | None = None) -> dict[str, Any] | None:
        timeout = STATUS_TIMEOUT_SECONDS if timeout is None else timeout
        waiter = _StatusWaiter()
        with self._replies_guard:
            self._status_waiters.setdefault(ip, []).append(waiter)
        try:
            payload = encode_message("devStatus", {})
            for _ in range(2):
                self._send(payload, (ip, self._control_port))
                if waiter.event.wait(timeout / 2):
                    return waiter.data
            return None
        finally:
            with self._replies_guard:
                waiters = self._status_waiters.get(ip, [])
                if waiter in waiters:
                    waiters.remove(waiter)
                if not waiters:
                    self._status_waiters.pop(ip, None)

    def _ensure_listener(self) -> bool:
        sock = self._socket()
        if not self._listening:
            # Another program may have released the reply port since.
            self._reset_socket(sock)
            self._socket()
        return self._listening

    def _socket(self) -> socket.socket:
        with self._socket_guard:
            if self._closed:
                raise RuntimeError("Govee adapter is closed.")
            if self._sock is not None:
                return self._sock
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.bind((self._bind_host, self._reply_port))
            except OSError as exc:
                sock.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                self._listening = False
                self._listen_error = (
                    f"Can't listen for light replies on UDP port {self._reply_port} ({exc}). "
                    "Another program, or a second copy of this app, may be using it."
                )
                logger.warning("Govee LAN: %s", self._listen_error)
            else:
                self._listening = True
                self._listen_error = ""
                sock.settimeout(RECEIVE_POLL_SECONDS)
            # Published before the receiver starts: its loop runs while this is its socket.
            self._sock = sock
            if self._listening:
                self._receiver = threading.Thread(
                    target=self._receive_loop, args=(sock,), name="govee-lan-rx", daemon=True
                )
                self._receiver.start()
            return sock

    def _reset_socket(self, sock: socket.socket) -> None:
        with self._socket_guard:
            if self._sock is not sock:
                return
            self._sock = None
            self._listening = False
            receiver = self._receiver
            self._receiver = None
        try:
            # Wakes a receiver blocked in recvfrom (Linux), so the port frees now.
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=RECEIVE_POLL_SECONDS * 4)

    def _receive_loop(self, sock: socket.socket) -> None:
        while self._sock is sock:
            try:
                raw, address = sock.recvfrom(RECEIVE_BUFFER_BYTES)
            except TimeoutError:
                continue
            except ConnectionResetError:
                # Windows: ICMP "port unreachable" from an earlier send.
                continue
            except OSError:
                if self._sock is not sock:
                    break
                time.sleep(RECEIVE_POLL_SECONDS)
                continue
            if self._sock is not sock:
                break
            if not raw or not address:
                continue
            try:
                self._handle_datagram(raw, str(address[0]))
            except Exception:  # pragma: no cover - defensive, keep receiving
                logger.exception("Govee LAN reply handling failed")

    def _handle_datagram(self, raw: bytes, source_ip: str) -> None:
        message = decode_message(raw)
        if message is None:
            logger.debug("Ignoring malformed Govee LAN datagram from %s", source_ip)
            return
        cmd, data = message
        if cmd == "scan":
            entry = scan_entry(data, source_ip)
            if entry is None:
                logger.debug("Ignoring Govee scan reply without a device id from %s", source_ip)
                return
            with self._replies_guard:
                previous = self._learned_ips.get(entry["device_id"])
                self._learned_ips[entry["device_id"]] = entry["ip"]
                collectors = list(self._scan_collectors)
            if previous and previous != entry["ip"]:
                logger.info("Govee light %s moved from %s to %s", entry["device_id"], previous, entry["ip"])
            for collector in collectors:
                collector.add(entry)
        elif cmd == "devStatus":
            with self._replies_guard:
                for waiter in self._status_waiters.get(source_ip, []):
                    waiter.data = data
                    waiter.event.set()

    @staticmethod
    def _error_message(exc: Exception) -> str:
        return str(exc) or exc.__class__.__name__
