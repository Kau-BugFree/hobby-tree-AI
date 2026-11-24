import asyncio
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from item_recommender.batch import BatchWorker
from item_recommender.catalog_domain import RefreshJob, SEOUL
from item_recommender.config import Settings
from item_recommender.domain import Offer, ProviderError, SearchResult


def seoul(hour=3, minute=0, second=0, day=19):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=SEOUL)


class Clock:
    def __init__(self, now=None):
        self.now = now or seoul()

    def __call__(self):
        return self.now


class Store:
    """A recording store; actual SQL atomicity is tested in the store's suite."""

    def __init__(self):
        self.job = RefreshJob("job-1", "계량스푼", "계량스푼", "2026-11-19", 1, "lease-1")
        self.claims = []
        self.reservations = []
        self.failures = []
        self.deferrals = []
        self.completions = []
        self.offers = (sample_offer(),)
        self.retry = None
        self.claim_hook = self.reserve_hook = None
        self.saved = True
        self.scheduled = self.initialized = self.closed = 0
        self.registered = []
        self.fallbacks = []

    def claim_job(self, now, lease_seconds=30):
        self.claims.append((now, lease_seconds))
        if self.claim_hook:
            self.claim_hook()
        job, self.job = self.job, None
        return job

    def reserve_provider_slot(self, namespace, now, limit=6):
        self.reservations.append((namespace, now, limit))
        if self.reserve_hook:
            self.reserve_hook()
        return self.retry

    def complete_job(self, job, offers, now, expires_at, discarded_count=0, discard_reasons=()):
        self.completions.append((job, now, expires_at, discarded_count, discard_reasons))
        if self.saved:
            self.offers = tuple(offers)
        return self.saved

    def fail_job(self, job, code, now, retry_after_seconds=60, max_attempts=3):
        self.failures.append((job, code, now, retry_after_seconds))
        return self.saved

    def defer_job(self, job, code, now, retry_after_seconds=60):
        self.deferrals.append((job, code, now, retry_after_seconds))
        return self.saved

    def enqueue_due(self, now):
        self.scheduled += 1
        return 1

    def initialize(self):
        self.initialized += 1

    def register_queries(self, queries, now, *, origin="MANUAL"):
        self.registered_origin = origin
        self.registered.extend(queries)
        return len(set(queries))

    def set_fallbacks(self, primary, fallbacks, now):
        self.fallbacks.append((primary, fallbacks))

    def job_counts(self):
        return {"QUEUED": int(self.job is not None)}

    def purge_expired(self, now):
        return 2

    def close(self):
        self.closed += 1


class Provider:
    def __init__(self, *results):
        self.results = deque(results or (SearchResult((sample_offer(),)),))
        self.calls = []
        self.closed = False

    async def search(self, query, limit=20):
        self.calls.append((query, limit))
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self):
        self.closed = True


def sample_offer():
    return Offer(
        "ref-1", "계량스푼", "1000원", 1000, None, "https://deg.kr/example", "m", "몰", seoul()
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        adpick_api_key="test-not-real-key",
        adpick_filtering_approved=True,
        storage_approved=True,
        catalog_db_path=tmp_path / "catalog.sqlite3",
        state_db_path=tmp_path / "provider.sqlite3",
    )


def run(store, provider, settings, clock=None):
    return asyncio.run(BatchWorker(store, provider, settings, clock or Clock()).run_once())


@pytest.mark.parametrize("now", [seoul(2, 59, 59), seoul(5), seoul(13), seoul(0)])
def test_outside_window_neither_claims_nor_calls(now, settings):
    store, provider = Store(), Provider()
    result = run(store, provider, settings, Clock(now))
    assert result["status"] == "OUTSIDE_BATCH_WINDOW"
    assert result["retryAfterSeconds"] > 0
    assert not store.claims and not provider.calls


@pytest.mark.parametrize("now", [seoul(3), seoul(4, 59), seoul(3).astimezone(timezone.utc)])
def test_active_window_uses_existing_job_and_global_six_per_minute_slot(now, settings):
    store, provider = Store(), Provider()
    result = run(store, provider, settings, Clock(now))
    assert result["status"] == "COMPLETED"
    assert provider.calls == [("계량스푼", 20)]
    assert store.scheduled == 0
    assert len(store.reservations[0][0]) == 64
    assert settings.adpick_api_key not in store.reservations[0][0]
    assert store.reservations[0][2] == 6
    assert store.completions[0][2] == now + timedelta(seconds=86400)


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"adpick_api_key": None}, "PROVIDER_NOT_CONFIGURED"),
        ({"adpick_filtering_approved": False}, "PROVIDER_FILTERING_NOT_APPROVED"),
        ({"storage_approved": False}, "STORAGE_NOT_APPROVED"),
    ],
)
def test_configuration_gate_does_not_consume_job_or_quota(changes, status, settings):
    store, provider = Store(), Provider()
    assert run(store, provider, replace(settings, **changes))["status"] == status
    assert not store.claims and not store.reservations and not provider.calls


def test_empty_queue_is_idle_and_never_auto_enqueued(settings):
    store, provider = Store(), Provider()
    store.job = None
    assert run(store, provider, settings) == {"status": "IDLE"}
    assert not provider.calls and not store.reservations and not store.scheduled


def test_default_clock_prefers_database_for_window_snapshot_and_offer_time(settings, monkeypatch):
    from item_recommender import batch

    store, provider = Store(), Provider()
    database_now = seoul(4).astimezone(timezone.utc)
    store.current_time = lambda: database_now
    monkeypatch.setattr(batch, "utcnow", lambda: seoul(5))
    result = asyncio.run(BatchWorker(store, provider, settings).run_once())
    assert result["status"] == "COMPLETED"
    assert store.claims[0][0] == database_now
    assert store.completions[0][1] == database_now
    assert store.completions[0][2] == database_now + timedelta(seconds=86400)
    assert all(offer.fetched_at == database_now for offer in store.offers)


def test_database_time_outside_window_prevents_call_despite_local_clock(settings, monkeypatch):
    from item_recommender import batch

    store, provider = Store(), Provider()
    store.current_time = lambda: seoul(5).astimezone(timezone.utc)
    monkeypatch.setattr(batch, "utcnow", lambda: seoul(3))
    result = asyncio.run(BatchWorker(store, provider, settings).run_once())
    assert result["status"] == "OUTSIDE_BATCH_WINDOW"
    assert not store.claims and not store.reservations and not provider.calls


def test_explicit_test_clock_overrides_database_clock(settings):
    store, provider = Store(), Provider()

    def forbidden_clock():
        raise AssertionError("an explicitly injected clock must take precedence")

    store.current_time = forbidden_clock
    assert run(store, provider, settings, Clock(seoul(4)))["status"] == "COMPLETED"


def test_store_without_database_clock_uses_utcnow(settings, monkeypatch):
    from item_recommender import batch

    store, provider = Store(), Provider()
    monkeypatch.setattr(batch, "utcnow", lambda: seoul(3))
    assert asyncio.run(BatchWorker(store, provider, settings).run_once())["status"] == "COMPLETED"


@pytest.mark.parametrize("fail_on_read", [1, 2, 3, 4])
def test_database_clock_failure_is_safe_and_never_uses_local_fallback(fail_on_read, settings):
    store, provider = Store(), Provider()
    readings = 0

    def database_clock():
        nonlocal readings
        readings += 1
        if readings == fail_on_read:
            raise RuntimeError("postgresql://private:secret@example.invalid/db")
        return seoul(4)

    store.current_time = database_clock
    result = asyncio.run(BatchWorker(store, provider, settings).run_once())
    assert result["status"] == "CATALOG_UNAVAILABLE"
    assert "secret" not in json.dumps(result)
    assert len(provider.calls) == (1 if fail_on_read == 4 else 0)
    assert not store.completions


def test_failure_recording_uses_database_clock(settings):
    store = Store()
    database_now = seoul(4).astimezone(timezone.utc)
    store.current_time = lambda: database_now
    provider = Provider(ProviderError("PROVIDER_TIMEOUT"))
    result = asyncio.run(BatchWorker(store, provider, settings).run_once())
    assert result["status"] == "FAILED"
    assert store.failures[0][2] == database_now


def test_naive_database_clock_fails_closed(settings):
    store, provider = Store(), Provider()
    store.current_time = lambda: datetime(2026, 11, 19, 4)
    result = asyncio.run(BatchWorker(store, provider, settings).run_once())
    assert result["status"] == "CATALOG_UNAVAILABLE"
    assert not provider.calls and not store.claims


@pytest.mark.parametrize("phase", ["claim", "reserve"])
def test_rechecks_window_after_database_operations(phase, settings):
    store, provider, clock = Store(), Provider(), Clock(seoul(4, 59, 59))
    setattr(store, phase + "_hook", lambda: setattr(clock, "now", seoul(5)))
    result = run(store, provider, settings, clock)
    assert result["status"] == "DEFERRED"
    assert result["providerStatus"] == "BATCH_WINDOW_CLOSED"
    assert not provider.calls and not store.failures
    assert store.deferrals[0][3] >= 22 * 60 * 60


def test_global_quota_defers_without_api_call_or_failed_attempt(settings):
    store, provider = Store(), Provider()
    store.retry = 47
    result = run(store, provider, settings)
    assert result["status"] == "DEFERRED"
    assert result["retryAfterSeconds"] == 47
    assert not provider.calls and not store.failures
    assert store.deferrals[0][1] == "PROVIDER_RATE_LIMITED"


@pytest.mark.parametrize("code", ["PROVIDER_RATE_LIMITED", "PROVIDER_CIRCUIT_OPEN"])
def test_provider_backoff_defers_instead_of_using_failure_budget(code, settings):
    store, provider = Store(), Provider(ProviderError(code, 90))
    result = run(store, provider, settings)
    assert result["status"] == "DEFERRED"
    assert result["retryAfterSeconds"] == 90
    assert store.deferrals and not store.failures


def test_excessive_retry_after_does_not_break_database_update(settings):
    store = Store()
    result = run(store, Provider(ProviderError("PROVIDER_RATE_LIMITED", 10**10)), settings)
    assert result["status"] == "DEFERRED"
    assert result["retryAfterSeconds"] == 604800


def test_inflight_request_is_cancelled_at_window_deadline(settings):
    class SlowProvider:
        async def search(self, query, limit=20):
            await asyncio.sleep(1)
            pytest.fail("request should have been cancelled at the window boundary")

    store = Store()
    before_close = Clock(seoul(4, 59, 59).replace(microsecond=999999))
    result = run(store, SlowProvider(), settings, before_close)
    assert result["status"] == "FAILED"
    assert result["providerStatus"] == "PROVIDER_TIMEOUT"
    assert not store.completions


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("PROVIDER_TIMEOUT"),
        ProviderError("PROVIDER_AUTH_ERROR"),
        RuntimeError("secret-key in private URL"),
    ],
)
def test_failures_keep_previous_snapshot_and_hide_exception_text(error, settings):
    store, provider = Store(), Provider(error)
    old_offers = store.offers
    result = run(store, provider, settings)
    assert result["status"] == "FAILED"
    assert store.offers == old_offers
    assert not store.completions
    assert "secret-key" not in json.dumps(result)


def test_true_empty_success_replaces_old_snapshot(settings):
    store, provider = Store(), Provider(SearchResult())
    result = run(store, provider, settings)
    assert result["status"] == "COMPLETED"
    assert result["offerCount"] == 0
    assert store.offers == ()


def test_only_irrelevant_results_replace_old_snapshot_with_empty(settings):
    store = Store()
    irrelevant = replace(sample_offer(), title="계량스푼 보관함")
    result = run(store, Provider(SearchResult((irrelevant,))), settings)
    assert result["status"] == "COMPLETED"
    assert result["offerCount"] == 0
    assert result["irrelevantCount"] == 1
    assert store.offers == ()


def test_only_irrelevant_plus_malformed_results_do_not_erase_previous_snapshot(settings):
    store = Store()
    old = store.offers
    irrelevant = replace(sample_offer(), title="계량스푼 보관함")
    result = run(store, Provider(SearchResult((irrelevant,), 1)), settings)
    assert result["providerStatus"] == "PROVIDER_UNUSABLE_RESULTS"
    assert store.offers == old


def test_all_discarded_is_failure_not_empty_success(settings):
    store = Store()
    provider = Provider(SearchResult((), 3, (("PURCHASE_HOST_NOT_ALLOWED", 3),)))
    old_offers = store.offers
    result = run(store, provider, settings)
    assert result["providerStatus"] == "PROVIDER_UNUSABLE_RESULTS"
    assert result["discardReasons"] == {"PURCHASE_HOST_NOT_ALLOWED": 3}
    assert store.offers == old_offers and not store.completions


def test_partial_usable_success_keeps_discard_diagnostics(settings):
    store = Store()
    provider = Provider(SearchResult((sample_offer(),), 1, (("INVALID_TITLE", 1),)))
    result = run(store, provider, settings)
    assert result["status"] == "COMPLETED"
    assert result["discardedCount"] == 1
    assert store.completions[0][-1] == (("INVALID_TITLE", 1),)


def test_expired_lease_does_not_claim_success(settings):
    store = Store()
    store.saved = False
    assert run(store, Provider(), settings)["status"] == "LEASE_LOST"


def test_next_day_can_process_new_database_job_after_yesterday_failure(settings):
    store, clock = Store(), Clock(seoul(4, 59))
    provider = Provider(ProviderError("PROVIDER_TIMEOUT"), SearchResult((sample_offer(),)))
    assert run(store, provider, settings, clock)["status"] == "FAILED"
    clock.now = seoul(5)
    assert run(store, provider, settings, clock)["status"] == "OUTSIDE_BATCH_WINDOW"
    clock.now = seoul(3, day=20)
    store.job = RefreshJob("job-next-day", "계량스푼", "계량스푼", "2026-11-20", 1, "lease-2")
    assert run(store, provider, settings, clock)["status"] == "COMPLETED"
    assert len(provider.calls) == 2
    assert store.scheduled == 0


def test_sqlite_worker_preserves_live_snapshot_on_failure_then_replaces_empty(settings):
    from item_recommender.catalog_store import SQLiteCatalogStore

    store = SQLiteCatalogStore(settings.catalog_db_path)
    store.initialize()
    clock = Clock(seoul(4, 59))
    provider = Provider(
        SearchResult((sample_offer(),)), ProviderError("PROVIDER_TIMEOUT"), SearchResult()
    )
    try:
        store.register_queries(["계량스푼"], clock())
        store.enqueue_due(clock())
        assert run(store, provider, settings, clock)["status"] == "COMPLETED"
        original = store.read_snapshot("계량스푼")
        clock.now = seoul(3, day=20)
        store.enqueue_due(clock())
        assert run(store, provider, settings, clock)["status"] == "FAILED"
        failed = store.read_snapshot("계량스푼")
        assert failed.offers == original.offers
        assert failed.revision == original.revision
        assert failed.expires_at > clock()
        clock.now += timedelta(minutes=1)
        assert run(store, provider, settings, clock)["status"] == "COMPLETED"
        empty = store.read_snapshot("계량스푼")
        assert empty.offers == () and empty.last_status == "EMPTY"
        assert empty.revision != original.revision
    finally:
        store.close()


def test_workers_share_six_per_minute_quota_and_defer_seventh_job(settings):
    from item_recommender.catalog_store import SQLiteCatalogStore

    first = SQLiteCatalogStore(settings.catalog_db_path)
    first.initialize()
    second = SQLiteCatalogStore(settings.catalog_db_path)
    provider = Provider(*(SearchResult() for _ in range(7)))
    clock = Clock()
    try:
        first.register_queries([f"준비물 {i}" for i in range(7)], clock())
        first.enqueue_due(clock())
        for i in range(6):
            assert (
                run(first if i % 2 else second, provider, settings, clock)["status"] == "COMPLETED"
            )
        deferred = run(second, provider, settings, clock)
        assert deferred["status"] == "DEFERRED"
        assert deferred["providerStatus"] == "PROVIDER_RATE_LIMITED"
        assert len(provider.calls) == 6
        clock.now += timedelta(seconds=61)
        assert run(first, provider, settings, clock)["status"] == "COMPLETED"
        assert first.job_counts().get("DONE") == 7
    finally:
        first.close()
        second.close()


@pytest.fixture
def cli_setup(monkeypatch, settings):
    from item_recommender import cli

    store = Store()
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "open_store", lambda settings: store)
    monkeypatch.setattr(cli, "utcnow", lambda: seoul())
    return cli, store


def test_cli_registers_only_exported_material_names(cli_setup, tmp_path, capsys):
    cli, store = cli_setup
    path = tmp_path / "materials.json"
    path.write_text(
        json.dumps(
            {
                "result": {"materials": [{"name": "계량스푼"}, {"name": "앞치마"}]},
                "contextText": "PRIVATE",
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["register", "--file", str(path)]) == 0
    assert store.registered == ["계량스푼", "앞치마"]
    assert "PRIVATE" not in capsys.readouterr().out
    assert store.closed == 1


def test_cli_validates_entire_import_before_writes(cli_setup, tmp_path, capsys):
    cli, store = cli_setup
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"materials": [{"name": "계량스푼"}, {"name": 123}]}))
    assert cli.main(["register", "--file", str(path)]) == 2
    assert not store.registered
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID_INPUT"


def test_cli_fallbacks_are_operator_input_not_generated_queries(cli_setup, tmp_path):
    cli, store = cli_setup
    path = tmp_path / "fallbacks.json"
    path.write_text(json.dumps({"primary": "계량스푼", "fallbacks": ["계량 스푼"]}))
    assert cli.main(["fallbacks", "--file", str(path)]) == 0
    assert store.fallbacks == [("계량스푼", ["계량 스푼"])]


def test_cli_schedule_is_local_sqlite_only(cli_setup, monkeypatch, settings, capsys):
    cli, store = cli_setup
    monkeypatch.setattr(
        cli.Settings,
        "from_env",
        lambda: replace(settings, catalog_database_url="postgresql://example.invalid/db"),
    )
    assert cli.main(["schedule"]) == 2
    assert not store.scheduled
    assert json.loads(capsys.readouterr().out)["status"] == "SCHEDULE_MANAGED_BY_DATABASE"


def test_cli_schedule_outside_window_does_not_enqueue(cli_setup, monkeypatch):
    cli, store = cli_setup
    monkeypatch.setattr(cli, "utcnow", lambda: seoul(5))
    assert cli.main(["schedule"]) == 0
    assert not store.scheduled


def test_cli_schedule_at_three_enqueues_without_external_provider(cli_setup):
    cli, store = cli_setup
    assert cli.main(["schedule"]) == 0
    assert store.scheduled == 1


def test_cli_status_and_init_and_purge_need_no_provider(cli_setup, capsys):
    cli, store = cli_setup
    assert cli.main(["init-db"]) == 0
    assert store.initialized == 1
    assert cli.main(["status"]) == 0
    assert cli.main(["purge"]) == 0
    summaries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["status"] for row in summaries] == ["INITIALIZED", "OK", "PURGED"]


def test_cli_never_prints_driver_error_with_secret_dsn(cli_setup, monkeypatch, capsys):
    cli, _ = cli_setup

    def fail(settings):
        raise RuntimeError("postgresql://private:secret@example.invalid/db")

    monkeypatch.setattr(cli, "open_store", fail)
    assert cli.main(["status"]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "COMMAND_FAILED"}


def test_cli_has_no_force_time_or_clock_override(cli_setup):
    cli, store = cli_setup
    with pytest.raises(SystemExit):
        cli.main(["worker", "--force"])
    assert not store.claims


def test_worker_loop_honors_backoff_and_closes_provider(cli_setup, monkeypatch, settings):
    cli, store = cli_setup
    provider = Provider()
    delays = []

    class DeferredWorker:
        def __init__(self, *args):
            pass

        async def run_once(self):
            return {"status": "DEFERRED", "retryAfterSeconds": 47}

    async def sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError()

    monkeypatch.setattr(cli, "AdpickClient", lambda settings: provider)
    monkeypatch.setattr(cli, "BatchWorker", DeferredWorker)
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli._worker(settings, store, False))
    assert delays == [47]
    assert provider.closed


@pytest.mark.parametrize("origin", [None, "MANUAL", "BE_MATERIAL"])
def test_cli_passes_registration_origin(cli_setup, tmp_path, capsys, origin):
    cli, store = cli_setup
    path = tmp_path / "materials.json"
    path.write_text(json.dumps({"materials": [{"name": "계량스푼"}]}))
    args = ["register", "--file", str(path)]
    if origin is not None:
        args += ["--origin", origin]
    assert cli.main(args) == 0
    assert store.registered_origin == (origin or "MANUAL")
    assert json.loads(capsys.readouterr().out)["origin"] == (origin or "MANUAL")


def test_cli_rejects_unknown_origin_before_registration(cli_setup, tmp_path):
    cli, store = cli_setup
    with pytest.raises(SystemExit) as error:
        cli.main(["register", "--file", str(tmp_path / "unused.json"), "--origin", "OTHER"])
    assert error.value.code == 2
    assert not store.registered
