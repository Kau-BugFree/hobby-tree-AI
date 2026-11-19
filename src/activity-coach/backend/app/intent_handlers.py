"""규칙 경로의 진입: classify가 정한 종류(Intent.kind)를 행동으로 바꿔 실행하고, 정하지 못한 말을 처리합니다."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .action_executors import (OVERLAP_TEXT, UNCLEAR_DEICTIC, UNCLEAR_SELECTED, UNCLEAR_UNSELECTED, RuleExtras, Turn,
                               execute)
from . import confirmation
from .actions import AnswerConfirmation, AskUser, CoachTurn, Notice
from .dialogue import closing_proposal
from .evidence_lookup import lookup_evidence
from .intent_rules import TurnContext, classify, deictic_without_activity, wanted_step
from .rule_arguments import recommend_arguments, to_action
from .service_level import claude_down
from .vocabulary import vocabulary_for

if TYPE_CHECKING:
    from .coach import Coach

__all__ = ["CAPABILITIES_TEXT", "GREETING_TEXT", "OUT_OF_SCOPE_TEXT", "OVERLAP_TEXT", "UNCLEAR_DEICTIC",
           "UNCLEAR_SELECTED", "UNCLEAR_UNSELECTED", "Turn", "context_for", "handle_unknown", "run_rules",
           "take_handoff"]

# 요리의 안내 문구. 실행 함수는 세션의 취미로 고른다. 예전 가져오기 경로를 유지한다.
CAPABILITIES_TEXT = vocabulary_for("cooking").capabilities_notice
OUT_OF_SCOPE_TEXT = vocabulary_for("cooking").scope_notice
GREETING_TEXT = vocabulary_for("cooking").greeting_notice
# 해결·중단 제안의 답(설계 4.4절 9번). 지금의 새 문제 확인과 같은 낱말이다.
TRANSITION_YES = re.compile(r"예|네|응|맞아|맞아요|1번?")
TRANSITION_NO = re.compile(r"아니|아니요|2번?")


def context_for(session: dict, selected: dict | None, constraints_changed: bool,
                interprets_turns: bool) -> TurnContext:
    """세션에서 분류에 쓸 맥락을 만듭니다."""
    dialogue = session["dialogue"]
    return TurnContext(
        selected=selected,
        candidate_count=len(session["candidate_ids"]),
        candidate_selection_pending=bool(session.get("candidate_selection_pending")),
        pending_question=dialogue["pending_question"],
        issue_open=bool(dialogue.get("issue_id")),
        issue_phase=dialogue.get("phase"),
        constraints_changed=constraints_changed,
        hobby_key=session["hobby_key"],
        interprets_turns=interprets_turns,
    )


async def _ask_unclear(coach: Coach, turn: Turn) -> None:
    """뜻을 정할 수 없을 때는 추천이나 문제 기록으로 추측하지 않고 되묻습니다."""
    await execute(coach, turn, AskUser(action="ask_user", kind="unclear"))


async def take_handoff(coach: Coach, turn: Turn) -> None:
    """대화 처리기가 문제 코칭이 아니라고 판단한 말(response['handoff'])을 여기서 이어받습니다."""
    response = turn.response
    handoff = response.pop("handoff", None)
    if handoff:
        response.pop("metacognition", None)
    if handoff == "out_of_scope":
        await execute(coach, turn, Notice(action="notice", kind="out_of_scope"))
    elif handoff == "meta":
        await execute(coach, turn, Notice(action="notice", kind="capabilities"))
    elif handoff == "recommend":
        await execute(coach, turn, *recommend_arguments(turn, handed_off=True))
    elif handoff:
        # 모델이 판단한 '모름'은 되묻는다. 공급자 실패로 규칙만 남은 턴은 자료부터 찾아 본다(설계 4.5절).
        if not (claude_down(coach, turn) and await lookup_evidence(coach, turn)):
            await _ask_unclear(coach, turn)


async def handle_unknown(coach: Coach, turn: Turn) -> None:
    """어느 규칙으로도 정하지 못한 말. 모델이 있으면 모델 해석에 맡기고, 없으면 자료를 찾아 보고 없으면 되묻습니다."""
    text = turn.text
    if deictic_without_activity(text, turn.context):
        await execute(coach, turn, AskUser(action="ask_user", kind="which_activity"))
        return
    # 에이전트의 판단 호출이 답하지 못한 턴은 키가 없을 때처럼 해석을 부르지 않는다(설계 4.7절).
    if coach.dialogue.interprets_turns() and not turn.provider_failure:
        await execute(coach, turn, CoachTurn(action="coach", step=wanted_step(text)),
                      RuleExtras(router_interprets=True))
        await take_handoff(coach, turn)
        return
    if not await lookup_evidence(coach, turn):
        await _ask_unclear(coach, turn)


def _spoken_transition_answer(text: str) -> bool | None:
    """해결·중단 제안에 대한 규칙 경로의 답: 긍정이면 True, 부정이면 False, 그 밖은 None입니다."""
    word = text.strip()
    return True if TRANSITION_YES.fullmatch(word) else False if TRANSITION_NO.fullmatch(word) else None


async def _answered_pending(coach: Coach, turn: Turn) -> bool:
    """대기 확인에 답한 말이면 처리하고 True. 답이 아니면 대기 확인을 버리고 False입니다."""
    session = turn.session
    if confirmation.pending(session):
        accept = confirmation.spoken_answer(turn.text)
        if accept is not None:
            await execute(coach, turn, AnswerConfirmation(action="answer_confirmation", accept=accept))
            return True
        confirmation.clear(session)
    if not closing_proposal(session["dialogue"]):
        return False
    # 건너뛰면 '네'가 TurnRouter에서 새 문제 동의로 읽힌다.
    accept = _spoken_transition_answer(turn.text)
    if accept is not None:
        await execute(coach, turn, AnswerConfirmation(action="answer_confirmation", accept=accept))
        return True
    coach.dialogue.drop_proposal(session)
    # 분류는 제안이 대기 중인 맥락으로 했다. 묻기 전 상태로 되돌린 맥락으로 다시 분류한다.
    turn.context = context_for(session, turn.selected, turn.constraints_changed, turn.context.interprets_turns)
    turn.intent = classify(turn.text, turn.context)
    turn.response["diagnostics"].update(intent_category=turn.intent.category if turn.intent else "unknown",
                                        intent_rule=turn.intent.rule if turn.intent else None)
    return False


async def run_rules(coach: Coach, turn: Turn) -> None:
    """규칙 경로: 분류 → 변환 → 실행. 코칭이 넘긴 말은 이어받고, 정하지 못한 말은 모름 처리입니다.

    에이전트 설정이 켜져 있으면 대기 중인 완료 확인, 그다음 해결·중단 제안을 분류보다 먼저 봅니다(설계 4.4절).
    긍정·부정 낱말이면 그 답으로 처리하고, 그 밖의 말이면 지우고 평소처럼 처리합니다.
    """
    if coach.agent_enabled and await _answered_pending(coach, turn):
        return
    if not turn.intent:
        await handle_unknown(coach, turn)
        return
    action, extras = to_action(turn.intent, turn)
    await execute(coach, turn, action, extras)
    if action.action == "coach":
        await take_handoff(coach, turn)
