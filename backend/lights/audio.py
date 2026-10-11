"""How loud this PC's music is right now, for the Pulse light effect.

The sound going to the speakers is read through Windows' loopback capture
(Core Audio, WASAPI) and reduced on the spot to one number about 30 times a
second: its average energy. No audio is kept, saved or sent anywhere.

Loud modern masters hit the same peak level in verse and chorus alike, so
peaks cannot tell them apart; average energy can. The capture follows the
output device Spotify plays on. Spotify's own level meter (the bar in the
Windows Volume mixer) decides whether the sound is Spotify's: while the
Spotify app is silent, other sounds do not move the lights. Without the
Spotify app (the web player, say) all of the PC's sound counts.

Audio is read only while something asks for a level, and stops shortly
after the last request.
"""

from __future__ import annotations

import array
import logging
import math
import operator
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any, Protocol
from collections.abc import Callable

logger = logging.getLogger("spotify-lights")

SAMPLE_INTERVAL_SECONDS = 1 / 30
# Loopback sends no data at all while nothing plays; after this long without
# any, that counts as silence.
NO_DATA_SILENCE_SECONDS = 0.3
# Sessions and devices are looked up again this often, so the meter follows
# Spotify starting later or a switch to headphones.
SOURCE_REFRESH_SECONDS = 3.0
# Audio stops being read this long after the last level request.
IDLE_STOP_SECONDS = 15.0
SPOTIFY_PROCESS_NAME = "spotify.exe"

# Loudness follower, in dB relative to full scale.
FLOOR_DB = -70.0
# Below this energy the output is silence: paused, between tracks, or
# Spotify playing on another device.
SILENCE_POWER = 1e-7  # -70 dB
SILENT_AFTER_SECONDS = 2.5
# Short-term loudness rises quickly with the music and falls back more
# slowly, so the lights swell with each section without flickering on
# every drum hit. Smoothing is applied to the energy, not to decibels: a
# brief gap in the music is then a small dip instead of a plunge to black.
ATTACK_SECONDS = 0.15
RELEASE_SECONDS = 0.6
# The reference is how loud the music's loud parts are: this percentile of
# the last REFERENCE_WINDOW_SECONDS of sound (silence excluded). A quiet
# verse cannot pull it down while a chorus is in the window.
REFERENCE_WINDOW_SECONDS = 45
REFERENCE_PERCENTILE = 0.8
# Music this far under the reference for this long is not a quiet passage
# but a lower volume (or a much quieter song): the reference is learned
# again from the most recent seconds.
RELEARN_DROP_DB = 7.0
RELEARN_AFTER_SECONDS = 15.0
# Loudness this far below the reference shows as the dimmest level.
RANGE_DB = 12.0
# Bulbs look nearly as bright at 60% as at 100%, so a straight line from
# loudness to brightness kept loud passages looking maxed out. The level is
# raised to this power: a chorus then swings between its beats and only its
# loudest moments reach full brightness.
LEVEL_CURVE = 2.0


class LoudnessFollower:
    """Turns raw audio energy into a 0-1 loudness for the lights.

    The level is relative to the music's own loud parts, not to the volume
    knob: a chorus's loudest moments read 1 and a quiet verse lower, at any
    volume.
    """

    def __init__(self) -> None:
        self.loudness_db = FLOOR_DB
        self.reference_db = FLOOR_DB
        self.silent_for = math.inf
        self._power = 0.0
        self._second_peak_db = FLOOR_DB
        self._second_elapsed = 0.0
        self._history: deque[float] = deque(maxlen=REFERENCE_WINDOW_SECONDS)
        self._quiet_for = 0.0

    def update(self, power: float, dt: float) -> None:
        dt = max(0.0, min(1.0, dt))
        if dt <= 0:
            return
        if power < SILENCE_POWER:
            self.silent_for += dt
        else:
            self.silent_for = 0.0
        tau = ATTACK_SECONDS if power > self._power else RELEASE_SECONDS
        self._power += (power - self._power) * (1 - math.exp(-dt / tau))
        self.loudness_db = max(FLOOR_DB, 10 * math.log10(max(self._power, 1e-12)))

        if self.silent_for > 0:
            return
        # One history entry per second of sound: its loudest moment.
        self._second_peak_db = max(self._second_peak_db, self.loudness_db)
        self._second_elapsed += dt
        if self._second_elapsed >= 1.0:
            second_db = self._second_peak_db
            self._history.append(second_db)
            self._second_peak_db = FLOOR_DB
            self._second_elapsed = 0.0
            if second_db < self.reference_db - RELEARN_DROP_DB:
                self._quiet_for += 1.0
            else:
                self._quiet_for = 0.0
            if self._quiet_for >= RELEARN_AFTER_SECONDS:
                recent = list(self._history)[-int(RELEARN_AFTER_SECONDS) :]
                self._history.clear()
                self._history.extend(recent)
                self._quiet_for = 0.0
            ranked = sorted(self._history)
            self.reference_db = ranked[min(len(ranked) - 1, int(len(ranked) * REFERENCE_PERCENTILE))]
        if self.loudness_db > self.reference_db and len(self._history) < 3:
            # Until a few seconds are known, the loudest so far is the reference.
            self.reference_db = self.loudness_db

    @property
    def hearing(self) -> bool:
        return self.silent_for < SILENT_AFTER_SECONDS

    @property
    def level(self) -> float:
        if self.reference_db <= FLOOR_DB:
            return 0.0
        linear = max(0.0, min(1.0, (self.loudness_db - self.reference_db + RANGE_DB) / RANGE_DB))
        return linear**LEVEL_CURVE


class AudioSource(Protocol):
    def refresh(self) -> None:
        """Look up what to listen to again (apps and devices change)."""

    def read(self) -> tuple[float | None, str | None]:
        """(mean energy 0-1 since the last read, "spotify" | "pc" | None).

        Energy is None when no new sound data arrived since the last read.
        """

    def close(self) -> None:
        ...


class AudioMeter:
    """Live loudness of this PC's music, read on demand.

    reading() is cheap and safe from any thread. The first call starts a
    sampler thread; it stops IDLE_STOP_SECONDS after the last call.
    """

    def __init__(self, source_factory: Callable[[], AudioSource] | None = None) -> None:
        self._source_factory = source_factory or default_audio_source
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._demand_at = 0.0
        self._follower = LoudnessFollower()
        self._source: str | None = None
        self._error = ""

    def reading(self) -> dict[str, Any]:
        with self._lock:
            self._demand_at = time.monotonic()
            if self._thread is None:
                self._follower = LoudnessFollower()
                self._source = None
                self._thread = threading.Thread(target=self._run, name="audio-meter", daemon=True)
                self._thread.start()
            return {
                "available": not self._error,
                "source": self._source,
                "hearing": self._follower.hearing,
                "level": round(self._follower.level, 3),
                "message": self._error,
            }

    def _demanded(self) -> bool:
        """False once requests stopped; the thread then marks itself gone."""
        with self._lock:
            if time.monotonic() - self._demand_at > IDLE_STOP_SECONDS:
                self._thread = None
                return False
            return True

    def _run(self) -> None:
        source: AudioSource | None = None
        try:
            source = self._source_factory()
            source.refresh()
            with self._lock:
                self._error = ""
        except Exception as exc:
            logger.warning("Audio meter unavailable: %s", exc)
            if source is not None:
                source.close()
            with self._lock:
                self._error = "This computer's sound can't be read."
            # Stay "running" until requests stop, so a failing meter is not
            # set up again on every request.
            while self._demanded():
                time.sleep(0.5)
            return

        logger.info("Audio meter started.")
        last = time.monotonic()
        next_refresh = last + SOURCE_REFRESH_SECONDS
        try:
            while self._demanded():
                time.sleep(SAMPLE_INTERVAL_SECONDS)
                now = time.monotonic()
                if now >= next_refresh:
                    next_refresh = now + SOURCE_REFRESH_SECONDS
                    try:
                        source.refresh()
                    except Exception as exc:
                        logger.debug("Audio meter refresh failed: %s", exc)
                try:
                    power, kind = source.read()
                except Exception as exc:
                    logger.debug("Audio meter read failed: %s", exc)
                    power, kind = 0.0, None
                    next_refresh = now
                if power is None:
                    if now - last < NO_DATA_SILENCE_SECONDS:
                        # Nothing new yet; the next reading covers this time too.
                        continue
                    power = 0.0  # nothing is playing at all
                with self._lock:
                    self._follower.update(power, now - last)
                    self._source = kind
                last = now
        finally:
            source.close()
            logger.info("Audio meter stopped.")


class _UnavailableSource:
    def refresh(self) -> None:
        raise OSError("Sound can only be read on Windows.")

    def read(self) -> tuple[float, str | None]:
        return 0.0, None

    def close(self) -> None:
        pass


def default_audio_source() -> AudioSource:
    if sys.platform != "win32":
        return _UnavailableSource()
    return _CoreAudioSource()


def _sum_of_squares(samples: array.array) -> float:
    sumprod = getattr(math, "sumprod", None)  # Python 3.12+, runs in C
    if sumprod is not None:
        return float(sumprod(samples, samples))
    return float(sum(map(operator.mul, samples, samples)))


# --- Windows Core Audio (COM through ctypes) ---------------------------------
# Interfaces are called through their vtables; indexes count IUnknown's
# QueryInterface, AddRef and Release as 0-2.

CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAUDIO_CLIENT = "1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"
IID_IAUDIO_CAPTURE_CLIENT = "C8ADBD64-E71E-48A0-A4DE-185C395CD317"
IID_IAUDIO_METER_INFORMATION = "C02216F6-8C67-4B5B-9D00-D008E73E0064"
IID_IAUDIO_SESSION_MANAGER2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
IID_IAUDIO_SESSION_CONTROL2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
SUBTYPE_IEEE_FLOAT = "00000003-0000-0010-8000-00AA00389B71"
SUBTYPE_PCM = "00000001-0000-0010-8000-00AA00389B71"

E_RENDER = 0
E_MULTIMEDIA = 1
DEVICE_STATE_ACTIVE = 0x1
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: COM already set up on this thread
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
CAPTURE_BUFFER_HNS = 2_000_000  # 200 ms, in 100 ns units
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# Spotify's meter must read above this for the sound to count as Spotify's.
SPOTIFY_SILENT_PEAK = 1e-3

# vtable indexes
ENUM_AUDIO_ENDPOINTS = 3  # IMMDeviceEnumerator
GET_DEFAULT_AUDIO_ENDPOINT = 4
ACTIVATE = 3  # IMMDevice
GET_ID = 5
COLLECTION_GET_COUNT = 3  # IMMDeviceCollection
COLLECTION_ITEM = 4
CLIENT_INITIALIZE = 3  # IAudioClient
CLIENT_GET_MIX_FORMAT = 8
CLIENT_START = 10
CLIENT_STOP = 11
CLIENT_GET_SERVICE = 14
CAPTURE_GET_BUFFER = 3  # IAudioCaptureClient
CAPTURE_RELEASE_BUFFER = 4
CAPTURE_GET_NEXT_PACKET_SIZE = 5
METER_GET_PEAK_VALUE = 3  # IAudioMeterInformation
GET_SESSION_ENUMERATOR = 5  # IAudioSessionManager2
SESSIONS_GET_COUNT = 3  # IAudioSessionEnumerator
SESSIONS_GET_SESSION = 4
SESSION_GET_PROCESS_ID = 14  # IAudioSessionControl2


class _Capture:
    """Loopback capture of one output device: an audio client and its reader."""

    def __init__(self, device_id: str, client: Any, reader: Any, sample_format: str, channels: int) -> None:
        self.device_id = device_id
        self.client = client
        self.reader = reader
        self.sample_format = sample_format  # array typecode: "f" float32 or "h" int16
        self.channels = channels


class _CoreAudioSource:
    """Loopback energy of Spotify's output device, gated by Spotify's meter.

    Must be created, used and closed on one thread (COM apartment rules).
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._void_p = ctypes.c_void_p

        class Guid(ctypes.Structure):
            _fields_ = [("raw", ctypes.c_ubyte * 16)]

        class WaveFormat(ctypes.Structure):
            # WAVEFORMATEXTENSIBLE; only the WAVEFORMATEX part is valid
            # unless format_tag is WAVE_FORMAT_EXTENSIBLE.
            _pack_ = 1
            _fields_ = [
                ("format_tag", ctypes.c_ushort),
                ("channels", ctypes.c_ushort),
                ("samples_per_sec", ctypes.c_ulong),
                ("avg_bytes_per_sec", ctypes.c_ulong),
                ("block_align", ctypes.c_ushort),
                ("bits_per_sample", ctypes.c_ushort),
                ("extra_size", ctypes.c_ushort),
                ("valid_bits", ctypes.c_ushort),
                ("channel_mask", ctypes.c_ulong),
                ("sub_format", Guid),
            ]

        self._guid_type = Guid
        self._wave_format = WaveFormat
        self._guid = lambda text: Guid.from_buffer_copy(uuid.UUID(text).bytes_le)
        ole32 = ctypes.OleDLL("ole32")
        self._co_create_instance = ole32.CoCreateInstance
        self._co_create_instance.argtypes = [
            ctypes.POINTER(Guid),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Guid),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._co_task_mem_free = ctypes.WinDLL("ole32").CoTaskMemFree
        self._co_task_mem_free.argtypes = [ctypes.c_void_p]
        self._co_task_mem_free.restype = None
        self._co_uninitialize = ole32.CoUninitialize
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._open_process.restype = wintypes.HANDLE
        self._image_name = kernel32.QueryFullProcessImageNameW
        self._image_name.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self._image_name.restype = wintypes.BOOL
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

        self._com_initialized = False
        try:
            ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            self._com_initialized = True
        except OSError as exc:
            if getattr(exc, "winerror", None) != RPC_E_CHANGED_MODE:
                raise
        self._enumerator = self._create(CLSID_MM_DEVICE_ENUMERATOR, IID_IMM_DEVICE_ENUMERATOR)
        self._spotify_meters: list[Any] = []
        self._capture: _Capture | None = None

    # --- AudioSource ---

    def refresh(self) -> None:
        """Find Spotify's sessions and capture the device it plays on."""
        self._release_meters()
        spotify_device_id = None
        loudest = -1.0
        for device in self._output_devices():
            try:
                meters = self._spotify_session_meters(device)
                if meters:
                    peak = max(self._peak(meter) for meter in meters)
                    if peak > loudest:
                        loudest, spotify_device_id = peak, self._device_id(device)
                    self._spotify_meters.extend(meters)
            finally:
                self._release(device)

        device_id = spotify_device_id or self._default_output_id()
        if self._capture and self._capture.device_id == device_id:
            return
        self._stop_capture()
        if device_id:
            self._capture = self._start_capture(device_id)

    def read(self) -> tuple[float | None, str | None]:
        if not self._capture:
            return 0.0, None
        try:
            power = self._drain()
        except OSError:
            # Usually the device went away; the next refresh starts over.
            self._stop_capture()
            raise
        if self._spotify_meters:
            spotify_playing = max(self._peak(meter) for meter in self._spotify_meters) >= SPOTIFY_SILENT_PEAK
            return (power if spotify_playing else 0.0), "spotify"
        return power, "pc"

    def close(self) -> None:
        self._release_meters()
        self._stop_capture()
        if self._enumerator:
            self._release(self._enumerator)
            self._enumerator = None
        if self._com_initialized:
            self._co_uninitialize()
            self._com_initialized = False

    # --- COM plumbing ---

    def _method(self, pointer: Any, index: int, *argtypes: Any) -> Callable[..., Any]:
        ctypes = self._ctypes
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        function = prototype(vtable[index])
        return lambda *args: function(pointer, *args)

    def _release(self, pointer: Any) -> None:
        ctypes = self._ctypes
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(pointer)

    def _query(self, pointer: Any, iid: str) -> Any:
        ctypes = self._ctypes
        result = self._void_p()
        guid = self._guid(iid)
        self._method(pointer, 0, ctypes.POINTER(self._guid_type), ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(guid), ctypes.byref(result)
        )
        return result

    def _create(self, clsid: str, iid: str) -> Any:
        ctypes = self._ctypes
        result = self._void_p()
        self._co_create_instance(
            ctypes.byref(self._guid(clsid)), None, CLSCTX_ALL, ctypes.byref(self._guid(iid)), ctypes.byref(result)
        )
        return result

    def _activate(self, device: Any, iid: str) -> Any:
        ctypes = self._ctypes
        result = self._void_p()
        guid = self._guid(iid)
        self._method(
            device,
            ACTIVATE,
            ctypes.POINTER(self._guid_type),
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(ctypes.byref(guid), CLSCTX_ALL, None, ctypes.byref(result))
        return result

    # --- devices and sessions ---

    def _device_id(self, device: Any) -> str:
        ctypes = self._ctypes
        text = ctypes.c_void_p()
        self._method(device, GET_ID, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(text))
        try:
            return ctypes.wstring_at(text.value) if text.value else ""
        finally:
            self._co_task_mem_free(text)

    def _default_output_id(self) -> str | None:
        ctypes = self._ctypes
        device = self._void_p()
        try:
            self._method(
                self._enumerator, GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
            )(E_RENDER, E_MULTIMEDIA, ctypes.byref(device))
        except OSError:
            return None  # no output device at all
        try:
            return self._device_id(device)
        finally:
            self._release(device)

    def _output_devices(self) -> list[Any]:
        ctypes = self._ctypes
        collection = self._void_p()
        self._method(
            self._enumerator, ENUM_AUDIO_ENDPOINTS, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)
        )(E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(collection))
        try:
            count = ctypes.c_uint()
            self._method(collection, COLLECTION_GET_COUNT, ctypes.POINTER(ctypes.c_uint))(ctypes.byref(count))
            devices = []
            for index in range(count.value):
                device = self._void_p()
                self._method(collection, COLLECTION_ITEM, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                    index, ctypes.byref(device)
                )
                devices.append(device)
            return devices
        finally:
            self._release(collection)

    def _device_by_id(self, device_id: str) -> Any:
        for device in self._output_devices():
            if self._device_id(device) == device_id:
                return device
            self._release(device)
        return None

    def _spotify_session_meters(self, device: Any) -> list[Any]:
        """Level meters of the Spotify app's audio sessions on one device."""
        ctypes = self._ctypes
        meters: list[Any] = []
        try:
            manager = self._activate(device, IID_IAUDIO_SESSION_MANAGER2)
        except OSError:
            return meters
        sessions = self._void_p()
        try:
            self._method(manager, GET_SESSION_ENUMERATOR, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(sessions))
            count = ctypes.c_int()
            self._method(sessions, SESSIONS_GET_COUNT, ctypes.POINTER(ctypes.c_int))(ctypes.byref(count))
            for index in range(count.value):
                control = self._void_p()
                self._method(sessions, SESSIONS_GET_SESSION, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    index, ctypes.byref(control)
                )
                try:
                    if self._session_process_name(control) == SPOTIFY_PROCESS_NAME:
                        meters.append(self._query(control, IID_IAUDIO_METER_INFORMATION))
                except OSError:
                    pass
                finally:
                    self._release(control)
        except OSError:
            pass
        finally:
            if sessions:
                self._release(sessions)
            self._release(manager)
        return meters

    def _session_process_name(self, control: Any) -> str:
        ctypes = self._ctypes
        control2 = self._query(control, IID_IAUDIO_SESSION_CONTROL2)
        try:
            pid = ctypes.c_ulong()
            self._method(control2, SESSION_GET_PROCESS_ID, ctypes.POINTER(ctypes.c_ulong))(ctypes.byref(pid))
        finally:
            self._release(control2)
        return self._process_name(pid.value)

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        from ctypes import wintypes

        handle = self._open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = self._ctypes.create_unicode_buffer(size.value)
            if self._image_name(handle, 0, buffer, self._ctypes.byref(size)):
                return buffer.value.rsplit("\\", 1)[-1].lower()
            return ""
        finally:
            self._close_handle(handle)

    def _peak(self, meter: Any) -> float:
        ctypes = self._ctypes
        value = ctypes.c_float()
        self._method(meter, METER_GET_PEAK_VALUE, ctypes.POINTER(ctypes.c_float))(ctypes.byref(value))
        return max(0.0, min(1.0, float(value.value)))

    def _release_meters(self) -> None:
        for meter in self._spotify_meters:
            self._release(meter)
        self._spotify_meters = []

    # --- loopback capture ---

    def _sample_format(self, mix: Any) -> str:
        bits = mix.bits_per_sample
        tag = mix.format_tag
        if tag == WAVE_FORMAT_EXTENSIBLE:
            sub = uuid.UUID(bytes_le=bytes(mix.sub_format.raw))
            tag = (
                WAVE_FORMAT_IEEE_FLOAT
                if sub == uuid.UUID(SUBTYPE_IEEE_FLOAT)
                else WAVE_FORMAT_PCM if sub == uuid.UUID(SUBTYPE_PCM) else 0
            )
        if tag == WAVE_FORMAT_IEEE_FLOAT and bits == 32:
            return "f"
        if tag == WAVE_FORMAT_PCM and bits == 16:
            return "h"
        raise OSError(f"Unsupported output format (tag {mix.format_tag}, {bits}-bit).")

    def _start_capture(self, device_id: str) -> _Capture | None:
        ctypes = self._ctypes
        device = self._device_by_id(device_id)
        if device is None:
            return None
        try:
            client = self._activate(device, IID_IAUDIO_CLIENT)
        finally:
            self._release(device)
        try:
            mix_pointer = ctypes.c_void_p()
            self._method(client, CLIENT_GET_MIX_FORMAT, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(mix_pointer))
            try:
                mix = ctypes.cast(mix_pointer, ctypes.POINTER(self._wave_format)).contents
                sample_format = self._sample_format(mix)
                channels = max(1, int(mix.channels))
                self._method(
                    client,
                    CLIENT_INITIALIZE,
                    ctypes.c_int,
                    ctypes.c_ulong,
                    ctypes.c_longlong,
                    ctypes.c_longlong,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                )(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, CAPTURE_BUFFER_HNS, 0, mix_pointer, None)
            finally:
                self._co_task_mem_free(mix_pointer)
            reader = self._void_p()
            guid = self._guid(IID_IAUDIO_CAPTURE_CLIENT)
            self._method(client, CLIENT_GET_SERVICE, ctypes.POINTER(self._guid_type), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(guid), ctypes.byref(reader)
            )
            try:
                self._method(client, CLIENT_START)()
            except OSError:
                self._release(reader)
                raise
        except OSError:
            self._release(client)
            raise
        return _Capture(device_id, client, reader, sample_format, channels)

    def _stop_capture(self) -> None:
        capture, self._capture = self._capture, None
        if capture is None:
            return
        try:
            self._method(capture.client, CLIENT_STOP)()
        except OSError:
            pass
        self._release(capture.reader)
        self._release(capture.client)

    def _drain(self) -> float | None:
        """Mean energy of everything captured since the last call.

        None when no packet arrived since the last call: packets come every
        10 ms or so, so that is usually just an early read, but loopback also
        stops delivering packets when nothing plays at all (see AudioMeter).
        """
        ctypes = self._ctypes
        capture = self._capture
        assert capture is not None
        reader = capture.reader
        next_packet = self._method(reader, CAPTURE_GET_NEXT_PACKET_SIZE, ctypes.POINTER(ctypes.c_uint))
        get_buffer = self._method(
            reader,
            CAPTURE_GET_BUFFER,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        release_buffer = self._method(reader, CAPTURE_RELEASE_BUFFER, ctypes.c_uint)
        item_size = 4 if capture.sample_format == "f" else 2
        scale = 1.0 if capture.sample_format == "f" else 1 / (32768.0 * 32768.0)

        total = 0.0
        count = 0
        packet = ctypes.c_uint()
        data = ctypes.c_void_p()
        frames = ctypes.c_uint()
        flags = ctypes.c_ulong()
        next_packet(ctypes.byref(packet))
        while packet.value:
            get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
            try:
                samples_in_packet = frames.value * capture.channels
                if samples_in_packet and data.value and not flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                    raw = ctypes.string_at(data.value, samples_in_packet * item_size)
                    total += _sum_of_squares(array.array(capture.sample_format, raw)) * scale
                count += samples_in_packet
            finally:
                release_buffer(frames.value)
            next_packet(ctypes.byref(packet))
        return total / count if count else None
