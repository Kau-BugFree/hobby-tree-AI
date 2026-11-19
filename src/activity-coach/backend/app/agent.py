"""에이전트 경로: 문맥을 만들어 choose_action을 한 번 부르고, 판단을 검증해 실행할 행동으로 돌려줍니다.

None을 돌려주면 그 턴은 규칙 경로로 갑니다.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from . import confirmation
from .action_executors import Turn
from .actions import ACTION_NAMES, Action, CoachTurn, Explain, Recommend, parse_decision
from .dialogue import transition_question
from .dialogue_state import CONTEXT_LIMIT, build_issue_context, normalize_dialogue
from .slots import MAX_MINUTES
from .vocabulary import vocabulary_for

if TYPE_CHECKING:
    from .coach import Coach


# 행동 이름 -> diagnostics.intent_category(설계 4.8절). ask_user의 two_requests는 overlap이다(category_for).
CATEGORY: dict[str, str] = {
    "select_candidate": "state_change", "start_activity": "state_change", "decline_start": "state_change",
    "complete_activity": "state_change", "answer_confirmation": "state_change",
    "show_materials": "lookup", "show_duration": "lookup", "show_steps": "lookup", "show_records": "lookup",
    "recommend": "recommend", "explain": "question", "coach": "question", "notice": "notice", "ask_user": "unknown",
}
# Claude가 답하지 못한 상태(설계 4.7절). 그 턴은 Claude를 다시 부르지 않고 키가 없을 때와 같은 경로를 탄다.
UNANSWERED = frozenset({"timeout", "network_error", "rate_limited", "authentication_error", "provider_error",
                        "blocked"})
# recommend·explain의 topic 상한. 주제는 검색어이고 '왜 추천했어?'에 그대로 보이므로 짧은 한 줄만 받는다(설계 4.3절).
MAX_TOPIC_CHARS = 60


def category_for(action: Action) -> str:
    """에이전트가 정한 턴의 intent_category입니다."""
    if action.action == "ask_user" and action.kind == "two_requests":
        return "overlap"
    return CATEGORY[action.action]


def available(coach: Coach) -> bool:
    """설정이 켜져 있고, 키가 있고, 모델에 choose_action이 있으면 True입니다."""
    return coach.agent_enabled and getattr(coach.llm, "configured", True) and hasattr(coach.llm, "choose_action")


async def _execution_status(coach: Coach, session: dict) -> str:
    if not session["execution_id"]:
        return "none"
    execution = await asyncio.to_thread(coach.store.get_execution, session["execution_id"])
    return execution["status"]


def _candidates_just_shown(session: dict) -> bool:
    """바로 앞 코치 답이 새 후보를 보여 준 추천이고 아직 고르지 않았으면 True입니다.

    세션의 candidate_selection_pending은 추천 뒤 고를 때까지 참으로 남으므로(규칙 경로가 쓰는 값) 여기서만 좁힌다.
    """
    recommendation, messages = session.get("last_recommendation"), session.get("messages")
    return bool(session.get("candidate_selection_pending") and recommendation and messages
                and recommendation.get("message_id") == messages[-1].get("message_id"))


async def build_context(coach: Coach, turn: Turn) -> dict:
    """에이전트가 받는 문맥(설계 4.2절). 대화 상태·조건·현재 단계·최근 대화에 선택 활동과 후보를 더합니다."""
    session, selected = turn.session, turn.selected
    context = build_issue_context(normalize_dialogue(session["dialogue"], session), session)
    context["selected"] = {
        "activity_id": selected["activity_id"], "title": selected["title"], "step_count": len(selected["steps"]),
        "current_step": session["current_step"], "execution_status": await _execution_status(coach, session),
    } if selected else None
    # 카탈로그에서 빠진 후보는 건너뛴다. 번호는 세션의 후보 순서 그대로다.
    items = [(number, coach.retrieval.get(aid)) for number, aid in enumerate(session["candidate_ids"], 1)]
    context["candidates"] = [{"number": number, "activity_id": item["activity_id"], "title": item["title"]}
                             for number, item in items if item]
    context["candidates_just_shown"] = _candidates_just_shown(session)
    # 대기 확인의 활동은 선택한 활동과 같다(confirmation.pending이 다르면 지운다).
    waiting = confirmation.pending(session)
    context["pending_confirmation"] = {"kind": waiting["kind"], "title": selected["title"]} if waiting else None
    return context


def _trimmed(turn: Turn, action: CoachTurn) -> CoachTurn:
    """발화의 일부가 아닌 인용과 선택한 활동의 단계 밖 step을 버립니다. 판단 전체를 거부하지 않습니다(설계 4.3절)."""
    text, selected = turn.text, turn.selected
    step = action.step if selected and action.step is not None and 1 <= action.step <= len(selected["steps"]) else None
    return action.model_copy(update={
        "observation_updates": [u for u in action.observation_updates if u.quote in text],
        "requirement_proposals": [p for p in action.requirement_proposals if p.source_quote in text],
        "transition_quote": action.transition_quote if action.transition_quote in text else "", "step": step})


def validate(action: Action, turn: Turn) -> str | None:
    """실행 전에 인자를 검증해 쓸 수 없는 사유를 돌려줍니다. 쓸 수 있으면 None입니다."""
    if action.action == "answer_confirmation":
        waiting = confirmation.pending(turn.session) or transition_question(turn.session["dialogue"])
        return None if waiting else "no_pending_confirmation"
    if isinstance(action, (Recommend, Explain)) and (
            len(action.topic) > MAX_TOPIC_CHARS or "\n" in action.topic or "\r" in action.topic):
        return "topic_too_long"
    if isinstance(action, Recommend):
        if action.max_minutes is not None and not 0 < action.max_minutes <= MAX_MINUTES:
            return "max_minutes_out_of_range"
        if not set(action.excluded_tools) <= set(vocabulary_for(turn.session["hobby_key"]).tools):
            return "unknown_tool"
    return None


def _rejected(raw: object) -> str:
    """parse_decision이 받지 않은 판단의 사유."""
    return "invalid_arguments" if isinstance(raw, dict) and raw.get("action") in ACTION_NAMES else "unknown_action"


async def _choose(coach: Coach, turn: Turn, context: dict) -> dict:
    """choose_action을 시간 제한 안에서 부릅니다. 넘기면 timeout 상태입니다(턴 예산 소진이 아님)."""
    try:
        async with asyncio.timeout(coach.agent_timeout_seconds):
            return await turn.budget.model_call("agent", coach.llm.choose_action, turn.text, context)
    except TimeoutError:
        return {"decision": None, "status": "timeout"}


def _fall_back(turn: Turn, reason: str) -> None:
    """규칙 경로로 내려간 사유를 적습니다. 답하지 못한 턴은 그 턴의 Claude 호출을 막습니다."""
    diag = turn.response["diagnostics"]
    diag["agent_reason"] = reason
    if reason in UNANSWERED:
        diag["llm_status"] = turn.provider_failure = reason
    elif reason != "context_too_long":
        diag["llm_status"] = "invalid_decision"


async def decide(coach: Coach, turn: Turn) -> Action | None:
    """choose_action을 한 번 불러 실행할 행동을 돌려줍니다. 쓸 수 없거나 답하지 못했으면 사유를 적고 None입니다."""
    try:
        context = await build_context(coach, turn)
    except ValueError as error:
        if str(error) != "context_limit":
            raise
        context = None
    if context is None or len(json.dumps(context, ensure_ascii=False)) > CONTEXT_LIMIT:
        _fall_back(turn, "context_too_long")  # 문맥이 한도를 넘은 턴은 부르지 않는다(설계 4.2절)
        return None
    result = await _choose(coach, turn, context)
    raw = result["decision"]
    action = parse_decision(raw) if raw is not None else None
    reason = ((result.get("status") or "invalid_decision") if raw is None
              else _rejected(raw) if action is None else validate(action, turn))
    if reason:
        _fall_back(turn, reason)
        return None
    return _trimmed(turn, action) if action.action == "coach" else action
