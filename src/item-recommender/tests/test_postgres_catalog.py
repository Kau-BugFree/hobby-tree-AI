"""Optional real PostgreSQL checks. Set CATALOG_TEST_DATABASE_URL to a dedicated test DB.

No database is contacted when that dedicated variable is absent. Cleanup removes only each
test's unique query namespace; public backend tables are never read or modified here.
"""

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from item_recommender.catalog_domain import SEOUL
from item_recommender.catalog_store import PostgresCatalogStore
from item_recommender.domain import Offer

NOW = datetime(2026, 11, 19, 3, tzinfo=SEOUL)


@pytest.fixture
def pg_store():
    dsn = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN not configured")
    pytest.importorskip("psycopg")
    store = PostgresCatalogStore(dsn)
    store.initialize()
    prefix = "pgtest_" + uuid.uuid4().hex + " "
    namespace = hashlib.sha256(prefix.encode()).hexdigest()
    # Do not enqueue real material queries if this was accidentally pointed at a live catalog.
    with store._transaction() as connection:
        live = connection.execute(
            "SELECT COUNT(*) AS n FROM item_recommender.catalog_queries "
            "WHERE query_key NOT LIKE 'pgtest_%'"
        ).fetchone()["n"]
    if live:
        store.close()
        pytest.fail("PostgreSQL tests require a dedicated database without live catalog queries")
    try:
        yield store, prefix, namespace
    finally:
        with store._transaction() as connection:
            for table in ("catalog_snapshots", "refresh_jobs"):
                connection.execute(
                    f"DELETE FROM item_recommender.{table} WHERE query_key LIKE %s", (prefix + "%",)
                )
            connection.execute(
                "DELETE FROM item_recommender.query_fallbacks WHERE primary_key LIKE %s OR fallback_key LIKE %s",
                (prefix + "%", prefix + "%"),
            )
            connection.execute(
                "DELETE FROM item_recommender.catalog_queries WHERE query_key LIKE %s",
                (prefix + "%",),
            )
            for table in ("provider_calls", "provider_locks"):
                connection.execute(
                    f"DELETE FROM item_recommender.{table} WHERE namespace = %s", (namespace,)
                )
        store.close()


def product():
    return Offer(
        "pg-fixture",
        "스푼",
        "2000원",
        2000,
        None,
        "https://link.adpick.co.kr/pg-fixture",
        "SHOP",
        "가상상점",
        NOW,
    )


def test_postgres_migration_and_database_scheduler_window(pg_store):
    store, prefix, _ = pg_store
    store.initialize()
    store.register_queries([prefix + "스푼"], NOW)
    assert store.enqueue_due(NOW.replace(hour=2, minute=59, second=59)) == 0
    with store._transaction() as connection:
        connection.execute("SET LOCAL TIME ZONE 'Pacific/Honolulu'")
        inserted = connection.execute(
            "SELECT item_recommender.enqueue_daily_refresh(%s) AS n", (NOW,)
        ).fetchone()["n"]
    assert inserted == 1
    assert store.enqueue_due(NOW) == 0
    assert store.claim_job(NOW.replace(hour=5)) is None
    job = store.claim_job(NOW)
    assert job.run_date == "2026-11-19"
    assert store.complete_job(job, [product()], NOW, NOW + timedelta(days=2))
    assert store.read_snapshot(prefix + "스푼").offers == (product(),)


def test_postgres_concurrent_enqueue_and_skip_locked_claims(pg_store):
    store, prefix, _ = pg_store
    store.register_queries([prefix + str(index) for index in range(8)], NOW)
    with ThreadPoolExecutor(max_workers=4) as workers:
        inserted = list(workers.map(lambda _: store.enqueue_due(NOW), range(4)))
    assert sum(inserted) == 8
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: store.claim_job(NOW), range(12)))
    jobs = [job for job in results if job is not None]
    assert len(jobs) == 8
    assert len({job.job_id for job in jobs}) == 8


def test_postgres_lease_fencing_failure_preservation_and_expiry(pg_store):
    store, prefix, _ = pg_store
    key = prefix + "스푼"
    store.register_queries([key], NOW)
    store.enqueue_due(NOW)
    original = store.claim_job(NOW)
    later = NOW + timedelta(seconds=30)
    replacement = store.claim_job(later)
    expiry = NOW + timedelta(days=2)
    assert not store.complete_job(original, [product()], later, expiry)
    assert store.complete_job(replacement, [product()], later, expiry)
    previous = store.read_snapshot(key)
    tomorrow = NOW + timedelta(days=1)
    store.enqueue_due(tomorrow)
    job = store.claim_job(tomorrow)
    assert store.fail_job(job, "PROVIDER_TIMEOUT", tomorrow)
    assert store.read_snapshot(key).offers == previous.offers
    store.put_curated(key, [product()], tomorrow, expiry)
    with store._transaction() as connection:
        deleted = connection.execute(
            "SELECT item_recommender.purge_expired_catalog(%s) AS n", (expiry,)
        ).fetchone()["n"]
    assert deleted == 2
    assert store.read_snapshot(key).offers == ()
    assert store.read_snapshot(key, "CURATED").offers == ()
    assert store.read_snapshot(key).revision == previous.revision


def test_postgres_quota_is_atomic_between_connections(pg_store):
    store, _, namespace = pg_store
    server_now = store.current_time()
    assert server_now.tzinfo is not None
    assert server_now.utcoffset() == timedelta(0)
    with store._transaction() as connection:
        observed_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    assert timedelta(0) <= observed_now - server_now < timedelta(seconds=10)
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(
            workers.map(lambda _: store.reserve_provider_slot(namespace, NOW), range(12))
        )
    assert results.count(None) == 6
    blocked = [result for result in results if result is not None]
    assert len(blocked) == 6
    assert all(1 <= result <= 60 for result in blocked)
    # A fast caller must not prune the six slots reserved by another host.
    assert store.reserve_provider_slot(namespace, NOW + timedelta(seconds=61)) is not None
    assert store.reserve_provider_slot(namespace, server_now + timedelta(seconds=61)) is not None
    # Advance only this fixture's persisted slots, using the database clock, without sleeping.
    with store._transaction() as connection:
        connection.execute(
            "UPDATE item_recommender.provider_calls "
            "SET called_at = extract(epoch FROM clock_timestamp()) - 61 WHERE namespace = %s",
            (namespace,),
        )
    assert store.reserve_provider_slot(namespace, NOW) is None
