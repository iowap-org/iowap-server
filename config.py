"""Pydantic-Settings for AI-Relay-Service."""

from pathlib import Path
from typing import Optional

import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Server configuration. Env vars with RELAY_ prefix set defaults."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8788
    log_level: str = "info"
    reload: bool = False
    enable_mdns: bool = False
    mdns_hostname: str = "ai-relay"

    # Paths
    db_path: Path = Path.home() / ".relay" / "server.db"
    config_path: Optional[Path] = Path.home() / ".relay" / "config.yaml"
    artifacts_dir: Path = Path.home() / ".relay" / "artifacts"
    static_dir: Optional[Path] = None

    # Auth
    token_ttl_hours: int = 168
    claim_ttl_seconds: int = 60
    heartbeat_interval_seconds: int = 10
    heartbeat_timeout_multiplier: int = 5

    # Dashboard session cookie
    session_secret: Optional[str] = None
    session_cookie_secure: bool = False

    # Scheduler
    default_timeout_seconds: int = 300
    max_retries: int = 2

    # Capabilities
    capabilities_config_path: Path = Path.home() / ".relay" / "capabilities.yaml"

    class Config:
        env_prefix = "RELAY_"
        env_file = ".env"


def _load_yaml_config(path: Optional[Path]) -> dict:
    """Load optional YAML config and return as plain dict."""
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_path(value):
    """Convert string values from YAML to Path where needed."""
    if value is None:
        return None
    if isinstance(value, str):
        return Path(value).expanduser()
    return value


def _apply_yaml_overrides(base: Settings, path: Optional[Path]) -> Settings:
    """YAML config overrides env-var defaults."""
    yaml_data = _load_yaml_config(path)
    if not yaml_data:
        return base

    # Coerce path-like fields.
    for key in [
        "db_path",
        "config_path",
        "artifacts_dir",
        "static_dir",
        "capabilities_config_path",
    ]:
        if key in yaml_data:
            yaml_data[key] = _coerce_path(yaml_data[key])

    merged = {**base.model_dump(), **yaml_data}
    return Settings(**merged)


# Env vars provide defaults; ~/.relay/config.yaml overrides them.
settings = _apply_yaml_overrides(Settings(), Settings().config_path)
