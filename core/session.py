"""Signed session cookie helpers for the dashboard user cookie."""

from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from relay_server.config import settings

SESSION_COOKIE_SALT = "relay_user_session"
SESSION_MAX_AGE_SECONDS = 604800  # 7 days


def _get_serializer() -> URLSafeTimedSerializer:
    secret = settings.session_secret
    if not secret:
        raise RuntimeError(
            "RELAY_SESSION_SECRET is not configured. Set a persistent secret "
            "via the RELAY_SESSION_SECRET environment variable or session_secret "
            "in ~/.relay/config.yaml."
        )
    return URLSafeTimedSerializer(secret, salt=SESSION_COOKIE_SALT)


def sign_user_cookie(user: dict) -> str:
    """Return a signed, URL-safe string for the relay_user cookie."""
    data = {"user_id": user["user_id"], "username": user["username"]}
    return _get_serializer().dumps(data)


def unsign_user_cookie(value: str) -> Optional[dict]:
    """Verify and return the signed relay_user cookie payload, or None."""
    try:
        return _get_serializer().loads(value, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
