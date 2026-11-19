"""실행 함수가 원문 없이 행동과 인자만으로 실행되는지 확인합니다."""
import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import action_executors
from app.action_executors import EXECUTORS, Turn, execute
from app.actions import (ACTION_NAMES, AskUser, CoachTurn, Explain, Notice, Recommend, SelectCandidate, ShowMaterials,
                         ShowSteps, StartActivity)
from app.dialogue_models import TurnInterpretation
from app.intent_handlers import OVERLAP_TEXT, UNCLEAR_DEICTIC, UNCLEAR_SELECTED, UNCLEAR_UNSELECTED, context_for
from app.turn_budget import BudgetExceeded, TurnBudget
from test_api import app_factory, create_session, select_activity  # noqa: F401

PARTIAL_TEXT = ("단계나 일부 작업을 마친 것만으로 활동 전체를 완료 처리하지 않아요. "
                "모든 작업을 마쳤다면 '활동 완료했어'라고 알려주세요.")


def turn_for(app, sid: str, text: str = "", request_id: str = "r1") -> Turn:
    """Coach._respond_locked가 만드는 것과 같은 Turn을 원문 없이 만든다."""
    coach = app.state.coach
    session = coach.store.get_session(sid)
    selected = coach.activity(session["selected_activity_id"]) if session["selected_activity_id"] else None
    response = {"message_id": "m1", "session_id": sid, "mode": "direct", "text": "", "activities": [], "sources": [],
                "actions": [], "diagnostics": {"intent": "unknown", "search_performed": False, "search_mode": "none",
                                               "llm_calls": 0, "llm_status": "not_needed"}}
    return Turn(text, session, response, selected, TurnBudget(), request_id, "u1", None,
                context_for(session, selected, False, False))


@pytest.fixture
def app_client(app_factory):
    app = app_factory()
    with TestClient(app) as client:
        yield app, client


def selected_turn(app_client) -> tuple:
    """cook-0-2를 선택한 세션의 (coach, turn)."""
    app, client = app_client
    sid = create_session(client)
    select_activity(client, sid)
    return app.state.coach, turn_for(app, sid)


def test_whole_steps_are_listed_from_the_argument_alone(app_client):
    coach, turn = selected_turn(app_client)

    asyncio.run(execute(coach, turn, ShowSteps(action="show_steps", whole=True)))

    assert turn.response["text"].startswith("1. ") and turn.response["diagnostics"]["intent"] == "activity_lookup"
    assert turn.response["text"].count("\n") == len(turn.selected["steps"]) - 1


def test_restart_argument_starts_a_new_execution(app_client):
    coach, turn = selected_turn(app_client)

    asyncio.run(execute(coach, turn, StartActivity(action="start_activity", restart=True)))
    first = turn.session["execution_id"]
    turn.request_id = "r2"
    asyncio.run(execute(coach, turn, StartActivity(action="start_activity", restart=True)))

    assert first and turn.session["execution_id"] != first
    assert turn.response["mode"] == "action" and turn.response["diagnostics"]["intent"] == "start"
    assert "활동을 시작했어요" in turn.response["text"]


def test_move_argument_moves_the_current_step(app_client):
    coach, turn = selected_turn(app_client)

    asyncio.run(execute(coach, turn, ShowMaterials(action="show_materials", move="next")))

    assert turn.session["current_step"] == 2
    assert turn.response["text"].startswith("준비물: ") and turn.response["diagnostics"]["intent"] == "activity_lookup"


def test_step_out_of_range_keeps_the_step_and_asks(app_client):
    coach, turn = selected_turn(app_client)

    asyncio.run(execute(coach, turn, ShowSteps(action="show_steps", step=99)))

    assert turn.response["mode"] == "clarify"
    assert turn.response["text"].startswith("단계 범위를 확인해 주세요")
    assert turn.session["current_step"] == 1


@pytest.mark.parametrize("number", [0, 9])
def test_select_out_of_range_changes_nothing(app_client, number):
    app, client = app_client
    turn = turn_for(app, create_session(client))

    asyncio.run(execute(app.state.coach, turn, SelectCandidate(action="select_candidate", number=number)))

    assert turn.response["mode"] == "clarify" and turn.response["text"].startswith("해당 순서의 추천이 없습니다")
    assert turn.selected is None and turn.session["selected_activity_id"] is None
    assert turn.response["diagnostics"]["intent"] == "select"


def test_partial_completion_notice_records_nothing(app_client):
    coach, turn = selected_turn(app_client)

    asyncio.run(execute(coach, turn, Notice(action="notice", kind="partial_completion")))

    assert coach.store.records(turn.session["session_id"]) == []
    assert turn.response["mode"] == "clarify" and turn.response["text"] == PARTIAL_TEXT
    assert turn.response["diagnostics"]["intent"] == "complete"


def test_certainty_writes_its_intent_only_after_the_lookup(app_client, monkeypatch):
    # 예전 handle_certainty처럼 검색이 예산 초과로 끊기면 진단 값은 실행 전 그대로다.
    coach, turn = selected_turn(app_client)

    def exhausted(activity_id: str) -> list:
        raise BudgetExceeded()

    monkeypatch.setattr(coach.retrieval, "knowledge_for_activity", exhausted)

    with pytest.raises(BudgetExceeded):
        asyncio.run(execute(coach, turn, Notice(action="notice", kind="certainty")))
    assert turn.response["diagnostics"]["intent"] == "unknown"


def fresh_turn(app_client) -> tuple:
    """활동을 선택하지 않은 새 세션의 (coach, turn)."""
    app, client = app_client
    return app.state.coach, turn_for(app, create_session(client))


def recommend(coach, turn, **arguments) -> None:
    asyncio.run(execute(coach, turn, Recommend(action="recommend", **arguments)))


def test_recommend_topic_and_time_come_from_arguments(app_client):
    coach, turn = fresh_turn(app_client)

    recommend(coach, turn, topic="달걀", max_minutes=20)

    assert turn.session["constraints"]["max_minutes"] == 20
    assert turn.session["candidate_ids"]
    assert turn.session["search_query"] == "달걀"
    assert turn.response["diagnostics"]["intent"] == "update_constraints"


def test_recommend_clear_time_limit_removes_the_limit(app_client):
    coach, turn = fresh_turn(app_client)
    recommend(coach, turn, topic="달걀", max_minutes=20)

    recommend(coach, turn, clear_time_limit=True)

    assert turn.session["constraints"]["max_minutes"] is None
    assert turn.response["diagnostics"]["intent"] == "update_constraints"


def test_recommend_excluded_tools_accumulate(app_client):
    coach, turn = fresh_turn(app_client)

    recommend(coach, turn, topic="달걀", excluded_tools=["오븐"])
    recommend(coach, turn, excluded_tools=["칼"])

    assert turn.session["constraints"]["excluded_tools"] == ["오븐", "칼"]


def test_recommend_other_skips_shown_candidates(app_client):
    coach, turn = fresh_turn(app_client)
    recommend(coach, turn, topic="달걀")
    first = list(turn.response["diagnostics"]["candidate_ids"])

    recommend(coach, turn, topic="달걀", other=True)
    second = turn.response["diagnostics"]["candidate_ids"]

    assert first and second and not set(first) & set(second)
    assert turn.response["diagnostics"]["intent"] == "recommend"


def test_recommend_other_without_a_topic_continues_the_shown_topic(app_client):
    coach, turn = fresh_turn(app_client)
    recommend(coach, turn, topic="달걀")
    first = list(turn.response["diagnostics"]["candidate_ids"])

    recommend(coach, turn, other=True)   # 에이전트 경로(RuleExtras 기본값): topic이 비면 앞서 보여 준 주제
    second = turn.response["diagnostics"]["candidate_ids"]

    assert turn.session["last_recommendation"]["query"] == "달걀"
    assert first and second and not set(first) & set(second)


def test_recommend_empty_topic_reuses_the_previous_topic(app_client):
    coach, turn = fresh_turn(app_client)
    recommend(coach, turn, topic="달걀")

    recommend(coach, turn, max_minutes=30)

    # 검색어를 다시 쓰지 않으면 빈 검색어로 입문 목록이 나와도 search_query는 그대로라, 이번 추천의 검색어를 본다.
    assert turn.session["last_recommendation"]["query"] == "달걀"
    assert turn.session["search_query"] == "달걀"
    assert turn.session["constraints"]["max_minutes"] == 30


def test_recommend_unindexed_scope_searches_activities(app_client):
    coach, turn = fresh_turn(app_client)

    recommend(coach, turn, topic="달걀", scope="recipes")

    assert turn.session["search_scope"] == "activities"


def test_explain_candidates_out_of_range_asks_instead_of_searching(app_client):
    coach, turn = fresh_turn(app_client)

    asyncio.run(execute(coach, turn, Explain(action="explain", target="candidates", candidate_numbers=[7])))

    assert turn.response["mode"] == "clarify"
    assert turn.response["text"] == "해당 순서의 추천이 없습니다. 먼저 활동을 추천받아 주세요."
    assert turn.response["diagnostics"]["search_performed"] is False
    assert turn.response["diagnostics"]["intent"] == "explain"


def test_explain_original_skips_the_reranker(app_client, monkeypatch):
    coach, turn = selected_turn(app_client)
    calls = []

    def rank(question: str, evidence: list) -> tuple:
        calls.append(question)
        return None, None

    monkeypatch.setattr(coach.retrieval, "rank_evidence", rank)
    turn.text = "재료 원문 그대로 보여줘"

    asyncio.run(execute(coach, turn, Explain(action="explain", target="selected", original=True)))
    assert calls == []
    # 원문 요청이 아니면 같은 턴에서 재순위를 부른다(Claude가 없는 턴이라는 확인).
    asyncio.run(execute(coach, turn, Explain(action="explain", target="selected")))
    assert calls == ["재료 원문 그대로 보여줘"]
    assert turn.response["sources"][0]["activity_id"] == turn.selected["activity_id"]


@pytest.mark.parametrize(("kind", "selected", "expected", "stop_reason"), [
    ("unclear", False, UNCLEAR_UNSELECTED, "ambiguous_intent"),
    ("unclear", True, UNCLEAR_SELECTED, "ambiguous_intent"),
    ("which_activity", False, UNCLEAR_DEICTIC, None),
    ("two_requests", True, OVERLAP_TEXT, "ambiguous_intent"),
], ids=["unclear-unselected", "unclear-selected", "which_activity", "two_requests"])
def test_ask_user_kinds_use_the_fixed_texts(app_client, kind, selected, expected, stop_reason):
    coach, turn = selected_turn(app_client) if selected else fresh_turn(app_client)

    asyncio.run(execute(coach, turn, AskUser(action="ask_user", kind=kind)))

    assert turn.response["mode"] == "clarify" and turn.response["text"] == expected
    assert turn.response["diagnostics"]["intent"] == "ambiguous"
    assert turn.response["diagnostics"].get("stop_reason") == stop_reason


def test_coach_passes_the_action_as_the_interpretation(app_client, monkeypatch):
    coach, turn = selected_turn(app_client)
    seen = {}

    async def respond(text, session, response, message_id, problem_step=None, interpretation=None, budget=None,
                      unavailable=None, confirmation=None):
        seen.update(text=text, problem_step=problem_step, interpretation=interpretation)

    monkeypatch.setattr(coach.dialogue, "respond", respond)
    turn.text = "3단계에서 설탕이 안 녹아"
    action = CoachTurn(action="coach", transition="new_issue", transition_quote="설탕이 안 녹아",
                       observation_updates=[{"quote": "설탕이 안 녹아"}], step=3)

    asyncio.run(execute(coach, turn, action))

    interpretation = seen["interpretation"]
    assert isinstance(interpretation, TurnInterpretation)
    assert interpretation.route == "dialogue" and interpretation.transition == "new_issue"
    assert interpretation.transition_quote == "설탕이 안 녹아"
    assert [u.quote for u in interpretation.observation_updates] == ["설탕이 안 녹아"]
    assert seen["problem_step"] == 3 and seen["text"] == "3단계에서 설탕이 안 녹아"


def test_every_action_has_an_executor():
    assert set(ACTION_NAMES) == set(EXECUTORS)


def test_executors_do_not_judge_the_raw_text():
    source = Path(action_executors.__file__).read_text(encoding="utf-8")
    assert "import re" not in source
    allowed = re.compile(r"(answer|rank_evidence|respond)\(")
    assert all(allowed.search(line) for line in source.splitlines() if "turn.text" in line)
