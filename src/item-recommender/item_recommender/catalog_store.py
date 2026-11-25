"""Owned catalog storage and durable daily work; never writes backend application tables."""

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .catalog_domain import (
    SEOUL,
    QueryOrigin,
    RefreshJob,
    StoredSnapshot,
    in_batch_window,
    validated_query_key as _key,
)
from .domain import Offer

MAX_OFFERS = 20
MAX_PAYLOAD_BYTES = 256 * 1024
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_queries (
    query_key TEXT PRIMARY KEY, query_text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, origin TEXT NOT NULL DEFAULT 'MANUAL',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS material_source_refs (
    source_id TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    material_id INTEGER NOT NULL CHECK (material_id > 0),
    activity_id INTEGER NOT NULL CHECK (activity_id > 0),
    hobby_id INTEGER NOT NULL CHECK (hobby_id > 0),
    activity_content_version INTEGER NOT NULL CHECK (activity_content_version > 0),
    raw_name TEXT NOT NULL CHECK (length(raw_name) BETWEEN 1 AND 160),
    query_key TEXT NOT NULL REFERENCES catalog_queries(query_key),
    PRIMARY KEY (source_id, source_epoch, material_id)
);
CREATE INDEX IF NOT EXISTS material_source_refs_by_key
    ON material_source_refs(query_key, source_id, source_epoch);
CREATE TABLE IF NOT EXISTS query_fallbacks (
    primary_key TEXT NOT NULL REFERENCES catalog_queries(query_key),
    fallback_key TEXT NOT NULL REFERENCES catalog_queries(query_key),
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(primary_key, fallback_key), UNIQUE(primary_key, position),
    CHECK(primary_key <> fallback_key)
);
CREATE TABLE IF NOT EXISTS catalog_snapshots (
    query_key TEXT NOT NULL REFERENCES catalog_queries(query_key),
    source TEXT NOT NULL CHECK(source IN ('BATCH', 'CURATED')),
    offers_json TEXT CHECK(offers_json IS NULL OR length(CAST(offers_json AS BLOB)) <= 262144),
    revision TEXT, fetched_at REAL, expires_at REAL,
    last_attempt_at REAL, last_status TEXT NOT NULL DEFAULT 'NOT_COLLECTED',
    discarded_count INTEGER NOT NULL DEFAULT 0 CHECK(discarded_count >= 0),
    discard_reasons_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(query_key, source)
);
CREATE TABLE IF NOT EXISTS refresh_jobs (
    job_id TEXT PRIMARY KEY, query_key TEXT NOT NULL REFERENCES catalog_queries(query_key),
    run_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK(status IN ('QUEUED', 'LEASED', 'DONE', 'FAILED', 'EXPIRED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10),
    lease_token TEXT, lease_until REAL, next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, last_error TEXT,
    UNIQUE(query_key, run_date)
);
CREATE INDEX IF NOT EXISTS refresh_jobs_claim ON refresh_jobs(run_date, status, next_attempt_at);
CREATE TABLE IF NOT EXISTS provider_locks (namespace TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS provider_calls (namespace TEXT NOT NULL, called_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_provider_window ON provider_calls(namespace, called_at);
"""


def _timestamp(value: datetime) -> float:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("catalog timestamps must be timezone-aware")
    result = value.timestamp()
    if not math.isfinite(result):
        raise ValueError("invalid catalog timestamp")
    return result


def _datetime(value) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(float(value), timezone.utc)


def _source(value: str) -> str:
    if value not in {"BATCH", "CURATED"}:
        raise ValueError("invalid catalog source")
    return value


def _code(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        raise ValueError("status must be a fixed diagnostic code")
    return value


def _serialize_offers(offers: Sequence[Offer]) -> str:
    if len(offers) > MAX_OFFERS:
        raise ValueError("catalog snapshot exceeds offer limit")
    rows = []
    for offer in offers:
        if not isinstance(offer, Offer):
            raise ValueError("catalog offers must be normalized Offer objects")
        _timestamp(offer.fetched_at)
        row = asdict(offer)
        row["fetched_at"] = offer.fetched_at.isoformat()
        rows.append(row)
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(payload.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("catalog snapshot exceeds payload limit")
    return payload


def _deserialize_offers(payload: str | None) -> tuple[Offer, ...]:
    if payload is None:
        return ()
    try:
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError
        rows = json.loads(payload)
        if not isinstance(rows, list) or len(rows) > MAX_OFFERS:
            raise ValueError
        offers = []
        for row in rows:
            row["fetched_at"] = datetime.fromisoformat(row["fetched_at"])
            _timestamp(row["fetched_at"])
            offers.append(Offer(**row))
        return tuple(offers)
    except (TypeError, ValueError, KeyError, AttributeError):
        raise ValueError("invalid stored catalog payload") from None


class _CatalogStore:
    _postgres = False

    def _execute(self, connection, sql: str, parameters=()):
        return connection.execute(sql.replace("?", "%s") if self._postgres else sql, parameters)

    def _table(self, name: str) -> str:
        return f"item_recommender.{name}" if self._postgres else name

    def _register(
        self,
        connection,
        queries: Sequence[str],
        timestamp: float,
        *,
        origin: QueryOrigin = "MANUAL",
    ) -> int:
        if origin not in ("MANUAL", "BE_MATERIAL"):
            raise ValueError("invalid query origin")
        if isinstance(queries, (str, bytes)):
            raise ValueError("queries must be a sequence of material names")
        keys = dict.fromkeys(_key(query) for query in queries)
        count = 0
        for key in keys:
            cursor = self._execute(
                connection,
                f"""
                INSERT INTO {self._table("catalog_queries")}
                    (query_key, query_text, origin, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(query_key) DO NOTHING
                """,
                (key, key, origin, timestamp, timestamp),
            )
            count += cursor.rowcount
            self._execute(
                connection,
                f"""
                UPDATE {self._table("catalog_queries")} SET active = 1,
                    origin = CASE WHEN ? = 'MANUAL' THEN 'MANUAL' ELSE origin END,
                    updated_at = ?
                WHERE query_key = ?
                """,
                (origin, timestamp, key),
            )
        return count

    def register_queries(
        self, queries: Sequence[str], now: datetime, *, origin: QueryOrigin = "MANUAL"
    ) -> int:
        """Register names; explicit manual ownership survives later automatic imports.

        BE_MATERIAL only upserts eligible names supplied by a trusted synchronizer.
        It does not check publication status or deactivate names absent from a partial list.
        """
        with self._transaction() as connection:
            return self._register(connection, queries, _timestamp(now), origin=origin)

    def preview_material_references(self, snapshot) -> dict:
        """Compare validated references with existing ownership in one DB snapshot."""
        incoming = {ref.query_key for ref in snapshot.references}
        result = {
            "status": "PREVIEW" if incoming else "HOLD_EMPTY",
            "referenceCount": len(snapshot.references),
            "uniqueQueryCount": len(incoming),
            "insertKeys": [],
            "reactivateKeys": [],
            "keepKeys": [],
            "deactivationCandidates": [],
        }
        if not incoming:
            return result

        refs = self._table("material_source_refs")
        catalog = self._table("catalog_queries")
        # Bind the entire key set once so the 10,000-row contract does not depend
        # on SQLite's host parameter limit. Only this origin's previous keys and
        # incoming keys can affect this preview.
        key_rows = (
            "SELECT jsonb_array_elements_text(CAST(? AS jsonb))"
            if self._postgres
            else "SELECT value FROM json_each(?)"
        )
        scope = (snapshot.source_id, snapshot.source_epoch)
        with self._transaction(write=False) as connection:
            rows = self._execute(
                connection,
                f"""
                WITH incoming(query_key) AS ({key_rows}),
                affected(query_key) AS (
                    SELECT query_key FROM incoming
                    UNION
                    SELECT query_key FROM {refs} WHERE source_id = ? AND source_epoch = ?
                )
                SELECT a.query_key, q.active, q.origin,
                    EXISTS (
                        SELECT 1 FROM {refs} r WHERE r.query_key = a.query_key
                        AND (r.source_id <> ? OR r.source_epoch <> ?)
                    ) AS referenced_elsewhere
                FROM affected a LEFT JOIN {catalog} q ON q.query_key = a.query_key
                """,
                (json.dumps(sorted(incoming), ensure_ascii=False), *scope, *scope),
            ).fetchall()
        for row in sorted(rows, key=lambda row: row["query_key"]):
            key = row["query_key"]
            if key in incoming:
                if row["active"] is None:
                    result["insertKeys"].append(key)
                elif not row["active"]:
                    result["reactivateKeys"].append(key)
                else:
                    result["keepKeys"].append(key)
            elif row["active"]:
                if row["origin"] == "BE_MATERIAL" and not row["referenced_elsewhere"]:
                    result["deactivationCandidates"].append(key)
                else:
                    result["keepKeys"].append(key)
        return result

    def set_fallbacks(self, primary: str, fallbacks: Sequence[str], now: datetime) -> None:
        primary = _key(primary)
        if isinstance(fallbacks, (str, bytes)):
            raise ValueError("fallbacks must be a sequence")
        keys = tuple(dict.fromkeys(_key(value) for value in fallbacks))
        if primary in keys or len(keys) > 20:
            raise ValueError("invalid fallback mapping")
        with self._transaction() as connection:
            self._register(connection, (primary, *keys), _timestamp(now))
            self._execute(
                connection,
                f"DELETE FROM {self._table('query_fallbacks')} WHERE primary_key = ?",
                (primary,),
            )
            for position, key in enumerate(keys):
                self._execute(
                    connection,
                    f"""
                    INSERT INTO {self._table("query_fallbacks")}
                        (primary_key, fallback_key, position) VALUES (?, ?, ?)
                    """,
                    (primary, key, position),
                )

    def fallback_keys(self, primary: str) -> tuple[str, ...]:
        with self._transaction(write=False) as connection:
            rows = self._execute(
                connection,
                f"""
                SELECT fallback_key FROM {self._table("query_fallbacks")}
                WHERE primary_key = ? ORDER BY position
                """,
                (_key(primary),),
            ).fetchall()
        return tuple(row["fallback_key"] for row in rows)

    def read_snapshot(self, key: str, source: str = "BATCH") -> StoredSnapshot:
        key, source = _key(key), _source(source)
        with self._transaction(write=False) as connection:
            row = self._execute(
                connection,
                f"""
                SELECT * FROM {self._table("catalog_snapshots")} WHERE query_key = ? AND source = ?
                """,
                (key, source),
            ).fetchone()
        if row is None:
            return StoredSnapshot(query_key=key, source=source)
        return StoredSnapshot(
            query_key=key,
            offers=_deserialize_offers(row["offers_json"]),
            revision=row["revision"],
            fetched_at=_datetime(row["fetched_at"]),
            expires_at=_datetime(row["expires_at"]),
            last_attempt_at=_datetime(row["last_attempt_at"]),
            last_status=row["last_status"],
            discarded_count=row["discarded_count"],
            discard_reasons=tuple(
                tuple(reason) for reason in json.loads(row["discard_reasons_json"])
            ),
            source=source,
        )

    def _purge(self, connection, timestamp: float) -> int:
        return self._execute(
            connection,
            f"""
            UPDATE {self._table("catalog_snapshots")} SET offers_json = NULL
            WHERE offers_json IS NOT NULL AND expires_at <= ?
            """,
            (timestamp,),
        ).rowcount

    def _expire_old_jobs(self, connection, day: str, timestamp: float) -> None:
        self._execute(
            connection,
            f"""
            UPDATE {self._table("refresh_jobs")}
            SET status = 'EXPIRED', lease_token = NULL, lease_until = NULL, updated_at = ?
            WHERE run_date < ? AND status IN ('QUEUED', 'LEASED')
            """,
            (timestamp, day),
        )

    def enqueue_due(self, now: datetime) -> int:
        timestamp = _timestamp(now)
        day = now.astimezone(SEOUL).date().isoformat()
        with self._transaction() as connection:
            self._purge(connection, timestamp)
            if not in_batch_window(now):
                return 0
            self._expire_old_jobs(connection, day, timestamp)
            rows = self._execute(
                connection,
                f"SELECT query_key FROM {self._table('catalog_queries')} WHERE active = 1",
            ).fetchall()
            count = 0
            for row in rows:
                key = row["query_key"]
                job_id = hashlib.sha256(f"{key}|{day}".encode()).hexdigest()
                count += self._execute(
                    connection,
                    f"""
                    INSERT INTO {self._table("refresh_jobs")}
                        (job_id, query_key, run_date, next_attempt_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(query_key, run_date) DO NOTHING
                    """,
                    (job_id, key, day, timestamp, timestamp, timestamp),
                ).rowcount
            return count

    def claim_job(self, now: datetime, lease_seconds: int = 30) -> RefreshJob | None:
        timestamp = _timestamp(now)
        if not 1 <= lease_seconds <= 600:
            raise ValueError("lease_seconds must be between 1 and 600")
        if not in_batch_window(now):
            return None
        day = now.astimezone(SEOUL).date().isoformat()
        with self._transaction() as connection:
            self._expire_old_jobs(connection, day, timestamp)
            exhausted = self._execute(
                connection,
                f"""
                UPDATE {self._table("refresh_jobs")}
                SET status = 'FAILED', last_error = 'LEASE_EXPIRED', lease_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE run_date = ? AND status = 'LEASED' AND lease_until <= ?
                    AND attempts >= max_attempts RETURNING query_key
                """,
                (timestamp, day, timestamp),
            ).fetchall()
            for expired in exhausted:
                self._mark_status(connection, expired["query_key"], timestamp, "LEASE_EXPIRED")
            self._purge(connection, timestamp)
            locking = " FOR UPDATE OF j SKIP LOCKED" if self._postgres else ""
            row = self._execute(
                connection,
                f"""
                SELECT j.*, q.query_text FROM {self._table("refresh_jobs")} j
                JOIN {self._table("catalog_queries")} q ON q.query_key = j.query_key
                LEFT JOIN {self._table("catalog_snapshots")} s
                    ON s.query_key = j.query_key AND s.source = 'BATCH'
                WHERE j.run_date = ? AND q.active = 1 AND j.attempts < j.max_attempts AND
                    ((j.status = 'QUEUED' AND j.next_attempt_at <= ?) OR
                     (j.status = 'LEASED' AND j.lease_until <= ?))
                ORDER BY CASE WHEN s.fetched_at IS NULL THEN 0 ELSE 1 END,
                    COALESCE(s.fetched_at, s.last_attempt_at, 0),
                    j.attempts, j.next_attempt_at, j.created_at, j.job_id LIMIT 1{locking}
                """,
                (day, timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            attempts = row["attempts"] + 1
            self._execute(
                connection,
                f"""
                UPDATE {self._table("refresh_jobs")} SET status = 'LEASED', attempts = ?,
                    lease_token = ?, lease_until = ?, updated_at = ? WHERE job_id = ?
                """,
                (attempts, token, timestamp + lease_seconds, timestamp, row["job_id"]),
            )
            return RefreshJob(
                row["job_id"], row["query_key"], row["query_text"], day, attempts, token
            )

    def _leased(self, connection, job: RefreshJob, timestamp: float):
        locking = " FOR UPDATE" if self._postgres else ""
        return self._execute(
            connection,
            f"""
            SELECT * FROM {self._table("refresh_jobs")}
            WHERE job_id = ? AND query_key = ? AND status = 'LEASED'
                AND lease_token = ? AND lease_until > ?{locking}
            """,
            (job.job_id, job.query_key, job.lease_token, timestamp),
        ).fetchone()

    def _snapshot(
        self,
        connection,
        key,
        source,
        offers_json,
        timestamp,
        expires_at,
        discarded_count=0,
        discard_reasons=(),
    ) -> str:
        revision = uuid.uuid4().hex
        if type(discarded_count) is not int or discarded_count < 0:
            raise ValueError("invalid discard count")
        if len(discard_reasons) > 20 or any(
            not _SAFE_CODE.fullmatch(code) or type(count) is not int or count < 1
            for code, count in discard_reasons
        ):
            raise ValueError("invalid discard diagnostics")
        status = "EMPTY" if offers_json == "[]" else "SUCCESS"
        self._execute(
            connection,
            f"""
            INSERT INTO {self._table("catalog_snapshots")}
                (query_key, source, offers_json, revision, fetched_at, expires_at,
                 last_attempt_at, last_status, discarded_count, discard_reasons_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_key, source) DO UPDATE SET offers_json = excluded.offers_json,
                revision = excluded.revision, fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at, last_attempt_at = excluded.last_attempt_at,
                last_status = excluded.last_status, discarded_count = excluded.discarded_count,
                discard_reasons_json = excluded.discard_reasons_json
            """,
            (
                key,
                source,
                offers_json,
                revision,
                timestamp,
                expires_at,
                timestamp,
                status,
                discarded_count,
                json.dumps(discard_reasons, separators=(",", ":")),
            ),
        )
        return revision

    def complete_job(
        self,
        job: RefreshJob,
        offers: Sequence[Offer],
        now: datetime,
        expires_at: datetime,
        discarded_count: int = 0,
        discard_reasons: tuple[tuple[str, int], ...] = (),
    ) -> bool:
        timestamp, expiry = _timestamp(now), _timestamp(expires_at)
        if expiry <= timestamp:
            raise ValueError("snapshot expiry must be in the future")
        if not offers and discarded_count:
            raise ValueError("all-discarded results must fail the refresh job")
        payload = _serialize_offers(offers)
        with self._transaction() as connection:
            if self._leased(connection, job, timestamp) is None:
                return False
            self._snapshot(
                connection,
                job.query_key,
                "BATCH",
                payload,
                timestamp,
                expiry,
                discarded_count,
                discard_reasons,
            )
            self._execute(
                connection,
                f"""
                UPDATE {self._table("refresh_jobs")} SET status = 'DONE', lease_token = NULL,
                    lease_until = NULL, last_error = NULL, updated_at = ? WHERE job_id = ?
                """,
                (timestamp, job.job_id),
            )
            return True

    def _failed_or_deferred(self, job, code, now, retry_after_seconds, max_attempts, defer):
        timestamp, code = _timestamp(now), _code(code)
        if not 0 <= retry_after_seconds <= 604800 or not 1 <= max_attempts <= 10:
            raise ValueError("invalid retry policy")
        with self._transaction() as connection:
            row = self._leased(connection, job, timestamp)
            if row is None:
                return False
            attempts = max(0, row["attempts"] - 1) if defer else row["attempts"]
            maximum = row["max_attempts"] if defer else max_attempts
            state = "QUEUED" if attempts < maximum else "FAILED"
            self._execute(
                connection,
                f"""
                UPDATE {self._table("refresh_jobs")} SET status = ?, attempts = ?, max_attempts = ?,
                    lease_token = NULL, lease_until = NULL, next_attempt_at = ?, updated_at = ?,
                    last_error = ? WHERE job_id = ?
                """,
                (
                    state,
                    attempts,
                    maximum,
                    timestamp + retry_after_seconds,
                    timestamp,
                    code,
                    job.job_id,
                ),
            )
            self._mark_status(connection, job.query_key, timestamp, code)
            self._purge(connection, timestamp)
            return True

    def _mark_status(self, connection, key: str, timestamp: float, code: str) -> None:
        self._execute(
            connection,
            f"""
            INSERT INTO {self._table("catalog_snapshots")}
                (query_key, source, last_attempt_at, last_status)
            VALUES (?, 'BATCH', ?, ?) ON CONFLICT(query_key, source) DO UPDATE
                SET last_attempt_at = excluded.last_attempt_at, last_status = excluded.last_status
            """,
            (key, timestamp, code),
        )

    def fail_job(
        self,
        job: RefreshJob,
        code: str,
        now: datetime,
        retry_after_seconds: int = 60,
        max_attempts: int = 3,
    ) -> bool:
        return self._failed_or_deferred(job, code, now, retry_after_seconds, max_attempts, False)

    def defer_job(
        self, job: RefreshJob, code: str, now: datetime, retry_after_seconds: int = 60
    ) -> bool:
        return self._failed_or_deferred(job, code, now, retry_after_seconds, 3, True)

    def put_curated(
        self, key: str, offers: Sequence[Offer], now: datetime, expires_at: datetime
    ) -> str:
        key, timestamp, expiry = _key(key), _timestamp(now), _timestamp(expires_at)
        if expiry <= timestamp:
            raise ValueError("snapshot expiry must be in the future")
        payload = _serialize_offers(offers)
        with self._transaction() as connection:
            self._register(connection, (key,), timestamp)
            return self._snapshot(connection, key, "CURATED", payload, timestamp, expiry)

    def purge_expired(self, now: datetime) -> int:
        with self._transaction() as connection:
            return self._purge(connection, _timestamp(now))

    def job_counts(self) -> dict[str, int]:
        with self._transaction(write=False) as connection:
            rows = self._execute(
                connection,
                f"SELECT status, COUNT(*) AS total FROM {self._table('refresh_jobs')} GROUP BY status",
            ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    def reserve_provider_slot(self, namespace: str, now: datetime, limit: int = 6) -> int | None:
        if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{64}", namespace):
            raise ValueError("provider namespace must be a SHA-256 hex digest")
        if type(limit) is not int or not 1 <= limit <= 6:
            raise ValueError("provider rate must be between 1 and 6")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            self._execute(
                connection,
                f"INSERT INTO {self._table('provider_locks')}(namespace) VALUES (?) ON CONFLICT(namespace) DO NOTHING",
                (namespace,),
            )
            if self._postgres:
                self._execute(
                    connection,
                    f"SELECT namespace FROM {self._table('provider_locks')} WHERE namespace = ? FOR UPDATE",
                    (namespace,),
                ).fetchone()
                # Read after taking the lock: host clock skew or lock wait must not age out
                # another worker's reservations or timestamp a new slot in the past.
                timestamp = float(
                    connection.execute(
                        "SELECT extract(epoch FROM clock_timestamp()) AS now"
                    ).fetchone()["now"]
                )
            self._execute(
                connection,
                f"DELETE FROM {self._table('provider_calls')} WHERE namespace = ? AND called_at <= ?",
                (namespace, timestamp - 60),
            )
            row = self._execute(
                connection,
                f"SELECT COUNT(*) AS total, MIN(called_at) AS oldest FROM {self._table('provider_calls')} WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            if row["total"] >= limit:
                return max(1, math.ceil(row["oldest"] + 60 - timestamp))
            self._execute(
                connection,
                f"INSERT INTO {self._table('provider_calls')}(namespace, called_at) VALUES (?, ?)",
                (namespace, timestamp),
            )
        return None


class SQLiteCatalogStore(_CatalogStore):
    def __init__(self, path, *, read_only=False):
        self._lock = threading.RLock()
        use_uri = read_only and str(path) != ":memory:"
        if use_uri:
            database = Path(path).resolve().as_uri() + "?mode=ro"
        else:
            database = str(path)
            if database != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            database, timeout=5, isolation_level=None, check_same_thread=False, uri=use_uri
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(_SQLITE_SCHEMA)

    @contextmanager
    def _transaction(self, write=True):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class PostgresCatalogStore(_CatalogStore):
    _postgres = True

    def __init__(self, dsn: str, *, read_only=False):
        self._dsn = dsn
        self._read_only = read_only
        self._closed = False

    @contextmanager
    def _transaction(self, write=True):
        if self._closed:
            raise RuntimeError("catalog store is closed")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError("PostgreSQL catalog requires psycopg") from None
        with psycopg.connect(self._dsn, connect_timeout=5, row_factory=dict_row) as connection:
            with connection.transaction():
                if self._read_only:
                    connection.execute("SET TRANSACTION READ ONLY")
                yield connection

    def initialize(self) -> None:
        migration = Path(__file__).resolve().parent.parent / "sql" / "001_catalog.sql"
        with self._transaction() as connection:
            connection.execute(migration.read_text(encoding="utf-8"))

    def enqueue_due(self, now: datetime) -> int:
        _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT item_recommender.enqueue_daily_refresh(%s) AS total", (now,)
            ).fetchone()
        return row["total"]

    def current_time(self) -> datetime:
        with self._transaction(write=False) as connection:
            row = connection.execute("SELECT clock_timestamp() AS now").fetchone()
        return row["now"].astimezone(timezone.utc)

    def close(self) -> None:
        self._closed = True


def open_store(settings, *, read_only=False):
    if settings.catalog_database_url:
        return PostgresCatalogStore(settings.catalog_database_url, read_only=read_only)
    return SQLiteCatalogStore(settings.catalog_db_path, read_only=read_only)
