"""Spring 백엔드가 호출하는 POST /backend/coach/chat 계약을 검증합니다.

검색은 BM25만 사용하고 Claude 키가 없으므로 원격 호출이 없습니다. 실제 DB와 .env는 읽지 않습니다.
"""

from dataclasses import replace
import importlib
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

TOKEN = "test-service-token"
URL = "/backend/coach/chat"
DATA = Path(__file__).resolve().parents[1] / "data"
# 백엔드 DB의 요리 활동 코드는 이 카탈로그의 activity_id와 같습니다(프런트 카탈로그 162건과 동일).
CATALOG = {json.loads(line)["activity_id"] for line in (DATA / "activities.jsonl").open(encoding="utf-8") if line.strip()}


def java_length(text):
    """Java의 String.length()처럼 UTF-16 단위로 셉니다. 이모지 한 글자가 2입니다."""
    return len(text.encode("utf-16-le")) // 2


def filled(value, limit):
    """Spring 검증기의 text(): null·공백뿐·길이 초과는 거절입니다.

    공백 판정은 Java보다 넓습니다(줄바꿈 없는 공백 U+00A0도 공백으로 봄). 놓치는 쪽이 아니라 더 엄격한 쪽입니다.
    """
    return isinstance(value, str) and bool(value.strip()) and java_length(value) <= limit


def spring_rejections(conversation_id, reply):
    """백엔드의 CoachAiResponseValidator와 CoachTurnTransactions.finish가 502로 바꾸는 사유를 모읍니다."""
    found = [] if reply["conversationId"] == conversation_id else ["conversationId"]
    found += [name for name, limit in (("answer", 32000), ("answerVersion", 80), ("mode", 80))
              if not filled(reply[name], limit)]
    if not isinstance(reply["replayed"], bool) or len(reply["references"]) > 50 or len(reply["suggestedActivities"]) > 20:
        found.append("shape")
    codes = set()
    for reference in reply["references"]:
        kind, code = reference["sourceType"], reference["activityCode"]
        if not (kind in {"ACTIVITY", "KNOWLEDGE", "RECIPE"}
                and filled(reference["sourceCode"], 200) and filled(reference["title"], 1000)
                and (code is None if kind == "RECIPE" else filled(code, 100))
                and (kind != "ACTIVITY" or reference["sourceCode"] == code)):
            found.append(f"reference:{reference}")
        if code is not None:
            codes.add(code)
    for suggested in reply["suggestedActivities"]:
        if not (filled(suggested["activityCode"], 100) and filled(suggested["title"], 1000)
                and (suggested["reason"] is None or java_length(suggested["reason"]) <= 4000)):
            found.append(f"suggestion:{suggested}")
        codes.add(suggested["activityCode"])
    # 백엔드는 이 코드들을 DB의 공개된 요리 활동에서 찾습니다. 하나라도 없으면 답 전체가 502입니다.
    return found + sorted(f"unknown-code:{code}" for code in codes - CATALOG)


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
    """설명·문제 보고·실행·완료처럼 근거의 형태가 다른 턴도 백엔드 검증기가 받는 모양입니다."""
    recommended = ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))
    selected = ok(chat(client, 7, "두 번째 걸로 할게", "key-2"))

    reply = ok(chat(client, 7, text, "key-3"))

    for turn in (recommended, selected, reply):
        assert spring_rejections(7, turn) == []


def test_completion_points_to_the_app_instead_of_claiming_a_record(client):
    """V3의 완료 기록은 이 대화 안에만 남습니다. 앱의 활동 기록과 나무 성장은 앱 화면의 완료(API-29)로만 바뀝니다."""
    ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))
    ok(chat(client, 7, "두 번째 걸로 할게", "key-2"))

    done = ok(chat(client, 7, "완료했어", "key-3"))
    repeated = ok(chat(client, 7, "완료했어", "key-4"))  # 이미 완료로 적힌 활동
    records = ok(chat(client, 7, "기록 보여줘", "key-5"))

    for reply in (done, repeated):
        assert reply["mode"] == "action"
        assert "앱의 활동 기록과 나무 성장" in reply["answer"] and "활동 화면에서 완료" in reply["answer"]
        assert "기록했어요" not in reply["answer"] and "저장되어" not in reply["answer"]
    # 대화 안의 완료 표시는 그대로 남아, 기록을 물으면 이 대화의 완료로 답합니다.
    assert records["answer"].startswith("이 대화에서 완료한 활동은 1개입니다.")


def test_spring_rejections_names_what_the_backend_would_refuse():
    """위 검사가 빈 목록만 돌려주는 검사가 아님을 확인합니다."""
    reply = {
        "conversationId": 8, "answer": " ", "answerVersion": "coach-v3.0.0", "mode": "direct", "replayed": False,
        "references": [{"sourceType": "KNOWLEDGE", "sourceCode": "cook-0-2:tip", "activityCode": None, "title": "팁"}],
        "suggestedActivities": [{"activityCode": "cook-9-9", "title": "없는 활동", "reason": None}],
    }

    found = spring_rejections(7, reply)

    assert found[:2] == ["conversationId", "answer"]
    assert found[2].startswith("reference:") and found[-1] == "unknown-code:cook-9-9"


def test_slow_turn_is_answered_before_the_backend_stops_waiting(settings):
    """Spring은 45초만 기다립니다. 그 전에 한도 안내로 답해 504와 그 뒤의 대화 잠김(COACH409_1)을 막습니다."""
    app = importlib.import_module("app.main").create_app(replace(settings, backend_turn_seconds=0.3))
    search = app.state.retrieval.search
    entered, release = threading.Event(), threading.Event()

    def stuck_search(*args, **kwargs):
        entered.set()
        release.wait(timeout=10)  # 제한 시간에 끊기지 않으면 10초 뒤에야 답한다
        return search(*args, **kwargs)

    app.state.retrieval.search = stuck_search
    with TestClient(app) as client:
        started = time.perf_counter()
        reply = ok(chat(client, 7, "요리 활동 추천해줘", "key-1"))
        elapsed = time.perf_counter() - started
        # 공개 경로(/coach/messages)의 제한은 60초 그대로라, 이 경로의 제한(0.3초)보다 오래 걸리는 검색도 기다립니다.
        threading.Timer(0.6, release.set).start()  # 끊긴 턴의 검색 스레드도 이때 풀린다
        public = client.post("/coach/messages", json={"message": "요리 활동 추천해줘"}).json()

    assert entered.is_set() and elapsed < 5
    assert reply["mode"] == "insufficient_evidence"
    assert spring_rejections(7, reply) == []
    assert public["mode"] == "direct" and public["activities"]


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
