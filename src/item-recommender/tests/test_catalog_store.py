import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from item_recommender.catalog_domain import SEOUL
from item_recommender.catalog_store import SQLiteCatalogStore
from item_recommender.domain import Offer

NOW = datetime(2026, 11, 19, 3, 0, tzinfo=SEOUL)
NAMESPACE = hashlib.sha256(b"offline-test-namespace").hexdigest()


def offer(now=NOW, name="계량 스푼"):
    return Offer(
        "fixture-offer",
        name,
        "2,000원",
        2000,
        None,
        "https://link.adpick.co.kr/fixture",
        "SHOP",
        "가상 상점",
        now,
    )


@pytest.fixture
def store(tmp_path):
    result = SQLiteCatalogStore(tmp_path / "catalog.sqlite3")
    result.initialize()
    yield result
    result.close()


def queued(store, now=NOW, query="계량 스푼"):
    store.register_queries([query], now)
    store.enqueue_due(now)
    job = store.claim_job(now)
    assert job is not None
    return job


def test_idempotent_initialize_registration_and_normalization(store):
    store.initialize()
    assert store.register_queries(["  계량　스푼 ", "계량 스푼", "ＳＰＯＯＮ", "spoon"], NOW) == 2
    assert store.register_queries(["SPOON"], NOW) == 0
    assert store.enqueue_due(NOW) == 2
    unknown = store.read_snapshot(" Unknown ")
    assert unknown.query_key == "unknown"
    assert unknown.last_status == "NOT_COLLECTED"
    assert unknown.offers == ()


def test_fallback_mapping_order_replace_and_validation(store):
    store.set_fallbacks("계량스푼", ["계량 스푼", "  계량 스푼 ", "계량 도구"], NOW)
    assert store.fallback_keys(" 계량스푼 ") == ("계량 스푼", "계량 도구")
    store.set_fallbacks("계량스푼", ["스푼"], NOW)
    assert store.fallback_keys("계량스푼") == ("스푼",)
    with pytest.raises(ValueError):
        store.set_fallbacks("스푼", ["스푼"], NOW)


@pytest.mark.parametrize(
    "hours,minutes,seconds,expected",
    [
        (2, 59, 59, 0),
        (3, 0, 0, 1),
        (4, 59, 59, 1),
        (5, 0, 0, 0),
    ],
)
def test_seoul_window_inclusive_exclusive_independent_of_input_timezone(
    store, hours, minutes, seconds, expected
):
    now = NOW.replace(hour=hours, minute=minutes, second=seconds).astimezone(timezone.utc)
    store.register_queries(["스푼"], now)
    assert store.enqueue_due(now) == expected
    assert (store.claim_job(now) is not None) == bool(expected)


def test_one_job_per_local_date_and_old_work_does_not_block_today(store):
    store.register_queries(["스푼"], NOW)
    assert store.enqueue_due(NOW) == 1
    assert store.enqueue_due(NOW + timedelta(minutes=1)) == 0
    tomorrow = NOW + timedelta(days=1)
    assert store.enqueue_due(tomorrow) == 1
    job = store.claim_job(tomorrow)
    assert job.run_date == "2026-11-20"
    assert store.job_counts() == {"EXPIRED": 1, "LEASED": 1}


def test_expired_lease_fences_old_worker_and_duplicate_completion(store):
    first = queued(store)
    later = NOW + timedelta(seconds=31)
    second = store.claim_job(later)
    assert second.job_id == first.job_id
    assert second.lease_token != first.lease_token
    assert second.attempts == 2
    assert not store.complete_job(first, [offer()], later, later + timedelta(hours=1))
    assert not store.fail_job(first, "PROVIDER_TIMEOUT", later)
    assert store.complete_job(second, [offer()], later, later + timedelta(hours=1))
    assert not store.complete_job(second, [], later, later + timedelta(hours=1))
    snapshot = store.read_snapshot("계량 스푼")
    assert snapshot.offers == (offer(),)
    assert snapshot.last_status == "SUCCESS"
    assert snapshot.revision


def test_expired_lease_without_reclaim_also_rejects_completion(store):
    job = queued(store)
    now = NOW + timedelta(seconds=30)
    assert not store.complete_job(job, [offer()], now, now + timedelta(hours=1))


def test_crashed_workers_have_bounded_attempts(store):
    first = queued(store)
    assert first.attempts == 1
    assert store.claim_job(NOW + timedelta(seconds=30)).attempts == 2
    assert store.claim_job(NOW + timedelta(seconds=60)).attempts == 3
    assert store.claim_job(NOW + timedelta(seconds=90)) is None
    assert store.job_counts() == {"FAILED": 1}
    assert store.read_snapshot(first.query_key).last_status == "LEASE_EXPIRED"


def test_failed_refresh_keeps_unexpired_success_and_empty_success_clears_it(store):
    first = queued(store)
    expiry = NOW + timedelta(days=3)
    assert store.complete_job(first, [offer()], NOW, expiry)
    original = store.read_snapshot(first.query_key)
    tomorrow = NOW + timedelta(days=1)
    store.enqueue_due(tomorrow)
    retry = store.claim_job(tomorrow)
    assert store.fail_job(retry, "PROVIDER_UNAVAILABLE", tomorrow)
    retained = store.read_snapshot(first.query_key)
    assert retained.offers == original.offers
    assert retained.revision == original.revision
    assert retained.expires_at == original.expires_at
    assert retained.last_attempt_at == tomorrow
    assert retained.last_status == "PROVIDER_UNAVAILABLE"
    assert store.claim_job(tomorrow + timedelta(seconds=59)) is None
    later = tomorrow + timedelta(seconds=60)
    retry = store.claim_job(later)
    assert store.complete_job(retry, [], later, expiry)
    empty = store.read_snapshot(first.query_key)
    assert empty.offers == ()
    assert empty.last_status == "EMPTY"
    assert empty.revision != original.revision


def test_all_discarded_is_not_success(store):
    job = queued(store)
    with pytest.raises(ValueError, match="all-discarded"):
        store.complete_job(job, [], NOW, NOW + timedelta(hours=1), discarded_count=20)
    assert store.job_counts() == {"LEASED": 1}
    assert store.fail_job(job, "PROVIDER_UNUSABLE_RESULTS", NOW)


def test_failures_stop_at_max_and_deferrals_do_not_consume_attempts(store):
    job = queued(store)
    for count in range(5):
        now = NOW + timedelta(seconds=count)
        assert job.attempts == 1
        assert store.defer_job(job, "PROVIDER_RATE_LIMITED", now, retry_after_seconds=1)
        job = store.claim_job(now + timedelta(seconds=1))
    for count in range(3):
        now = NOW + timedelta(seconds=5 + count)
        assert job.attempts == count + 1
        assert store.fail_job(job, "PROVIDER_TIMEOUT", now, retry_after_seconds=1)
        job = store.claim_job(now + timedelta(seconds=1))
    assert job is None
    assert store.job_counts() == {"FAILED": 1}


def test_curated_and_batch_are_separate_and_purge_removes_both_payloads(store):
    job = queued(store)
    expiry = NOW + timedelta(minutes=1)
    store.complete_job(job, [offer()], NOW, expiry)
    curated_revision = store.put_curated(job.query_key, [offer(name="검토한 스푼")], NOW, expiry)
    assert store.read_snapshot(job.query_key).offers[0].title == "계량 스푼"
    assert store.read_snapshot(job.query_key, "CURATED").revision == curated_revision
    assert store.purge_expired(expiry - timedelta(microseconds=1)) == 0
    assert store.purge_expired(expiry) == 2
    for source in ("BATCH", "CURATED"):
        snapshot = store.read_snapshot(job.query_key, source)
        assert snapshot.offers == ()
        assert snapshot.revision is not None
        assert snapshot.fetched_at == NOW
        assert snapshot.expires_at == expiry
        assert snapshot.last_status == "SUCCESS"
    assert store.purge_expired(expiry) == 0


def test_enqueue_purges_even_outside_batch_window(store):
    store.put_curated("스푼", [offer()], NOW, NOW + timedelta(hours=1))
    assert store.enqueue_due(NOW.replace(hour=12)) == 0
    assert store.read_snapshot("스푼", "CURATED").offers == ()


def test_expired_old_payload_is_not_retained_on_failure(store):
    job = queued(store)
    store.complete_job(job, [offer()], NOW, NOW + timedelta(hours=1))
    tomorrow = NOW + timedelta(days=1)
    store.enqueue_due(tomorrow)
    job = store.claim_job(tomorrow)
    store.fail_job(job, "PROVIDER_TIMEOUT", tomorrow)
    assert store.read_snapshot(job.query_key).offers == ()


def test_snapshot_limits_and_diagnostics_roundtrip(store):
    with pytest.raises(ValueError, match="offer limit"):
        store.put_curated("스푼", [offer()] * 21, NOW, NOW + timedelta(hours=1))
    with pytest.raises(ValueError, match="payload limit"):
        store.put_curated(
            "스푼", [replace(offer(), title="가" * 100000)], NOW, NOW + timedelta(hours=1)
        )
    with pytest.raises(ValueError, match="future"):
        store.put_curated("스푼", [offer()], NOW, NOW)
    job = queued(store)
    store.complete_job(job, [offer()], NOW, NOW + timedelta(hours=1), 2, (("INVALID_TITLE", 2),))
    snapshot = store.read_snapshot(job.query_key)
    assert snapshot.discarded_count == 2
    assert snapshot.discard_reasons == (("INVALID_TITLE", 2),)


def test_naive_datetimes_and_arbitrary_error_text_are_rejected(store):
    with pytest.raises(ValueError, match="timezone-aware"):
        store.enqueue_due(datetime(2026, 11, 19, 3))
    job = queued(store)
    with pytest.raises(ValueError, match="fixed diagnostic"):
        store.fail_job(job, "raw secret or URL", NOW)


def test_shared_rate_limit_across_connections_and_window(tmp_path):
    path = tmp_path / "shared.sqlite3"
    stores = [SQLiteCatalogStore(path) for _ in range(4)]
    for store in stores:
        store.initialize()
    try:
        with ThreadPoolExecutor(max_workers=12) as threads:
            results = list(
                threads.map(
                    lambda index: stores[index % 4].reserve_provider_slot(NAMESPACE, NOW), range(12)
                )
            )
        assert results.count(None) == 6
        assert results.count(60) == 6
        assert stores[0].reserve_provider_slot(NAMESPACE, NOW + timedelta(seconds=59)) == 1
        assert stores[1].reserve_provider_slot(NAMESPACE, NOW + timedelta(seconds=60)) is None
        with pytest.raises(ValueError, match="SHA-256"):
            stores[0].reserve_provider_slot("raw-api-key", NOW)
        with pytest.raises(ValueError, match="between 1 and 6"):
            stores[0].reserve_provider_slot(NAMESPACE, NOW, limit=7)
    finally:
        for store in stores:
            store.close()


def test_parallel_claims_never_double_lease_a_job(tmp_path):
    path = tmp_path / "shared.sqlite3"
    stores = [SQLiteCatalogStore(path) for _ in range(4)]
    for store in stores:
        store.initialize()
    stores[0].register_queries([f"품목{index}" for index in range(10)], NOW)
    stores[0].enqueue_due(NOW)
    try:
        with ThreadPoolExecutor(max_workers=16) as threads:
            results = list(threads.map(lambda index: stores[index % 4].claim_job(NOW), range(16)))
        jobs = [job for job in results if job is not None]
        assert len(jobs) == 10
        assert len({job.job_id for job in jobs}) == 10
        assert all(job.attempts == 1 for job in jobs)
    finally:
        for store in stores:
            store.close()


def test_sqlite_persistence_survives_reopen_and_purge_removes_column_value(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    first = SQLiteCatalogStore(path)
    first.initialize()
    first.put_curated("스푼", [offer()], NOW, NOW + timedelta(minutes=1))
    first.close()
    second = SQLiteCatalogStore(path)
    second.initialize()
    assert second.read_snapshot("스푼", "CURATED").offers == (offer(),)
    assert second.purge_expired(NOW + timedelta(minutes=1)) == 1
    second.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT offers_json, revision FROM catalog_snapshots").fetchone()
    assert row[0] is None
    assert row[1] is not None


def test_claim_prioritizes_never_collected_then_oldest_snapshot(store):
    first = queued(store, query="이전 수집")
    store.complete_job(first, [offer()], NOW, NOW + timedelta(days=5))
    later = NOW + timedelta(seconds=5)
    recent = queued(store, now=later, query="최근 수집")
    store.complete_job(recent, [offer()], later, NOW + timedelta(days=5))
    store.register_queries(["미수집"], later)
    tomorrow = NOW + timedelta(days=1)
    store.enqueue_due(tomorrow)
    assert store.claim_job(tomorrow).query_key == "미수집"
    assert store.claim_job(tomorrow).query_key == "이전 수집"
    assert store.claim_job(tomorrow).query_key == "최근 수집"


def test_failed_never_collected_query_cannot_starve_unattempted_query(store):
    job = queued(store, query="실패한 품목")
    store.fail_job(job, "PROVIDER_TIMEOUT", NOW, retry_after_seconds=0)
    store.register_queries(["미시도 품목"], NOW)
    store.enqueue_due(NOW)
    assert store.claim_job(NOW).query_key == "미시도 품목"


@pytest.mark.parametrize("initial", ["MANUAL", "BE_MATERIAL"])
@pytest.mark.parametrize("incoming", ["MANUAL", "BE_MATERIAL"])
def test_registration_origin_precedence_and_reactivation(store, initial, incoming):
    assert store.register_queries(["계량 스푼"], NOW, origin=initial) == 1
    with store._transaction() as connection:
        row = connection.execute("SELECT origin FROM catalog_queries").fetchone()
        assert row["origin"] == initial
        connection.execute("UPDATE catalog_queries SET active = 0")
    later = NOW + timedelta(seconds=1)
    for _ in range(2):
        assert store.register_queries(["계량 스푼"], later, origin=incoming) == 0
    expected = "MANUAL" if "MANUAL" in (initial, incoming) else "BE_MATERIAL"
    with store._transaction(write=False) as connection:
        row = connection.execute(
            "SELECT active, origin, updated_at FROM catalog_queries"
        ).fetchone()
        assert tuple(row) == (1, expected, later.timestamp())


@pytest.mark.parametrize("origin", ["OTHER", "", None])
def test_invalid_origin_does_not_register_any_queries(store, origin):
    with pytest.raises(ValueError, match="invalid query origin"):
        store.register_queries(["계량스푼"], NOW, origin=origin)
    with store._transaction(write=False) as connection:
        assert connection.execute("SELECT COUNT(*) FROM catalog_queries").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["fallback", "curated"])
def test_operator_edits_promote_automatic_queries_to_manual(store, operation):
    store.register_queries(["계량스푼", "계량 스푼"], NOW, origin="BE_MATERIAL")
    if operation == "fallback":
        store.set_fallbacks("계량스푼", ["계량 스푼"], NOW)
    else:
        store.put_curated("계량스푼", [offer()], NOW, NOW + timedelta(hours=1))
    with store._transaction(write=False) as connection:
        rows = dict(connection.execute("SELECT query_key, origin FROM catalog_queries"))
        assert rows["계량스푼"] == "MANUAL"
        assert rows["계량 스푼"] == ("MANUAL" if operation == "fallback" else "BE_MATERIAL")
