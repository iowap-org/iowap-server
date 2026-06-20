"""AI-Relay-Service — Main Entry Point"""

import argparse
import logging
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
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--config", help="Path to config YAML (overrides default ~/.relay/config.yaml)"
    )
    args = parser.parse_args()

    uvicorn.run(
        "relay_server.main:app",
        host=args.host,
        port=args.port,
        log_level=settings.log_level,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
