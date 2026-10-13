"""Bare invocation of the relay-server CLI runs the server with settings defaults.

Regression: bare `relay-server` crashed with AttributeError ('Namespace' object
has no attribute 'host') because only the `server` subparser sets host/port —
the "Server command (default)" intent (comment in main()) was never implemented.
Seen live as a systemd restart-loop on Ronnys-MiniPC (2026-10-13).
"""

import relay_server.main as entry


def test_bare_invocation_uses_settings_defaults(monkeypatch):
    calls = {}

    def fake_run(app, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    entry.main([])
    assert calls["host"] == entry.settings.host
    assert calls["port"] == entry.settings.port


def test_server_subcommand_overrides(monkeypatch):
    calls = {}

    def fake_run(app, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    entry.main(["server", "--host", "127.0.0.1", "--port", "9999"])
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 9999