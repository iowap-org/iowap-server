"""AI-Relay-Service — Main Entry Point"""

import argparse
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from relay_server import __version__
from relay_server.api.v2 import router as v2_router
from relay_server.config import settings
from relay_server.core.db import init_db
from relay_server.core.events import event_bus

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("relay")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / Shutdown hook."""
    logger.info("Initializing database at %s", settings.db_path)
    init_db()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    logger.info("AI-Relay-Service v%s starting on %s:%s", __version__, settings.host, settings.port)
    yield
    logger.info("Shutting down AI-Relay-Service")


app = FastAPI(
    title="AI-Relay-Service",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(v2_router, prefix="/relay/v2")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "mode": "core",
        "event_subscribers": event_bus.subscriber_count(),
    }


def main():
    parser = argparse.ArgumentParser(description="AI-Relay-Service v2")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Server command (default)
    server_parser = subparsers.add_parser("server", help="Run the relay server")
    server_parser.add_argument("--host", default=settings.host)
    server_parser.add_argument("--port", type=int, default=settings.port)
    server_parser.add_argument(
        "--config", help="Path to config YAML (overrides default ~/.relay/config.yaml)"
    )

    # Admin command
    admin_parser = subparsers.add_parser("admin", help="Administration commands")
    admin_sub = admin_parser.add_subparsers(dest="admin_command", help="Admin subcommands")
    init_master_parser = admin_sub.add_parser("init-master", help="Initialize master admin seed")
    init_master_parser.add_argument("--config", help="Path to config YAML")

    args = parser.parse_args()

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

    init_db()
    if args.admin_command == "init-master":
        secret = init_master_seed()
        if secret:
            print("Master admin seed created.")
            print("WARNING: Store this secret securely. It will not be shown again.")
            print(f"SECRET: {secret}")
        else:
            print("Master admin seed already exists.")
            sys.exit(1)
    else:
        print("Unknown admin command")
        sys.exit(1)


if __name__ == "__main__":
    main()
