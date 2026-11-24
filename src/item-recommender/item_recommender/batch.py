"""The only application path allowed to fetch external product search results.

The database schedules jobs; a worker only leases existing work. Each API call
needs a database-wide quota reservation and a fresh Seoul batch-window check.
"""

import asyncio
from dataclasses import replace
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .catalog_domain import SEOUL, CatalogStore, RefreshJob, in_batch_window
from .config import Settings
from .domain import ProviderError, SearchResult
from .matching import material_matches


class ProductProvider(Protocol):
    async def search(self, query: str, limit: int = 20) -> SearchResult: ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _until_next_window(now: datetime) -> int:
    local = now.astimezone(SEOUL)
    start = local.replace(hour=3, minute=0, second=0, microsecond=0)
    if local >= start:
        start += timedelta(days=1)
    return max(1, int((start - local).total_seconds()) + 1)


def _window_remaining(now: datetime) -> float:
    local = now.astimezone(SEOUL)
    end = local.replace(hour=5, minute=0, second=0, microsecond=0)
    return max(0.0, (end - local).total_seconds())


def _safe_code(code: str) -> str:
    # Never expose an arbitrary provider message, credential-bearing URL or DSN.
    allowed = {
        "PROVIDER_NOT_CONFIGURED",
        "PROVIDER_FILTERING_NOT_APPROVED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_AUTH_ERROR",
        "PROVIDER_INVALID_RESPONSE",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_CIRCUIT_OPEN",
        "PROVIDER_STATE_UNAVAILABLE",
        "PROVIDER_INVALID_QUERY",
        "PROVIDER_UNUSABLE_RESULTS",
        "BATCH_WINDOW_CLOSED",
    }
    return code if code in allowed else "PROVIDER_UNAVAILABLE"


def _diagnostics(result: SearchResult) -> dict:
    return {
        "discardedCount": result.discarded_count,
        "discardReasons": {
            code: count
            for code, count in result.discard_reasons
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) and type(count) is int and count >= 0
        },
    }


class BatchWorker:
    def __init__(
        self,
        store: CatalogStore,
        provider: ProductProvider,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.provider = provider
        self.settings = settings
        self.clock = clock
        self.namespace = hashlib.sha256((settings.adpick_api_key or "").encode()).hexdigest()

    async def _now(self) -> datetime:
        if self.clock is not None:
            now = self.clock()
        else:
            database_clock = getattr(self.store, "current_time", None)
            now = await asyncio.to_thread(database_clock) if callable(database_clock) else utcnow()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("batch clock must return a timezone-aware datetime")
        return now

    def _preflight(self) -> str | None:
        if not self.settings.adpick_api_key:
            return "PROVIDER_NOT_CONFIGURED"
        if not self.settings.adpick_filtering_approved:
            return "PROVIDER_FILTERING_NOT_APPROVED"
        if not self.settings.storage_approved:
            return "STORAGE_NOT_APPROVED"
        return None

    async def _failed(
        self, job: RefreshJob, code: str, retry: int = 60, *, deferred: bool = False
    ) -> dict:
        now = await self._now()
        # The store bounds queued delays to seven days. Provider Retry-After can
        # be unreasonably large; do not turn it into a failed database update.
        retry = max(1, min(int(retry), 604800))
        method = self.store.defer_job if deferred else self.store.fail_job
        saved = await asyncio.to_thread(method, job, code, now, retry)
        return {
            "status": "DEFERRED" if saved and deferred else "FAILED" if saved else "LEASE_LOST",
            "jobId": job.job_id,
            "providerStatus": code,
            "retryAfterSeconds": retry,
        }

    async def run_once(self) -> dict:
        """Process at most one existing job; never enqueue, sleep or log secrets."""
        policy = self._preflight()
        if policy:
            return {"status": policy}
        try:
            now = await self._now()
            if not in_batch_window(now):
                return {
                    "status": "OUTSIDE_BATCH_WINDOW",
                    "retryAfterSeconds": _until_next_window(now),
                }
            job = await asyncio.to_thread(self.store.claim_job, now, self.settings.lease_seconds)
            if job is None:
                return {"status": "IDLE"}

            now = await self._now()
            if not in_batch_window(now):
                return await self._failed(
                    job, "BATCH_WINDOW_CLOSED", _until_next_window(now), deferred=True
                )
            retry = await asyncio.to_thread(
                self.store.reserve_provider_slot,
                self.namespace,
                now,
                self.settings.rate_limit_per_minute,
            )
            if retry is not None:
                return await self._failed(job, "PROVIDER_RATE_LIMITED", retry, deferred=True)

            # A lease or quota operation can take time. Do not use the earlier
            # clock reading as permission to start a request at/after 05:00.
            now = await self._now()
            if not in_batch_window(now):
                return await self._failed(
                    job, "BATCH_WINDOW_CLOSED", _until_next_window(now), deferred=True
                )
            try:
                async with asyncio.timeout(
                    min(self.settings.timeout_seconds, _window_remaining(now))
                ):
                    result = await self.provider.search(job.query, limit=self.settings.search_limit)
            except (TimeoutError, ProviderError) as exc:
                code = "PROVIDER_TIMEOUT" if isinstance(exc, TimeoutError) else _safe_code(exc.code)
                retry = getattr(exc, "retry_after_seconds", None) or 60
                return await self._failed(
                    job,
                    code,
                    retry,
                    deferred=code in {"PROVIDER_RATE_LIMITED", "PROVIDER_CIRCUIT_OPEN"},
                )
            except Exception:
                return await self._failed(job, "PROVIDER_UNAVAILABLE")

            relevant = tuple(
                offer for offer in result.offers if material_matches(job.query, offer.title)
            )
            irrelevant_count = len(result.offers) - len(relevant)
            if not relevant and result.discarded_count:
                summary = await self._failed(job, "PROVIDER_UNUSABLE_RESULTS")
                return {**summary, "irrelevantCount": irrelevant_count, **_diagnostics(result)}
            now = await self._now()
            # Timestamp the published snapshot and its offers with the same
            # authoritative clock, independent of a worker host's clock skew.
            relevant = tuple(replace(offer, fetched_at=now) for offer in relevant)
            saved = await asyncio.to_thread(
                self.store.complete_job,
                job,
                relevant,
                now,
                now + timedelta(seconds=self.settings.retention_seconds),
                result.discarded_count,
                result.discard_reasons,
            )
            return {
                "status": "COMPLETED" if saved else "LEASE_LOST",
                "jobId": job.job_id,
                "offerCount": len(relevant),
                "irrelevantCount": irrelevant_count,
                **_diagnostics(result),
            }
        except Exception:
            # A claimed job remains leased and is recoverable after expiry.
            # Do not print the driver exception: it may contain a DSN or data.
            return {
                "status": "CATALOG_UNAVAILABLE",
                "retryAfterSeconds": self.settings.worker_poll_seconds,
            }
