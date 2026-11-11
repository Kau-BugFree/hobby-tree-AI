"""실제 카탈로그와 임시 SQLite로 Swagger 시연 흐름을 검증합니다.

검색은 BM25만 사용하므로 GPU 모델 로딩이나 Claude 원격 호출이 발생하지 않습니다.
기존 DB, 모델 캐시, .env 파일은 읽거나 수정하지 않습니다.
"""

from dataclasses import replace
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.retrieval import build_lexical_index


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """서버를 재생성해도 같은 임시 DB를 쓰는 앱 생성 함수를 제공합니다."""
    settings = Settings(
        data_dir=Path(__file__).resolve().parents[1] / "data",
        index_dir=tmp_path / "index",
        database_path=tmp_path / "coach.sqlite",
        embedding_enabled=False,
        reranker_enabled=False,
        anthropic_api_key="",
        allowed_hosts=("testserver",),  # TestClient가 보내는 Host
    )
    # 설정을 넘기지 않고 create_app()을 부르는 경로가 생겨도 실제 .env를 읽지 않게 합니다.
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    create_app = importlib.import_module("app.main").create_app

    def build(*, llm=None, api_key=""):
        """외부 생성기만 교체하고 검색·저장·라우팅은 실제 구현을 사용합니다."""
        return create_app(replace(settings, anthropic_api_key=api_key), llm=llm)

    return build


@pytest.fixture
def client(app_factory):
    """각 테스트에 수명이 제한된 실제 ASGI HTTP 클라이언트를 제공합니다."""
    with TestClient(app_factory()) as test_client:
        yield test_client


def create_session(client):
    """후속 질문을 연결할 요리 세션을 만들고 ID를 반환합니다."""
    response = client.post("/sessions", json={"hobby_key": "cooking"})
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def message(client, session_id, text, request_id, **fields):
    """정상 응답을 기대하는 대화 요청에서 공통 HTTP 검증을 수행합니다."""
    response = client.post(
        "/coach/messages",
        json={
            "session_id": session_id,
            "message": text,
            "request_id": request_id,
            **fields,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def select_activity(client, session_id, activity_id="cook-0-2"):
    """검색 순위에 의존하지 않는 개별 기능 테스트를 위해 활동을 선택합니다."""
    response = client.post(
        f"/sessions/{session_id}/select", json={"activity_id": activity_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def start_execution(client, session_id, request_id="start-1", activity_id="cook-0-2"):
    """단계 진행·완료 기록 API를 검증할 실행 건을 생성합니다."""
    response = client.post(
        f"/sessions/{session_id}/executions",
        json={
            "activity_id": activity_id,
            "request_id": request_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


class FakeLLM:
    """생성 성공·실패의 오케스트레이션만 검증하는 외부 API 대역입니다."""

    def __init__(self, status="generated"):
        """예정된 결과와 호출 인자를 저장할 목록을 준비합니다."""
        self.status = status
        self.calls = []

    async def explain(self, question, evidence, context, validate=True):
        """실제 네트워크 요청 없이 ClaudeService의 반환 계약을 재현합니다."""
        self.calls.append(
            {"question": question, "evidence": evidence, "context": context}
        )
        return {
            "text": (
                "계량스푼을 수평으로 잡고 눈금을 확인해요."
                if self.status == "generated"
                else None
            ),
            "llm_calls": 1,
            "status": self.status,
            "validation_errors": [],
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0},
            "model": "fake-claude",
            "cited_activity_ids": [evidence[0]["activity_id"]],
        }


def test_swagger_health_and_openapi_contract(client):
    """Swagger가 열리고 실제 시연에 필요한 요청 예시·경로가 노출됩니다."""
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger-ui" in docs.text
    schema = client.get("/openapi.json").json()
    expected = {
        "/sessions",
        "/sessions/{session_id}",
        "/coach/messages",
        "/search",
        "/activities/{activity_id}",
        "/sessions/{session_id}/executions",
        "/executions/{execution_id}",
        "/activities/{activity_id}/records",
        "/records",
    }
    assert expected <= schema["paths"].keys()
    examples = schema["paths"]["/coach/messages"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert {"recommend", "select", "explain", "complete"} <= examples.keys()
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["llm_configured"] is False
    assert health["supported_hobbies"] == ["cooking"]
    assert "anthropic_api_key" not in json.dumps(schema)
    assert "anthropic_api_key" not in health


def test_swagger_demo_flow_survives_restart_and_repeat(client, app_factory):
    """추천부터 기록까지 이어지는 시연과 서버 재시작·반복 수행을 검증합니다."""
    sid = create_session(client)
    recommended = message(client, sid, "오늘 20분 안에 할 요리 활동 추천해줘", "demo-1")
    assert len(recommended["activities"]) >= 2
    assert recommended["diagnostics"]["search_performed"] is True
    assert recommended["diagnostics"]["llm_calls"] == 0
    expected_id = recommended["activities"][1]["activity_id"]

    selected = message(client, sid, "두 번째 걸로 할게", "demo-2")
    assert selected["selected_activity_id"] == expected_id
    assert selected["current_step"] == 1
    assert selected["diagnostics"]["search_performed"] is False

    materials = message(client, sid, "준비물이 뭐야?", "demo-3")
    assert "준비물:" in materials["text"]
    assert materials["sources"][0]["activity_id"] == expected_id
    assert materials["diagnostics"]["llm_calls"] == 0
    assert materials["diagnostics"]["search_performed"] is False

    step = message(client, sid, "다음 단계", "demo-4")
    assert step["current_step"] == 2
    assert step["diagnostics"]["llm_calls"] == 0
    replay = message(client, sid, "다음 단계", "demo-4")
    assert replay["message_id"] == step["message_id"]
    assert replay["current_step"] == 2
    assert replay["diagnostics"]["replayed"] is True

    explained = message(client, sid, "현재 단계를 쉽게 설명해줘", "demo-5")
    assert explained["mode"] == "reference"
    assert explained["diagnostics"]["llm_status"] == "reference"
    assert explained["diagnostics"]["llm_calls"] == 0
    assert explained["sources"][0]["excerpt"] in explained["text"]

    completed = message(client, sid, "완료했어", "demo-6")
    assert completed["mode"] == "action"
    assert completed["record"]["created"] is True
    first_execution = completed["execution_id"]
    first_record = completed["record"]["record_id"]
    replay = message(client, sid, "완료했어", "demo-6")
    assert replay["record"]["record_id"] == first_record
    assert replay["diagnostics"]["replayed"] is True
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 1

    # 같은 DB로 앱을 다시 만들면 메모리 캐시 없이도 선택과 메시지·기록이 복원됩니다.
    with TestClient(app_factory()) as restarted:
        restored = restarted.get(f"/sessions/{sid}").json()
        assert restored["selected_activity_id"] == expected_id
        assert restored["current_step"] == 2
        assert restored["execution_id"] == first_execution
        assert len(restored["messages"]) == 12
        replay = message(restarted, sid, "완료했어", "demo-6")
        assert replay["record"]["record_id"] == first_record

        repeated = message(restarted, sid, "다시 시작할게", "demo-7")
        assert repeated["execution_id"] != first_execution
        assert repeated["current_step"] == 1
        second = message(restarted, sid, "완료했어", "demo-8")
        assert second["record"]["record_id"] != first_record
        history = message(restarted, sid, "내 기록 보여줘", "demo-9")
        assert len(history["records"]) == 2
        assert history["diagnostics"]["llm_calls"] == 0
        records = restarted.get("/records", params={"session_id": sid}).json()
        assert records["total"] == 2


def test_explicit_null_resets_only_provided_constraint(client):
    """조건 누락은 유지하고 명시한 null만 초기화하여 후속 질문을 이어갑니다."""
    sid = create_session(client)
    message(
        client,
        sid,
        "요리 활동 추천해줘",
        "limit",
        constraints={"max_minutes": 20, "excluded_tools": ["오븐"]},
    )
    message(client, sid, "다른 활동 보여줘", "reset", constraints={"max_minutes": None})
    state = client.get(f"/sessions/{sid}").json()
    assert state["constraints"]["max_minutes"] is None
    assert state["constraints"]["excluded_tools"] == ["오븐"]
    message(client, sid, "요리 활동 추천해줘", "omit")
    assert client.get(f"/sessions/{sid}").json()["constraints"] == state["constraints"]


def test_zero_ordinal_does_not_select_last_candidate(client):
    """0번째라는 잘못된 번호가 파이썬의 음수 인덱스로 해석되지 않습니다."""
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "recommend")
    result = message(client, sid, "0번째 걸로 할게", "zero")
    assert result["mode"] == "clarify"
    assert result["selected_activity_id"] is None


def test_invalid_selection_and_step_do_not_mutate_session(client):
    """없는 활동·단계 범위 오류는 HTTP 오류로 반환하고 기존 맥락을 유지합니다."""
    sid = create_session(client)
    assert client.get("/activities/does-not-exist").status_code == 404
    assert (
        client.post(
            f"/sessions/{sid}/select", json={"activity_id": "does-not-exist"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/coach/messages",
            json={"session_id": sid, "message": "현재 단계", "current_step": 1},
        ).status_code
        == 422
    )
    select_activity(client, sid)
    activity = client.get("/activities/cook-0-2").json()
    for step in (0, len(activity["steps"]) + 1):
        response = client.post(
            "/coach/messages",
            json={"session_id": sid, "message": "현재 단계", "current_step": step},
        )
        assert response.status_code == 422
    assert (
        client.post(
            "/coach/messages",
            json={
                "session_id": sid,
                "message": "현재 단계",
                "activity_id": "does-not-exist",
            },
        ).status_code
        == 404
    )
    restored = client.get(f"/sessions/{sid}").json()
    assert restored["selected_activity_id"] == "cook-0-2"
    assert restored["current_step"] == 1


def test_complete_requires_all_steps_and_records_once(client):
    """체크되지 않은 실행은 저장할 수 없고 같은 실행은 한 번만 기록됩니다."""
    sid = create_session(client)
    execution = start_execution(client, sid)
    eid = execution["execution_id"]
    body = {"execution_id": eid, "request_id": "record-1", "note": "첫 시연"}
    path = "/activities/cook-0-2/records"
    assert client.post(path, json=body).status_code == 409
    assert client.patch(f"/executions/{eid}", json={"done": [True]}).status_code == 422
    done = [True] * len(execution["done"])
    assert client.patch(f"/executions/{eid}", json={"done": done}).status_code == 200
    saved = client.post(path, json=body)
    assert saved.status_code == 200
    assert saved.json()["created"] is True
    replay = client.post(path, json=body)
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["record_id"] == saved.json()["record_id"]
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 1
    assert client.get(f"/executions/{eid}").json()["status"] == "completed"
    assert (
        client.patch(
            f"/executions/{eid}", json={"done": [False] * len(done)}
        ).status_code
        == 409
    )


def test_reusing_message_request_id_for_different_payload_is_conflict(client):
    """동일 요청 ID로 질문 또는 조건을 바꾸면 중복 실행 대신 409를 반환합니다."""
    sid = create_session(client)
    message(client, sid, "안녕", "same")
    for changes in (
        {"message": "요리 활동 추천해줘"},
        {"constraints": {"max_minutes": 20}},
    ):
        response = client.post(
            "/coach/messages",
            json={
                "session_id": sid,
                "request_id": "same",
                "message": "안녕",
                **changes,
            },
        )
        assert response.status_code == 409
    assert len(client.get(f"/sessions/{sid}").json()["messages"]) == 2


def test_execution_request_id_reuses_same_activity_and_rejects_different_activity(
    client,
):
    """동일 시작 요청은 같은 실행을 반환하며 다른 활동으로 재사용할 수 없습니다."""
    sid = create_session(client)
    first = start_execution(client, sid, "start-once", "cook-0-2")
    replay = start_execution(client, sid, "start-once", "cook-0-2")
    assert replay["execution_id"] == first["execution_id"]
    response = client.post(
        f"/sessions/{sid}/executions",
        json={"activity_id": "cook-0-3", "request_id": "start-once"},
    )
    assert response.status_code == 409
    state = client.get(f"/sessions/{sid}").json()
    assert state["selected_activity_id"] == "cook-0-2"
    assert state["execution_id"] == first["execution_id"]


def test_replaying_old_start_request_keeps_current_execution(client):
    """이전 시작 요청의 재전송은 기존 결과만 돌려주고 현재 실행으로 되돌리지 않습니다."""
    sid = create_session(client)
    first = start_execution(client, sid, "start-a")
    second = start_execution(client, sid, "start-b")
    replay = start_execution(client, sid, "start-a")
    assert replay == first
    assert client.get(f"/sessions/{sid}").json()["execution_id"] == second["execution_id"]
    message(client, sid, "완료했어", "done")
    assert client.get(f"/executions/{second['execution_id']}").json()["status"] == "completed"
    assert client.get(f"/executions/{first['execution_id']}").json()["status"] == "in_progress"


def test_sessions_keep_candidates_conditions_progress_and_records_separate(client):
    """같은 요청 ID를 써도 대화 세션별 후보·조건·기록은 섞이지 않습니다."""
    first, second = create_session(client), create_session(client)
    message(client, first, "오늘 20분 안에 할 요리 활동 추천해줘", "same-id")
    message(client, first, "두 번째 걸로 할게", "select")
    message(client, first, "완료했어", "complete")
    second_result = message(client, second, "안녕", "same-id")
    assert second_result["selected_activity_id"] is None
    second_state = client.get(f"/sessions/{second}").json()
    assert second_state["candidate_ids"] == []
    assert second_state["constraints"]["max_minutes"] is None
    assert second_state["execution_id"] is None
    assert client.get("/records", params={"session_id": first}).json()["total"] == 1
    assert client.get("/records", params={"session_id": second}).json()["total"] == 0


def test_no_results_returns_clarification_without_generation(client):
    """검색 결과가 없으면 생성으로 빈 근거를 채우지 않고 조건 변경을 안내합니다."""
    sid = create_session(client)
    result = message(
        client,
        sid,
        "요리 활동 추천해줘",
        "no-results",
        constraints={"max_minutes": 0.1},
    )
    assert result["mode"] == "clarify"
    assert result["activities"] == []
    assert result["diagnostics"]["search_performed"] is True
    assert result["diagnostics"]["llm_calls"] == 0
    assert client.get(f"/sessions/{sid}").json()["candidate_ids"] == []
    selection = message(client, sid, "두 번째 걸로 할게", "no-candidate")
    assert selection["mode"] == "clarify"


def test_unknown_hobby_and_excluded_scope_are_explicit(client):
    """출시 범위 밖의 취미·알레르기 요청을 요리 추천으로 오인하지 않습니다."""
    assert client.post("/sessions", json={"hobby_key": "running"}).status_code == 422
    assert (
        client.post(
            "/search", json={"query": "달리기", "hobby_key": "running"}
        ).status_code
        == 422
    )
    sid = create_session(client)
    for number, text in enumerate(("러닝 추천해줘", "알레르기를 고려해줘")):
        result = message(client, sid, text, f"scope-{number}")
        assert result["mode"] == "clarify"
        assert result["diagnostics"]["intent"] == "out_of_scope"
        assert result["diagnostics"]["llm_calls"] == 0


@pytest.mark.parametrize(
    "status", ["generated", "timeout", "rate_limited", "validation_failed"]
)
def test_source_only_explanation_without_claim_generation_preserves_evidence(
    app_factory, status
):
    """구조화 생성기를 사용할 수 없으면 호출 없이 원래 근거를 직접 표시합니다."""
    generator = FakeLLM(status)
    secret = "test-only-never-a-real-api-key"
    with TestClient(app_factory(llm=generator, api_key=secret)) as client:
        sid = create_session(client)
        message(client, sid, "요리 활동 추천해줘", "recommend")
        select_activity(client, sid)
        message(client, sid, "준비물 알려줘", "materials")
        assert generator.calls == []
        explained = message(client, sid, "현재 단계를 쉽게 설명해줘", "explain")
        assert len(generator.calls) == 0
        assert explained["diagnostics"]["llm_calls"] == 0
        assert explained["diagnostics"]["llm_status"] == "reference"
        assert explained["mode"] == "reference"
        if status != "generated":
            assert explained["sources"][0]["excerpt"] in explained["text"]
        replay = message(client, sid, "현재 단계를 쉽게 설명해줘", "explain")
        assert len(generator.calls) == 0
        assert replay["diagnostics"]["llm_calls"] == 0
        assert replay["diagnostics"]["replayed"] is True
        state = client.get(f"/sessions/{sid}").json()
        assert state["execution_id"] is None
        assert state["current_step"] == 1
        assert client.get("/records", params={"session_id": sid}).json()["total"] == 0
        health = client.get("/health").json()
        assert health["llm_configured"] is True
        trace = client.get("/diagnostics/traces", params={"session_id": sid}).json()
        for payload in (
            explained,
            replay,
            state,
            health,
            trace,
            client.get("/openapi.json").json(),
        ):
            assert secret not in json.dumps(payload, ensure_ascii=False)
        assert "현재 단계를 쉽게 설명해줘" not in json.dumps(trace, ensure_ascii=False)


@pytest.mark.parametrize(
    "text",
    [
        "아직 다 했다고 할 수 없어",
        "완료했는지 확인해줘",
        "완료했어?",
        "완료했다고 기록하지 마",
        "완료했다고 가정해줘",
        "완료했어, 취소해줘",
    ],
)
def test_question_or_negation_does_not_complete_activity(client, text):
    """완료에 관한 질문이나 부정 표현을 실제 완료 선언으로 저장하지 않습니다."""
    sid = create_session(client)
    select_activity(client, sid)
    message(client, sid, text, "not-a-completion")
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 0
    assert client.get(f"/sessions/{sid}").json()["execution_id"] is None


def test_recipe_scope_survives_followup_constraints(client, tmp_path):
    """레시피 색인이 있으면 검색 뒤 시간만 바꾸어도 기본 활동 검색으로 몰래 전환하지 않습니다."""
    recipe = {"activity_id": "recipe-potato-soup", "hobby_key": "cooking", "kind": "recipe",
              "title": "potato soup", "steps": ["boil potatoes"], "quality": {"status": "structural_pass", "flags": []}}
    file = tmp_path / "recipes.jsonl"
    file.write_text(json.dumps(recipe), encoding="utf-8")
    build_lexical_index([file], tmp_path / "index", "recipes")
    sid = create_session(client)
    first = message(client, sid, "감자 수프 레시피 추천해줘", "recipe")
    assert first["diagnostics"]["scope"] == "recipes"
    changed = message(client, sid, "10분만 가능해", "time")
    assert changed["diagnostics"]["scope"] == "recipes"
    state = client.get(f"/sessions/{sid}").json()
    assert state["search_scope"] == "recipes"
    assert state["search_query"] == "감자 수프 레시피 추천해줘"
    switched = message(client, sid, "요리 활동 추천해줘", "activity")
    assert switched["diagnostics"]["scope"] == "activities"


def test_recipe_request_without_recipe_index_searches_activities(client):
    """레시피 자료가 색인되지 않았으면 빈 범위에 고정하지 않고 활동에서 찾습니다."""
    sid = create_session(client)
    result = message(client, sid, "달걀 레시피 추천해줘", "recipe")
    assert result["diagnostics"]["scope"] == "activities"
    assert result["activities"]
    assert client.get(f"/sessions/{sid}").json()["search_scope"] == "activities"


@pytest.mark.parametrize("field", ["available_tools", "excluded_tools"])
@pytest.mark.parametrize("tool", ["", "   "])
def test_blank_tool_name_is_rejected(client, field, tool):
    """빈 도구명은 모든 활동을 걸러내므로 입력 단계에서 거부합니다."""
    response = client.post("/search", json={"query": "달걀", "constraints": {field: [tool]}})
    assert response.status_code == 422


def test_count_phrase_is_not_candidate_number_in_explanation(client):
    """'1번만 더'는 횟수이므로 선택한 활동 대신 추천 1번 후보를 설명하지 않습니다."""
    sid = create_session(client)
    second = message(client, sid, "달걀 요리 추천해줘", "r")["activities"][1]["activity_id"]
    message(client, sid, "두 번째 걸로 할게", "s")
    result = message(client, sid, "1번만 더 저어도 돼?", "q")
    assert result["diagnostics"]["intent"] == "explain"
    assert [source["activity_id"] for source in result["sources"]] == [second]


def test_count_phrase_with_select_word_keeps_selected_activity(client):
    """'1번만 더 할게'는 추천 1번 선택이 아니므로 선택 활동을 바꾸지 않습니다."""
    sid = create_session(client)
    second = message(client, sid, "달걀 요리 추천해줘", "r")["activities"][1]["activity_id"]
    message(client, sid, "두 번째 걸로 할게", "s")
    result = message(client, sid, "1번만 더 할게", "q")
    assert result["selected_activity_id"] == second


@pytest.mark.parametrize("text", ["전자레인지로 하면 안 돼?", "버터를 빼면 안 되나요?"])
def test_permission_question_is_explained_not_troubleshot(client, text):
    """'~하면 안 돼?'는 실패 신고가 아니라 허용 여부 질문이므로 설명으로 처리합니다."""
    sid = create_session(client)
    select_activity(client, sid)
    result = message(client, sid, text, "q")
    assert result["diagnostics"]["intent"] == "explain"


def test_explanation_evidence_carries_self_check_goal_and_review_state(app_factory):
    """자기 점검·학습 목표·검토 여부를 물어도 답할 수 있도록 근거에 확인 항목·목표·검토 상태를 넣습니다."""
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator)) as client:
        sid = create_session(client)
        select_activity(client, sid)
        result = message(client, sid, "잘했는지 스스로 점검할 체크리스트 있어?", "checklist")
    excerpt = result["sources"][0]["excerpt"]
    assert "가루와 액체를 각각 알맞은 방법으로 계량했다" in excerpt
    assert "맛의 특징을 한 줄로 메모했다" in excerpt
    assert "계량(큰술·작은술), 맛보기" in excerpt
    assert "사람 검토 전" in excerpt


@pytest.mark.parametrize("text", ["너는 어떤 질문까지 답할 수 있어?", "무슨 질문을 할 수 있어?", "뭘 도와줄 수 있어?"])
def test_capability_question_describes_coach_scope(client, text):
    """코치의 능력 범위를 물으면 검색하지 않고 할 수 있는 일과 하지 않는 일을 안내합니다."""
    sid = create_session(client)
    result = message(client, sid, text, "scope")
    assert result["diagnostics"]["intent"] == "capabilities"
    assert result["diagnostics"]["search_performed"] is False
    assert "알레르기" in result["text"]


def test_too_much_question_is_not_capability_question(client):
    """'너무'의 '너'를 코치를 부르는 말로 오인하지 않습니다."""
    sid = create_session(client)
    select_activity(client, sid)
    result = message(client, sid, "너무 짜면 물을 더 넣을 수 있어?", "salty")
    assert result["diagnostics"]["intent"] != "capabilities"


@pytest.mark.parametrize("text", ["오늘 내가 잘한 점과 부족한 점을 평가해줘", "다음 난이도 활동으로 넘어가도 될까?"])
def test_self_assessment_question_offers_activity_checks_without_judging(client, text):
    """수행을 직접 보지 못하므로 평가를 지어내지 않고 활동의 자기 점검 항목을 안내합니다."""
    sid = create_session(client)
    select_activity(client, sid)
    result = message(client, sid, text, "assess")
    assert result["diagnostics"]["intent"] == "self_assessment"
    assert result["diagnostics"]["llm_calls"] == 0
    assert "가루와 액체를 각각 알맞은 방법으로 계량했다" in result["text"]
    assert "맛의 특징을 한 줄로 메모했다" in result["text"]


def test_recommendation_reason_uses_last_search_without_new_search(client):
    """'왜 그걸 추천했어?'에 직전 검색어와 후보로 답하고 추천 목록을 바꾸지 않습니다."""
    sid = create_session(client)
    recommended = message(client, sid, "달걀 요리 추천해줘", "recommend")
    result = message(client, sid, "왜 그걸 추천했어?", "why")
    assert result["diagnostics"]["intent"] == "recommend_reason"
    assert result["diagnostics"]["search_performed"] is False
    assert "달걀 요리 추천해줘" in result["text"]
    assert all(item["title"] in result["text"] for item in recommended["activities"])
    state = client.get(f"/sessions/{sid}").json()
    assert state["candidate_ids"] == [item["activity_id"] for item in recommended["activities"]]


def test_recommendation_reason_lists_applied_time_and_tool_conditions(client):
    """추천 이유에 실제로 적용한 시간·제외 도구 조건을 함께 밝힙니다."""
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "recommend", constraints={"max_minutes": 20, "excluded_tools": ["오븐"]})
    result = message(client, sid, "추천한 이유가 뭐야?", "why")
    assert result["diagnostics"]["intent"] == "recommend_reason"
    assert "20분 이내" in result["text"]
    assert "제외한 도구: 오븐" in result["text"]


def test_meta_questions_before_context_say_what_is_missing(client):
    """추천이나 활동 선택 전의 메타 질문에는 판단할 근거가 아직 없다고 알립니다."""
    sid = create_session(client)
    assert "아직 추천한 활동이 없어요" in message(client, sid, "왜 그걸 추천했어?", "why")["text"]
    assert "활동을 먼저 선택하면" in message(client, sid, "내 실력 평가해줘", "assess")["text"]


def test_followup_certainty_reports_actual_source_review_without_generation(app_factory):
    """'그거 확실해?' 같은 지시어를 풀 수 있도록 직전 코치 답변을 설명 요청에 함께 넘깁니다."""
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator)) as client:
        sid = create_session(client)
        select_activity(client, sid)
        first = message(client, sid, "2단계를 쉽게 설명해줘", "first")
        result = message(client, sid, "그거 확실해?", "sure")
    assert not generator.calls
    assert all(value == "source_only" for value in result["metacognition"]["source_review"].values())
    assert "원인 확정" in result["text"]


def ask(client, **body):
    """RAG 검증 요청에서 공통 HTTP 검증을 수행합니다."""
    response = client.post("/rag/ask", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_rag_ask_returns_ranked_evidence_and_answer(app_factory):
    """검색 순위·근거 원문·생성 답변·지연을 한 응답에서 확인할 수 있습니다."""
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator, api_key="test-only-key")) as client:
        result = ask(client, question="간장 드레싱을 계량해서 만들기", top_k=3)
    assert result["question"] == "간장 드레싱을 계량해서 만들기"
    items = result["retrieval"]["items"]
    assert items[0] == {"rank": 1, "activity_id": "cook-0-2", "title": items[0]["title"]}
    assert result["retrieval"]["diagnostics"]["ranking"][0]["activity_id"] == "cook-0-2"
    assert [e["activity_id"] for e in result["evidence"]] == [item["activity_id"] for item in items]
    assert "준비물" in result["evidence"][0]["excerpt"]
    assert not generator.calls
    assert result["status"] == "reference"
    assert all(e["excerpt"] in result["answer"] for e in result["evidence"])
    assert result["cited_activity_ids"] == [e["activity_id"] for e in result["evidence"]]
    assert result["llm"] == {"model":None,"calls":0,"usage":{}}
    assert set(result["latency_ms"]) == {"retrieval", "llm", "total"}


def test_rag_ask_without_evidence_does_not_call_llm(app_factory):
    """근거가 없으면 생성 API를 부르지 않고 no_evidence로 끝냅니다."""
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator, api_key="test-only-key")) as client:
        result = ask(client, question="양자역학 강의 추천해줘")
    assert result["retrieval"]["items"] == []
    assert result["status"] == "no_evidence"
    assert result["answer"] is None
    assert result["llm"]["calls"] == 0
    assert generator.calls == []


def test_empty_session_id_does_not_expose_other_sessions(client):
    """빈 session_id는 '전체'가 아닙니다. 다른 세션의 완료 기록과 진단을 돌려주지 않습니다."""
    sid = create_session(client)
    select_activity(client, sid)
    assert message(client, sid, "완료했어", "done")["record"]["created"] is True
    assert client.get("/records", params={"session_id": ""}).status_code == 422
    assert client.get("/diagnostics/traces", params={"session_id": ""}).status_code == 422


def test_rag_ask_reports_search_timeout_as_budget_exhausted(monkeypatch):
    """검색이 시간 안에 끝나지 않은 것을 '근거 없음'으로 보고하지 않습니다."""
    import asyncio
    import time
    import app.ask as ask_module
    from app.turn_budget import TurnBudget

    class SlowRetrieval:
        def search(self, *args):
            time.sleep(0.3)
            return {"items": [], "diagnostics": {}}

    monkeypatch.setattr(ask_module, "TurnBudget", lambda: TurnBudget(0.05))
    result = asyncio.run(ask_module.RagAsk(SlowRetrieval(), object()).answer("질문"))
    assert result["status"] == "budget_exhausted"
    assert result["answer"] is None and result["llm"]["calls"] == 0


@pytest.mark.parametrize('endpoint', ['/rag/ask', '/coach/messages'])
def test_measuring_question_answers_the_requested_method(app_factory, endpoint):
    class GroundedAnswer:
        configured = True
        model = 'test-claude'

        async def explain_claims(self, question, evidence, context):
            row = next(row for row in evidence if row['activity_id'] == 'cook-0-2')
            return {'claims': [{'text': '큰술은 15ml, 작은술은 5ml입니다. 가루 재료는 깎아서, 액체는 계량스푼을 수평으로 잡아 계량하세요.',
                'evidence_ids': [row['evidence_id']],
                'quotes': [{'evidence_id': row['evidence_id'], 'quote': row['excerpt']}]}],
                'status': 'received', 'llm_calls': 1, 'usage': {}}

        async def verify_claims(self, claims, evidence, context):
            return {'verdicts': [{'claim_index': 0, 'verdict': 'supported',
                                  'evidence_ids': claims[0]['evidence_ids']}],
                    'status': 'received', 'llm_calls': 1, 'usage': {}}

    field = 'question' if endpoint == '/rag/ask' else 'message'
    with TestClient(app_factory(llm=GroundedAnswer(), api_key='test-only-key')) as client:
        response = client.post(endpoint, json={field: '큰술과 작은술 계량은 어떻게 해?'})
    assert response.status_code == 200, response.text
    result = response.json()
    answer_text = result['answer' if endpoint == '/rag/ask' else 'text']
    assert '큰술은 15ml, 작은술은 5ml' in answer_text
    assert '가루 재료는 깎아서' in answer_text
    assert '준비물:' not in answer_text and '설탕이 녹을' not in answer_text
    sources = result['evidence' if endpoint == '/rag/ask' else 'sources']
    assert '준비물:' in sources[0]['excerpt']
    assert result['cited_activity_ids'] == ['cook-0-2']
    assert result['status' if endpoint == '/rag/ask' else 'mode'] == 'generated'
    assert result['validation']['semantic_support'] == 'model_assessed'
    assert all(row['review_status'] == 'source_only' for row in sources)


def test_rag_ask_swagger_examples_do_not_prefill_placeholder_filters(client):
    """Swagger 기본 예시가 'string' 도구·예산 0 같은 자리표시 조건으로 모든 활동을 걸러내지 않습니다."""
    body = client.get("/openapi.json").json()["paths"]["/rag/ask"]["post"]["requestBody"]
    examples = body["content"]["application/json"]["examples"]
    assert {"basic", "with_time_limit"} <= examples.keys()
    for example in examples.values():
        value = example["value"]
        assert set(value.get("constraints", {})) <= {"max_minutes"}
        assert "string" not in json.dumps(value)


@pytest.mark.parametrize(
    "example_name, expected_id",
    [("activities", "cook-0-2"), ("with_time_limit", "cook-0-2"),
     ("recipes", "recipe-potato-soup"), ("knowledge", "cook-0-2:steps")],
)
def test_search_swagger_examples_return_results(client, tmp_path, example_name, expected_id):
    """화면 예시를 그대로 실행해도 자리표시 필터 때문에 검색 결과가 사라지지 않습니다."""
    recipe = {
        "activity_id": "recipe-potato-soup", "hobby_key": "cooking", "kind": "recipe",
        "title": "potato soup", "steps": ["boil potatoes"],
        "quality": {"status": "structural_pass", "flags": []},
    }
    recipe_file = tmp_path / "recipes.jsonl"
    recipe_file.write_text(json.dumps(recipe), encoding="utf-8")
    build_lexical_index([recipe_file], tmp_path / "index", "recipes")
    body = client.get("/openapi.json").json()["paths"]["/search"]["post"]["requestBody"]
    value = body["content"]["application/json"]["examples"][example_name]["value"]

    response = client.post("/search", json=value)

    assert response.status_code == 200, response.text
    result = response.json()
    assert expected_id in [item["activity_id"] for item in result["items"]]
    assert result["diagnostics"]["scope"] == value["scope"]
    if example_name == "with_time_limit":
        assert all(item["duration"]["total_minutes"] <= 20 for item in result["items"])


@pytest.mark.parametrize("body", [{"question": ""}, {"question": "드레싱", "top_k": 0},
                                  {"question": "드레싱", "top_k": 6}, {"question": "드레싱", "extra": 1}])
def test_rag_ask_rejects_invalid_input(client, body):
    """빈 질문, 범위 밖 top_k, 정의되지 않은 필드는 입력 단계에서 거부합니다."""
    assert client.post("/rag/ask", json=body).status_code == 422


def constraints_of(client, sid):
    """세션에 저장된 조건을 읽습니다."""
    return client.get(f"/sessions/{sid}").json()["constraints"]


def test_ordinal_step_question_shows_that_step_without_switching_activity(client):
    """'세 번째 단계 보여줘'는 후보 선택이 아니라 선택 활동의 해당 단계 조회입니다."""
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "recommend")
    select_activity(client, sid, "cook-0-2")
    message(client, sid, "활동 시작할게", "start")
    before = client.get(f"/sessions/{sid}").json()
    result = message(client, sid, "세 번째 단계 보여줘", "step-3")
    after = client.get(f"/sessions/{sid}").json()
    assert result["diagnostics"]["intent"] == "activity_lookup"
    assert after["selected_activity_id"] == "cook-0-2"
    assert before["execution_id"] is not None and after["execution_id"] == before["execution_id"]
    assert after["current_step"] == 3
    assert result["text"].startswith("3/4단계")
    assert message(client, sid, "2단계 보여줘", "step-2")["text"].startswith("2/4단계")


def test_short_on_time_keeps_the_stated_limit(client):
    """'시간 없어서 10분만'은 제한 해제가 아니라 10분 제한입니다. 해제는 '시간 제한 없이'로 합니다."""
    sid = create_session(client)
    message(client, sid, "오늘 시간 없어서 10분만 할 요리 추천해줘", "busy")
    assert constraints_of(client, sid)["max_minutes"] == 10
    message(client, sid, "시간 제한 없이 추천해줘", "unlimited")
    assert constraints_of(client, sid)["max_minutes"] is None


def test_hours_and_minutes_are_added(client):
    """'1시간 30분'은 90분, '2시간'은 120분으로 읽습니다."""
    sid = create_session(client)
    message(client, sid, "1시간 30분 안에 할 요리 추천해줘", "ninety")
    assert constraints_of(client, sid)["max_minutes"] == 90
    message(client, sid, "2시간 이내로 추천해줘", "two-hours")
    assert constraints_of(client, sid)["max_minutes"] == 120


def test_recommendation_mentioning_start_is_not_a_start_request(client):
    """'시작할 만한 요리 추천'은 실행 시작이 아니라 추천입니다."""
    sid = create_session(client)
    result = message(client, sid, "초보가 시작할 만한 요리 추천해줘", "beginner")
    assert result["diagnostics"]["intent"] == "recommend"
    assert client.get(f"/sessions/{sid}").json()["execution_id"] is None
    select_activity(client, sid)
    assert message(client, sid, "다시 시작할게", "restart")["diagnostics"]["intent"] == "start"


def test_coffee_cooking_request_is_in_scope_but_coffee_hobby_is_not(client):
    """커피가 들어간 요리 요청은 추천하고, 커피 취미 질문만 범위 밖으로 안내합니다."""
    sid = create_session(client)
    cooking = message(client, sid, "커피 젤리 만드는 요리 추천해줘", "jelly")
    assert cooking["diagnostics"]["intent"] == "recommend"
    hobby = message(client, sid, "커피 원두 고르는 법 알려줘", "beans")
    assert hobby["diagnostics"]["intent"] == "out_of_scope"


def test_sessionless_retry_with_same_request_id_reuses_session(client):
    """session_id 없이 같은 요청을 재전송하면 새 세션을 만들지 않고 첫 응답을 재사용합니다.

    재전송으로 보는 것은 UUID 형식의 request_id뿐입니다. 여러 클라이언트 사이에서 겹치지 않는 값이어야 하기 때문입니다.
    """
    request_id = "0b6f4a52-7c1e-4d0a-9a3e-5f2c8d1e7b90"
    body = {"message": "안녕", "request_id": request_id}
    first = client.post("/coach/messages", json=body)
    second = client.post("/coach/messages", json=body)
    assert first.status_code == second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]
    assert second.json()["diagnostics"]["replayed"] is True
    conflict = client.post("/coach/messages", json={"message": "요리 활동 추천해줘", "request_id": request_id})
    assert conflict.status_code == 409


@pytest.mark.parametrize("second_message", ["안녕", "요리 활동 추천해줘"])
def test_sessionless_requests_reusing_a_weak_request_id_stay_separate(client, second_message):
    """'demo-1' 같은 값은 여러 사용자가 겹쳐 씁니다. 세션 없는 요청을 남의 대화에 이어 붙이지 않습니다."""
    first = client.post("/coach/messages", json={"message": "안녕", "request_id": "demo-1"})
    second = client.post("/coach/messages", json={"message": second_message, "request_id": "demo-1"})
    assert first.status_code == second.status_code == 200
    assert second.json()["session_id"] != first.json()["session_id"]
    assert not second.json()["diagnostics"].get("replayed")


@pytest.mark.parametrize("request_id", [
    "00000000-0000-0000-0000-000000000000",  # nil
    "0B6F4A52-7C1E-4D0A-9A3E-5F2C8D1E7B90",  # 대문자
    "0b6f4a527c1e4d0a9a3e5f2c8d1e7b90",      # 하이픈 없음
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # v1: 시각과 장치로 만들어 추측할 수 있다
])
def test_sessionless_reuse_needs_a_canonical_random_uuid(client, request_id):
    """세션을 다시 찾는 열쇠가 되므로 무작위(v4) UUID의 표준 표기만 받습니다."""
    body = {"message": "안녕", "request_id": request_id}
    first = client.post("/coach/messages", json=body).json()
    second = client.post("/coach/messages", json=body).json()
    assert second["session_id"] != first["session_id"]


def test_sessionless_turn_finds_or_creates_its_session_off_the_event_loop(client):
    import threading
    loop = client.portal.call(threading.get_ident)
    store, seen = client.app.state.store, []
    for name in ("session_for_origin", "create_session"):
        def recording(*args, _original=getattr(store, name), **kwargs):
            seen.append(threading.get_ident())
            return _original(*args, **kwargs)
        setattr(store, name, recording)
    for request_id in ("0b6f4a52-7c1e-4d0a-9a3e-5f2c8d1e7b90", "demo-1"):
        assert client.post("/coach/messages", json={"message": "안녕", "request_id": request_id}).status_code == 200
    assert len(seen) == 2 and loop not in seen


def test_execution_and_record_responses_do_not_reveal_the_session_id(client):
    """실행 ID만 아는 사람이 대화 전체의 열쇠(session_id)를 얻지 못합니다."""
    sid = create_session(client)
    execution = start_execution(client, sid)
    eid = execution["execution_id"]
    patched = client.patch(f"/executions/{eid}", json={"done": [True] * len(execution["done"])}).json()
    record = client.post("/activities/cook-0-2/records", json={"execution_id": eid, "request_id": "record-1"}).json()
    fetched = client.get(f"/executions/{eid}").json()
    listed = client.get("/records", params={"session_id": sid}).json()["items"]
    assert record["created"] is True and len(listed) == 1
    for payload in (execution, patched, record, fetched, *listed):
        assert "session_id" not in payload


def test_session_responses_hide_internal_keys(client):
    """세션을 다시 찾는 값(origin_request_id)과 저장 개정 번호는 응답에 싣지 않습니다."""
    request_id = "0b6f4a52-7c1e-4d0a-9a3e-5f2c8d1e7b90"
    sid = client.post("/coach/messages", json={"message": "안녕", "request_id": request_id}).json()["session_id"]
    for payload in (client.get(f"/sessions/{sid}").json(), select_activity(client, sid)):
        assert payload["session_id"] == sid
        assert "origin_request_id" not in payload and "revision" not in payload


def test_explicit_null_is_not_replayed_as_the_request_that_omitted_the_field(client):
    """조건을 생략한 요청(유지)과 null로 보낸 요청(해제)은 뜻이 다릅니다. 같은 request_id로는 받지 않습니다."""
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "limit", constraints={"max_minutes": 20})
    message(client, sid, "요리 활동 추천해줘", "same", constraints={})
    response = client.post("/coach/messages", json={
        "session_id": sid, "message": "요리 활동 추천해줘", "request_id": "same", "constraints": {"max_minutes": None}})
    assert response.status_code == 409
    assert client.get(f"/sessions/{sid}").json()["constraints"]["max_minutes"] == 20


def test_record_and_trace_listing_require_an_existing_session(client):
    """범위 없는 조회는 다른 세션의 ID와 진단을 드러냅니다. session_id가 없거나 없는 세션이면 거부합니다."""
    sid = create_session(client)
    message(client, sid, "안녕", "hello")
    for path in ("/records", "/diagnostics/traces"):
        assert client.get(path).status_code == 422
        assert client.get(path, params={"session_id": "missing"}).status_code == 404
        assert client.get(path, params={"session_id": sid}).status_code == 200


def test_requests_with_a_foreign_host_header_are_rejected(client):
    """허용하지 않은 Host로 온 요청은 받지 않습니다. 다른 사이트가 이 서버를 같은 출처처럼 읽는 것을 막습니다."""
    assert client.get("/health", headers={"host": "rebind.evil.example:8003"}).status_code == 400
    assert client.get("/health").status_code == 200


def test_recommendation_reads_index_counts_off_the_event_loop(client):
    """추천 턴의 색인 문서 수 조회(레시피 54만 건 집계)가 다른 사용자의 요청까지 멈추지 않습니다."""
    import threading
    loop = client.portal.call(threading.get_ident)
    retrieval, seen = client.app.state.retrieval, []
    original = retrieval.status

    def recording():
        seen.append(threading.get_ident())
        return original()

    retrieval.status = recording
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "recommend")
    assert seen and loop not in seen


def test_coach_turn_reads_and_saves_the_session_off_the_event_loop(client):
    """대화 턴의 세션 읽기·재전송 확인·응답 저장이 이벤트 루프 스레드에서 돌지 않습니다."""
    import threading
    loop = client.portal.call(threading.get_ident)
    store, seen = client.app.state.store, {}
    for name in ("get_session", "cached_request", "save_reply"):
        def recording(*args, _name=name, _original=getattr(store, name), **kwargs):
            seen.setdefault(_name, set()).add(threading.get_ident())
            return _original(*args, **kwargs)
        setattr(store, name, recording)
    sid = create_session(client)
    message(client, sid, "안녕", "hello")
    assert set(seen) == {"get_session", "cached_request", "save_reply"}
    assert all(loop not in threads for threads in seen.values())


def test_select_and_start_run_store_calls_off_the_event_loop(client):
    """async 엔드포인트의 SQLite 호출이 이벤트 루프 스레드에서 돌지 않습니다."""
    import threading
    loop = client.portal.call(threading.get_ident)
    store, seen = client.app.state.store, []
    for name in ("get_session", "save_session", "start_execution"):
        def recording(*args, _original=getattr(store, name), **kwargs):
            seen.append(threading.get_ident())
            return _original(*args, **kwargs)
        setattr(store, name, recording)
    sid = create_session(client)
    select_activity(client, sid)
    start_execution(client, sid)
    assert seen and loop not in seen


def test_session_locks_do_not_accumulate(client):
    """없는 세션 ID로는 잠금을 만들지 않고, 끝난 요청의 잠금은 남기지 않습니다."""
    coach = client.app.state.coach
    for number in range(5):
        response = client.post(f"/sessions/missing-{number}/select", json={"activity_id": "cook-0-2"})
        assert response.status_code == 404
    sid = create_session(client)
    message(client, sid, "안녕", "hello")
    select_activity(client, sid)
    start_execution(client, sid)
    assert len(coach.locks) == 0


def test_coach_search_runs_off_the_event_loop_thread(app_factory):
    """대화 경로의 검색(GPU 추론)이 이벤트 루프 스레드를 막지 않도록 별도 스레드에서 실행합니다."""
    import threading
    generator = FakeLLM()
    threads = {}
    with TestClient(app_factory(llm=generator, api_key="test-only-key")) as client:
        original_respond = client.app.state.coach.respond
        async def respond(*args, **kwargs):
            threads["loop"] = threading.get_ident()
            return await original_respond(*args, **kwargs)
        client.app.state.coach.respond = respond
        retrieval = client.app.state.retrieval
        original_search = retrieval.search

        def recording(*args, **kwargs):
            threads["search"] = threading.get_ident()
            return original_search(*args, **kwargs)

        retrieval.search = recording
        sid = create_session(client)
        message(client, sid, "간장 드레싱을 쉽게 설명해줘", "explain")
    assert threads["search"] != threads["loop"]


@pytest.mark.parametrize("text", ["1단계 완료했어", "현재 단계 다 했어", "마지막 단계 끝냈어"])
def test_step_completion_does_not_complete_activity(client, text):
    sid = create_session(client)
    execution = start_execution(client, sid)
    result = message(client, sid, text, "step-complete")
    assert result["mode"] == "clarify"
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 0
    assert client.get(f"/executions/{execution['execution_id']}").json() == execution


@pytest.mark.parametrize("text", ["활동 시작하지 마", "다시 시작하지 말아줘", "시작할 생각 없어", "활동 시작 취소"])
def test_negated_start_preserves_execution_and_step(client, text):
    sid = create_session(client)
    execution = start_execution(client, sid)
    message(client, sid, "다음 단계", "next")
    result = message(client, sid, text, "no-start")
    assert result["mode"] == "clarify"
    assert result["execution_id"] == execution["execution_id"]
    assert result["current_step"] == 2
    assert result["diagnostics"]["search_performed"] is False


@pytest.mark.parametrize("text", ["3단계를 쉽게 설명해줘", "세 번째 단계를 설명해줘"])
def test_explicit_step_explanation_uses_requested_evidence(app_factory, text):
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator)) as client:
        sid = create_session(client)
        select_activity(client, sid)
        activity = client.get("/activities/cook-0-2").json()
        result = message(client, sid, text, "explain-third")
        expected = f"3단계: {activity['steps'][2]}"
        assert result["sources"][0]["excerpt"] == expected
        assert not generator.calls
        assert expected in result["text"]
        assert result["current_step"] == 1


@pytest.mark.parametrize("text", ["땅콩 알레르기가 있는데 재료 보여줘", "알러지가 있는데 활동 시작할게", "알레르기가 있는데 완료했어"])
def test_allergy_guard_precedes_lookup_and_actions(client, text):
    sid = create_session(client)
    select_activity(client, sid)
    result = message(client, sid, text, "allergy")
    assert result["diagnostics"]["intent"] == "out_of_scope"
    assert "알레르기 조건 판정을 지원하지 않습니다" in result["text"]
    assert result["execution_id"] is None
    assert result["sources"] == []
    assert result["diagnostics"]["llm_calls"] == 0
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 0


def test_short_followup_time_limit_updates_existing_search(client):
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "recommend", constraints={"max_minutes": 20})
    result = message(client, sid, "시간 없어서 10분만", "short-time")
    assert result["diagnostics"]["intent"] == "update_constraints"
    state = client.get(f"/sessions/{sid}").json()
    assert state["constraints"]["max_minutes"] == 10
    assert state["search_query"] == "요리 활동 추천해줘"
    assert all(x["duration"]["total_minutes"] <= 10 for x in result["activities"])


def test_natural_language_tool_exclusion_merges_without_duplicates(client):
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "tools", constraints={"excluded_tools": ["가스레인지"]})
    for rid in ("oven-1", "oven-2"):
        message(client, sid, "오븐 없이 추천해줘", rid)
        assert client.get(f"/sessions/{sid}").json()["constraints"]["excluded_tools"] == ["가스레인지", "오븐"]


@pytest.mark.parametrize("text", ["99단계 보여줘", "0단계 보여줘", "99단계를 쉽게 설명해줘", "99단계 완료했어"])
def test_invalid_text_step_is_explicit_and_preserves_state(client, text):
    sid = create_session(client)
    select_activity(client, sid)
    message(client, sid, "다음 단계", "next")
    result = message(client, sid, text, "invalid-step")
    assert result["mode"] == "clarify"
    assert "단계 범위" in result["text"]
    assert result["current_step"] == 2
    assert result["execution_id"] is None
    assert result["sources"] == []
    assert result["diagnostics"]["llm_calls"] == 0
    assert client.get(f"/sessions/{sid}").json()["current_step"] == 2


def test_evidence_includes_duration_so_time_answers_can_be_grounded():
    """근거에 소요 시간을 넣어, 시간 조건 질문에 '15분'이라고 답해도 근거 있는 숫자로 검증됩니다."""
    from app.answer_policy import evidence_for
    from app.claude import validate_answer
    activity = {"activity_id": "a", "title": "간장 드레싱", "kind": "activity", "materials": ["간장 2큰술"],
                "steps": ["섞기", "맛보기"], "duration": {"total_minutes": 15, "is_estimate": True}}
    evidence = evidence_for(activity)
    assert "예상 소요 시간: 15분 (추정)" in evidence["excerpt"]
    assert not validate_answer({"answer": "15분 정도면 끝나요.", "cited_activity_ids": ["a"]}, [evidence])
    unknown = dict(activity, duration={"total_minutes": None, "is_estimate": False})
    assert "소요 시간" not in evidence_for(unknown)["excerpt"]
    assert evidence_for(activity, step=2)["excerpt"] == "2단계: 맛보기"


def recommend(client, sid):
    """기본 추천을 받고 후보 ID 순서를 반환합니다."""
    result = message(client, sid, "요리 활동 추천해줘", "recommend")
    return [x["activity_id"] for x in result["activities"]]


@pytest.mark.parametrize("text", ["두 번째", "2번", "2번으로 할게", "2번으로 해줘", "두 번째 거요", "두번째꺼"])
def test_bare_or_numbered_ordinal_selects_from_current_candidates(client, text):
    """번호만 말하거나 'N번'으로 말해도 새 검색 없이 직전 추천에서 고릅니다."""
    sid = create_session(client)
    candidates = recommend(client, sid)
    result = message(client, sid, text, "pick")
    assert result["diagnostics"]["intent"] == "select"
    assert result["selected_activity_id"] == candidates[1]
    assert client.get(f"/sessions/{sid}").json()["candidate_ids"] == candidates


@pytest.mark.parametrize("text", ["고마워요", "감사해요", "고맙습니다", "알겠어요", "네 알겠어요", "감사합니다ㅎㅎ"])
def test_thanks_and_acknowledgements_keep_candidates(client, text):
    """감사·맞장구는 검색하지 않으므로 직전 추천 목록이 바뀌지 않습니다."""
    sid = create_session(client)
    candidates = recommend(client, sid)
    result = message(client, sid, text, "thanks")
    assert result["diagnostics"]["intent"] == "greeting"
    assert result["diagnostics"]["search_performed"] is False
    assert client.get(f"/sessions/{sid}").json()["candidate_ids"] == candidates


def test_search_without_results_keeps_previous_candidates(client):
    """결과 없는 검색이 직전 추천 목록을 지우지 않아 번호 선택을 이어갈 수 있습니다."""
    sid = create_session(client)
    candidates = recommend(client, sid)
    empty = message(client, sid, "양자역학 강의 추천해줘", "empty")
    assert empty["activities"] == []
    assert client.get(f"/sessions/{sid}").json()["candidate_ids"] == candidates
    assert message(client, sid, "두 번째 걸로 할게", "pick")["selected_activity_id"] == candidates[1]


@pytest.mark.parametrize(
    "activity, text",
    [
        ("cook-0-2", "재료 손질 다 했어"),
        ("cook-0-2", "타이머 끝났어"),
        ("cook-0-2", "간장 다 했어"),
        ("cook-2-2", "밥 다 했어"),
        ("cook-3-5", "반죽 끝냈어"),
    ],
)
def test_partial_task_completion_does_not_record_activity(client, activity, text):
    """재료 손질·타이머나 활동 제목의 한 단어(간장, 밥, 반죽)만 끝냈다는 말은 활동 전체 완료로 기록하지 않습니다."""
    sid = create_session(client)
    select_activity(client, sid, activity)
    result = message(client, sid, text, "partial")
    assert result["diagnostics"]["intent"] == "complete"
    assert result["mode"] == "clarify"
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 0
    assert client.get(f"/sessions/{sid}").json()["execution_id"] is None


@pytest.mark.parametrize(
    "activity, text",
    [
        ("cook-0-2", "다 끝났어"),
        ("cook-0-2", "간장 드레싱을 다 했어"),
        ("cook-0-2", "간장드레싱 다 했어"),
        ("cook-2-2", "김치볶음밥 다 했어"),
        ("cook-0-2", "요리 완료했어요"),
        ("cook-0-2", "완료했어요 감사합니다"),
    ],
)
def test_whole_activity_completion_variants_are_recorded(client, activity, text):
    """활동 전체나 선택 활동 이름을 가리키는 완료 선언은 기록합니다."""
    sid = create_session(client)
    select_activity(client, sid, activity)
    result = message(client, sid, text, "done")
    assert result["record"]["created"] is True
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 1


@pytest.mark.parametrize(
    "text, intent",
    [("주식 정했어 다음 단계 알려줘", "activity_lookup"), ("러닝 다녀와서 계속 진행할게", "start")],
)
def test_out_of_scope_words_do_not_block_step_progress(client, text, intent):
    """범위 밖 단어가 섞여도 단계 조회·이어가기는 막지 않습니다."""
    sid = create_session(client)
    start_execution(client, sid)
    assert message(client, sid, text, "progress")["diagnostics"]["intent"] == intent


def test_out_of_scope_completion_does_not_record_selected_activity(client):
    """'러닝 끝냈어'는 선택해 둔 요리 활동의 완료가 아닙니다."""
    sid = create_session(client)
    select_activity(client, sid)
    result = message(client, sid, "러닝 끝냈어", "running")
    assert result["diagnostics"]["intent"] == "out_of_scope"
    assert client.get("/records", params={"session_id": sid}).json()["total"] == 0


@pytest.mark.parametrize("text", ["시작하자", "시작해줘"])
def test_start_variants_start_selected_activity(client, text):
    sid = create_session(client)
    select_activity(client, sid)
    result = message(client, sid, text, "start")
    assert result["diagnostics"]["intent"] == "start"
    assert result["execution_id"] is not None


@pytest.mark.parametrize("text", ["계속 진행할게", "활동 시작할게"])
def test_start_request_during_execution_keeps_current_step(client, text):
    """진행 중인 실행을 이어갈 때는 현재 단계를 1로 되돌리지 않습니다."""
    sid = create_session(client)
    execution = start_execution(client, sid)
    message(client, sid, "다음 단계", "next-1")
    message(client, sid, "다음 단계", "next-2")
    result = message(client, sid, text, "continue")
    assert result["diagnostics"]["intent"] == "start"
    assert result["execution_id"] == execution["execution_id"]
    assert result["current_step"] == 3


@pytest.mark.parametrize("text", ["지금 시작해도 돼?", "처음부터 다시 시작해야 돼?"])
def test_question_about_starting_does_not_restart_execution(client, text):
    """시작에 관한 질문은 실행을 새로 만들거나 진행 단계를 되돌리지 않습니다."""
    sid = create_session(client)
    execution = start_execution(client, sid)
    message(client, sid, "다음 단계", "next-1")
    message(client, sid, "다음 단계", "next-2")
    result = message(client, sid, text, "ask-start")
    assert result["diagnostics"]["intent"] == "explain"
    assert result["execution_id"] == execution["execution_id"]
    assert result["current_step"] == 3


def test_start_after_completed_execution_creates_new_execution(client):
    """완료한 실행은 이어가지 않고, 다시 시작하면 새 실행을 1단계부터 만듭니다."""
    sid = create_session(client)
    execution = start_execution(client, sid)
    assert message(client, sid, "완료했어", "done")["record"]["created"] is True
    result = message(client, sid, "활동 시작할게", "again")
    assert result["execution_id"] != execution["execution_id"]
    assert result["current_step"] == 1


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("text", ["감자 재료로 만들 수 있는 요리 알려줘", "도구 없이 할 수 있는 요리 알려줘"])
def test_dish_request_by_ingredient_or_tool_is_recommendation(client, text, selected):
    """재료·도구를 기준으로 요리를 찾는 질문은 선택 활동 조회가 아니라 추천입니다."""
    sid = create_session(client)
    if selected:
        select_activity(client, sid)
    result = message(client, sid, text, "find")
    assert result["diagnostics"]["intent"] == "recommend"
    assert result["diagnostics"]["search_performed"] is True


@pytest.mark.parametrize(
    "text, intent",
    [
        ("15분 안에 되는 요리 있어?", "update_constraints"),
        ("오븐 없이 되는 거 있어?", "update_constraints"),
        ("더 쉬운 거 있어?", "recommend"),
        ("이거 말고 다른 거 없어?", "recommend"),
    ],
)
def test_asking_for_other_dishes_after_selection_is_recommendation(client, text, intent):
    """활동을 고른 뒤에도 다른 요리를 찾는 물음은 설명이 아니라 추천이며 말한 조건을 저장합니다."""
    sid = create_session(client)
    recommend(client, sid)
    message(client, sid, "첫 번째 걸로 할게", "pick")
    result = message(client, sid, text, "other")
    assert result["diagnostics"]["intent"] == intent
    assert result["diagnostics"]["search_performed"] is True


def test_dish_question_after_selection_saves_spoken_conditions(client):
    sid = create_session(client)
    recommend(client, sid)
    message(client, sid, "첫 번째 걸로 할게", "pick")
    message(client, sid, "15분 안에 되는 요리 있어?", "time")
    message(client, sid, "오븐 없이 되는 거 있어?", "oven")
    state = constraints_of(client, sid)
    assert state["max_minutes"] == 15
    assert state["excluded_tools"] == ["오븐"]


@pytest.mark.parametrize(
    "text, intent",
    [
        ("3단계에서 10분 끓여도 가능해?", "activity_lookup"),
        ("3분만 더 기다리면 돼?", "explain"),
        ("10분 끓여도 가능해", "explain"),
        ("냄비 안에 넣고 10분 끓여도 돼", "explain"),
        ("0분 기다려도 가능해?", "explain"),
    ],
)
def test_time_in_question_about_selected_activity_is_not_a_limit(client, text, intent):
    """선택한 활동에 대한 질문 속 시간은 물음표가 없어도 시간 제한으로 저장하지 않고 추천 후보도 유지합니다."""
    sid = create_session(client)
    candidates = recommend(client, sid)
    message(client, sid, "첫 번째 걸로 할게", "pick")
    result = message(client, sid, text, "question")
    assert result["diagnostics"]["intent"] == intent
    state = client.get(f"/sessions/{sid}").json()
    assert state["constraints"]["max_minutes"] is None
    assert state["candidate_ids"] == candidates


def test_stated_time_limit_after_selection_still_updates_search(client):
    """활동을 고른 뒤에도 '10분만 가능해'는 시간 제한입니다."""
    sid = create_session(client)
    recommend(client, sid)
    message(client, sid, "첫 번째 걸로 할게", "pick")
    result = message(client, sid, "10분만 가능해", "limit")
    assert result["diagnostics"]["intent"] == "update_constraints"
    assert constraints_of(client, sid)["max_minutes"] == 10


def test_invalid_spoken_time_limit_is_rejected_only_when_applied(client):
    sid = create_session(client)
    response = client.post("/coach/messages", json={"session_id": sid, "message": "0분만 가능해"})
    assert response.status_code == 422


def test_place_word_an_is_not_a_time_limit():
    """'냉장고 안에 30분'의 '안에'는 장소이므로 시간 제한이 아닙니다."""
    from app.coach import spoken_constraints
    assert spoken_constraints("냉장고 안에 30분 두면 돼", {}) == {}
    assert spoken_constraints("30분 안에 할 요리", {}) == {"max_minutes": 30}


def test_explanation_without_selection_applies_spoken_limit(client):
    """선택 활동 없이 검색해서 설명할 때는 말한 시간 조건을 검색과 세션에 반영합니다."""
    sid = create_session(client)
    result = message(client, sid, "20분 안에 만드는 요리 설명해줘", "explain")
    assert result["diagnostics"]["intent"] == "explain"
    assert constraints_of(client, sid)["max_minutes"] == 20


def test_lower_bound_time_is_not_saved_as_limit(client):
    """'30분 이상'을 30분 이하 제한으로 뒤집어 저장하지 않습니다."""
    sid = create_session(client)
    message(client, sid, "오늘 30분 이상 걸리는 요리 추천해줘", "at-least")
    assert constraints_of(client, sid)["max_minutes"] is None


@pytest.mark.parametrize("text", ["20분 안에 할 수 있는 요리 알려줘", "20분 내로 만들 요리 알려줘"])
def test_within_time_phrases_set_limit(client, text):
    sid = create_session(client)
    message(client, sid, text, "within")
    assert constraints_of(client, sid)["max_minutes"] == 20


def test_time_limit_release_with_particle_reuses_previous_search(client):
    """'시간은 상관없어'도 제한 해제이며 직전 검색어로 다시 찾습니다."""
    sid = create_session(client)
    message(client, sid, "오늘 20분 안에 할 요리 추천해줘", "limit")
    result = message(client, sid, "시간은 상관없어", "release")
    assert result["diagnostics"]["intent"] == "update_constraints"
    state = client.get(f"/sessions/{sid}").json()
    assert state["constraints"]["max_minutes"] is None
    assert state["search_query"] == "오늘 20분 안에 할 요리 추천해줘"


@pytest.mark.parametrize("text", ["오븐이 없어요", "오븐은 없어"])
def test_oven_absence_with_particle_excludes_oven(client, text):
    sid = create_session(client)
    message(client, sid, text, "oven")
    assert constraints_of(client, sid)["excluded_tools"] == ["오븐"]


def test_step_count_in_recommendation_is_not_step_lookup(client):
    sid = create_session(client)
    result = message(client, sid, "5단계 넘는 건 빼고 추천해줘", "steps")
    assert result["diagnostics"]["intent"] == "recommend"


@pytest.mark.parametrize("text", ["두 번째 재료 보여줘", "2번째 재료 보여줘", "2번째 과정 보여줘", "1번 더 설명해줘"])
def test_ordinal_that_is_not_a_candidate_number_does_not_switch_activity(client, text):
    """'두 번째 재료', '2번째 과정', '1번 더'의 서수는 후보 선택이 아닙니다."""
    sid = create_session(client)
    candidates = recommend(client, sid)
    message(client, sid, "첫 번째 걸로 할게", "pick")
    result = message(client, sid, text, "not-a-pick")
    assert result["diagnostics"]["intent"] != "select"
    assert result["selected_activity_id"] == candidates[0]


@pytest.mark.parametrize("text", ["1번이랑 2번 비교해줘", "첫 번째랑 두 번째 비교해줘"])
def test_comparing_candidates_by_number_is_explanation(app_factory, text):
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator)) as client:
        sid = create_session(client)
        candidates = recommend(client, sid)
        result = message(client, sid, text, "compare")
    assert result["diagnostics"]["intent"] == "explain"
    assert result["selected_activity_id"] is None
    assert [e["activity_id"] for e in result["sources"]] == candidates


def test_explaining_candidate_by_number_uses_that_candidate(app_factory):
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator)) as client:
        sid = create_session(client)
        candidates = recommend(client, sid)
        result = message(client, sid, "두 번째 거 설명해줘", "explain-second")
    assert result["diagnostics"]["intent"] == "explain"
    assert [e["activity_id"] for e in result["sources"]] == [candidates[1]]


@pytest.mark.parametrize(
    "activity, text, expected, searched",
    [
        ("cook-0-2", "김치볶음밥은 어떻게 만들어?", "cook-2-2", True),
        ("cook-0-2", "왜 설탕을 넣어?", "cook-0-2", True),
        ("cook-0-2", "다시 설명해줘", "cook-0-2", True),
        ("cook-2-2", "버터로 볶아도 돼?", "cook-2-2", True),
        ("cook-0-2", "현재 단계를 쉽게 설명해줘", "cook-0-2", False),
    ],
)
def test_explanation_switches_only_when_question_names_another_dish(app_factory, activity, text, expected, searched):
    """선택 활동이 있어도 다른 활동의 제목을 그대로 말하면 그 활동을, 아니면 선택 활동만 근거로 설명합니다."""
    generator = FakeLLM()
    with TestClient(app_factory(llm=generator)) as client:
        sid = create_session(client)
        select_activity(client, sid, activity)
        result = message(client, sid, text, "explain")
    assert [e["activity_id"] for e in result["sources"]] == [expected]
    assert result["diagnostics"]["search_performed"] is searched
