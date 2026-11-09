"""HTTP failure contracts used by the app's error and retry UI."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


ORIGIN = "http://localhost:8081"


def test_recommendation_failure_returns_safe_json_and_logs_cause(monkeypatch, caplog):
    """500은 안전한 JSON/CORS를 반환하고 실제 원인은 서버 로그에 남기는지 확인한다."""
    def fail(_body):
        """서버 내부 오류 상황을 재현하도록 추천 계산에서 예외를 발생시킨다."""
        raise RuntimeError("internal diagnostic that must not reach the client")

    monkeypatch.setenv("CORS_ORIGINS", ORIGIN)
    with TestClient(create_app()) as client:
        monkeypatch.setattr(client.app.state.recommender, "recommend", fail)
        with caplog.at_level(logging.ERROR, logger="app.api"):
            response = client.post(
                "/api/v1/recommendations", json={"answers": {}}, headers={"Origin": ORIGIN}
            )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "추천 처리 중 서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    }
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "internal diagnostic" not in response.text
    assert "internal diagnostic" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("state", ["missing", "unavailable"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/recommendations"),
        ("GET", "/api/v1/catalog"),
        ("GET", "/api/v1/questionnaire"),
        ("GET", "/health"),
    ],
)
def test_unavailable_service_returns_503(monkeypatch, state, method, path):
    """엔진 참조가 없거나 None이면 모든 관련 경로가 503과 CORS를 반환하는지 확인한다."""
    monkeypatch.setenv("CORS_ORIGINS", ORIGIN)
    with TestClient(create_app()) as client:
        if state == "missing":
            del client.app.state.recommender
        else:
            client.app.state.recommender = None
        kwargs = {"json": {"answers": {}}} if method == "POST" else {}
        response = client.request(method, path, headers={"Origin": ORIGIN}, **kwargs)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "추천 서비스를 일시적으로 이용할 수 없습니다. 잠시 후 다시 시도해 주세요."
    }
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_service_can_resume_after_unavailability():
    """일시적으로 엔진을 잃었다가 복구한 뒤 정상 요청이 가능한지 확인한다."""
    with TestClient(create_app()) as client:
        service = client.app.state.recommender
        client.app.state.recommender = None
        assert client.post("/api/v1/recommendations", json={}).status_code == 503
        client.app.state.recommender = service
        assert client.post("/api/v1/recommendations", json={}).status_code == 200
        assert client.get("/health").json()["status"] == "ok"


def test_server_error_responses_are_documented():
    """500·503 JSON 응답과 기존 422가 OpenAPI에 포함되는지 확인한다."""
    schema = create_app().openapi()
    responses = schema["paths"]["/api/v1/recommendations"]["post"]["responses"]
    for status in ("500", "503"):
        assert responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
    assert "422" in responses
    assert "503" in schema["paths"]["/health"]["get"]["responses"]
