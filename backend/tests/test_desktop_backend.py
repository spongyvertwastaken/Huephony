"""Windowless desktop startup must not configure console-only logging."""

import sys

import uvicorn

import desktop_backend


def test_desktop_server_configures_without_console_streams(monkeypatch) -> None:
    configured: list[uvicorn.Config] = []

    def configure_only(app, **kwargs) -> None:
        configured.append(uvicorn.Config(app, **kwargs))

    monkeypatch.setattr(desktop_backend.uvicorn, "run", configure_only)
    monkeypatch.setenv("BACKEND_HOST", "127.0.0.1")
    monkeypatch.setenv("BACKEND_PORT", "18973")

    # PyInstaller's --noconsole mode supplies None for both console streams.
    with monkeypatch.context() as console:
        console.setattr(sys, "stdout", None)
        console.setattr(sys, "stderr", None)
        desktop_backend.run_server(object())

    assert len(configured) == 1
    assert configured[0].log_config is None
    assert configured[0].access_log is False
    assert configured[0].host == "127.0.0.1"
    assert configured[0].port == 18973
