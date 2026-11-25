"""모델 판단이 끼는 대화에서 상태 변경 규칙이 끼어들지 않는지 실제 API로 확인합니다."""

import pytest
from fastapi.testclient import TestClient
from test_ambiguity_routing import Interpreter, clarify
from test_api import app_factory, create_session, message, select_activity  # noqa: F401

from app.answer_policy import UNAVAILABLE_PREFIX


class AskingLLM:
    """코칭 판단에서 확인 질문을 한 번 내는 가짜 모델."""
    configured = True
    model = "fake"

    async def interpret_turn(self, text: str, context: dict) -> dict:
        return {"decision": {"route": "dialogue", "transition": "continue"},
                "status": "ok", "llm_calls": 1, "usage": {}}

    async def decide(self, question: str, context: dict, evidence: list) -> dict:
        return {"decision": {"next_action": "clarify", "question": "어느 단계에서 그랬나요?"},
                "status": "ok", "llm_calls": 1, "usage": {}}


def test_done_while_question_pending_is_an_answer_not_a_completion(app_factory) -> None:
    with TestClient(app_factory(llm=AskingLLM(), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        asked = message(client, sid, "반죽이 자꾸 찢어져요", "q1")
        assert asked["mode"] == "clarify"
        assert "어느 단계에서 그랬나요?" in asked["text"]
        reply = message(client, sid, "다 했어요", "q2")
        assert reply["diagnostics"]["intent"] != "complete"
        assert "record" not in reply
        records = client.get("/records", params={"session_id": sid}).json()
        assert records["total"] == 0


class FailingLLM:
    """모든 호출이 공급자 장애로 끝나는 가짜 모델."""
    configured = True
    model = "fake"

    def __init__(self, status: str = "timeout") -> None:
        self.status = status

    async def interpret_turn(self, text: str, context: dict) -> dict:
        return {"decision": None, "status": self.status, "llm_calls": 1, "usage": {}}

    async def decide(self, question: str, context: dict, evidence: list) -> dict:
        return {"decision": None, "status": self.status, "llm_calls": 1, "usage": {}}

    async def explain_claims(self, *args: object, **kwargs: object) -> dict:
        return {"claims": None, "status": self.status, "llm_calls": 1, "usage": {}}

    async def verify_claims(self, *args: object, **kwargs: object) -> dict:
        return {"verdicts": None, "status": self.status, "llm_calls": 1, "usage": {}}


def test_problem_report_without_model_gets_source_text_not_a_question(app_factory) -> None:
    with TestClient(app_factory()) as client:  # 키 없음
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, "반죽이 자꾸 찢어져요", "p1")
        assert reply["mode"] == "reference"
        assert "clarification" not in reply
        assert reply["sources"]


@pytest.mark.parametrize("status", ["timeout", "network_error", "rate_limited", "authentication_error"])
def test_provider_failure_during_coaching_falls_back_to_source_text(app_factory, status: str) -> None:
    with TestClient(app_factory(llm=FailingLLM(status), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, "반죽이 자꾸 찢어져요", "f1")
        assert reply["mode"] == "reference"
        assert reply["diagnostics"]["llm_status"] == status
        assert reply["diagnostics"].get("stop_reason") != "execution_failure"
        assert reply["text"].startswith("설명을 생성하지 못해 자료 원문을 그대로 안내합니다.")


def test_problem_report_without_key_is_not_marked_as_a_failure(app_factory) -> None:
    with TestClient(app_factory()) as client:  # 키 없음: 실패가 아니라 원래 원문 안내다
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, "반죽이 자꾸 찢어져요", "k1")
        assert reply["mode"] == "reference"
        assert not reply["text"].startswith(UNAVAILABLE_PREFIX)


STATE_SENTENCE = "설탕이 아직 덜 녹은 상태예요"   # classify가 None이고 규칙만으로는 뜻이 분명하지 않다


@pytest.mark.parametrize("text", [STATE_SENTENCE, "예"])
def test_interpretation_failure_without_reranker_asks_back(app_factory, text: str) -> None:
    """해석 실패 뒤의 말은 키 없는 경로와 같다(설계 4.4절). 재순위가 꺼져 있으면 선택 활동이 있어도 되묻는다."""
    with TestClient(app_factory(llm=FailingLLM("timeout"), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, text, "l1")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["intent"] == "ambiguous"
        assert reply["sources"] == [] and not reply["text"].startswith(UNAVAILABLE_PREFIX)
        assert reply["diagnostics"]["llm_status"] == "timeout"


def test_interpretation_failure_with_reranker_looks_up_the_selected_activity(tmp_path) -> None:
    from test_service_levels import HEAD, reranked_app   # test_service_levels가 이 모듈을 가져오므로 여기서 가져온다
    from app.answer_policy import REFERENCE_TOP_K
    with TestClient(reranked_app(tmp_path, llm=FailingLLM("timeout"), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, STATE_SENTENCE, "l2")
        assert reply["diagnostics"]["intent"] == "evidence_lookup" and reply["text"].startswith(HEAD)
        assert 0 < len(reply["sources"]) <= REFERENCE_TOP_K
        assert all(source["activity_id"] == "cook-0-2" for source in reply["sources"])
        assert reply["diagnostics"]["llm_status"] == "timeout"


def test_interpretation_failure_reply_without_topic_is_asked_back_before_lookup(tmp_path) -> None:
    from test_service_levels import reranked_app
    with TestClient(reranked_app(tmp_path, llm=FailingLLM("timeout"), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, "예", "l3")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["intent"] == "ambiguous"
        assert reply["sources"] == [] and reply["diagnostics"]["search_performed"] is False


def test_replaying_a_fallback_turn_does_not_record_the_report_twice(app_factory) -> None:
    with TestClient(app_factory(llm=FailingLLM(), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        first = message(client, sid, "반죽이 자꾸 찢어져요", "same")
        again = message(client, sid, "반죽이 자꾸 찢어져요", "same")
        assert again["text"] == first["text"] and again["diagnostics"]["replayed"] is True
        session = client.get(f"/sessions/{sid}").json()
        quotes = [o["quote"] for o in session["dialogue"]["observations"]]
        assert quotes.count("반죽이 자꾸 찢어져요") == 1


def test_contract_violation_still_stops(app_factory) -> None:
    with TestClient(app_factory(llm=FailingLLM("invalid_decision"), api_key="x")) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, "반죽이 자꾸 찢어져요", "c1")
        assert reply["diagnostics"]["stop_reason"] == "execution_failure"


def test_provider_failure_during_interpretation_asks_back(app_factory) -> None:
    """규칙이 정하지 못한 말에서 해석 호출이 실패해도 턴을 실패 처리하지 않고 되묻습니다."""
    with TestClient(app_factory(llm=FailingLLM("timeout"), api_key="x")) as client:
        sid = create_session(client)
        reply = message(client, sid, "음 글쎄 그게", "u1")
        assert reply["mode"] == "clarify"
        assert reply["diagnostics"]["llm_status"] == "timeout"
        assert reply["diagnostics"].get("stop_reason") != "execution_failure"
        assert "요청을 해석하는 중 오류가 발생했어요" not in reply["text"]


def test_model_dialogue_route_keeps_the_step_named_in_a_keywordless_report(app_factory) -> None:
    """모델이 문제로 해석한 말에서 말한 단계가 문제 단계가 되고 현재 단계는 그대로입니다."""
    text = "3단계에서 간장이 너무 짜요"  # 규칙으로는 정하지 못하는 말(classify가 None)
    llm = Interpreter("dialogue", [clarify(1)])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client)
        select_activity(client, sid)
        before = client.get(f"/sessions/{sid}").json()["current_step"]
        message(client, sid, text, "r1")
        session = client.get(f"/sessions/{sid}").json()
        assert llm.interpreted == [text]
        assert session["dialogue"]["step_number"] == 3
        assert session["current_step"] == before
