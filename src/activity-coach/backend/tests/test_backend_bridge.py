"""Spring 백엔드가 호출하는 POST /backend/coach/chat 계약을 검증합니다.

검색은 BM25만 사용하고 Claude 키가 없으므로 원격 호출이 없습니다. 실제 DB와 .env는 읽지 않습니다.
"""

from dataclasses import replace
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

TOKEN = "test-service-token"
URL = "/backend/coach/chat"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """임시 DB와 실제 카탈로그를 쓰는 설정입니다."""
    settings = Settings(
        data_dir=Path(__file__).resolve().parents[1] / "data",
        index_dir=tmp_path / "index",
        database_path=tmp_path / "coach.sqlite",
        embedding_enabled=False,
        reranker_enabled=False,
        anthropic_api_key="",
        allowed_hosts=("testserver",),  # TestClient가 보내는 Host
        backend_token=TOKEN,
    )
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    return settings


def build_client(settings):
    return TestClient(importlib.import_module("app.main").create_app(settings))


@pytest.fixture
def client(settings):
    with build_client(settings) as test_client:
        yield test_client


def chat(client, conversation_id, text, request_id, token=TOKEN, **fields):
    """백엔드가 보내는 형식 그대로 요청합니다. token=None이면 헤더를 보내지 않습니다."""
    headers = {} if token is None else {"X-Coach-Service-Token": token}
    return client.post(
        URL,
        headers=headers,
        json={"conversationId": conversation_id, "message": text, "requestId": request_id, **fields},
    )


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_recommendation_uses_the_spring_field_names(client):
    """Spring의 CoachChatResponse로 옮길 이름으로 답하고, 활동은 숫자 ID 대신 activity_code로 알립니다."""
    reply = ok(chat(client, 7, "요리 활동 추천해줘", "key-1", hobbyCode="cooking"))

    assert set(reply) == {
        "conversationId", "answer", "answerVersion", "mode", "references", "suggestedActivities", "replayed"
    }
    assert reply["conversationId"] == 7
    assert reply["answer"]
    assert reply["answerVersion"] == "coach-v3.0.0"
    assert reply["replayed"] is False
    assert len(reply["suggestedActivities"]) >= 2
    for suggested in reply["suggestedActivities"]:
        assert set(suggested) == {"activityCode", "title", "reason"}
        assert suggested["activityCode"].startswith("cook-")
        assert suggested["title"]
    for reference in reply["references"]:
        assert set(reference) == {"sourceType", "sourceCode", "activityCode", "title"}
        assert reference["sourceType"] == "ACTIVITY"
        assert reference["sourceCode"] == reference["activityCode"]


def test_recipes_and_coaching_notes_are_references_not_suggested_activities():
    """백엔드 DB에는 카탈로그 활동만 있습니다. 레시피를 활동으로 제안하면 백엔드가 ID를 찾지 못합니다."""
    from app.backend_bridge import backend_reply

    reply = {
        "text": "답", "mode": "reference", "diagnostics": {},
        "activities": [
            {"activity_id": "cook-0-2", "kind": "activity", "title": "드레싱", "description": "계량"},
            {"activity_id": "recipe-9", "kind": "recipe", "title": "Rice", "description": None},
            {"activity_id": "cook-0-9", "kind": "activity", "title": "요약 없는 활동"},
        ],
        "sources": [
            {"activity_id": "recipe-9", "kind": "recipe", "title": "Rice"},
            {"activity_id": "cook-0-2", "evidence_id": "cook-0-2:tip", "kind": "principle", "title": "계량 팁"},
        ],
    }

    result = backend_reply(7, reply)

    # 요약이 없는 활동의 reason은 빈 문자열이 아니라 null입니다. Spring의 reason도 null을 허용합니다.
    assert result["suggestedActivities"] == [
        {"activityCode": "cook-0-2", "title": "드레싱", "reason": "계량"},
        {"activityCode": "cook-0-9", "title": "요약 없는 활동", "reason": None},
    ]
    assert result["references"] == [
        {"sourceType": "RECIPE", "sourceCode": "recipe-9", "activityCode": None, "title": "Rice"},
        {"sourceType": "KNOWLEDGE", "sourceCode": "cook-0-2:tip", "activityCode": "cook-0-2", "title": "계량 팁"},
    ]


def test_hobby_code_may_be_omitted(client):
    """취미 없이 시작한 대화(userHobbyId 없음)는 요리 대화로 받습니다."""
    assert ok(chat(client, 1, "요리 활동 추천해줘", "key-1"))["suggestedActivities"]


def test_same_conversation_id_continues_the_conversation(client):
    """백엔드는 conversationId만 보냅니다. 세션 ID를 따로 보관하지 않아도 앞 턴의 추천·선택이 이어집니다."""
    recommended = ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))
    second = recommended["suggestedActivities"][1]["activityCode"]

    ok(chat(client, 7, "두 번째 걸로 할게", "key-2"))
    materials = ok(chat(client, 7, "준비물이 뭐야?", "key-3"))

    assert "준비물:" in materials["answer"]
    assert materials["references"][0]["activityCode"] == second


@pytest.mark.parametrize(
    "text", ["현재 단계를 쉽게 설명해줘", "2단계 계량이 잘 안돼", "몇 분 걸려?", "시작할게", "완료했어", "기록 보여줘"]
)
def test_every_kind_of_turn_converts_to_the_backend_shape(client, text):
    """설명·문제 보고·실행·완료처럼 근거의 형태가 다른 턴도 변환에서 실패하지 않습니다."""
    ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))
    ok(chat(client, 7, "두 번째 걸로 할게", "key-2"))

    reply = ok(chat(client, 7, text, "key-3"))

    assert reply["answer"]
    for reference in reply["references"]:
        assert reference["sourceType"] in {"ACTIVITY", "RECIPE", "KNOWLEDGE"}
        assert reference["sourceCode"]


def test_conversations_do_not_share_candidates(client):
    """다른 대화의 추천 후보를 선택할 수 없습니다."""
    ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))

    other = ok(chat(client, 8, "두 번째 걸로 할게", "key-1"))

    assert other["mode"] == "clarify"
    assert other["references"] == []


def test_conversation_survives_a_server_restart(settings):
    """대화와 세션의 연결은 DB에 저장되어 서버를 다시 띄워도 유지됩니다."""
    with build_client(settings) as first:
        second_code = ok(chat(first, 7, "요리 활동 추천해줘", "key-1"))["suggestedActivities"][1]["activityCode"]
    with build_client(settings) as restarted:
        ok(chat(restarted, 7, "두 번째 걸로 할게", "key-2"))
        materials = ok(chat(restarted, 7, "준비물이 뭐야?", "key-3"))

    assert materials["references"][0]["activityCode"] == second_code


def test_resent_request_returns_the_stored_answer(client):
    """같은 Idempotency-Key의 재전송은 다시 처리하지 않고 같은 답을 돌려줍니다."""
    first = ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))
    again = ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))

    assert again["replayed"] is True
    assert {**again, "replayed": False} == first


def test_request_id_reused_for_another_message_is_a_conflict(client):
    ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))

    response = chat(client, 7, "안녕", "key-1")

    assert response.status_code == 409
    assert response.json() == {"detail": "같은 request_id를 다른 질문에 사용할 수 없습니다."}


def test_unregistered_hobby_is_rejected(client):
    """요리 외 취미의 대화를 요리 코치로 조용히 바꾸지 않습니다."""
    response = chat(client, 7, "추천해줘", "key-1", hobbyCode="reading")

    assert response.status_code == 422
    assert response.json() == {"detail": "등록되지 않은 취미입니다."}


@pytest.mark.parametrize(
    "body",
    [
        {"conversationId": 0, "message": "안녕", "requestId": "key-1"},
        {"conversationId": 7, "message": "", "requestId": "key-1"},
        {"conversationId": 7, "message": "안녕"},
        {"conversationId": 7, "message": "안녕", "requestId": "key-1", "userId": 3},
        {"conversation_id": 7, "message": "안녕", "request_id": "key-1"},
        {"conversationId": 2**63, "message": "안녕", "requestId": "key-1"},
    ],
    ids=["conversation-id-below-1", "empty-message", "missing-request-id", "unknown-field",
         "snake-case-names", "conversation-id-beyond-java-long"],
)
def test_malformed_request_is_rejected(client, body):
    response = client.post(URL, headers={"X-Coach-Service-Token": TOKEN}, json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("token", [None, "", "wrong-token", TOKEN + "x"])
def test_missing_or_wrong_token_is_unauthorized(client, token):
    response = chat(client, 7, "요리 활동 추천해줘", "key-1", token=token)

    assert response.status_code == 401
    assert TOKEN not in response.text


def test_route_is_closed_until_a_token_is_configured(settings):
    """토큰을 설정하지 않은 서버는 빈 토큰을 보낸 요청도 받지 않습니다."""
    with build_client(replace(settings, backend_token="")) as client:
        for token in (None, ""):
            response = chat(client, 7, "요리 활동 추천해줘", "key-1", token=token)
            assert response.status_code == 503


def test_reply_does_not_reveal_the_internal_session(client):
    """백엔드 대화의 세션 ID는 응답에 드러나지 않습니다. 공개 경로로 그 대화를 읽는 열쇠가 되기 때문입니다."""
    reply = ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))

    assert "session_id" not in str(reply)
