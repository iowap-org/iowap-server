"""T-208 / T-187 (server side): mDNS toggling and naming rules.

Rules (Ronny, 2026-10-03):
- TLS cert installed  -> mDNS MUST be off, regardless of the switch.
- ``enable_mdns`` is the HOME ja/nein switch (default: off).
- Service name + hostname default/fallback must be IOWAP-based
  ("IOWAP Relay Service" / "iowap") and be configurable.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_server.core.zeroconf import DEFAULT_SERVICE_NAME, RelayZeroconf


# ---------------------------------------------------------------- naming

def test_default_hostname_is_iowap() -> None:
    zc = RelayZeroconf(port=8788)
    assert zc.hostname == "iowap"


def test_default_service_name_is_iowap() -> None:
    assert DEFAULT_SERVICE_NAME == "IOWAP Relay Service"


def test_custom_hostname_configurable() -> None:
    zc = RelayZeroconf(hostname="keller", port=9999)
    assert zc.hostname == "keller"
    assert zc.port == 9999


def test_service_name_uses_settings() -> None:
    """RelayZeroconf must read the (configurable) service name from settings."""
    import relay_server.core.zeroconf as zc_mod

    original = zc_mod.settings
    try:
        zc_mod.settings = SimpleNamespace(
            mdns_service_name="Keller Relay", mdns_hostname="keller",
            port=8788, tls_certfile=None, tls_keyfile=None,
        )
        info = zc_mod.build_service_info()
        assert info.name == "Keller Relay._http._tcp.local."
        assert info.server == "keller.local."
    finally:
        zc_mod.settings = original


# ---------------------------------------------------------------- tls gate

def test_start_refuses_when_tls_configured(tmp_path: Path) -> None:
    zc = RelayZeroconf(port=8788)
    zc._tls_active = lambda: True
    calls: list[str] = []
    zc._start_registered = lambda: calls.append("register")  # type: ignore[attr-defined]
    registered = zc.start()
    assert registered is False
    assert calls == []


# ---------------------------------------------------------------- config defaults

def test_settings_mdns_defaults() -> None:
    from relay_server.config import Settings

    s = Settings()
    assert s.enable_mdns is False
    assert s.mdns_hostname == "iowap"
    assert s.mdns_service_name == "IOWAP Relay Service"