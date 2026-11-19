"""에이전트 설정이 켜져 있으면 완료를 한 번 묻고 다음 턴의 수락으로 기록하는지 실제 API로 확인합니다(설계 4.4절)."""

import pytest
from fastapi.testclient import TestClient

from app import confirmation
from test_agent_path import AgentLLM, agent_client
from test_api import app_factory, create_session, message, select_activity  # noqa: F401

ASKED = "활동을 완료로 기록할까요?"


def records(client: TestClient, sid: str) -> int:
    return client.get("/records", params={"session_id": sid}).json()["total"]


def session_of(client: TestClient, sid: str) -> dict:
    return client.get(f"/sessions/{sid}").json()


def rules_client(app_factory) -> TestClient:  # noqa: F811
    """설정은 켜져 있고 키가 없는 서버(규칙 경로)."""
    return TestClient(app_factory(agent_enabled=True))


def ask_on_rules(client: TestClient) -> str:
    sid = create_session(client)
    select_activity(client, sid)
    asked = message(client, sid, "활동 완료했어", "k1")
    assert asked["mode"] == "clarify" and asked["text"].endswith(ASKED)
    assert records(client, sid) == 0
    return sid


def test_completion_is_asked_before_it_is_recorded(app_factory):  # noqa: F811
    llm = AgentLLM({"action": "complete_activity"}, {"action": "answer_confirmation", "accept": True})
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        asked = message(client, sid, "이제 다 만들었어", "c1")
        assert asked["mode"] == "clarify" and asked["text"].endswith(ASKED)
        assert records(client, sid) == 0
        pending = session_of(client, sid)["pending_confirmation"]
        assert (pending["kind"], pending["activity_id"], pending["message_id"]) == (
            "complete", "cook-0-2", asked["message_id"])
        answered = message(client, sid, "응 기록해줘", "c2")
        assert answered["mode"] == "action"
        assert llm.contexts[1]["pending_confirmation"]["kind"] == "complete"
        assert llm.contexts[1]["pending_confirmation"]["title"] in asked["text"]
        assert records(client, sid) == 1
        assert session_of(client, sid).get("pending_confirmation") is None
    for reply in (asked, answered):
        diag = reply["diagnostics"]
        assert (diag["intent"], diag["intent_category"], diag["decided_by"]) == ("complete", "state_change", "agent")


def test_declined_completion_records_nothing(app_factory):  # noqa: F811
    llm = AgentLLM({"action": "complete_activity"}, {"action": "answer_confirmation", "accept": False},
                   {"action": "answer_confirmation", "accept": True})
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "d1")
        declined = message(client, sid, "아니 아직이야", "d2")
        assert declined["text"] == confirmation.DECLINED and declined["diagnostics"]["intent"] == "complete"
        assert records(client, sid) == 0 and session_of(client, sid).get("pending_confirmation") is None
        after = message(client, sid, "네", "d3")
        assert after["diagnostics"]["agent_reason"] == "no_pending_confirmation"
        assert records(client, sid) == 0


def test_another_action_drops_the_pending_confirmation(app_factory):  # noqa: F811
    llm = AgentLLM({"action": "complete_activity"}, {"action": "show_materials"},
                   {"action": "answer_confirmation", "accept": True})
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "o1")
        shown = message(client, sid, "준비물 뭐였지", "o2")
        assert shown["text"].startswith("준비물:")
        assert session_of(client, sid).get("pending_confirmation") is None
        after = message(client, sid, "응", "o3")
    assert after["diagnostics"]["decided_by"] == "rules"
    assert after["diagnostics"]["agent_reason"] == "no_pending_confirmation"
    assert llm.contexts[2]["pending_confirmation"] is None


def test_rule_path_confirms_completion_too(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = ask_on_rules(client)
        answered = message(client, sid, "네", "k2")
        assert (answered["mode"], answered["diagnostics"]["intent"]) == ("action", "complete")
        assert answered["diagnostics"]["decided_by"] == "rules"
        assert records(client, sid) == 1


def test_rule_path_decline_records_nothing(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = ask_on_rules(client)
        declined = message(client, sid, "취소!", "k2")
        assert declined["text"] == confirmation.DECLINED
        assert records(client, sid) == 0 and session_of(client, sid).get("pending_confirmation") is None


def test_rule_path_other_words_drop_the_confirmation(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = ask_on_rules(client)
        shown = message(client, sid, "준비물 알려줘", "k2")
        assert shown["text"].startswith("준비물:")
        assert session_of(client, sid).get("pending_confirmation") is None
        message(client, sid, "네", "k3")
        assert records(client, sid) == 0


def test_allergy_turn_drops_the_confirmation(app_factory):  # noqa: F811
    llm = AgentLLM({"action": "complete_activity"})
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "a1")
        message(client, sid, "땅콩 알레르기 있는데 괜찮아?", "a2")
        assert session_of(client, sid).get("pending_confirmation") is None


def test_setting_off_records_at_once(app_factory):  # noqa: F811
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, "활동 완료했어", "s1")
        assert reply["mode"] == "action" and reply["text"] == "활동 완료를 기록했어요."
        assert records(client, sid) == 1
        assert "pending_confirmation" not in session_of(client, sid)


def test_completion_without_an_activity_keeps_the_existing_guidance(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = create_session(client)
        reply = message(client, sid, "활동 완료했어", "n1")
        assert (reply["mode"], reply["text"]) == ("clarify", "완료한 활동을 먼저 선택해 주세요.")
        assert session_of(client, sid).get("pending_confirmation") is None


def test_yes_answers_the_completion_question_not_the_coaching_one(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = create_session(client)
        select_activity(client, sid)
        store = client.app.state.coach.store
        session = store.get_session(sid)
        question = {"question_id": "q", "text": "어느 단계인가요?", "options": ["예", "아니요"],
                    "purpose": "clarification"}
        session["dialogue"].update(issue_id="i", phase="awaiting_observation", pending_question=question)
        store.save_session(session)
        asked = message(client, sid, "활동 완료했어", "y1")
        assert asked["text"].endswith(ASKED)
        assert session_of(client, sid)["dialogue"]["pending_question"] == question
        answered = message(client, sid, "네", "y2")
        assert (answered["mode"], answered["diagnostics"]["intent"]) == ("action", "complete")
        assert records(client, sid) == 1
        assert session_of(client, sid)["dialogue"]["pending_question"] == question


def test_selecting_another_activity_cancels_the_confirmation(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = ask_on_rules(client)
        select_activity(client, sid, "cook-0-3")
        message(client, sid, "네", "k2")
        assert records(client, sid) == 0
        assert session_of(client, sid).get("pending_confirmation") is None


def test_activity_in_the_request_cancels_the_confirmation(app_factory):  # noqa: F811
    llm = AgentLLM({"action": "complete_activity"}, {"action": "answer_confirmation", "accept": True})
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "r1")
        reply = message(client, sid, "응", "r2", activity_id="cook-0-3")
    assert reply["diagnostics"]["agent_reason"] == "no_pending_confirmation"
    assert llm.contexts[1]["pending_confirmation"] is None


def test_replayed_acceptance_records_once(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = ask_on_rules(client)
        first = message(client, sid, "네", "k2")
        again = message(client, sid, "네", "k2")
        assert again["text"] == first["text"] and again["diagnostics"]["replayed"] is True
        assert records(client, sid) == 1


def test_confirmation_survives_a_restart(app_factory):  # noqa: F811
    with rules_client(app_factory) as client:
        sid = ask_on_rules(client)
    with rules_client(app_factory) as client:
        answered = message(client, sid, "네", "k2")
        assert answered["mode"] == "action"
        assert records(client, sid) == 1


@pytest.mark.parametrize("text,expected", [
    ("네", True), (" 응! ", True), ("예.", True), ("맞아~", True), ("기록해줘", True),
    ("아니", False), ("아니요.", False), ("취소", False),
    ("네 그런데", None), ("준비물 알려줘", None), ("", None)])
def test_spoken_answer(text, expected):
    assert confirmation.spoken_answer(text) is expected


@pytest.mark.parametrize("second", [None, {"action": "select_candidate"}], ids=["timeout", "unusable_decision"])
def test_yes_on_the_rules_path_accepts_the_pending_completion(app_factory, second):  # noqa: F811
    """Claude가 답하지 못하거나 쓸 수 없는 판단을 내 규칙 경로로 내려온 턴에도 '네'가 완료를 수락한다."""
    llm = AgentLLM({"action": "complete_activity"}, second, status="timeout")
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "p1")
        answered = message(client, sid, "네", "p2")
        assert records(client, sid) == 1
    assert (answered["mode"], answered["diagnostics"]["decided_by"]) == ("action", "rules")
    assert answered["diagnostics"]["agent_reason"] == ("timeout" if second is None else "invalid_arguments")


def test_a_turn_ending_in_an_exception_drops_the_confirmation(app_factory):  # noqa: F811
    from app.turn_budget import BudgetExceeded

    class RaisingLLM(AgentLLM):
        async def choose_action(self, text: str, context: dict) -> dict:
            if len(self.texts) == 1:
                self.texts.append(text)
                raise BudgetExceeded()
            return await super().choose_action(text, context)

    llm = RaisingLLM({"action": "complete_activity"}, {"action": "answer_confirmation", "accept": True})
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "e1")
        stopped = message(client, sid, "음", "e2")
        assert stopped["diagnostics"]["stop_reason"] == "budget_exhausted"
        assert session_of(client, sid).get("pending_confirmation") is None
        after = message(client, sid, "네", "e3")
        assert records(client, sid) == 0
    assert after["diagnostics"]["agent_reason"] == "no_pending_confirmation"


def test_setting_off_ignores_a_leftover_confirmation(app_factory):  # noqa: F811
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        select_activity(client, sid)
        store = client.app.state.coach.store
        session = store.get_session(sid)
        session["pending_confirmation"] = {"kind": "complete", "activity_id": "cook-0-2",
                                           "execution_id": None, "message_id": "m"}
        store.save_session(session)
        message(client, sid, "네", "l1")
        assert records(client, sid) == 0


def test_turning_the_setting_off_and_on_drops_a_stale_confirmation(app_factory):  # noqa: F811
    """켠 서버에서 물음 → 끈 서버에서 2턴 → 다시 켠 서버에서 '네'. app_factory의 서버는 같은 임시 DB를 쓴다."""
    with agent_client(app_factory, AgentLLM({"action": "complete_activity"})) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "이제 다 만들었어", "t1")
        assert session_of(client, sid)["pending_confirmation"]["kind"] == "complete"
    with TestClient(app_factory(llm=AgentLLM(), api_key="x")) as client:  # 설정 꺼짐
        message(client, sid, "준비물 뭐였지", "t2")
        message(client, sid, "음", "t3")
        assert session_of(client, sid).get("pending_confirmation") is None
    with agent_client(app_factory, AgentLLM({"action": "answer_confirmation", "accept": True})) as client:
        after = message(client, sid, "네", "t4")
        assert records(client, sid) == 0
    assert after["diagnostics"]["agent_reason"] == "no_pending_confirmation"
