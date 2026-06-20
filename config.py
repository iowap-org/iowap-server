"""Pydantic-Settings for AI-Relay-Service."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration loaded from env or ~/.relay/config.yaml."""

    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=".env",
        yaml_file=str(Path.home() / ".relay" / "config.yaml"),
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8788
    log_level: str = "info"
    reload: bool = False

    # Paths
    db_path: Path = Path.home() / ".relay" / "server.db"
    static_dir: Optional[Path] = None

    # Auth
    token_ttl_hours: int = 168  # 7 days
    claim_ttl_seconds: int = 60
    heartbeat_interval_seconds: int = 30
    heartbeat_timeout_multiplier: int = 2

    # Scheduler
    default_timeout_seconds: int = 300
    max_retries: int = 2
    aging_boost_minutes: dict = {
        "critical": 5,
        "high": 10,
        "normal": 30,
        "low": 60,
    }

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
