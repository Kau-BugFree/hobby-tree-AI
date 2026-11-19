"""Explicit environment configuration. Loading a .env file is opt-in via uvicorn."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value.lower() == "true"


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    service_token: str | None = field(default=None, repr=False)
    adpick_api_key: str | None = field(default=None, repr=False)
    adpick_filtering_approved: bool = False
    storage_approved: bool = False
    catalog_database_url: str | None = field(default=None, repr=False)
    catalog_db_path: Path = Path(".runtime/catalog.sqlite3")
    retention_seconds: int = 86400
    worker_poll_seconds: float = 10
    lease_seconds: int = 30
    state_db_path: Path = Path(".runtime/provider-state.sqlite3")
    timeout_seconds: float = 5
    rate_limit_per_minute: int = 6
    circuit_failure_threshold: int = 3
    circuit_reset_seconds: int = 30
    allowed_purchase_hosts: tuple[str, ...] = ("link.adpick.co.kr", "deg.kr", "adpick.co.kr")
    search_limit: int = 20

    def __post_init__(self):
        if self.app_env not in {"local", "production"}:
            raise ValueError("APP_ENV must be local or production")
        if self.app_env == "production" and (
            not self.service_token or len(self.service_token) < 32
        ):
            raise ValueError("production requires a SERVICE_TOKEN of at least 32 characters")
        if self.service_token is not None and not self.service_token.strip():
            raise ValueError("SERVICE_TOKEN must not be blank")
        if self.catalog_database_url is not None and not self.catalog_database_url.startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("CATALOG_DATABASE_URL must be a PostgreSQL connection URL")
        if self.app_env == "production" and not self.catalog_database_url:
            raise ValueError("production requires CATALOG_DATABASE_URL for the DB scheduler")
        if not 60 <= self.retention_seconds <= 604800:
            raise ValueError("CATALOG_RETENTION_SECONDS must be between 60 and 604800")
        if not 1 <= self.worker_poll_seconds <= 60:
            raise ValueError("WORKER_POLL_SECONDS must be between 1 and 60")
        if not 15 <= self.lease_seconds <= 300 or self.lease_seconds <= self.timeout_seconds + 2:
            raise ValueError("WORKER_LEASE_SECONDS must cover the provider timeout (15..300)")
        if self.adpick_api_key is not None and (
            not self.adpick_api_key.isascii()
            or not 1 <= len(self.adpick_api_key) <= 256
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.adpick_api_key)
        ):
            raise ValueError("ADPICK_API_KEY has an invalid format")
        if not 0 < self.timeout_seconds <= 10:
            raise ValueError("ADPICK_TIMEOUT_SECONDS must be in (0, 10]")
        if not 1 <= self.rate_limit_per_minute <= 6:
            raise ValueError("ADPICK_RATE_LIMIT_PER_MINUTE must be between 1 and 6")
        if not 1 <= self.circuit_failure_threshold <= 10:
            raise ValueError("ADPICK_CIRCUIT_FAILURE_THRESHOLD must be between 1 and 10")
        if not 1 <= self.circuit_reset_seconds <= 600:
            raise ValueError("ADPICK_CIRCUIT_RESET_SECONDS must be between 1 and 600")
        if not 1 <= self.search_limit <= 20:
            raise ValueError("ADPICK_SEARCH_LIMIT must be between 1 and 20")
        if not self.allowed_purchase_hosts or any(
            not host or any(c in host for c in "/:@* ") for host in self.allowed_purchase_hosts
        ):
            raise ValueError("purchase hosts must be exact hostnames")

    @classmethod
    def from_env(cls):
        hosts = os.getenv("ADPICK_ALLOWED_PURCHASE_HOSTS", "link.adpick.co.kr,deg.kr,adpick.co.kr")
        return cls(
            app_env=os.getenv("APP_ENV", "local"),
            service_token=os.getenv("SERVICE_TOKEN") or None,
            adpick_api_key=os.getenv("ADPICK_API_KEY") or None,
            adpick_filtering_approved=_boolean("ADPICK_FILTERING_APPROVED"),
            storage_approved=_boolean("ADPICK_STORAGE_APPROVED"),
            catalog_database_url=os.getenv("CATALOG_DATABASE_URL") or None,
            catalog_db_path=Path(os.getenv("CATALOG_DB_PATH", ".runtime/catalog.sqlite3")),
            retention_seconds=int(os.getenv("CATALOG_RETENTION_SECONDS", "86400")),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "10")),
            lease_seconds=int(os.getenv("WORKER_LEASE_SECONDS", "30")),
            state_db_path=Path(os.getenv("STATE_DB_PATH", ".runtime/provider-state.sqlite3")),
            timeout_seconds=float(os.getenv("ADPICK_TIMEOUT_SECONDS", "5")),
            rate_limit_per_minute=int(os.getenv("ADPICK_RATE_LIMIT_PER_MINUTE", "6")),
            circuit_failure_threshold=int(os.getenv("ADPICK_CIRCUIT_FAILURE_THRESHOLD", "3")),
            circuit_reset_seconds=int(os.getenv("ADPICK_CIRCUIT_RESET_SECONDS", "30")),
            allowed_purchase_hosts=tuple(h.strip().lower() for h in hosts.split(",")),
            search_limit=int(os.getenv("ADPICK_SEARCH_LIMIT", "20")),
        )
