"""Real loopback HTTP over an isolated catalog; upstream uses MockTransport only."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from item_recommender.batch import BatchWorker  # noqa: E402
from item_recommender.catalog_domain import SEOUL  # noqa: E402
from item_recommender.catalog_store import SQLiteCatalogStore  # noqa: E402
from item_recommender.config import Settings  # noqa: E402
from item_recommender.main import create_app  # noqa: E402
from item_recommender.provider import AdpickClient  # noqa: E402

NOW = datetime(2026, 11, 19, 4, tzinfo=SEOUL)
PATH = "/internal/v1/item-recommendations"
QUERIES_PATH = "/internal/v1/catalog/queries"
BODY = {
    "activityId": 101,
    "activityContentVersion": 2,
    "reviewStatus": "PUBLISHED",
    "preferencesVersion": 4,
    "contextText": "상품당 1천원 이하, 플라스틱 제외",
    "materials": [{"materialId": 201, "name": "요가 매트", "isRequired": True}],
    "materialSelections": [{"materialId": 201, "budgetKrw": 20000}],
    "maxProductsPerMaterial": 5,
}


@contextmanager
def running(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("local server did not start")
            time.sleep(0.02)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False, timeout=5
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("local server did not stop")


def recommend(client, body=BODY):
    response = client.post(PATH, json=body)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["isSuccess"] is True
    assert payload["result"]["onboardingFilteringApplied"] is False
    assert payload["result"]["sort"] == "PRICE_ASC_NULLS_LAST"
    return payload["result"]


async def collect(store, settings, handler):
    """Only the worker holds a fake key; every search is synthetic."""
    provider = AdpickClient(settings, httpx.MockTransport(handler), clock=lambda: NOW.timestamp())
    try:
        worker = BatchWorker(store, provider, settings, clock=lambda: NOW)
        results = [await worker.run_once(), await worker.run_once()]
        assert all(result["status"] == "COMPLETED" for result in results)
        assert sorted(result["offerCount"] for result in results) == [0, 4]
        assert sum(result["irrelevantCount"] for result in results) == 1
        assert (await worker.run_once())["status"] == "IDLE"
    finally:
        await provider.aclose()


def main():
    report = {
        "upstream": "synthetic MockTransport; no external requests",
        "http": "real 127.0.0.1 loopback",
        "clock": "fixed 04:00 Asia/Seoul for batch and serving",
        "checks": {},
    }
    with tempfile.TemporaryDirectory(prefix="hobbytree-catalog-smoke-") as tmp:
        settings = Settings(
            catalog_db_path=Path(tmp) / "catalog.sqlite3",
            state_db_path=Path(tmp) / "state.sqlite3",
            storage_approved=True,
            service_token="smoke-internal-token-not-real",
            # Serving has neither an API key nor filtering approval.
            adpick_api_key=None,
            adpick_filtering_approved=False,
        )
        worker_settings = replace(
            settings, adpick_api_key="smoke-worker-key-not-real", adpick_filtering_approved=True
        )
        queries = []

        def mock_adpick(request):
            assert request.url.host == "biz.adpick.co.kr" and request.method == "GET"
            assert set(request.url.params) == {"q", "limit"}
            query = request.url.params["q"]
            assert query in {"요가 매트", "앞치마"}
            queries.append(query)
            rows = []
            if query == "요가 매트":
                for index, price in enumerate([30000, "19,000원", "12,000원", "가격 문의"]):
                    rows.append(
                        {
                            "title": "테스트 요가 매트",
                            "price": price,
                            "photo": "https://cdn.example.com/mat.jpg",
                            "cp_code": "TEST",
                            "cp_name": "테스트몰",
                            "commissionlink": f"https://link.adpick.co.kr/mock-{index}",
                        }
                    )
                rows.append(
                    {
                        **rows[0],
                        "title": "요가 매트 가방",
                        "commissionlink": "https://link.adpick.co.kr/mock-accessory",
                    }
                )
            return httpx.Response(200, json={"success": True, "data": rows})

        store = SQLiteCatalogStore(settings.catalog_db_path)
        try:
            app = create_app(settings, store=store, clock=lambda: NOW)
            with running(app) as client:
                health = client.get("/health").json()
                assert health["servingMode"] == "CATALOG_ONLY"
                assert health["upstreamCallsOnRequest"] is False
                assert health["providerConnectivityChecked"] is False
                assert settings.adpick_api_key is None
                assert client.post(PATH, json=BODY).status_code == 401
                assert client.post(QUERIES_PATH, json={"queries": ["요가 매트"]}).status_code == 401
                report["checks"]["internal_http_auth"] = "PASS"
                client.headers["X-Service-Token"] = settings.service_token

                before = recommend(client)
                assert before["status"] == "NOT_COLLECTED"
                assert not before["materialResults"][0]["products"] and queries == []
                report["checks"]["no_key_catalog_miss_never_searches"] = "PASS"

                registered = client.post(QUERIES_PATH, json={"queries": ["요가 매트", "앞치마"]})
                assert registered.status_code == 200
                assert registered.json()["result"]["registeredCount"] == 2
                assert store.enqueue_due(NOW) == 2
                asyncio.run(collect(store, worker_settings, mock_adpick))
                assert sorted(queries) == ["앞치마", "요가 매트"]
                report["checks"]["http_register_database_enqueue_mock_worker"] = "PASS"

                result = recommend(client)
                material = result["materialResults"][0]
                assert (
                    material["status"] == "AVAILABLE" and material["selectionSource"] == "PRIMARY"
                )
                assert material["necessityReason"] is None
                products = material["products"]
                assert [p["priceKrw"] for p in products] == [12000, 19000, 30000, None]
                assert all(p["availability"] == "UNKNOWN" for p in products)
                assert all(
                    p["purchaseUrl"].startswith("https://link.adpick.co.kr/") for p in products
                )
                comparisons = [p["budgetComparison"] for p in products]
                assert [c["status"] for c in comparisons] == [
                    "WITHIN_BUDGET",
                    "WITHIN_BUDGET",
                    "OVER_BUDGET",
                    "PRICE_UNKNOWN",
                ]
                assert [c["differenceKrw"] for c in comparisons] == [-8000, -1000, 10000, None]
                report["checks"]["price_ascending_unknown_last"] = "PASS"
                report["checks"]["budget_comparison_keeps_over_budget_products"] = "PASS"
                report["checks"]["necessity_reason_remains_null"] = "PASS"

                for context in (None, "스테인리스만", "상품당 0원 이하", "밤에만 할 수 있어요"):
                    ignored = recommend(client, {**BODY, "contextText": context})
                    assert ignored["materialResults"] == result["materialResults"]
                report["checks"]["onboarding_does_not_filter_or_rank"] = "PASS"

                less_budget = recommend(
                    client,
                    {**BODY, "materialSelections": [{"materialId": 201, "budgetKrw": 12000}]},
                )
                less_products = less_budget["materialResults"][0]["products"]
                assert [p["offerRef"] for p in less_products] == [p["offerRef"] for p in products]
                assert less_products[0]["budgetComparison"]["differenceKrw"] == 0
                assert less_products[1]["budgetComparison"]["status"] == "OVER_BUDGET"

                filtered = recommend(
                    client,
                    {
                        **BODY,
                        "materialSelections": [
                            {"materialId": 201, "filters": {"maxPriceKrw": 15000}}
                        ],
                    },
                )
                assert [p["priceKrw"] for p in filtered["materialResults"][0]["products"]] == [
                    12000
                ]
                assert (
                    filtered["materialResults"][0]["products"][0]["budgetComparison"]["status"]
                    == "NOT_SET"
                )
                report["checks"]["explicit_filter_is_separate_from_budget"] = "PASS"

                no_match = recommend(
                    client,
                    {
                        **BODY,
                        "materials": [{"materialId": 202, "name": "앞치마"}],
                        "materialSelections": [],
                    },
                )
                empty = no_match["materialResults"][0]
                assert empty["status"] == "NO_MATCH"
                assert empty["products"] == [] and empty["necessityReason"] is None
                assert empty["fallbackAction"] == "SHOW_MATERIAL_GUIDE"
                assert (
                    recommend(client, {**BODY, "materials": [], "materialSelections": []})["status"]
                    == "NO_MATERIALS"
                )
                report["checks"]["empty_catalog_and_empty_materials"] = "PASS"

                assert len(queries) == 2
                report["checks"]["repeated_http_reads_add_zero_upstream_calls"] = "PASS"
                report["mockUpstreamCallCount"] = len(queries)
        finally:
            store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
