"""Signed session cookie helpers for the dashboard user cookie."""

from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from relay_server.config import settings

SESSION_COOKIE_SALT = "relay_user_session"
CSRF_COOKIE_SALT = "relay_csrf_token"
SESSION_MAX_AGE_SECONDS = 604800  # 7 days
CSRF_MAX_AGE_SECONDS = 604800  # 7 days (rotated with session)


def _get_serializer(salt: str) -> URLSafeTimedSerializer:
    secret = settings.session_secret
    if not secret:
        raise RuntimeError(
            "RELAY_SESSION_SECRET is not configured. Set a persistent secret "
            "via the RELAY_SESSION_SECRET environment variable or session_secret "
            "in ~/.relay/config.yaml."
        )
    return URLSafeTimedSerializer(secret, salt=salt)


def sign_user_cookie(user: dict) -> str:
    """Return a signed, URL-safe string for the relay_user cookie."""
    data = {"user_id": user["user_id"], "username": user["username"]}
    return _get_serializer(SESSION_COOKIE_SALT).dumps(data)


def unsign_user_cookie(value: str) -> Optional[dict]:
    """Verify and return the signed relay_user cookie payload, or None."""
    try:
        return _get_serializer(SESSION_COOKIE_SALT).loads(value, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def generate_csrf_token() -> str:
    """Return a raw CSRF token value (double-submit cookie pattern).

    The token itself is a random value; it is not signed. The browser receives
    it in the relay_csrf cookie (non-HttpOnly so JavaScript can read it) and
    must send the same value back in the X-CSRF-Token header. This protects
    against CSRF because a cross-origin attacker cannot read the cookie.
    """
    import secrets

    return secrets.token_urlsafe(32)
