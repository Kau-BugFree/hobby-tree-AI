"""Preparation-name length boundaries for API, administration, and CLI."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from item_recommender.catalog_store import SQLiteCatalogStore
from item_recommender.cli import _material_queries
from item_recommender.config import Settings
from item_recommender.main import create_app
from item_recommender.models import QueryRegistration


@pytest.mark.parametrize("length", [160, 161, 200, 201])
def test_recommendation_name_length(length):
    store = SQLiteCatalogStore(":memory:")
    try:
        app = create_app(Settings(storage_approved=True), store=store)
        with TestClient(app) as client:
            response = client.post(
                "/internal/v1/item-recommendations",
                json={
                    "activityId": 1,
                    "activityContentVersion": 1,
                    "reviewStatus": "PUBLISHED",
                    "materials": [{"materialId": 1, "name": "가" * length}],
                },
            )
            assert response.status_code == (200 if length <= 200 else 422)
    finally:
        store.close()


@pytest.mark.parametrize("length", [160, 161, 200, 201])
def test_registered_query_length(length):
    payload = {"queries": ["가" * length]}
    if length <= 200:
        assert QueryRegistration.model_validate(payload).queries == payload["queries"]
    else:
        with pytest.raises(ValidationError):
            QueryRegistration.model_validate(payload)


@pytest.mark.parametrize("length", [160, 161, 200, 201])
def test_cli_material_name_length(length):
    document = {"materials": [{"name": "가" * length}]}
    if length <= 200:
        assert _material_queries(document) == ["가" * length]
    else:
        with pytest.raises(ValueError):
            _material_queries(document)
