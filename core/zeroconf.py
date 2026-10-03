"""mDNS / Zeroconf advertisement for the relay service.

Registers an ``_http._tcp`` service entry so LAN clients can discover the
relay (T-187/T-208). Defaults and fallbacks are IOWAP-based and both names
are configurable via settings (env ``RELAY_MDNS_HOSTNAME`` /
``RELAY_MDNS_SERVICE_NAME``):

- ``mdns_hostname``     — host/FQDN part (default ``iowap`` → ``iowap.local.``)
- ``mdns_service_name`` — human-readable service name
  (default ``IOWAP Relay Service``)

Startup rules (enforced in ``main.py`` AND as a hard fallback inside
:meth:`RelayZeroconf.start`):

- ``enable_mdns`` defaults to **off** — the switch is the explicit
  HOME ja/nein decision, never implicit.
- TLS cert/key installed → mDNS is refused **regardless of the switch**:
  Internet/Community-Relay mode (T-111) must not advertise on a LAN.
"""

import logging
import socket
from ipaddress import ip_address
from typing import List, Optional

from zeroconf import ServiceInfo, Zeroconf

from relay_server.config import settings

logger = logging.getLogger(__name__)

DEFAULT_HOSTNAME = "iowap"
DEFAULT_SERVICE_NAME = "IOWAP Relay Service"


def _local_ip() -> str:
    """Return a routable local IP address for mDNS advertisement."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 53))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except Exception:
        return "127.0.0.1"


def _ip_to_bytes(addr: str) -> bytes:
    return ip_address(addr).packed


def build_service_info(
    hostname: Optional[str] = None,
    port: Optional[int] = None,
    addresses: Optional[List[str]] = None,
    service_name: Optional[str] = None,
) -> ServiceInfo:
    """Assemble the :class:`ServiceInfo` with IOWAP defaults and fallbacks.

    Empty/None values fall back to ``settings`` first, then to the
    IOWAP-based constants above — never to the old relay-era name.
    """
    host = (hostname or settings.mdns_hostname or DEFAULT_HOSTNAME).strip() or DEFAULT_HOSTNAME
    name = (
        service_name
        or settings.mdns_service_name
        or DEFAULT_SERVICE_NAME
    ).strip() or DEFAULT_SERVICE_NAME
    port = port or settings.port
    addrs = addresses or [_local_ip()]
    return ServiceInfo(
        type_="_http._tcp.local.",
        name=f"{name}._http._tcp.local.",
        addresses=[_ip_to_bytes(a) for a in addrs],
        port=port,
        properties={
            "path": "/health",
            "version": "2.0.0",
        },
        server=f"{host}.local.",
    )


class RelayZeroconf:
    """Manages mDNS registration for the relay service."""

    def __init__(
        self,
        hostname: Optional[str] = None,
        port: Optional[int] = None,
        addresses: Optional[List[str]] = None,
        service_name: Optional[str] = None,
    ):
        self.hostname = (
            (hostname or settings.mdns_hostname or DEFAULT_HOSTNAME).strip()
            or DEFAULT_HOSTNAME
        )
        self.service_name = (
            (service_name or settings.mdns_service_name or DEFAULT_SERVICE_NAME).strip()
            or DEFAULT_SERVICE_NAME
        )
        self.port = port or settings.port
        self.addresses = addresses or [_local_ip()]
        self.zeroconf: Optional[Zeroconf] = None
        self.info: Optional[ServiceInfo] = None

    def _tls_active(self) -> bool:
        """True when a TLS cert/key pair is installed (Internet mode)."""
        return bool(settings.tls_certfile and settings.tls_keyfile)

    def start(self) -> bool:
        """Register the service. Returns True when advertised.

        Refuses when TLS is configured — the gate lives here too, so an
        ``enable_mdns=true`` config can never advertise an Internet relay.
        """
        if self.zeroconf is not None:
            return True
        if self._tls_active():
            logger.info(
                "mDNS refused: TLS is active (Internet mode) — no LAN advertisement"
            )
            return False
        try:
            self._register_service()
            logger.info(
                "mDNS service registered: %s (%s) on port %s",
                f"{self.hostname}.local.",
                ", ".join(self.addresses),
                self.port,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to register mDNS service: %s", exc)
            self._cleanup()
            return False

    def _register_service(self) -> None:
        """Open the zeroconf stack and register (test hook — stub in tests)."""
        self.zeroconf = Zeroconf()
        self.info = build_service_info(
            hostname=self.hostname,
            port=self.port,
            addresses=self.addresses,
            service_name=self.service_name,
        )
        self.zeroconf.register_service(self.info)

    def stop(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            if self.zeroconf and self.info:
                self.zeroconf.unregister_service(self.info)
        except Exception as exc:
            logger.warning("Failed to unregister mDNS service: %s", exc)
        finally:
            if self.zeroconf:
                try:
                    self.zeroconf.close()
                except Exception:
                    pass
            self.zeroconf = None
            self.info = None