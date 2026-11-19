"""Contracts shared by database scheduling, batch workers and read-only serving."""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .domain import Offer

SEOUL = ZoneInfo("Asia/Seoul")


def query_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def in_batch_window(now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("batch clock must be timezone-aware")
    return 3 <= now.astimezone(SEOUL).hour < 5


@dataclass(frozen=True)
class RefreshJob:
    job_id: str
    query_key: str
    query: str
    run_date: str
    attempts: int
    lease_token: str


@dataclass(frozen=True)
class StoredSnapshot:
    query_key: str
    offers: tuple[Offer, ...] = ()
    revision: str | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_status: str = "NOT_COLLECTED"
    discarded_count: int = 0
    discard_reasons: tuple[tuple[str, int], ...] = ()
    source: str = "BATCH"


class CatalogStore(Protocol):
    def initialize(self) -> None: ...
    def register_queries(self, queries: Sequence[str], now: datetime) -> int: ...
    def set_fallbacks(self, primary: str, fallbacks: Sequence[str], now: datetime) -> None: ...
    def fallback_keys(self, primary: str) -> tuple[str, ...]: ...
    def read_snapshot(self, key: str, source: str = "BATCH") -> StoredSnapshot: ...
    def enqueue_due(self, now: datetime) -> int: ...
    def claim_job(self, now: datetime, lease_seconds: int = 30) -> RefreshJob | None: ...
    def complete_job(
        self,
        job: RefreshJob,
        offers: Sequence[Offer],
        now: datetime,
        expires_at: datetime,
        discarded_count: int = 0,
        discard_reasons: tuple[tuple[str, int], ...] = (),
    ) -> bool: ...
    def fail_job(
        self,
        job: RefreshJob,
        code: str,
        now: datetime,
        retry_after_seconds: int = 60,
        max_attempts: int = 3,
    ) -> bool: ...
    def defer_job(
        self, job: RefreshJob, code: str, now: datetime, retry_after_seconds: int = 60
    ) -> bool: ...
    def put_curated(
        self, key: str, offers: Sequence[Offer], now: datetime, expires_at: datetime
    ) -> str: ...
    def purge_expired(self, now: datetime) -> int: ...
    def job_counts(self) -> dict[str, int]: ...
    def reserve_provider_slot(
        self, namespace: str, now: datetime, limit: int = 6
    ) -> int | None: ...
    def close(self) -> None: ...
