"""규칙 경로의 변환: classify의 결과(Intent)와 원문에서 실행 함수에 넘길 행동과 인자를 만듭니다.

원문에서 값을 읽는 코드는 실행 함수가 아니라 여기에 둡니다.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .action_executors import RuleExtras, Turn
from .actions import (Action, AskUser, CoachTurn, CompleteActivity, DeclineStart, Explain, Notice, Recommend,
                      SelectCandidate, ShowDuration, ShowMaterials, ShowRecords, ShowSteps, StartActivity)
from .answer_policy import original_requested
from .intent_rules import Intent, asks_for_dishes, scope_patterns, wanted_step
from .retrieval import outside_cooking
from .slots import (NAMES_CANDIDATE, ORDINAL, candidate_number, names_topic, no_tool_pattern, other_dishes_pattern,
                    spoken_constraints, step_move)
from .vocabulary import vocabulary_for

Converter = Callable[[Intent, Turn], tuple[Action, RuleExtras]]

# 주제가 맞는 활동이 없어도 입문 목록을 대신 보여 줄 만큼 추천을 바라는 말.
WANTS_SUGGESTION = re.compile(r"추천|뭐부터|뭐\s*하지|뭘\s*할|할\s*만한|해볼\s*만한")
# 선택 활동을 가리키는 말. 없으면 질문이 다른 활동을 말하는지 검색으로 확인한다.
CURRENT_CONTEXT = re.compile(r"(이거|이것|그거|지금|현재|단계|여기|방금|이\s*(?:활동|요리|부분))")
# 말한 단계 번호를 문제 단계로 넘기던 사슬 분기의 규칙: 확인 질문의 답, 문제 보고, 코칭 요청.
STEP_RULES = frozenset({"pending_reply", "trouble_report", "trouble_question"})


def recommend_arguments(turn: Turn, handed_off: bool = False) -> tuple[Recommend, RuleExtras]:
    """추천 원문에서 조건·주제·범위와 앞 주제 이어 쓰기, 입문 목록 대체 여부를 정합니다.

    handed_off는 대화 처리기가 코칭이 아니라 추천이라고 넘긴 말이다. 그때는 주제가 맞는 활동이 없어도 입문 목록을 보여 준다.
    """
    text, session, selected = turn.text, turn.session, turn.selected
    vocab = vocabulary_for(session["hobby_key"])
    asks_dishes = asks_for_dishes(text)
    # 빈 조건과 합쳐 이번에 말한 도구만 얻는다. 세션의 제외 도구와 합치는 일은 실행 함수가 한다.
    spoken = spoken_constraints(text, {}, not selected or asks_dishes, hobby=session["hobby_key"])
    # 선택 활동이 있으면 다른 요리를 찾거나 조건을 직접 밝힌 경우에만 반영한다. '10분 끓여도 가능해'는 그 활동에 대한 물음이다.
    applies = bool(spoken) and bool(not selected or asks_dishes or scope_patterns(vocab).stated_limit.search(text))
    if not applies:
        spoken = {}
    changed = turn.constraints_changed or applies
    # '칼이 없어', '다른 거'는 조건과 요청이지 검색 주제가 아니다. 남는 주제가 없으면 앞의 주제를 이어 쓴다.
    other_dishes = other_dishes_pattern(vocab)
    other = bool(other_dishes.search(text))
    topic = other_dishes.sub(" ", no_tool_pattern(vocab).sub(" ", text))
    if other and not names_topic(topic):
        reuse_topic = "shown"
    elif changed and not re.search(r"(추천|레시피|요리)", text):
        reuse_topic = "unmet"
    else:
        reuse_topic = None
    # 후속 시간·도구 조건에서도 직전의 검색 범위를 유지한다. 레시피 또는 요리 활동을 명시할 때만 범위를 바꾼다.
    if "레시피" in text:
        scope = "recipes"
    elif re.search(r"(요리\s*활동|활동\s*추천)", text):
        scope = "activities"
    else:
        scope = None
    action = Recommend(action="recommend", topic=topic, max_minutes=spoken.get("max_minutes"),
                       clear_time_limit="max_minutes" in spoken and spoken["max_minutes"] is None,
                       excluded_tools=spoken.get("excluded_tools", []), scope=scope, other=other)
    may_fall_back = (bool(WANTS_SUGGESTION.search(text)) or handed_off) and not outside_cooking(text)
    return action, RuleExtras(reuse_topic=reuse_topic, may_fall_back=may_fall_back)


def _notice(kind: str) -> Converter:
    return lambda intent, turn: (Notice(action="notice", kind=kind), RuleExtras())


def _out_of_scope(intent: Intent, turn: Turn) -> tuple[Action, RuleExtras]:
    return Notice(action="notice", kind="out_of_scope"), RuleExtras(allergy=intent.rule == "allergy")


def _complete(intent: Intent, turn: Turn) -> tuple[Action, RuleExtras]:
    # 분류는 선택한 활동이 있을 때만 partial을 참으로 한다. 선택이 없으면 완료가 먼저 활동을 고르게 한다.
    if intent.slots["partial"]:
        return Notice(action="notice", kind="partial_completion"), RuleExtras()
    return CompleteActivity(action="complete_activity"), RuleExtras()


def _explain_target(turn: Turn) -> tuple[str, list[int]]:
    """비교는 앞의 후보들, 범위 안의 후보 번호는 그 후보, 그 밖은 선택 활동(없으면 검색)입니다."""
    text, selected, candidates = turn.text, turn.selected, turn.session["candidate_ids"]
    if "비교" in text or "차이" in text:
        return "candidates", []
    number = candidate_number(text)
    # 선택 활동이 있으면 후보를 가리킨 말일 때만 그 후보로 설명한다.
    if number and 1 <= number <= len(candidates) and (
            not selected or NAMES_CANDIDATE.match(text, ORDINAL.search(text).end())):
        return "candidates", [number]
    return ("selected" if selected else "search"), []


def explain_arguments(intent: Intent, turn: Turn) -> tuple[Explain, RuleExtras]:
    """설명의 대상, 이름 확인 검색어, 단계, 원문 요청과 대체 검색에서 반영할 조건을 원문에서 정합니다."""
    text, session, selected = turn.text, turn.session, turn.selected
    target, numbers = _explain_target(turn)
    # 선택 활동을 가리키는 말이 없으면 다른 활동 이름을 물었는지 원문으로 검색해 확인한다.
    named_query = text if target == "selected" and not CURRENT_CONTEXT.search(text) else None
    step = (wanted_step(text) or session["current_step"]) if selected and "단계" in text else None
    spoken = spoken_constraints(text, session["constraints"], not selected or asks_for_dishes(text),
                                hobby=session["hobby_key"])
    action = Explain(action="explain", target=target, candidate_numbers=numbers, topic=text, step=step,
                     original=original_requested(text))
    return action, RuleExtras(named_query=named_query, spoken_constraints=spoken)


def _coach(intent: Intent, turn: Turn) -> tuple[CoachTurn, RuleExtras]:
    """문제 보고·물음·문제 관리·확인 질문의 답(problem, ask, issue_control, reply). 해석은 TurnRouter가 합니다."""
    rule = intent.rule
    action = CoachTurn(action="coach", route="reflection" if rule == "self_assessment" else "dialogue",
                       step=wanted_step(turn.text) if rule in STEP_RULES else None)
    return action, RuleExtras(router_interprets=True)


CONVERTERS: dict[str, Converter] = {
    "greeting": _notice("greeting"),
    "capabilities": _notice("capabilities"),
    "recommend_reason": _notice("recommend_reason"),
    "certainty": _notice("certainty"),
    "out_of_scope": _out_of_scope,
    "materials": lambda intent, turn: (ShowMaterials(action="show_materials", move=step_move(turn.text)),
                                       RuleExtras()),
    "duration": lambda intent, turn: (ShowDuration(action="show_duration", move=step_move(turn.text)), RuleExtras()),
    "steps": lambda intent, turn: (ShowSteps(action="show_steps", step=intent.slots["step"],
                                             move=intent.slots["move"], whole="순서" in turn.text), RuleExtras()),
    "records": lambda intent, turn: (ShowRecords(action="show_records"), RuleExtras()),
    "select": lambda intent, turn: (SelectCandidate(action="select_candidate", number=intent.slots["number"]),
                                    RuleExtras()),
    "complete": _complete,
    "start": lambda intent, turn: (StartActivity(action="start_activity", restart="다시" in turn.text), RuleExtras()),
    "decline": lambda intent, turn: (DeclineStart(action="decline_start"), RuleExtras()),
    "recommend": lambda intent, turn: recommend_arguments(turn),
    "explain": explain_arguments,
    "problem": _coach,
    "ask": _coach,
    "issue_control": _coach,
    "reply": _coach,
    # 처리 분류 둘 이상에 걸린 말. 모델·검색을 부르지 않고 세션도 바꾸지 않고 되묻는다.
    "overlap": lambda intent, turn: (AskUser(action="ask_user", kind="two_requests"), RuleExtras()),
}


def to_action(intent: Intent, turn: Turn) -> tuple[Action, RuleExtras]:
    """분류 결과를 행동과 규칙 경로 인자로 바꿉니다."""
    return CONVERTERS[intent.kind](intent, turn)
