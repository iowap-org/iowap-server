"""T-260: deterministic test RELAY_SESSION_SECRET for the whole suite.

Historical failure mode: ``core.auth._get_token_pepper`` raises
``RuntimeError: RELAY_SESSION_SECRET must be set to at least 32 characters``
lazily, on the first token operation. Without the env var this dropped ~139
tests across the suite with a mass-FAIL that looked like a broad regression
(the "139x false-red") when in fact only the harness env was missing.

This conftest therefore guarantees a *deterministic* (fixed, test-only)
session secret BEFORE any ``relay_server`` module import (conftest is
imported before test modules; ``core/auth.py`` reads the env at import time
via ``settings.session_secret``).

Behavior:
* env missing  → the fixed test secret is injected and a visible note is
  added to the terminal summary (the operator must know a test-only
  secret backed the run — never use it in production).
* env present but < 32 chars → loud collection-time failure with the exact
  hint, instead of confusing mid-suite token errors.
* env present and ≥ 32 chars → untouched (a real/rotation secret is
  respected as-is).
"""

import os

import pytest

# Fixed, test-only secret (64 hex chars — matches the ≥32-char requirem-
# ent deterministically). NOT a production credential; it is public on
# purpose (determinism is the point).
_TEST_SESSION_SECRET = "0123456789abcdef" * 4

_secret = os.environ.get("RELAY_SESSION_SECRET")
_injected = False

if not _secret:
    os.environ["RELAY_SESSION_SECRET"] = _TEST_SESSION_SECRET
    _injected = True
elif len(_secret) < 32:
    raise RuntimeError(
        "tests/conftest.py: RELAY_SESSION_SECRET is set but unusable "
        f"({len(_secret)} chars; auth requires >= 32). Either export a valid "
        "secret or unset it — the suite then injects a deterministic "
        "test-only secret (see conftest docstring)."
    )


def pytest_terminal_summary(terminalreporter):
    """T-260: make the injected test secret visible in the run summary."""
    if _injected:
        terminalreporter.section(
            "T-260: RELAY_SESSION_SECRET was not exported — a deterministic "
            "test-only secret was injected by tests/conftest.py",
            purple=True,
        )
        terminalreporter.write_line(
            "Production runs must never use this value: "
            f"{_TEST_SESSION_SECRET[:8]}...(fixed test literal, see conftest)"
        )