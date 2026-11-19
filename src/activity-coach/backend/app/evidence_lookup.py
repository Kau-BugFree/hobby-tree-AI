"""Claude 없이 하는 자료 조회(설계 4.5절): 규칙이 정하지 못한 말에 관련 자료를 원문 그대로 보여 줍니다.

읽기 전용이다. 추천 후보, 선택 활동, 조건, 대화 상태(관찰·문제)를 바꾸지 않는다.
관련도는 재순위 점수가 기준(rerank_min_score)을 넘을 때만 인정한다. 낱말 일치만으로는 보여 주지 않는다.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from .answer_policy import REFERENCE_TOP_K, AnswerPolicy, evidence_for
from .intent_rules import deictic_without_activity
from .retrieval import outside_cooking
from .service_level import rank_refused, search_refused
from .slots import candidate_number, names_topic

if TYPE_CHECKING:
    from .coach import Coach
    from .action_executors import Turn

LOOKUP_HEAD = "설명은 생성하지 못했지만 관련 자료를 찾았어요."
# 후보 목록이 있을 때 '1번만', '2번 더'처럼 번호를 말하면 목록을 가리키는지 알 수 없다. ORDINAL은 이런 말을 횟수로 보아 거르지 않는다.
# 숫자 열의 처음에서만 읽는다(긴 숫자 열의 제곱 시간 방지, 맞는 말은 같다).
NUMBER_MENTION = re.compile(r"(?<!\d)\d+\s*번")


def _searchable(coach: Coach, turn: Turn) -> bool:
    """검색해 볼 만한 말인지. 가리키는 대상이 없는 말, 범위 밖, 주제어 없는 말, 목록 번호를 말한 말은 아니다."""
    text, context = turn.text, turn.context
    return bool(coach.retrieval.reranker_enabled
                and not deictic_without_activity(text, context) and not outside_cooking(text)
                and names_topic(text)
                and not (context.candidate_count > 0
                         and (candidate_number(text) is not None or NUMBER_MENTION.search(text))))


async def _activity_passages(coach: Coach, turn: Turn) -> list[dict] | None:
    """① 선택 활동의 자료 중 재순위 점수가 기준을 넘는 항목(점수 순, 최대 REFERENCE_TOP_K개). 거절했으면 None."""
    retrieval = coach.retrieval
    turn.budget.consume("retrieval")
    turn.response["diagnostics"]["search_performed"] = True
    rows = (await asyncio.to_thread(retrieval.knowledge_for_activity, turn.selected["activity_id"])
            or [evidence_for(turn.selected)])
    ranked, error = await asyncio.to_thread(retrieval.rank_evidence, turn.text, rows)
    if rank_refused(coach.llm, error, turn.response):
        return None
    return (ranked or [])[:REFERENCE_TOP_K]


async def _related_activities(coach: Coach, turn: Turn) -> list[dict] | None:
    """② 활동·레시피 검색에서 cross-encoder 점수가 기준을 넘은 결과. 거절했으면 None."""
    turn.budget.consume("retrieval")
    found = await asyncio.to_thread(coach.retrieval.search, turn.text, turn.session["hobby_key"], {}, 3,
                                    turn.session.get("search_scope", "activities"))
    diag = found["diagnostics"]
    turn.response["diagnostics"].update(search_performed=True, **diag)
    if search_refused(coach, turn, found):
        return None
    scored = diag["reranker"] == "cross_encoder" and diag["rerank_min_score"] is not None
    return found["items"] if scored else []


async def lookup_evidence(coach: Coach, turn: Turn) -> bool:
    """관련 자료를 찾아 원문으로 답하거나 거절(설계 4.6절)을 적고 True. 검색하지 않을 말이거나 찾지 못하면 응답을 두고 False."""
    text, response = turn.text, turn.response
    if not _searchable(coach, turn):
        return False
    if turn.selected:
        passages = await _activity_passages(coach, turn)
        if passages is None:
            return True
        if passages:
            policy = AnswerPolicy()
            context = {"allowed_activity_ids": [turn.selected["activity_id"]]}
            # 원문 안내와 같은 모양으로 보여 준다. 찾은 자료를 답으로 고쳐 쓰지 않는다.
            shown = policy.render_reference(policy.prepare(text, passages, context, []))
            if shown["sources"]:
                response.update(mode="reference", text=LOOKUP_HEAD + "\n" + shown["text"], sources=shown["sources"])
                response["diagnostics"]["intent"] = "evidence_lookup"
                return True
    # ② 선택 활동이 없거나 그 자료에 없으면 활동·레시피를 찾는다.
    items = await _related_activities(coach, turn)
    if items is None:
        return True
    if not items:
        return False
    # 번호 목록이 아니다. activities도 채우지 않아 '1번'이 이 목록에서 고르는 말이 되지 않는다.
    response.update(
        mode="reference",
        text=LOOKUP_HEAD + "\n" + "\n".join(f'- {x["title"]}' for x in items),
        sources=[{"activity_id": x["activity_id"], "title": x["title"], "kind": x["kind"],
                  "excerpt": x.get("description") or x["title"]} for x in items])
    response["diagnostics"]["intent"] = "evidence_lookup"
    return True
