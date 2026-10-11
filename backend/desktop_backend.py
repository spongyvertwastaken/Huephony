"""Desktop sidecar entry point: logging, parent watchdog, and local server."""

from __future__ import annotations

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any

import uvicorn

from runtime_paths import app_config_dir


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_parent_watchdog() -> None:
    parent_pid_value = os.getenv("SPOTIFY_LIGHTING_PARENT_PID")
    if not parent_pid_value:
        return

    try:
        parent_pid = int(parent_pid_value)
    except ValueError:
        logging.warning("Invalid parent PID for backend watchdog: %s", parent_pid_value)
        return

    def watch_parent() -> None:
        while True:
            time.sleep(3)
            if not process_is_running(parent_pid):
                logging.info("Parent app process %s is gone; stopping backend.", parent_pid)
                os._exit(0)

    threading.Thread(target=watch_parent, name="parent-watchdog", daemon=True).start()


def run_server(app: Any) -> None:
    uvicorn.run(
        app,
        host=os.getenv("BACKEND_HOST", "127.0.0.1"),
        port=int(os.getenv("BACKEND_PORT", "8000")),
        log_level=os.getenv("BACKEND_LOG_LEVEL", "info"),
        # A windowless PyInstaller executable has no stdout/stderr. Uvicorn's
        # console formatter assumes those streams exist; keep our file logger.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    config_dir = app_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    # Logs name the tracks being played, so they are capped rather than kept
    # forever (about 2 MB across the current file and one older copy).
    logging.basicConfig(
        handlers=[
            RotatingFileHandler(
                config_dir / "backend-python.log",
                maxBytes=1_000_000,
                backupCount=1,
                encoding="utf-8",
            )
        ],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    logging.info("Starting Huephony backend.")
    start_parent_watchdog()
    from main import app

    run_server(app)
