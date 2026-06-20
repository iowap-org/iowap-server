"""AI-Relay-Service — Main Entry Point"""

import argparse
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from relay_server.config import settings
from relay_server.database import init_db

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("relay")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / Shutdown hook."""
    logger.info("Initializing database...")
    init_db()
    logger.info("Database ready at %s", settings.db_path)
    yield
    logger.info("Shutting down relay server")


app = FastAPI(
    title="AI-Relay-Service",
    version=__import__("relay_server").__version__,
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "version": __import__("relay_server").__version__}


def main():
    parser = argparse.ArgumentParser(description="AI-Relay-Service")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--config", help="Path to config YAML")
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
