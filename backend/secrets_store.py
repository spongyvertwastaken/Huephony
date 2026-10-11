"""At-rest protection for sensitive settings values.

On Windows, secret strings are encrypted with DPAPI (tied to the current
Windows user account) before being written to settings.json, and transparently
decrypted on load. On other platforms, or if pywin32 is unavailable, values are
stored as plaintext. Plaintext values written by older builds are read back
unchanged, so existing settings keep working and are re-encrypted on the next
save.
"""

from __future__ import annotations

import base64
import logging

logger = logging.getLogger("spotify-lights")

ENC_PREFIX = "enc:v1:"

try:  # pragma: no cover - platform dependent
    import win32crypt  # type: ignore

    _DPAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - non-Windows or pywin32 missing
    win32crypt = None  # type: ignore
    _DPAPI_AVAILABLE = False


def can_encrypt() -> bool:
    return _DPAPI_AVAILABLE


def protect(value: str) -> str:
    """Encrypt a secret for storage. Returns plaintext if DPAPI is unavailable.

    Already-encrypted and empty values are returned unchanged.
    """
    if not value or value.startswith(ENC_PREFIX) or not _DPAPI_AVAILABLE:
        return value
    try:
        blob = win32crypt.CryptProtectData(value.encode("utf-8"), "spotify-room-lighting", None, None, None, 0)
        return ENC_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not encrypt secret; storing as plaintext: %s", exc)
        return value


def unprotect(value: str) -> str:
    """Decrypt a stored secret. Plaintext values are returned unchanged.

    Returns "" if a value is encrypted but cannot be decrypted (for example when
    settings were copied from another Windows user account), which forces the
    user to re-enter the secret.
    """
    if not value or not value.startswith(ENC_PREFIX):
        return value
    if not _DPAPI_AVAILABLE:
        logger.warning("Encrypted secret found but DPAPI is unavailable; ignoring it.")
        return ""
    try:
        blob = base64.b64decode(value[len(ENC_PREFIX):])
        _, decrypted = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return decrypted.decode("utf-8")
    except Exception as exc:
        logger.warning("Could not decrypt stored secret; clearing it: %s", exc)
        return ""
