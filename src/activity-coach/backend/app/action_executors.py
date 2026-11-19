"""실행 함수: 행동과 인자를 받아 응답과 세션을 채웁니다. 원문에서 뜻을 판정하지 않습니다.

원문은 질문으로만 넘깁니다(답변 생성, 재순위, 대화 처리기). 규칙 경로(rule_arguments.to_action)와
에이전트 경로가 같은 실행 함수를 씁니다.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .actions import (Action, AnswerConfirmation, AskUser, CoachTurn, CompleteActivity, DeclineStart, Explain, Notice,
                      Recommend, SelectCandidate, ShowDuration, ShowMaterials, ShowRecords, ShowSteps, StartActivity)
from . import confirmation
from .answer_policy import AnswerPolicy, evidence_for, mark_unavailable
from .dialogue import transition_question
from .dialogue_models import TurnInterpretation
from .dialogue_state import build_issue_context, normalize_dialogue, serialize_dialogue
from .intent_rules import Intent, TurnContext
from .metacognition import build_metacognition, certainty_text, recommendation_reason
from .service_level import claude_down, rank_refused, search_refused
from .slots import apply_constraints, has_conditions, names_topic
from .turn_budget import TurnBudget
from .vocabulary import Vocabulary, vocabulary_for

if TYPE_CHECKING:
    from .coach import Coach


ALLERGY_TEXT = "이 활동코치는 알레르기 조건 판정을 지원하지 않습니다. 활동 추천이나 진행 방법을 질문해 주세요."
PARTIAL_COMPLETION_TEXT = ("단계나 일부 작업을 마친 것만으로 활동 전체를 완료 처리하지 않아요. "
                           "모든 작업을 마쳤다면 '활동 완료했어'라고 알려주세요.")
NO_CANDIDATE_TEXT = "해당 순서의 추천이 없습니다. 먼저 활동을 추천받아 주세요."
# 뜻을 정할 수 없을 때는 추천이나 문제 기록으로 추측하지 않고 되묻는다.
UNCLEAR_SELECTED = (
    "어떤 도움이 필요한지 조금 더 알려주세요. 지금 활동에서 막힌 부분이라면 어느 단계에서 어떤 상태인지, "
    "다른 요리를 찾는다면 원하는 요리나 조건을 적어 주세요."
)
UNCLEAR_UNSELECTED = (
    "어떤 도움이 필요한지 조금 더 알려주세요. 요리를 추천받으려면 원하는 요리나 가능한 시간을, "
    "진행 중인 요리의 문제라면 어떤 활동인지 먼저 알려주세요."
)
UNCLEAR_DEICTIC = "어느 활동을 말하는지 알 수 없어요. 추천 목록의 번호나 활동 이름으로 먼저 선택해 주세요."
OVERLAP_TEXT = "요청이 두 가지로 읽혀요. 한 번에 한 가지씩 말씀해 주세요."
# 대화 처리기(dialogue.py)가 활동 없이 문제를 말할 때 묻는 문구와 같다.
NO_ACTIVITY_COACH_TEXT = "어떤 요리 활동의 어느 부분에서 막혔나요? 활동 이름이나 번호로 먼저 선택해 주세요."


@dataclass
class Turn:
    text: str
    session: dict
    response: dict          # response["diagnostics"]가 진단 기록
    selected: dict | None
    budget: TurnBudget
    request_id: str         # 지금의 rid
    user_message_id: str
    intent: Intent | None
    context: TurnContext    # 분류에 쓴 맥락(_respond_locked에서 한 번 만든다)
    constraints_changed: bool = False   # 화면에서 조건을 바꿔 보냄(request.constraints)
    # 에이전트의 판단 호출이 공급자 실패로 끝난 상태(agent.UNANSWERED). 있으면 이 턴은 Claude를 다시 부르지 않는다.
    provider_failure: str | None = None


@dataclass(frozen=True)
class RuleExtras:
    """규칙 경로만 채우는 인자. 기본값이 에이전트 경로의 동작이다."""
    allergy: bool = False            # 범위 밖 안내를 알레르기 문구로 낸다
    reuse_topic: str | None = "auto"
    may_fall_back: bool = True
    named_query: str | None = None
    spoken_constraints: dict | None = None
    router_interprets: bool = False


Executor = Callable[["Coach", Turn, Action, RuleExtras], Awaitable[None]]


def _diag(turn: Turn, intent: str) -> None:
    """diagnostics.intent에 지금 값(클라이언트와 기준선이 보는 값)을 적습니다."""
    turn.response["diagnostics"]["intent"] = intent


def _vocab(turn: Turn) -> Vocabulary:
    """세션 취미의 낱말 묶음. 없는 취미는 KeyError입니다."""
    return vocabulary_for(turn.session["hobby_key"])


def _needs_selection(turn: Turn) -> bool:
    """선택 활동이 없으면 어느 활동인지 되묻고 True를 반환합니다."""
    if turn.selected:
        return False
    turn.response.update(mode="clarify", text="어느 활동인지 먼저 선택해 주세요. 예: 두 번째 걸로 할게")
    return True


def _move_step(turn: Turn, move: str | None) -> None:
    """'다음 단계', '이전 단계'라고 말했을 때만 현재 단계를 한 칸 옮깁니다(설계 6절 2번)."""
    session, total = turn.session, len(turn.selected["steps"])
    if move == "next":
        session["current_step"] = min(total, session["current_step"] + 1)
    elif move == "prev":
        session["current_step"] = max(1, session["current_step"] - 1)


async def show_materials(coach: Coach, turn: Turn, action: ShowMaterials, extras: RuleExtras) -> None:
    _diag(turn, "activity_lookup")
    if _needs_selection(turn):
        return
    selected, response = turn.selected, turn.response
    _move_step(turn, action.move)
    materials = selected.get("materials", [])
    tools = selected.get("tools", [])
    response["text"] = (
        "준비물: " + (" · ".join(materials) if materials else "별도 재료가 기재되어 있지 않아요.")
        + "\n도구: " + (" · ".join(tools) if tools else "도구 정보가 기재되어 있지 않아요.")
    )
    if "quantity_units_unverified" in selected.get("quality", {}).get("flags", []):
        response["text"] += "\n계량 단위가 검증되지 않은 참고 자료이므로 수량을 확정해 안내할 수 없어요."
    response["sources"] = [evidence_for(selected)]


async def show_duration(coach: Coach, turn: Turn, action: ShowDuration, extras: RuleExtras) -> None:
    _diag(turn, "activity_lookup")
    if _needs_selection(turn):
        return
    selected, response = turn.selected, turn.response
    _move_step(turn, action.move)
    total = selected["duration"]["total_minutes"]
    response["text"] = (
        f"기재된 전체 예상 시간은 {total:g}분입니다."
        if total is not None
        else "전체 소요 시간이 확인되지 않습니다. 직접 작업 시간과 대기 시간을 합쳐 추측하지 않을게요."
    )
    response["sources"] = [{
        "activity_id": selected["activity_id"],
        "title": selected["title"],
        "kind": selected["kind"],
        "excerpt": json.dumps(selected["duration"], ensure_ascii=False),
    }]


def _step_out_of_range(turn: Turn, step: int | None) -> bool:
    """step이 선택한 활동의 1~단계 수 밖이면(활동이 없어도) 단계 범위를 되묻고 True를 반환합니다."""
    selected = turn.selected
    if step is None or (selected and 1 <= step <= len(selected["steps"])):
        return False
    turn.response.update(
        mode="clarify",
        text=(
            f"단계 범위를 확인해 주세요. 선택한 활동은 1~{len(selected['steps'])}단계입니다."
            if selected else "단계를 확인할 활동을 먼저 선택해 주세요."
        ),
    )
    return True


async def show_steps(coach: Coach, turn: Turn, action: ShowSteps, extras: RuleExtras) -> None:
    _diag(turn, "activity_lookup")
    selected, response, session = turn.selected, turn.response, turn.session
    if _step_out_of_range(turn, action.step):
        return
    if _needs_selection(turn):
        return
    _move_step(turn, action.move)
    if action.whole:
        response["text"] = "\n".join(f"{i+1}. {x}" for i, x in enumerate(selected["steps"]))
        response["sources"] = [evidence_for(selected)]
        return
    if action.step is not None:
        session["current_step"] = action.step
    step = session["current_step"]
    response["text"] = f'{step}/{len(selected["steps"])}단계\n{selected["steps"][step-1]}'
    response["sources"] = [evidence_for(selected, step)]


async def show_records(coach: Coach, turn: Turn, action: ShowRecords, extras: RuleExtras) -> None:
    _diag(turn, "records")
    records = await asyncio.to_thread(coach.store.records, turn.session["session_id"])
    turn.response.update(
        text=f"이 대화에서 완료한 활동은 {len(records)}개입니다."
        + ("\n" + "\n".join(x["activity_title"] for x in records[:10]) if records else ""),
        records=records,
        actions=[{"type": "view_records", "label": "완료 기록 보기"}],
    )


async def select_candidate(coach: Coach, turn: Turn, action: SelectCandidate, extras: RuleExtras) -> None:
    # 순서 선택은 새 검색을 하지 않는다.
    session, response, number = turn.session, turn.response, action.number
    if number < 1 or number > len(session["candidate_ids"]):
        response.update(mode="clarify", text=NO_CANDIDATE_TEXT)
    else:
        turn.selected = coach.select(session, session["candidate_ids"][number - 1])
        response.update(
            text=f"'{turn.selected['title']}' 활동을 선택했어요. 준비물이나 현재 단계를 물어보세요.",
            activities=[turn.selected],
        )
    _diag(turn, "select")


def _record_completion(coach: Coach, turn: Turn) -> None:
    """실행 확보 → 모든 단계 완료 표시 → 기록 → 응답."""
    selected, response, session, rid = turn.selected, turn.response, turn.session, turn.request_id
    execution = coach.ensure_execution(session, selected, "chat-start-" + rid)
    coach.store.update_progress(execution["execution_id"], [True] * len(selected["steps"]))
    record = coach.store.complete(selected["activity_id"], execution["execution_id"], rid)
    response.update(
        mode="action",
        text="활동 완료를 기록했어요." if record["created"] else "이미 완료 기록이 저장되어 있어요.",
        record=record,
        actions=[{"type": "view_records", "label": "완료 기록 보기"}],
    )


async def complete_activity(coach: Coach, turn: Turn, action: CompleteActivity, extras: RuleExtras) -> None:
    """에이전트 설정이 켜져 있으면 묻고 다음 턴에 기록합니다(설계 4.4절). 꺼져 있으면 바로 기록합니다."""
    _diag(turn, "complete")
    if not turn.selected:
        turn.response.update(mode="clarify", text="완료한 활동을 먼저 선택해 주세요.")
    elif coach.agent_enabled:
        confirmation.ask(turn)
    else:
        _record_completion(coach, turn)


async def decline_start(coach: Coach, turn: Turn, action: DeclineStart, extras: RuleExtras) -> None:
    # '활동 시작하지 마', '활동 시작 취소'처럼 전환을 부정하면 진행 중인 확인 질문·관찰을 지우지 않는다.
    _diag(turn, "start")
    turn.response.update(mode="clarify", text="활동을 시작하거나 다시 시작하지 않았어요. 현재 진행 상태를 유지할게요.")


async def start_activity(coach: Coach, turn: Turn, action: StartActivity, extras: RuleExtras) -> None:
    _diag(turn, "start")
    selected, response, session = turn.selected, turn.response, turn.session
    if not selected:
        response.update(mode="clarify", text="시작할 활동을 먼저 선택해 주세요.")
        return
    previous = session["execution_id"]
    # 완료한 실행은 이어가지 않는다. 완료 선언의 중복 기록 방지는 ensure_execution 재사용에 맡긴다.
    finished = bool(previous) and coach.store.get_execution(previous)["status"] == "completed"
    execution = coach.ensure_execution(
        session, selected, "chat-start-" + turn.request_id, force_new=action.restart or finished)
    total = len(selected["steps"])
    # 진행 중인 실행을 이어갈 때는 현재 단계를 유지한다. 새 실행만 1단계부터다.
    if execution["execution_id"] != previous:
        coach.attach_execution(session, execution)
        reply = f"'{selected['title']}' 활동을 시작했어요. 총 {total}단계입니다."
    else:
        reply = f"'{selected['title']}' 활동을 이어서 진행할게요. 현재 {session['current_step']}/{total}단계입니다."
    response.update(mode="action", text=reply, activities=[selected])


async def _certainty(coach: Coach, turn: Turn) -> None:
    session, response, selected = turn.session, turn.response, turn.selected
    state = normalize_dialogue(session["dialogue"], session)
    evidence = await asyncio.to_thread(
        coach.retrieval.knowledge_for_activity, selected["activity_id"]) if selected else []
    state.coverage = AnswerPolicy.assess_coverage(state.requirements, state.coverage, evidence, state.observations)
    session["dialogue"] = serialize_dialogue(state)
    response["metacognition"] = build_metacognition(state, evidence, None)
    response["text"] = certainty_text(response["metacognition"], evidence)
    # 예전 handle_certainty처럼 처리를 마친 뒤에 적는다. 검색 중 예산이 끊기면 진단 값은 그대로다.
    _diag(turn, "metacognition")


async def notice(coach: Coach, turn: Turn, action: Notice, extras: RuleExtras) -> None:
    """고정 안내. 인사·감사·맞장구와 메타인지 질문은 검색하지 않는다(검색하면 직전 추천 목록이 바뀐다)."""
    kind, session, response = action.kind, turn.session, turn.response
    if kind != "certainty":
        _diag(turn, "complete" if kind == "partial_completion" else kind)
    if kind == "greeting":
        response["text"] = _vocab(turn).greeting_notice
    elif kind == "capabilities":
        response["text"] = _vocab(turn).capabilities_notice
    elif kind == "recommend_reason":
        response["text"] = recommendation_reason(
            session.get("last_recommendation"), session["constraints"],
            [coach.activity(x) for x in session["candidate_ids"]])
    elif kind == "certainty":
        await _certainty(coach, turn)
    elif kind == "out_of_scope":
        response.update(mode="clarify", text=ALLERGY_TEXT if extras.allergy else _vocab(turn).scope_notice)
    elif not turn.selected:
        # partial_completion: 선택한 활동이 없으면 완료처럼 먼저 활동을 고르게 한다.
        response.update(mode="clarify", text="완료한 활동을 먼저 선택해 주세요.")
    else:
        response.update(mode="clarify", text=PARTIAL_COMPLETION_TEXT)


def _constraint_updates(constraints: dict, action: Recommend) -> dict:
    """인자를 세션 조건에 덮어쓸 값으로 바꿉니다. 제외 도구는 세션의 목록 뒤에 이번 도구를 더합니다."""
    updates = {}
    if action.max_minutes is not None:
        updates["max_minutes"] = action.max_minutes
    elif action.clear_time_limit:
        updates["max_minutes"] = None
    if action.excluded_tools:
        updates["excluded_tools"] = list(dict.fromkeys([*(constraints.get("excluded_tools") or []),
                                                        *action.excluded_tools]))
    return updates


def _query(session: dict, action: Recommend, reuse_topic: str | None, changed: bool) -> str:
    """검색어. 앞 주제를 이어 쓰면 그 주제이고, 앞 주제가 없으면 topic입니다."""
    if reuse_topic == "auto":
        # 에이전트 경로: 주제가 비었을 때만 앞 주제를 이어 쓴다.
        reuse_topic = None if action.topic.strip() else "shown" if action.other else "unmet" if changed else None
    if reuse_topic == "shown":
        # '다른 거'는 방금 본 후보의 주제를 이어 간다. 못 찾은 주제에는 '다른 것'이 없다.
        previous = session.get("search_query", "")
    elif reuse_topic == "unmet":
        # 조건만 바꾼 말은 가장 최근 요청에 적용한다. 그 요청이 조건에 걸려 못 찾았으면 그 주제를 다시 찾는다.
        previous = session.get("unmet_query") or session.get("search_query", "")
    else:
        previous = ""
    return previous or action.topic


async def _scope(coach: Coach, session: dict, scope: str | None) -> str:
    """검색 범위를 정해 세션에 남깁니다. 범위를 말하지 않으면 직전 범위를 유지합니다."""
    scope = scope or session.get("search_scope", "activities")
    # 레시피는 scripts.import_recipes로 따로 적재한다. 색인되지 않은 범위에 고정되면 이후 추천이 모두 비므로 활동에서 찾는다.
    # 문서 수 집계는 레시피 54만 건을 훑으므로 이벤트 루프 밖에서 읽는다.
    if not (await asyncio.to_thread(coach.retrieval.status))["documents"].get(scope):
        scope = "activities"
    session["search_scope"] = scope
    return scope


def _remember_candidates(turn: Turn, query: str, scope: str, items: list, found: dict, starter: bool) -> None:
    """찾은 후보와 주제를 세션에 남깁니다."""
    session, response = turn.session, turn.response
    # 찾은 주제와 못 찾은 주제를 따로 기억한다. 못 찾은 주제가 찾은 주제를 덮어쓰면 '다른 거'가 그 주제를 이어 쓰고,
    # 못 찾은 주제를 버리면 조건을 푼 뒤에 다시 찾을 수 없다. 입문 목록으로 대신한 요청도 주제는 못 찾은 것이다.
    if items and not starter:
        session["search_query"] = query
        session.pop("unmet_query", None)
    elif names_topic(query):
        # 조건에 걸려 못 찾았을 수 있는 주제만 기억한다. 조건 없이도 못 찾은 주제는 조건을 바꿔도 못 찾는다.
        # 주제 없는 말('다른 거 추천해줘')은 앞서 못 찾은 주제를 바꾸지 않는다.
        if has_conditions(session["constraints"]):
            session["unmet_query"] = query
        else:
            session.pop("unmet_query", None)
    # 결과가 없으면 직전 추천 목록을 남겨 번호 선택을 이어갈 수 있게 한다.
    if items:
        session["candidate_ids"] = [x["activity_id"] for x in items]
        session["shown_ids"] = list(dict.fromkeys([*session.get("shown_ids", []), *session["candidate_ids"]]))
        # 새 후보를 받은 뒤의 '1번'은 이전 확인 질문의 답이 아니라 후보 선택이다.
        session["candidate_selection_pending"] = True
        session["last_recommendation"] = {
            "message_id": response["message_id"], "query": query, "scope": scope,
            "constraints": dict(session["constraints"]), "candidate_ids": list(session["candidate_ids"]),
            "ranking": found["diagnostics"].get("ranking", []), "search_mode": found["diagnostics"]["search_mode"]}


def _reply_candidates(turn: Turn, items: list, found: dict, starter: bool) -> None:
    """후보 목록이나 못 찾은 이유로 답합니다."""
    session, response = turn.session, turn.response
    if not items:
        if found["items"]:
            reason = "조건에 맞는 활동은 이미 모두 보여드렸거나 완료했어요."
        elif session["constraints"].get("max_difficulty") is not None:
            reason = "별도 난이도가 검증된 자료가 없어 해당 조건의 충족을 확인할 수 없어요."
        elif session["constraints"].get("budget_max") is not None:
            reason = "검증된 비용 정보가 없어 예산 조건의 충족을 확인할 수 없어요."
        else:
            reason = "현재 자료에서 질문과 조건에 맞는 활동을 찾지 못했어요."
        response.update(mode="clarify", text=reason + " 조건을 바꾸거나 다른 활동을 요청해 주세요.")
        return
    response.update(
        text=("요청과 직접 맞는 활동은 찾지 못했어요. 지금 조건에서 시간이 짧은 활동부터 보여드릴게요.\n"
              if starter else "")
        + "다음 활동을 찾았어요. 번호로 선택하면 준비물과 순서를 이어서 안내할게요.\n"
        + "\n".join(f'{i+1}. {x["title"]}' for i, x in enumerate(items)),
        activities=items,
        sources=[{"activity_id": x["activity_id"], "title": x["title"], "kind": x["kind"],
                  "excerpt": x.get("description") or x["title"]} for x in items],
    )


async def recommend(coach: Coach, turn: Turn, action: Recommend, extras: RuleExtras) -> None:
    """새 추천/조건 변경에서만 검색합니다. 조건 변경은 이번 세션에만 저장합니다."""
    session, diag = turn.session, turn.response["diagnostics"]
    updates = _constraint_updates(session["constraints"], action)
    changed = turn.constraints_changed or bool(updates)
    _diag(turn, "update_constraints" if changed else "recommend")
    # 모델을 불러오지 못한 것을 이미 알면 검색하지 않고 거절한다(설계 4.6절). 거절한 턴은 조건을 반영하지 않았다.
    if search_refused(coach, turn):
        _diag(turn, "recommend")
        return
    # 이번 검색에서 추론이 실패해 거절하면 이 턴에 바꾼 조건·범위를 되돌린다. 후보·주제는 거절 판단 뒤에만 바꾼다.
    before = copy.deepcopy({key: session[key] for key in ("constraints", "search_scope") if key in session})
    if updates:
        apply_constraints(session["constraints"], updates)
    query = _query(session, action, extras.reuse_topic, changed)
    scope = await _scope(coach, session, action.scope)
    items, found, starter = await coach._candidates(session, query, scope, action.other, extras.may_fall_back,
                                                    turn.budget)
    diag.update(search_performed=True, **found["diagnostics"])
    if search_refused(coach, turn, found):
        # 턴 전에 없던 키는 지우고, 있던 키는 턴 전 값으로 되돌린다.
        session.pop("search_scope", None)
        session.update(before)
        _diag(turn, "recommend")
        return
    diag.update(candidate_ids=[x["activity_id"] for x in items], starter_fallback=starter)
    _remember_candidates(turn, query, scope, items, found, starter)
    _reply_candidates(turn, items, found, starter)


def names_activity(text: str, activity: dict) -> bool:
    """글에 활동 제목 전체가 들어 있으면 True를 반환합니다. 띄어쓰기는 무시합니다."""
    # ponytail: 제목 전체 일치만 본다. 긴 제목을 줄여 부르면 전환하지 않는다. 필요하면 리랭커 점수 비교로 바꾼다.
    return activity["title"].replace(" ", "") in text.replace(" ", "")


async def _named_activity(coach: Coach, turn: Turn, action: Explain, query: str) -> list | None:
    """query로 검색해 맨 위 활동의 제목 전체가 query에 들어 있으면 [그 활동], 아니면 []. 거절했으면 None입니다."""
    # 이름을 직접 말했으므로 세션 조건으로 거르지 않는다.
    turn.budget.consume('retrieval')
    found = await asyncio.to_thread(coach.retrieval.search, query, turn.session["hobby_key"], {}, 3, "activities")
    turn.response["diagnostics"].update(search_performed=True, **found["diagnostics"])
    # 원문 요청은 추리지 않은 저장 원문을 읽는다. 다른 활동은 제목 전체가 맞을 때만 고르므로
    # 검색 모델이 고장이어도 이 이름 확인 때문에 거절하지 않는다.
    if not action.original and search_refused(coach, turn, found):
        return None
    return found["items"][:1] if found["items"] and names_activity(query, found["items"][0]) else []


async def _search_items(coach: Coach, turn: Turn, action: Explain, extras: RuleExtras) -> list | None:
    """topic으로 세션 조건을 걸어 3건을 찾습니다. 말한 조건은 검색 전에 반영하고, 거절하면 되돌립니다."""
    session = turn.session
    before = copy.deepcopy(session["constraints"])
    if extras.spoken_constraints:
        apply_constraints(session["constraints"], extras.spoken_constraints)
    # GPU 추론이 이벤트 루프를 막지 않도록 스레드에서 검색한다.
    turn.budget.consume('retrieval')
    found = await asyncio.to_thread(coach.retrieval.search, action.topic, session["hobby_key"],
                                    session["constraints"], 3, "activities")
    turn.response["diagnostics"].update(search_performed=True, **found["diagnostics"])
    if search_refused(coach, turn, found):
        session["constraints"] = before
        return None
    return found["items"]


async def _explain_items(coach: Coach, turn: Turn, action: Explain, extras: RuleExtras) -> list | None:
    """설명할 활동을 고릅니다. 거절했으면(설계 4.6절) None이고 세션 조건은 그대로입니다.

    후보 번호가 범위 밖이면 되묻고 None입니다. 고른 활동이 없으면 topic으로 찾습니다.
    """
    candidates = turn.session["candidate_ids"]
    items = []
    if action.target == "candidates":
        if any(not 1 <= number <= len(candidates) for number in action.candidate_numbers):
            turn.response.update(mode="clarify", text=NO_CANDIDATE_TEXT)
            return None
        ids = [candidates[number - 1] for number in action.candidate_numbers] or candidates[:3]
        items = [coach.activity(x) for x in ids]
    elif action.target == "selected" and turn.selected:
        items = [turn.selected]
        if extras.named_query:
            named = await _named_activity(coach, turn, action, extras.named_query)
            if named is None:
                return None
            items = named or items
    if not items:
        # 설명할 활동도 주제도 없으면 찾지 않는다. 빈 검색어는 카탈로그 둘러보기라 무관한 활동이 나온다.
        return await _search_items(coach, turn, action, extras) if action.topic.strip() else []
    return items


async def explain(coach: Coach, turn: Turn, action: Explain, extras: RuleExtras) -> None:
    _diag(turn, "explain")
    session, response, selected = turn.session, turn.response, turn.selected
    items = await _explain_items(coach, turn, action, extras)
    if items is None:
        return
    # 단계는 선택한 활동의 근거를 좁힐 때만 쓴다. 선택한 활동이 설명 대상일 때만 범위를 보고, 밖이면 생성 없이 되묻는다.
    if selected and any(item["activity_id"] == selected["activity_id"] for item in items) and _step_out_of_range(
            turn, action.step):
        return
    if not items:
        response.update(
            mode="clarify",
            text="설명할 활동의 근거를 찾지 못했어요. 활동을 먼저 선택하거나 질문을 구체적으로 적어 주세요.",
        )
        return
    evidence = [evidence_for(item, action.step if selected and item["activity_id"] == selected["activity_id"]
                             else None) for item in items]
    response["sources"] = evidence
    context = build_issue_context(normalize_dialogue(session["dialogue"], session), session)
    context["allowed_activity_ids"] = [item["activity_id"] for item in items]
    # Claude가 없거나 실패한 턴의 원문 안내는 관련도가 높은 항목만 보여 준다(설계 6절 4번).
    # 재순위도 실패하면 추리지 않은 원문 대신 거절한다(설계 4.6절, _answer와 같은 판단).
    # 원문 요청은 추리지 않으므로 재순위를 부르지 않는다. 재순위 고장으로 거절할 일도 없다.
    ranked = None
    if claude_down(coach, turn) and not action.original:
        # 원문은 질문으로만 넘긴다. 줄 단위 확인 테스트(test_executors_do_not_judge_the_raw_text)가 이 줄을 보므로 한 줄에 둔다.
        ranked, error = await asyncio.to_thread(lambda: coach.retrieval.rank_evidence(turn.text, evidence))
        if rank_refused(coach.llm, error, response):
            return
    result = await AnswerPolicy().answer(turn.text, evidence, context, [], coach.llm, turn.budget, ranked=ranked,
                                         unavailable=turn.provider_failure)
    response.update({k:v for k,v in result.items() if k not in {"llm_calls", "usage", "status"}})
    if turn.provider_failure:
        mark_unavailable(response)  # llm_status는 판단 호출의 실패 상태 그대로 둔다
    else:
        response["diagnostics"]["llm_status"] = result["status"]


async def coach_turn(coach: Coach, turn: Turn, action: CoachTurn, extras: RuleExtras) -> None:
    """대화 처리기(문제 코칭)에 맡깁니다. 규칙 경로는 해석을 TurnRouter에, 에이전트 경로는 행동의 내용을 해석으로 넘깁니다.

    대화 처리기가 코칭이 아니라고 넘긴 말(response['handoff'])은 규칙 경로의 진입이 이어받습니다.
    """
    _diag(turn, "self_assessment" if action.route == "reflection" else "coaching")
    if not extras.router_interprets and not turn.selected:
        # 에이전트 경로: 선택한 활동이 없으면 대화 상태를 건드리지 않고 되묻는다(설계 4.3절).
        turn.response.update(mode="clarify", text=NO_ACTIVITY_COACH_TEXT)
        return
    interpretation = None if extras.router_interprets else TurnInterpretation(
        route=action.route, transition=action.transition, transition_quote=action.transition_quote,
        observation_updates=action.observation_updates, requirement_proposals=action.requirement_proposals)
    await coach.dialogue.respond(turn.text, turn.session, turn.response, turn.user_message_id,
                                 problem_step=action.step, interpretation=interpretation, budget=turn.budget,
                                 unavailable=turn.provider_failure)


async def ask_user(coach: Coach, turn: Turn, action: AskUser, extras: RuleExtras) -> None:
    """고정 문구로 되묻습니다. 모델·검색을 부르지 않고 세션도 바꾸지 않습니다."""
    diag, response = turn.response["diagnostics"], turn.response
    if action.kind == "which_activity":
        diag["intent"] = "ambiguous"
        response.update(mode="clarify", text=UNCLEAR_DEICTIC)
        return
    diag.update(intent="ambiguous", stop_reason="ambiguous_intent")
    if action.kind == "two_requests":
        response.update(mode="clarify", text=OVERLAP_TEXT)
    else:
        response.update(mode="clarify", text=UNCLEAR_SELECTED if turn.selected else UNCLEAR_UNSELECTED)


async def answer_confirmation(coach: Coach, turn: Turn, action: AnswerConfirmation, extras: RuleExtras) -> None:
    """대기 중인 확인의 답. 완료 확인이 먼저이고(더 나중에 한 질문), 없으면 문제 전환 확인의 답입니다(설계 4.4절).

    대기 확인이 없는 답은 실행 전에 걸러진다(agent.validate, intent_handlers.run_rules).
    """
    waiting = confirmation.pending(turn.session)
    if not waiting:
        if not transition_question(turn.session["dialogue"]):
            raise LookupError("answer_confirmation: 대기 중인 확인이 없습니다")
        turn.response["diagnostics"]["intent_category"] = "question"
        await coach.dialogue.respond(turn.text, turn.session, turn.response, turn.user_message_id, budget=turn.budget,
                                     unavailable=turn.provider_failure, confirmation=action.accept)
        return
    _diag(turn, "complete")
    confirmation.clear(turn.session)
    if action.accept:
        _record_completion(coach, turn)
    else:
        turn.response["text"] = confirmation.DECLINED


EXECUTORS: dict[str, Executor] = {
    "select_candidate": select_candidate,
    "start_activity": start_activity,
    "decline_start": decline_start,
    "complete_activity": complete_activity,
    "show_materials": show_materials,
    "show_duration": show_duration,
    "show_steps": show_steps,
    "show_records": show_records,
    "notice": notice,
    "recommend": recommend,
    "explain": explain,
    "coach": coach_turn,
    "ask_user": ask_user,
    "answer_confirmation": answer_confirmation,
}


async def execute(coach: Coach, turn: Turn, action: Action, extras: RuleExtras = RuleExtras()) -> None:
    """행동 이름에 맞는 실행 함수를 부릅니다."""
    await EXECUTORS[action.action](coach, turn, action, extras)
