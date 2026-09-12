"""Ziel-Guard fuer dynamische Node-Routen (T-206, schliesst T-199).

Der Relay proxyt Routen, die Nodes selbst deklarieren. Ohne Pruefung ist er
ein Open Proxy: ein Node — und bei ``auth: "none"`` jeder, der den Relay
erreicht — kann ihn auf Loopback-, Link-Local- und Metadata-Ziele ansetzen
(``http://169.254.169.254/…``) oder auf beliebige Fremdhosts.

Dieses Modul beantwortet drei Fragen fail-closed:

- :func:`blocked_target_reason` — darf ``host`` ueberhaupt Proxy-Ziel sein?
- :func:`validate_endpoint` — taugt ein node-deklarierter ``endpoint`` als
  Ursprung fuer Routen (sonst wird er verworfen)?
- :func:`origin_of` / :func:`target_allowlisted` — bindet ein absoluter
  ``upstream`` an die eigene Origin des Nodes?

RFC1918 bleibt **erlaubt** — der Cluster lebt dort. Geblockt werden
Loopback, Link-Local (inkl. Cloud-Metadata ``169.254.0.0/16``), Multicast,
Unspecified, Reserved und IPv4-gemappte Formen.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit, urlunsplit

from relay_server.config import settings

logger = logging.getLogger(__name__)

_HTTP_SCHEMES = ("http", "https")

# ipaddress-Attribute, die ein Ziel disqualifizieren.
_BLOCKED_ATTRS = (
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_unspecified",
    "is_reserved",
)


def _blocked_ip_reason(addr: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> str | None:
    # ::ffff:127.0.0.1 traegt is_loopback nicht selbst — auf die IPv4-Form
    # zurueckfuehren, sonst rutscht Loopback durch.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for attr in _BLOCKED_ATTRS:
        if getattr(addr, attr):
            return f"{addr} is {attr[3:].replace('_', '-')}"
    return None


def blocked_target_reason(host: str) -> str | None:
    """Return a reason string when ``host`` must not be a proxy target.

    Accepts an IP literal or a hostname. Hostnames are resolved and **every**
    returned address is checked, so a DNS name pointing at loopback or the
    metadata service is caught too. Names that do not resolve (e.g.
    Docker-internal names) pass — they cannot be a literal loopback target and
    ``httpx`` fails on them by itself.
    """
    candidate = (host or "").strip().strip("[]")
    if not candidate:
        return "missing host"

    try:
        addresses = [ipaddress.ip_address(candidate)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(candidate, None)
        except OSError:
            return None
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addresses:
            return None

    for addr in addresses:
        reason = _blocked_ip_reason(addr)
        if reason:
            return f"{candidate} resolves to blocked address ({reason})"
    return None


def origin_of(url: str) -> str | None:
    """Return a normalised ``scheme://host:port`` origin, or None.

    Default ports are filled in so ``http://node.test`` and
    ``http://node.test:80`` compare equal.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}"


def _allow_entries() -> set[str]:
    raw = getattr(settings, "route_target_allow_hosts", "") or ""
    return {entry.strip().lower() for entry in str(raw).split(",") if entry.strip()}


def target_allowlisted(host: str, port: int | None = None) -> bool:
    """True when ``host`` (or ``host:port``) is explicitly allow-listed.

    Escape hatch for setups where a node legitimately proxies to a host other
    than its own endpoint (Docker-internal names, co-located services).
    """
    entries = _allow_entries()
    if not entries:
        return False
    name = (host or "").lower()
    return name in entries or (port is not None and f"{name}:{port}" in entries)


def validate_endpoint(endpoint: str) -> tuple[str | None, str | None]:
    """Validate a node-declared ``endpoint``.

    Returns ``(value, None)`` when it may be stored, ``(None, reason)`` when it
    must be dropped (fail-closed: the node keeps working, its routes simply
    cannot be bound to an origin).
    """
    raw = (endpoint or "").strip()
    if not raw:
        return None, "empty endpoint"
    parts = urlsplit(raw)
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        return None, "endpoint must be an absolute http(s) URL"
    reason = blocked_target_reason(parts.hostname)
    if reason and not target_allowlisted(parts.hostname, parts.port):
        return None, f"blocked endpoint ({reason})"
    return raw, None


def bind_relative(endpoint: str, upstream: str) -> str | None:
    """Bind a relative ``upstream`` path to the node's own ``endpoint``.

    Only scheme + authority of the endpoint are used (the endpoint's own path,
    if any, is ignored) — the declared path wins, so ``/base`` + ``/page`` is
    ``http://host/base`` vs ``http://host/page``, not a surprise concatenation.
    """
    ep = urlsplit((endpoint or "").strip())
    rel = urlsplit((upstream or "").strip())
    if not ep.hostname or ep.scheme not in _HTTP_SCHEMES:
        return None
    if not rel.path.startswith("/"):
        return None
    return urlunsplit((ep.scheme, ep.netloc, rel.path, rel.query, ""))


def upstream_reject_reason(upstream: str, node_endpoint: str) -> str | None:
    """Why this ``upstream`` may not be stored for a node with ``node_endpoint``.

    ``None`` means it may be stored. Absolute upstreams are allowed only when
    they point at **the node's own origin** (migration path for routes declared
    before T-206) or at an allow-listed host; relative paths need a valid
    ``endpoint`` to bind to.
    """
    raw = (upstream or "").strip()
    if not raw:
        return "upstream is required"
    parts = urlsplit(raw)
    endpoint_origin = origin_of(node_endpoint)

    if parts.scheme in _HTTP_SCHEMES:
        host = parts.hostname or ""
        if not host:
            return "absolute upstream is missing a host"
        if target_allowlisted(host, parts.port):
            return None
        reason = blocked_target_reason(host)
        if reason:
            return f"upstream target not allowed ({reason})"
        if endpoint_origin is not None and origin_of(raw) != endpoint_origin:
            return "absolute upstream must match the node endpoint origin"
        return None

    if parts.netloc or raw.startswith("//"):
        return "upstream must be a path or an absolute http(s) URL"
    if endpoint_origin is None:
        return "relative upstream requires a node endpoint"
    return None


def resolve_route_target(upstream: str, node_endpoint: str) -> tuple[str | None, str | None]:
    """Resolve the concrete proxy URL for a stored route (T-206).

    Returns ``(url, None)`` or ``(None, reason)``. This is the **request-time**
    half of the guard: it re-checks stored values, so rows written before T-206
    (or by a stale writer) cannot smuggle loopback/metadata targets in.
    """
    raw = (upstream or "").strip()
    parts = urlsplit(raw)

    if parts.scheme in _HTTP_SCHEMES:
        host = parts.hostname or ""
        if not host:
            return None, "route upstream is missing a host"
        reason = blocked_target_reason(host)
        if reason and not target_allowlisted(host, parts.port):
            return None, f"route target not allowed ({reason})"
        endpoint_origin = origin_of(node_endpoint)
        if (
            endpoint_origin is not None
            and origin_of(raw) != endpoint_origin
            and not target_allowlisted(host, parts.port)
        ):
            return None, "absolute upstream must match the node endpoint origin"
        return raw, None

    if not raw.startswith("/"):
        return None, "route upstream must be a path or an absolute http(s) URL"
    _, endpoint_reason = validate_endpoint(node_endpoint)
    if endpoint_reason:
        return None, f"node endpoint unavailable ({endpoint_reason})"
    target = bind_relative(node_endpoint, raw)
    if target is None:
        return None, "node endpoint unavailable for relative upstream"
    return target, None
