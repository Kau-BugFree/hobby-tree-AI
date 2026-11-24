"""Catalog-only HTTP API with synthetic data, never a live commerce key."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from item_recommender.catalog_store import SQLiteCatalogStore
from item_recommender.config import Settings
from item_recommender.domain import Offer
from item_recommender.main import create_app
from item_recommender.matching import material_matches
from item_recommender.provider import AdpickClient

NOW = datetime(2026, 11, 18, 19, tzinfo=timezone.utc)  # 04:00 Seoul
PATH = "/internal/v1/item-recommendations"
BODY = {
    "activityId": 101,
    "activityContentVersion": 2,
    "reviewStatus": "PUBLISHED",
    "preferencesVersion": 3,
    "contextText": None,
    "materials": [{"materialId": 201, "name": "계량스푼", "quantity": "1개", "isRequired": True}],
}


def offer(price=10000, ref="one", title="스테인리스 계량스푼", merchant="TEST"):
    return Offer(
        ref,
        title,
        f"{price:,}원" if price is not None else "가격 확인 필요",
        price,
        "https://cdn.example.com/fixture.jpg",
        f"https://link.adpick.co.kr/{ref}",
        merchant,
        "테스트몰",
        NOW,
    )


@pytest.fixture
def store(tmp_path):
    catalog = SQLiteCatalogStore(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    yield catalog
    catalog.close()


def settings(tmp_path, **kwargs):
    return Settings(catalog_db_path=tmp_path / "catalog.sqlite3", storage_approved=True, **kwargs)


def seed(store, products, query="계량스푼", at=NOW, expires=None):
    store.register_queries([query], at)
    store.enqueue_due(at)
    job = store.claim_job(at)
    assert job and job.query == query
    assert store.complete_job(job, products, at, expires or at + timedelta(hours=24))
    return store.read_snapshot(query)


def test_read_only_price_sort_budget_and_no_onboarding(tmp_path, store, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Serving must never invoke commerce API")

    monkeypatch.setattr(AdpickClient, "search", forbidden)
    seed(
        store,
        [
            offer(30000, "expensive"),
            offer(None, "unknown"),
            offer(12000, "cheap"),
            offer(20000, "boundary"),
        ],
    )
    before = store.job_counts()
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: NOW)) as client:
        health = client.get("/health").json()
        assert health["servingMode"] == "CATALOG_ONLY" and not health["upstreamCallsOnRequest"]
        for context in [None, "플라스틱 제외, 상품당 1천원 이하", "내일 도착해야 해요"]:
            response = client.post(
                PATH,
                json={
                    **BODY,
                    "contextText": context,
                    "maxProductsPerMaterial": 5,
                    "materialSelections": [{"materialId": 201, "budgetKrw": 20000}],
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result["onboardingFilteringApplied"] is False
            material = result["materialResults"][0]
            assert material["status"] == "AVAILABLE" and material["necessityReason"] is None
            assert [p["priceKrw"] for p in material["products"]] == [12000, 20000, 30000, None]
            assert [p["budgetComparison"]["status"] for p in material["products"]] == [
                "WITHIN_BUDGET",
                "WITHIN_BUDGET",
                "OVER_BUDGET",
                "PRICE_UNKNOWN",
            ]
            assert [p["budgetComparison"]["differenceKrw"] for p in material["products"]] == [
                -8000,
                0,
                10000,
                None,
            ]
            assert response.headers["cache-control"] == "no-store"
    assert store.job_counts() == before


def test_unknown_material_does_not_enqueue_or_invent_ads(tmp_path, store):
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: NOW)) as client:
        item = client.post(PATH, json=BODY).json()["result"]["materialResults"][0]
        assert item["status"] == "NOT_COLLECTED"
        assert not item["products"] and item["fallbackAction"] == "SHOW_MATERIAL_GUIDE"
        assert item["necessityReason"] is None
    assert store.job_counts() == {}


def test_no_materials_and_storage_gate(tmp_path, store):
    with TestClient(create_app(Settings(catalog_db_path=tmp_path / "a.sqlite3"), store)) as client:
        assert client.post(PATH, json=BODY).json()["result"]["status"] == "STORAGE_NOT_APPROVED"
        assert (
            client.post(PATH, json={**BODY, "materials": []}).json()["result"]["status"]
            == "NO_MATERIALS"
        )


def test_alias_and_curated_fallback(tmp_path, store):
    seed(store, [offer(title="스테인리스 계량수저")], query="계량수저")
    store.set_fallbacks("계량스푼", ["계량수저"], NOW)
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: NOW)) as client:
        item = client.post(PATH, json=BODY).json()["result"]["materialResults"][0]
        assert item["status"] == "FALLBACK_AVAILABLE" and item["selectionSource"] == "ALIAS"
    store.set_fallbacks("계량스푼", [], NOW)
    store.put_curated("계량스푼", [offer()], NOW, NOW + timedelta(hours=1))
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: NOW)) as client:
        item = client.post(PATH, json=BODY).json()["result"]["materialResults"][0]
        assert item["selectionSource"] == "CURATED"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("empty", "NO_MATCH"),
        ("expired", "EXPIRED"),
        ("failure", "TEMPORARY_FAILURE"),
        ("invalid", "INVALID_CANDIDATES"),
        ("lease", "TEMPORARY_FAILURE"),
        ("window", "NOT_COLLECTED"),
    ],
)
def test_empty_states_are_distinct(tmp_path, store, kind, expected):
    if kind in {"empty", "expired"}:
        seed(store, [] if kind == "empty" else [offer()], expires=NOW + timedelta(seconds=1))
        clock = NOW + timedelta(seconds=2) if kind == "expired" else NOW
    else:
        store.register_queries(["계량스푼"], NOW)
        store.enqueue_due(NOW)
        job = store.claim_job(NOW)
        store.fail_job(
            job,
            {
                "failure": "PROVIDER_TIMEOUT",
                "invalid": "PROVIDER_UNUSABLE_RESULTS",
                "lease": "LEASE_EXPIRED",
                "window": "BATCH_WINDOW_CLOSED",
            }[kind],
            NOW,
        )
        clock = NOW
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: clock)) as client:
        item = client.post(PATH, json=BODY).json()["result"]["materialResults"][0]
        assert item["status"] == expected and not item["products"]


def test_failed_refresh_keeps_unexpired_catalog(tmp_path, store):
    seed(store, [offer()], expires=NOW + timedelta(hours=25))
    tomorrow = NOW + timedelta(days=1)
    store.enqueue_due(tomorrow)
    store.fail_job(store.claim_job(tomorrow), "PROVIDER_TIMEOUT", tomorrow)
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: tomorrow)) as client:
        item = client.post(PATH, json=BODY).json()["result"]["materialResults"][0]
        assert item["status"] == "AVAILABLE" and item["providerStatus"] == "PROVIDER_TIMEOUT"
        assert item["products"][0]["priceKrw"] == 10000


def test_pagination_filters_and_revision_reset(tmp_path, store):
    original = seed(
        store, [offer(p, str(p), merchant="A" if p < 30000 else "B") for p in [30000, 20000, 10000]]
    )
    with TestClient(create_app(settings(tmp_path), store, clock=lambda: NOW)) as client:

        def request(selection):
            return client.post(
                PATH,
                json={
                    **BODY,
                    "maxProductsPerMaterial": 1,
                    "materialSelections": [{"materialId": 201, **selection}],
                },
            ).json()["result"]["materialResults"][0]

        first = request({})
        assert first["nextOffset"] == 1 and first["totalCandidates"] == 3
        second = request({"offset": 1, "catalogRevision": original.revision})
        assert second["products"][0]["priceKrw"] == 20000
        assert request({"catalogRevision": "old-revision"})["status"] == "CATALOG_CHANGED"
        assert request({"offset": 3})["status"] == "PAGE_EMPTY"
        filtered = request({"filters": {"merchantCodes": ["B"]}, "budgetKrw": 1})
        assert filtered["products"][0]["priceKrw"] == 30000
        assert filtered["products"][0]["budgetComparison"]["status"] == "OVER_BUDGET"
        assert request({"filters": {"maxPriceKrw": 1}})["status"] == "FILTER_EMPTY"
        assert (
            request({"filters": {"minPriceKrw": 20000, "excludeOfferRefs": ["20000"]}})["products"][
                0
            ]["priceKrw"]
            == 30000
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"reviewStatus": "DRAFT"},
        {"activityId": True},
        {"activityContentVersion": "2"},
        {"preferencesVersion": -1},
        {"contextText": "x" * 201},
        {"materials": BODY["materials"] * 2},
        {"unexpected": "private-text"},
        {"materialSelections": [{"materialId": 999, "budgetKrw": 100}]},
        {"materialSelections": [{"materialId": 201, "budgetKrw": -1}]},
        {
            "materialSelections": [
                {"materialId": 201, "filters": {"minPriceKrw": 20, "maxPriceKrw": 10}}
            ]
        },
    ],
)
def test_invalid_requests_are_redacted(tmp_path, store, patch):
    with TestClient(create_app(settings(tmp_path), store)) as client:
        result = client.post(PATH, json={**BODY, **patch})
        assert result.status_code == 422
        assert "private-text" not in result.text


def test_auth_mount_and_body_limits(tmp_path, store):
    child = create_app(settings(tmp_path, service_token="internal-token"), store)
    parent = FastAPI()
    parent.mount("/items", child)
    with TestClient(child), TestClient(parent) as client:
        assert client.post("/items" + PATH, json=BODY).status_code == 401
        client.headers["X-Service-Token"] = "internal-token"
        assert client.post("/items" + PATH, json=BODY).status_code == 200
        assert client.post("/items" + PATH, content=b"x" * 65537).status_code == 413
    schema = child.openapi()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-Service-Token"


def test_operator_registration_and_curated_validation(tmp_path, store):
    config = settings(tmp_path, service_token="internal-token")
    with TestClient(create_app(config, store, clock=lambda: NOW)) as client:
        assert (
            client.post("/internal/v1/catalog/queries", json={"queries": ["계량스푼"]}).status_code
            == 401
        )
        client.headers["X-Service-Token"] = "internal-token"
        assert (
            client.post("/internal/v1/catalog/queries", json={"queries": ["계량스푼"]}).status_code
            == 200
        )
        assert (
            client.put(
                "/internal/v1/catalog/fallbacks",
                json={"primaryQuery": "계량스푼", "fallbackQueries": ["계량수저"]},
            ).status_code
            == 200
        )
        product = {
            "title": "계량스푼",
            "priceKrw": 10000,
            "purchaseUrl": "https://link.adpick.co.kr/fixture",
            "merchantCode": "TEST",
            "merchantName": "테스트몰",
        }
        payload = {
            "query": "계량스푼",
            "products": [product],
            "expiresAt": (NOW + timedelta(hours=1)).isoformat(),
        }
        assert client.put("/internal/v1/catalog/curated", json=payload).status_code == 200
        item = client.post(PATH, json=BODY).json()["result"]["materialResults"][0]
        assert item["selectionSource"] == "CURATED" and item["necessityReason"] is None
        bad = {**payload, "products": [{**product, "purchaseUrl": "javascript:alert(1)"}]}
        assert client.put("/internal/v1/catalog/curated", json=bad).status_code == 422
        assert (
            client.put(
                "/internal/v1/catalog/curated",
                json={**payload, "expiresAt": (NOW + timedelta(days=2)).isoformat()},
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/internal/v1/catalog/curated", json={**payload, "expiresAt": "2026-11-19T04:30:00"}
            ).status_code
            == 422
        )


def test_production_requires_internal_token_and_postgres():
    with pytest.raises(ValueError, match="SERVICE_TOKEN"):
        Settings(app_env="production")
    with pytest.raises(ValueError, match="CATALOG_DATABASE_URL"):
        Settings(app_env="production", service_token="x" * 32)
    with pytest.raises(ValueError, match="between 1 and 6"):
        Settings(rate_limit_per_minute=7)


def test_accessory_relevance():
    assert not material_matches("요가 매트", "요가 매트 가방")
    assert not material_matches("기타", "기타 피크")
    assert material_matches("기타 피크", "초보자 기타 피크")


def test_catalog_clock_controls_serving_expiry(tmp_path, store, monkeypatch):
    seed(store, [offer()], expires=NOW + timedelta(seconds=1))
    current = [NOW]
    monkeypatch.setattr(store, "current_time", lambda: current[0], raising=False)
    with TestClient(create_app(settings(tmp_path), store)) as client:
        assert client.post(PATH, json=BODY).json()["result"]["status"] == "AVAILABLE"
        current[0] += timedelta(seconds=2)
        assert client.post(PATH, json=BODY).json()["result"]["status"] == "EXPIRED"


def test_catalog_clock_failure_is_redacted(tmp_path, store, monkeypatch):
    def unavailable():
        raise RuntimeError("private-database-connection-details")

    monkeypatch.setattr(store, "current_time", unavailable, raising=False)
    with TestClient(create_app(settings(tmp_path), store)) as client:
        requests = [
            (client.post, PATH, BODY),
            (client.post, "/internal/v1/catalog/queries", {"queries": ["스푼"]}),
            (
                client.put,
                "/internal/v1/catalog/fallbacks",
                {"primaryQuery": "스푼", "fallbackQueries": []},
            ),
            (
                client.put,
                "/internal/v1/catalog/curated",
                {
                    "query": "스푼",
                    "products": [],
                    "expiresAt": (NOW + timedelta(hours=1)).isoformat(),
                },
            ),
        ]
        for method, path, body in requests:
            response = method(path, json=body)
            assert response.status_code == 503
            assert response.json()["code"] == "CATALOG_UNAVAILABLE"
            assert "private-database" not in response.text


def test_operator_registration_explicitly_keeps_manual_ownership(tmp_path, store):
    store.register_queries(["계량스푼"], NOW, origin="BE_MATERIAL")
    config = settings(tmp_path, service_token="internal-token")
    with TestClient(create_app(config, store, clock=lambda: NOW)) as client:
        client.headers["X-Service-Token"] = "internal-token"
        assert (
            client.post("/internal/v1/catalog/queries", json={"queries": ["계량스푼"]}).status_code
            == 200
        )
        with store._transaction(write=False) as connection:
            assert (
                connection.execute("SELECT origin FROM catalog_queries").fetchone()[0] == "MANUAL"
            )
        assert (
            client.post(
                "/internal/v1/catalog/queries",
                json={"queries": ["다른 준비물"], "origin": "BE_MATERIAL"},
            ).status_code
            == 422
        )
