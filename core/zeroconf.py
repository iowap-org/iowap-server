"""mDNS / Zeroconf advertisement for the relay service.

Registers `_http._tcp` service entries so clients can discover the relay as
`ai-relay.local` on the local network.
"""

import logging
import socket
from ipaddress import ip_address
from typing import List, Optional

from zeroconf import ServiceInfo, Zeroconf

from relay_server.config import settings

logger = logging.getLogger(__name__)


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


class RelayZeroconf:
    """Manages mDNS registration for the relay service."""

    def __init__(
        self,
        hostname: str = "ai-relay",
        port: Optional[int] = None,
        addresses: Optional[List[str]] = None,
    ):
        self.hostname = hostname
        self.port = port or settings.port
        self.addresses = addresses or [_local_ip()]
        self.zeroconf: Optional[Zeroconf] = None
        self.info: Optional[ServiceInfo] = None

    def start(self) -> None:
        if self.zeroconf is not None:
            return

        try:
            self.zeroconf = Zeroconf()
            fqdn = f"{self.hostname}.local."
            self.info = ServiceInfo(
                type_="_http._tcp.local.",
                name=f"AI Relay Service._http._tcp.local.",
                addresses=[_ip_to_bytes(a) for a in self.addresses],
                port=self.port,
                properties={
                    "path": "/health",
                    "version": "2.0.0",
                },
                server=fqdn,
            )
            self.zeroconf.register_service(self.info)
            logger.info(
                "mDNS service registered: %s (%s) on port %s",
                fqdn,
                ", ".join(self.addresses),
                self.port,
            )
        except Exception as exc:
            logger.warning("Failed to register mDNS service: %s", exc)
            self._cleanup()

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
