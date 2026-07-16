"""Signed session cookie helpers for the dashboard user cookie."""

from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from relay_server.config import settings

SESSION_COOKIE_SALT = "relay_user_session"
CSRF_COOKIE_SALT = "relay_csrf_token"
SESSION_MAX_AGE_SECONDS = 604800  # 7 days — regular dashboard users
CSRF_MAX_AGE_SECONDS = 604800  # 7 days (rotated with session)

# Master-seed dashboard sessions are deliberately short-lived (1h) to
# limit the blast radius of a leaked master seed in the browser. They
# are intended only for bootstrapping the first human admin or for
# recovery when no human admin can log in. (T-025)
MASTER_SEED_SESSION_MAX_AGE_SECONDS = 3600  # 1 hour


def _get_serializer(salt: str) -> URLSafeTimedSerializer:
    secret = settings.session_secret
    if not secret:
        raise RuntimeError(
            "RELAY_SESSION_SECRET is not configured. Set a persistent secret "
            "via the RELAY_SESSION_SECRET environment variable or session_secret "
            "in ~/.relay/config.yaml."
        )
    return URLSafeTimedSerializer(secret, salt=salt)


def sign_user_cookie(user: dict, max_age: Optional[int] = None) -> str:
    """Return a signed, URL-safe string for the relay_user cookie.

    ``max_age`` is embedded in the payload so that
    :func:`unsign_user_cookie` enforces the per-token TTL rather than
    the global :data:`SESSION_MAX_AGE_SECONDS`. This is used for
    master-seed sessions which have a shorter lifetime (T-025).
    """
    data = {"user_id": user["user_id"], "username": user["username"]}
    if max_age is not None:
        data["_max_age"] = max_age
    return _get_serializer(SESSION_COOKIE_SALT).dumps(data)


def unsign_user_cookie(value: str) -> Optional[dict]:
    """Verify and return the signed relay_user cookie payload, or None."""
    try:
        payload = _get_serializer(SESSION_COOKIE_SALT).loads(
            value, max_age=SESSION_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None
    # Honour the per-token TTL embedded in the payload (master-seed
    # sessions use a shorter max_age). Re-verify against the embedded
    # value so a master-seed cookie expires after 1h even though the
    # global SESSION_MAX_AGE_SECONDS is 7 days.
    embedded_max_age = payload.get("_max_age")
    if embedded_max_age is not None and embedded_max_age < SESSION_MAX_AGE_SECONDS:
        try:
            payload = _get_serializer(SESSION_COOKIE_SALT).loads(
                value, max_age=embedded_max_age
            )
        except (BadSignature, SignatureExpired):
            return None
    return payload


def generate_csrf_token() -> str:
    """Return a raw CSRF token value (double-submit cookie pattern).

    The token itself is a random value; it is not signed. The browser receives
    it in the relay_csrf cookie (non-HttpOnly so JavaScript can read it) and
    must send the same value back in the X-CSRF-Token header. This protects
    against CSRF because a cross-origin attacker cannot read the cookie.
    """
    import secrets

    return secrets.token_urlsafe(32)
