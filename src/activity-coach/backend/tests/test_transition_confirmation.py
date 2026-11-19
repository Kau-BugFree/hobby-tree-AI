"""에이전트 설정이 켜져 있으면 낱말 없는 문제 해결·중단을 한 번 묻고 다음 턴의 답으로 적용하는지 실제 API로 확인합니다(설계 4.4절)."""

from fastapi.testclient import TestClient

from app.dialogue import ABANDON_QUESTION, CLOSED_TEXT, KEPT_OPEN, RESOLVE_QUESTION, TRANSITION_QUESTION
from app.turn_budget import BudgetExceeded
from test_agent_path import AgentLLM, CountingLLM, agent_client
from test_api import app_factory, create_session, message, select_activity  # noqa: F401

OPEN = {"action": "coach", "observation_updates": [{"quote": "반죽이 좀 질어요"}]}
YES = {"action": "answer_confirmation", "accept": True}
NO = {"action": "answer_confirmation", "accept": False}
QUESTION = {"question_id": "q", "text": "어느 단계인가요?", "options": ["예", "아니요"], "purpose": "clarification"}


def resolve(quote: str = "") -> dict:
    return {"action": "coach", "transition": "resolve", "transition_quote": quote}


def session_of(client: TestClient, sid: str) -> dict:
    return client.get(f"/sessions/{sid}").json()


def open_issue(client: TestClient) -> tuple[str, dict]:
    """활동을 고르고 코칭 행동(관찰 인용 포함)으로 문제를 엽니다."""
    sid = create_session(client)
    select_activity(client, sid)
    opened = message(client, sid, "반죽이 좀 질어요", "t0")
    assert opened["issue_id"] and opened["issue_phase"] not in {"resolved", "abandoned"}
    return sid, opened


def put_question(client: TestClient, sid: str) -> None:
    """코칭의 확인 질문이 대기 중인 상태로 만듭니다."""
    store = client.app.state.coach.store
    session = store.get_session(sid)
    session["dialogue"].update(phase="awaiting_observation", pending_question=QUESTION)
    store.save_session(session)


def test_resolve_without_the_word_is_asked_first(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"))
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        before = session_of(client, sid)["dialogue"]
        asked = message(client, sid, "이제 잘 돼요", "t1")
        after = session_of(client, sid)["dialogue"]
    assert (asked["mode"], asked["text"]) == ("clarify", RESOLVE_QUESTION)
    assert asked["issue_phase"] != "resolved" and asked["issue_id"] == opened["issue_id"]
    assert len(after["observations"]) == len(before["observations"])
    assert after["clarification_count"] == before["clarification_count"]
    assert after["asked_questions"] == before["asked_questions"]
    pending = after["pending_question"]
    assert (pending["purpose"], pending["proposed_transition"], pending["proposed_goal"]) == (
        "transition", "resolve", "이제 잘 돼요")
    assert pending["options"] == ["예", "아니요"] and pending["source_message_id"]
    assert (pending["previous_phase"], pending["previous_question"]) == (before["phase"], before["pending_question"])
    assert after["outcome"] is None


def test_accepted_resolve_closes_the_issue_with_the_users_words(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), YES)
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        message(client, sid, "이제 잘 돼요", "t1")
        source = session_of(client, sid)["dialogue"]["pending_question"]["source_message_id"]
        answered = message(client, sid, "응 그렇게 해줘", "t2")
        dialogue = session_of(client, sid)["dialogue"]
    assert (answered["issue_phase"], answered["issue_id"]) == ("resolved", opened["issue_id"])
    assert (answered["mode"], answered["text"]) == ("reference", CLOSED_TEXT)
    assert dialogue["outcome"] == {"quote": "이제 잘 돼요", "source_message_id": source, "kind": "self_report"}
    assert dialogue["pending_question"] is None
    assert llm.contexts[-1]["dialogue"]["pending_question"]["proposed_transition"] == "resolve"
    diag = answered["diagnostics"]
    assert (diag["decided_by"], diag["agent_action"], diag["intent"], diag["intent_category"]) == (
        "agent", "answer_confirmation", "coaching", "question")


def test_declined_resolve_keeps_the_issue_and_its_pending_question(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), NO)
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        put_question(client, sid)
        message(client, sid, "이제 잘 돼요", "t1")
        declined = message(client, sid, "아니 아직이야", "t2")
        dialogue = session_of(client, sid)["dialogue"]
    assert declined["text"] == KEPT_OPEN
    assert (declined["issue_phase"], declined["issue_id"]) == ("awaiting_observation", opened["issue_id"])
    assert dialogue["pending_question"] == QUESTION and dialogue["outcome"] is None


def test_another_action_drops_the_proposal_and_runs(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"),
                   {"action": "coach", "transition": "continue", "observation_updates": [{"quote": "아직 좀 짜요"}]})
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        put_question(client, sid)
        message(client, sid, "이제 잘 돼요", "t1")
        reply = message(client, sid, "아직 좀 짜요", "t2")
        dialogue = session_of(client, sid)["dialogue"]
    assert "아직 좀 짜요" in [o["quote"] for o in dialogue["observations"]]
    # 묻기 전의 확인 질문에 대한 답으로 기록된다(제안을 버리고 묻기 전 상태로 되돌렸다).
    answer = next(o for o in dialogue["observations"] if o["quote"] == "아직 좀 짜요")
    assert answer["question_snapshot"]["question_id"] == "q"
    assert reply["issue_id"] == opened["issue_id"] and reply["issue_phase"] not in {"resolved", "abandoned"}
    assert (dialogue["pending_question"] or {}).get("purpose") != "transition"


def test_resolve_with_the_word_is_applied_at_once(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("해결됐어요"))
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        reply = message(client, sid, "해결됐어요", "t1")
        dialogue = session_of(client, sid)["dialogue"]
    assert reply["mode"] != "clarify" and reply["issue_phase"] == "resolved"
    assert dialogue["outcome"]["quote"] == "해결됐어요"


def test_abandon_without_the_word_is_asked_first(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, {"action": "coach", "transition": "abandon", "transition_quote": "이건 그만할래"}, YES)
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        asked = message(client, sid, "이건 그만할래", "t1")
        assert (asked["mode"], asked["text"]) == ("clarify", ABANDON_QUESTION)
        assert asked["issue_phase"] != "abandoned"
        answered = message(client, sid, "응", "t2")
    assert (answered["issue_phase"], answered["text"]) == ("abandoned", CLOSED_TEXT)


def test_resolve_can_be_asked_again_after_a_decline(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), NO, resolve("이제 진짜 잘 돼요"))
    with agent_client(app_factory, llm) as client:
        sid, _ = open_issue(client)
        message(client, sid, "이제 잘 돼요", "t1")
        message(client, sid, "아니", "t2")
        again = message(client, sid, "이제 진짜 잘 돼요", "t3")
    assert (again["mode"], again["text"]) == ("clarify", RESOLVE_QUESTION)


def test_no_open_issue_is_not_asked(app_factory):  # noqa: F811
    llm = AgentLLM(resolve("이제 잘 돼요"))
    with agent_client(app_factory, llm) as client:
        sid = create_session(client)
        select_activity(client, sid)
        without_issue = message(client, sid, "이제 잘 돼요", "t1")
    llm = AgentLLM(OPEN, resolve("해결됐어요"), resolve("이제 잘 돼요"))
    with agent_client(app_factory, llm) as client:
        sid, _ = open_issue(client)
        message(client, sid, "해결됐어요", "t1")
        closed = message(client, sid, "이제 잘 돼요", "t2")
    assert without_issue["text"] != RESOLVE_QUESTION
    assert closed["text"] != RESOLVE_QUESTION and closed["issue_phase"] == "resolved"


def test_new_issue_question_accepts_the_agents_yes(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, {"action": "coach", "transition": "new_issue",
                          "observation_updates": [{"quote": "이번엔 설탕이 안 녹아요"}]}, YES)
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        asked = message(client, sid, "이번엔 설탕이 안 녹아요", "t1")
        assert asked["text"] == TRANSITION_QUESTION
        assert session_of(client, sid)["dialogue"]["pending_question"]["proposed_transition"] == "new_issue"
        answered = message(client, sid, "응 새로운 거야", "t2")
        dialogue = session_of(client, sid)["dialogue"]
    assert answered["issue_id"] and answered["issue_id"] != opened["issue_id"]
    assert dialogue["goal"] == "이번엔 설탕이 안 녹아요"
    assert answered["diagnostics"]["intent_category"] == "question"


def test_new_issue_question_declined_by_the_agent_keeps_the_issue(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, {"action": "coach", "transition": "new_issue",
                          "observation_updates": [{"quote": "이번엔 설탕이 안 녹아요"}]}, NO)
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        message(client, sid, "이번엔 설탕이 안 녹아요", "t1")
        declined = message(client, sid, "아니 같은 거야", "t2")
    assert declined["text"] == "기존 문제를 유지했어요. 새 문제라면 새 문제라고 명시해 주세요."
    assert declined["issue_id"] == opened["issue_id"]


def test_rule_path_yes_accepts_the_pending_resolve(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), status="timeout")
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        message(client, sid, "이제 잘 돼요", "t1")
        answered = message(client, sid, "네", "t2")
    assert (answered["issue_phase"], answered["issue_id"]) == ("resolved", opened["issue_id"])
    assert answered["diagnostics"]["decided_by"] == "rules"


def test_rule_path_no_declines_the_pending_resolve(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), status="timeout")
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        message(client, sid, "이제 잘 돼요", "t1")
        declined = message(client, sid, "아니요", "t2")
    assert declined["text"] == KEPT_OPEN and declined["issue_id"] == opened["issue_id"]


def test_rule_path_other_words_drop_the_proposal(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), status="timeout")
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        message(client, sid, "이제 잘 돼요", "t1")
        shown = message(client, sid, "준비물 알려줘", "t2")
        dialogue = session_of(client, sid)["dialogue"]
    assert shown["text"].startswith("준비물:")
    assert dialogue["issue_id"] == opened["issue_id"] and dialogue["phase"] not in {"resolved", "abandoned"}
    assert (dialogue["pending_question"] or {}).get("purpose") != "transition"


class ResolvingLLM(CountingLLM):
    """interpret_turn이 낱말 없는 말을 해결로 해석하는 가짜 모델."""

    async def interpret_turn(self, text: str, context: dict) -> dict:
        self.other_calls.append("interpret_turn")
        return {"decision": {"route": "dialogue", "transition": "resolve", "transition_quote": text},
                "status": "planned", "llm_calls": 1, "usage": {}}


def test_interpret_turn_resolve_is_confirmed_too(app_factory):  # noqa: F811
    llm = ResolvingLLM(OPEN, {"action": "select_candidate"})  # 쓸 수 없는 판단
    with agent_client(app_factory, llm) as client:
        sid, _ = open_issue(client)
        asked = message(client, sid, "이제 잘 돼요", "t1")
    assert "interpret_turn" in llm.other_calls
    assert asked["diagnostics"]["decided_by"] == "rules"
    assert (asked["mode"], asked["text"]) == ("clarify", RESOLVE_QUESTION)


def test_setting_off_ignores_a_wordless_resolve_as_before(app_factory):  # noqa: F811
    llm = ResolvingLLM()
    with TestClient(app_factory(llm=llm, api_key="x")) as client:
        sid, opened = open_issue(client)
        reply = message(client, sid, "이제 잘 돼요", "t1")
        dialogue = session_of(client, sid)["dialogue"]
    assert llm.texts == [] and "interpret_turn" in llm.other_calls
    assert reply["text"] != RESOLVE_QUESTION and reply["issue_phase"] != "resolved"
    assert reply["issue_id"] == opened["issue_id"]
    assert (dialogue["pending_question"] or {}).get("purpose") != "transition"


def test_completion_question_wins_over_the_transition_one(app_factory):  # noqa: F811
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"), YES)
    with agent_client(app_factory, llm) as client:
        sid, _ = open_issue(client)
        message(client, sid, "이제 잘 돼요", "t1")
        store = client.app.state.coach.store
        session = store.get_session(sid)
        session["pending_confirmation"] = {"kind": "complete", "activity_id": "cook-0-2",
                                           "execution_id": session["execution_id"], "message_id": "m"}
        store.save_session(session)
        answered = message(client, sid, "응", "t2")
        records = client.get("/records", params={"session_id": sid}).json()["total"]
        dialogue = session_of(client, sid)["dialogue"]
    assert (answered["mode"], answered["diagnostics"]["intent"]) == ("action", "complete")
    assert records == 1
    assert dialogue["pending_question"]["proposed_transition"] == "resolve"


class RaisingLLM(AgentLLM):
    """정해 둔 차례에 판단 호출 중 턴 예산 초과로 끝나는 가짜 모델."""

    async def choose_action(self, text: str, context: dict) -> dict:
        if self.decisions and self.decisions[0] == "raise":
            self.decisions.pop(0)
            raise BudgetExceeded()
        return await super().choose_action(text, context)


def test_a_turn_ending_in_an_exception_drops_the_proposal(app_factory):  # noqa: F811
    llm = RaisingLLM(OPEN, resolve("이제 잘 돼요"), "raise", YES)
    with agent_client(app_factory, llm) as client:
        sid, opened = open_issue(client)
        put_question(client, sid)
        message(client, sid, "이제 잘 돼요", "t1")
        stopped = message(client, sid, "음", "t2")
        dialogue = session_of(client, sid)["dialogue"]
        after = message(client, sid, "네", "t3")
    assert stopped["diagnostics"]["stop_reason"] == "budget_exhausted"
    assert dialogue["pending_question"] == QUESTION and dialogue["phase"] == "awaiting_observation"
    assert after["diagnostics"]["agent_reason"] == "no_pending_confirmation"
    assert after["issue_phase"] != "resolved"


def leftover_resolve(app_factory) -> tuple[str, dict]:  # noqa: F811
    """설정을 켠 서버로 해결 제안을 남기고 세션 ID와 묻기 전 대화를 돌려줍니다."""
    llm = AgentLLM(OPEN, resolve("이제 잘 돼요"))
    with agent_client(app_factory, llm) as client:
        sid, _ = open_issue(client)
        before = session_of(client, sid)["dialogue"]
        message(client, sid, "이제 잘 돼요", "t1")
        assert session_of(client, sid)["dialogue"]["pending_question"]["proposed_transition"] == "resolve"
    return sid, before


def test_setting_off_drops_a_leftover_resolve_before_a_yes(app_factory):  # noqa: F811
    sid, before = leftover_resolve(app_factory)
    with TestClient(app_factory()) as client:  # 같은 임시 DB, 설정을 끈 서버
        reply = message(client, sid, "네", "t2")
        session = client.app.state.coach.store.get_session(sid)  # 보관된 문제(_issue_snapshots)까지 본다
    assert reply["issue_id"] == before["issue_id"]
    assert session["dialogue"]["issue_id"] == before["issue_id"]
    assert not session.get("_issue_snapshots")
    assert (session["dialogue"]["pending_question"] or {}).get("purpose") != "transition"


def test_setting_off_drops_a_leftover_resolve_and_keeps_coaching(app_factory):  # noqa: F811
    sid, before = leftover_resolve(app_factory)
    with TestClient(app_factory()) as client:
        reply = message(client, sid, "아직도 반죽이 찢어져요", "t2")
        dialogue = session_of(client, sid)["dialogue"]
    # 키 없는 규칙 경로에서 코칭으로 가는 말(문제 보고)로 확인한다. 예전에는 전환 질문의 답으로 읽혀 막혔다.
    assert reply["diagnostics"]["intent"] == "coaching"
    assert reply["diagnostics"].get("stop_reason") != "ambiguous_intent"
    assert reply["issue_id"] == before["issue_id"]
    assert "아직도 반죽이 찢어져요" in [o["quote"] for o in dialogue["observations"]]
    assert (dialogue["pending_question"] or {}).get("purpose") != "transition"


def test_setting_off_new_issue_question_keeps_its_stored_shape(app_factory):  # noqa: F811
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        select_activity(client, sid)
        opened = message(client, sid, "반죽이 자꾸 찢어져요", "t0")
        assert opened["issue_id"]
        asked = message(client, sid, "다른 부분도 실패했어요", "t1")
        pending = session_of(client, sid)["dialogue"]["pending_question"]
    assert asked["text"] == TRANSITION_QUESTION
    assert pending["purpose"] == "transition" and "proposed_transition" not in pending
