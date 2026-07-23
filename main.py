"""AI-Relay-Service — Main Entry Point"""

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from relay_server import __version__
from relay_server.api.v2 import router as v2_router
from relay_server.config import settings
from relay_server.core.db import init_db
from relay_server.core.events import event_bus
from relay_server.core.session import unsign_user_cookie
from relay_server.core.users import list_users
from relay_server.core.zeroconf import RelayZeroconf

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# Relaxed headers for capability dashboard pages — these are meant to be
# embedded in an <iframe> on the dashboard, so framing must be allowed
# same-origin. The page content itself is operator-supplied HTML.
_CAPABILITY_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("relay")

# Shared IP-based rate limiter used by auth and dashboard routers.
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / Shutdown hook."""
    logger.info("Initializing database at %s", settings.db_path)
    init_db()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    logger.info("AI-Relay-Service v%s starting on %s:%s", __version__, settings.host, settings.port)

    # Enforce a persistent session secret for cookie signing.
    if not settings.session_secret or len(settings.session_secret) < 32:
        raise RuntimeError(
            "RELAY_SESSION_SECRET must be set to a secret of at least 32 characters. "
            "Configure it via RELAY_SESSION_SECRET or session_secret in config.yaml."
        )

    watchdog_task = asyncio.create_task(_heartbeat_watchdog())
    claim_watchdog_task = asyncio.create_task(_claim_ttl_watchdog())
    token_cleanup_task = asyncio.create_task(_token_cleanup_watchdog())
    mdns = RelayZeroconf(hostname=settings.mdns_hostname, port=settings.port)
    if settings.enable_mdns:
        # Start mDNS in the background so it cannot block server startup.
        asyncio.get_running_loop().run_in_executor(None, mdns.start)
    try:
        yield
    finally:
        if settings.enable_mdns:
            mdns.stop()
        watchdog_task.cancel()
        claim_watchdog_task.cancel()
        token_cleanup_task.cancel()
        for task in (watchdog_task, claim_watchdog_task, token_cleanup_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("Shutting down AI-Relay-Service")


async def _claim_ttl_watchdog():
    """Periodically release or fail expired stage claims (T-060)."""
    from relay_server.core.scheduler import Scheduler

    while True:
        try:
            result = await asyncio.to_thread(Scheduler.release_or_fail_claims)
            if result["released"] or result["failed"]:
                logger.info(
                    "Claim TTL watchdog: released=%s failed=%s tasks_failed=%s",
                    result["released"], result["failed"], result["tasks_failed"],
                )
        except Exception as e:
            logger.exception("Claim TTL watchdog error: %s", e)
        await asyncio.sleep(settings.claim_ttl_seconds)


async def _heartbeat_watchdog():
    """Periodically mark nodes offline when heartbeats time out.

    T-061: nodes marked offline also have their ``claimed`` stages
    failed by ``mark_offline_nodes`` so stages are never stuck in
    ``claimed`` forever when a node dies.
    """
    from relay_server.core.discovery import mark_offline_nodes

    while True:
        try:
            offline = await asyncio.to_thread(mark_offline_nodes)
            if offline:
                logger.info("Marked nodes offline: %s", offline)
        except Exception as e:
            logger.exception("Heartbeat watchdog error: %s", e)
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def _token_cleanup_watchdog():
    """Periodically purge expired tokens (T-027 — background cleaner)."""
    from relay_server.core.db import get_conn

    while True:
        try:
            conn = get_conn()
            try:
                now = datetime.now(timezone.utc).isoformat()
                deleted = conn.execute(
                    "DELETE FROM node_tokens WHERE expires_at < ?",
                    (now,),
                ).rowcount
                conn.commit()
                if deleted:
                    logger.info("Purged %d expired token(s)", deleted)
            finally:
                conn.close()
        except Exception as e:
            logger.exception("Token cleanup watchdog error: %s", e)
        await asyncio.sleep(3600)  # once per hour


def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Convert slowapi's default error into JSON for API consumers."""
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Rate limit exceeded"},
        headers={"Retry-After": exc.headers.get("Retry-After", "60")} if exc.headers else {},
    )


app = FastAPI(
    title="AI-Relay-Service",
    version=__version__,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)


@app.middleware("http")
async def _security_headers_middleware(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/relay/v2/capabilities/") and path.endswith("/dashboard-page"):
        headers = _CAPABILITY_PAGE_HEADERS
    else:
        headers = _SECURITY_HEADERS
    for name, value in headers.items():
        response.headers[name] = value
    return response


@app.middleware("http")
async def _force_password_change_middleware(request, call_next):
    """Block normal dashboard use until a user with force_password_change changes their password."""
    path = request.url.path
    allowed_paths = {
        "/relay/v2/dashboard/login",
        "/relay/v2/dashboard/change-password",
        "/relay/v2/dashboard/bootstrap",
        "/relay/v2/dashboard/api/bootstrap",
        "/relay/v2/dashboard/api/me/password",
        "/relay/v2/dashboard/logout",
    }
    allowed_prefixes = ("/relay/v2/dashboard/static/",)
    if path not in allowed_paths and not any(path.startswith(p) for p in allowed_prefixes):
        relay_user = request.cookies.get("relay_user")
        if relay_user:
            from relay_server.api.v2.security import unsign_user_cookie, list_users

            user_data = unsign_user_cookie(relay_user)
            if user_data and user_data.get("user_id") and user_data.get("user_id") != "__master__":
                for user in list_users():
                    if user["user_id"] == user_data["user_id"] and user.get("force_password_change"):
                        return JSONResponse(
                            status_code=status.HTTP_403_FORBIDDEN,
                            content={"detail": "Password change required"},
                        )
    response = await call_next(request)
    return response


app.include_router(v2_router, prefix="/relay/v2")


@app.get("/dashboard", include_in_schema=False)
async def dashboard_root_redirect(request: Request):
    """Redirect shorthand /dashboard to login, change-password or dashboard."""
    relay_user = request.cookies.get("relay_user")
    if relay_user:
        try:
            user_data = unsign_user_cookie(relay_user)
            user_id = user_data.get("user_id")
            for user in list_users():
                if user["user_id"] == user_id and user["is_active"]:
                    if user.get("force_password_change"):
                        return RedirectResponse(url="/relay/v2/dashboard/change-password", status_code=303)
                    return RedirectResponse(url="/relay/v2/dashboard/", status_code=303)
        except Exception:
            pass
    return RedirectResponse(url="/relay/v2/dashboard/login", status_code=303)


@app.get("/dashboard/login", include_in_schema=False)
async def dashboard_login_redirect(request: Request):
    """Redirect shorthand /dashboard/login to the canonical login page."""
    return RedirectResponse(url="/relay/v2/dashboard/login", status_code=307)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "mode": "core",
        "event_subscribers": event_bus.subscriber_count(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI-Relay-Service v2")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Server command (default)
    server_parser = subparsers.add_parser("server", help="Run the relay server")
    server_parser.add_argument("--host", default=settings.host)
    server_parser.add_argument("--port", type=int, default=settings.port)
    server_parser.add_argument(
        "--enable-master-seed",
        action="store_true",
        help="Keep master-seed login enabled even if human admin users exist (recovery mode)",
    )
    server_parser.add_argument(
        "--config", help="Path to config YAML (overrides default ~/.relay/config.yaml)"
    )

    # Admin command
    admin_parser = subparsers.add_parser("admin", help="Administration commands")
    admin_sub = admin_parser.add_subparsers(dest="admin_command", help="Admin subcommands")
    init_master_parser = admin_sub.add_parser("init-master", help="Initialize master admin seed")
    init_master_parser.add_argument("--config", help="Path to config YAML")

    args = parser.parse_args(argv)

    if args.command == "admin":
        _run_admin_command(args)
        return

    uvicorn.run(
        "relay_server.main:app",
        host=args.host,
        port=args.port,
        log_level=settings.log_level,
        reload=settings.reload,
    )


def _run_admin_command(args):
    from relay_server.core.auth import init_master_seed
    from relay_server.core.db import init_db
    from relay_server.core.users import has_admin_user

    init_db()
    if args.admin_command == "init-master":
        secret = init_master_seed()
        if secret:
            print("Master admin seed created.")
            print("WARNING: Store this secret securely. It will not be shown again.")
            print(f"SECRET: {secret}")
            sys.exit(0)
        print("Master admin seed already exists.", file=sys.stderr)
        sys.exit(1)
    print("Unknown admin command", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
