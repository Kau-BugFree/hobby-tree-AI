import asyncio
import multiprocessing
from dataclasses import replace

import httpx
import pytest

from item_recommender.config import Settings
from item_recommender.domain import ProviderError
from item_recommender.provider import AdpickClient
from item_recommender.provider_state import ProviderState


def _worker_reservations(path):
    state = ProviderState(path, "shared-namespace-hash", 6, 3, 30, 2)
    outcomes = []
    for _ in range(4):
        try:
            state.reserve(1000)
            outcomes.append("RESERVED")
        except ProviderError as error:
            outcomes.append(error.code)
    return outcomes


@pytest.fixture
def settings(tmp_path):
    return Settings(
        adpick_api_key="offline-shared-key",
        adpick_filtering_approved=True,
        state_db_path=tmp_path / "shared.sqlite3",
    )


def test_shared_rolling_quota_across_clients(settings):
    now = [1000.0]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"success": True, "data": []})

    async def run():
        clients = [
            AdpickClient(settings, httpx.MockTransport(handler), clock=lambda: now[0])
            for _ in range(2)
        ]
        try:
            for i in range(6):
                await clients[i % 2].search("spoon")
            for client in clients:
                with pytest.raises(ProviderError, match="PROVIDER_RATE_LIMITED") as error:
                    await client.search("spoon")
                assert error.value.retry_after_seconds == 60
            assert len(calls) == 6
            now[0] += 59.99
            with pytest.raises(ProviderError, match="PROVIDER_RATE_LIMITED"):
                await clients[0].search("spoon")
            now[0] = 1060.0
            await clients[1].search("spoon")
            assert len(calls) == 7
        finally:
            for client in clients:
                await client.aclose()

    asyncio.run(run())


def test_atomic_parallel_quota(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"success": True, "data": []})

    async def run():
        clients = [
            AdpickClient(settings, httpx.MockTransport(handler), clock=lambda: 1000.0)
            for _ in range(4)
        ]
        try:
            results = await asyncio.gather(
                *[clients[i % 4].search("spoon") for i in range(16)], return_exceptions=True
            )
            assert len(calls) == 6
            assert sum(not isinstance(result, Exception) for result in results) == 6
            assert all(
                not isinstance(result, Exception) or result.code == "PROVIDER_RATE_LIMITED"
                for result in results
            )
        finally:
            for client in clients:
                await client.aclose()

    asyncio.run(run())


def test_quota_is_atomic_across_worker_processes(tmp_path):
    path = str(tmp_path / "workers.sqlite3")
    with multiprocessing.get_context("spawn").Pool(4) as workers:
        outcomes = [
            outcome for batch in workers.map(_worker_reservations, [path] * 4) for outcome in batch
        ]
    assert outcomes.count("RESERVED") == 6
    assert outcomes.count("PROVIDER_RATE_LIMITED") == 10


def test_failure_circuit_shared_and_reset(settings):
    now = [1000.0]
    calls = []
    status = [503]

    def handler(request):
        calls.append(request)
        return httpx.Response(status[0], json={"success": True, "data": []})

    async def run():
        clients = [
            AdpickClient(settings, httpx.MockTransport(handler), clock=lambda: now[0])
            for _ in range(2)
        ]
        try:
            for _ in range(3):
                with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE"):
                    await clients[0].search("spoon")
            with pytest.raises(ProviderError, match="PROVIDER_CIRCUIT_OPEN"):
                await clients[1].search("spoon")
            assert len(calls) == 3
            now[0] += settings.circuit_reset_seconds
            status[0] = 200
            await clients[1].search("spoon")
            await clients[0].search("spoon")
            assert len(calls) == 5
        finally:
            for client in clients:
                await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "http_code,expected",
    [(401, "PROVIDER_AUTH_ERROR"), (403, "PROVIDER_RATE_LIMITED"), (429, "PROVIDER_RATE_LIMITED")],
)
def test_auth_and_quota_immediately_block_further_calls(settings, http_code, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(http_code)

    async def run():
        client = AdpickClient(settings, httpx.MockTransport(handler), clock=lambda: 1000.0)
        try:
            with pytest.raises(ProviderError, match=expected):
                await client.search("spoon")
            with pytest.raises(ProviderError, match="PROVIDER_CIRCUIT_OPEN"):
                await client.search("spoon")
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


def test_keys_have_separate_quotas(settings):
    async def run():
        clients = [
            AdpickClient(
                replace(settings, adpick_api_key=key, rate_limit_per_minute=1),
                httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"success": True, "data": []})
                ),
            )
            for key in ("first-key", "second-key")
        ]
        try:
            for client in clients:
                await client.search("spoon")
                with pytest.raises(ProviderError, match="PROVIDER_RATE_LIMITED"):
                    await client.search("spoon")
        finally:
            for client in clients:
                await client.aclose()

    asyncio.run(run())


def test_older_success_cannot_close_newly_opened_circuit(tmp_path):
    state = ProviderState(tmp_path / "state.sqlite3", "hash", 10, 1, 30, 2)
    earlier = state.reserve(1000)
    state.reserve(1000)
    state.failure(1000)
    state.success(earlier)
    with pytest.raises(ProviderError, match="PROVIDER_CIRCUIT_OPEN"):
        state.reserve(1001)


def test_half_open_allows_only_one_probe(tmp_path):
    state = ProviderState(tmp_path / "state.sqlite3", "hash", 10, 1, 30, 2)
    state.reserve(1000)
    state.failure(1000)
    probe = state.reserve(1030)
    with pytest.raises(ProviderError, match="PROVIDER_CIRCUIT_OPEN"):
        state.reserve(1030)
    state.success(probe)
    state.reserve(1030)


def test_unwritable_state_fails_before_network(settings, tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    calls = []
    client = AdpickClient(
        replace(settings, state_db_path=directory),
        httpx.MockTransport(lambda r: calls.append(r)),
    )
    with pytest.raises(ProviderError, match="PROVIDER_STATE_UNAVAILABLE"):
        asyncio.run(client.search("spoon"))
    assert calls == []


@pytest.mark.parametrize(
    "retry_after,expected",
    [
        ("120", 120),
        ("Thu, 01 Jan 1970 00:20:00 GMT", 200),
        ("bad", 60),
        ("1", 60),
    ],
)
def test_provider_retry_after_is_shared_and_honored(settings, retry_after, expected):
    now = [1000.0]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": retry_after})

    async def run():
        clients = [
            AdpickClient(settings, httpx.MockTransport(handler), clock=lambda: now[0])
            for _ in range(2)
        ]
        try:
            with pytest.raises(ProviderError) as original:
                await clients[0].search("spoon")
            assert original.value.retry_after_seconds == expected
            now[0] += expected - 1
            with pytest.raises(ProviderError, match="PROVIDER_CIRCUIT_OPEN") as blocked:
                await clients[1].search("spoon")
            assert blocked.value.retry_after_seconds == 1
            assert len(calls) == 1
        finally:
            for client in clients:
                await client.aclose()

    asyncio.run(run())
