"""질문 유형별 처리기: 세션/조회 → 필요한 검색 → 조건부 설명 생성 순서로 처리합니다."""

import asyncio
import hashlib
import json
import time
import uuid
import weakref
from .store import StoreError
from .dialogue import DialogueCoach
from .dialogue_models import new_dialogue
from .dialogue_state import archive_issue, normalize_dialogue
from .turn_budget import TurnBudget, BudgetExceeded
from .intent_rules import classify, states_condition  # noqa: F401  states_condition: 예전 가져오기 경로 유지
from . import agent, confirmation
from .action_executors import execute
from .intent_handlers import Turn, context_for, run_rules
from .service_level import MODEL_ERROR_KEYS
# 예전 가져오기 경로 유지(from app.coach import spoken_constraints 등).
from .slots import requested_step, spoken_constraints, has_conditions  # noqa: F401


def globally_unique(request_id):
    """request_id가 무작위(v4) UUID의 표준 표기이면 True를 반환합니다.

    세션 없는 첫 요청의 세션을 다시 찾는 열쇠로 쓰는 값이라, 여러 클라이언트 사이에서 겹치지 않고 추측할 수도 없어야 합니다.
    nil UUID, 시각 기반(v1), 대문자나 하이픈 없는 표기는 받지 않습니다.
    """
    try:
        parsed = uuid.UUID(request_id or "")
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == request_id


class Coach:
    def __init__(self, store, retrieval, llm, agent_enabled: bool = False, agent_timeout_seconds: float = 10.0):
        """저장소·검색기·생성기를 주입하여 외부 API 없이도 각 처리 경로를 검증합니다."""
        self.store = store
        self.retrieval = retrieval
        self.llm = llm
        self.agent_enabled = agent_enabled
        self.agent_timeout_seconds = agent_timeout_seconds
        self.dialogue = DialogueCoach(retrieval, llm, confirm_transitions=agent_enabled)
        # 진행 중인 요청이 잡고 있는 동안만 남는 세션별 잠금. 끝난 세션의 잠금은 자동으로 사라진다.
        self.locks = weakref.WeakValueDictionary()

    def activity(self, aid):
        """내부 ID 또는 기존 앱의 별칭으로 활동을 찾고, 없는 ID는 명시적으로 거부합니다."""
        item = self.retrieval.get(aid)
        if not item:
            raise StoreError(404, "활동을 찾을 수 없습니다. 다른 활동을 선택해 주세요.")
        return item

    def select(self, session, aid):
        """이전 후보의 순서를 유지하면서 명시적으로 선택한 활동의 맥락을 변경합니다."""
        item = self.activity(aid)
        if item["hobby_key"] != session["hobby_key"]:
            raise StoreError(422, "현재 대화의 취미와 다른 활동입니다.")
        if session["selected_activity_id"] != item["activity_id"]:
            archive_issue(session)
            session['context_after_message_id'] = (session['messages'][-1].get('message_id')
                                                   if session.get('messages') else None)
            session.update(
                selected_activity_id=item["activity_id"],
                current_step=1,
                execution_id=None,
                dialogue=new_dialogue(),
            )
            # 활동을 골랐으면 못 찾은 주제는 접은 것이다. 뒤의 조건 변경이 그 주제를 되살리지 않게 한다.
            session.pop("unmet_query", None)
        session["candidate_selection_pending"] = False
        return item

    @staticmethod
    def attach_execution(session, execution):
        """시작한 새 수행을 현재 실행으로 정합니다. 1단계부터 시작하고 이전 수행의 확인 질문·관찰은 넘기지 않습니다."""
        archive_issue(session)
        session.update(execution_id=execution["execution_id"], current_step=1)

    def ensure_execution(self, session, activity, request_id, force_new=False):
        """진행하던 실행을 재사용합니다. 명시적인 다시 시작만 새 수행 건을 만듭니다."""
        if session["execution_id"] and not force_new:
            current = self.store.get_execution(session["execution_id"])
            if current["activity_id"] == activity["activity_id"]:
                return current
        current, _ = self.store.start_execution(session["session_id"], activity, request_id)
        session["execution_id"] = current["execution_id"]
        return current

    async def _candidates(self, session, query, scope, other, may_fall_back, budget):
        """추천 후보를 찾아 (후보, 검색 결과, 입문 목록으로 대신했는지)를 반환합니다.

        주제 없이 둘러볼 때는 이 대화에서 완료한 활동을, '다른 거'를 찾을 때는 이미 보여 준 활동까지 뺍니다.
        주제를 말한 검색은 완료한 활동도 그대로 보여 줍니다. 완료 기록이 세션 단위라 다른 대화의 기록은 모릅니다.
        """
        # 저장소·색인 조회는 스레드에서 읽는다. 한 사용자의 조회가 다른 사용자의 요청까지 멈추지 않게 한다.
        records = await asyncio.to_thread(self.store.records, session["session_id"])
        done = {record["activity_id"] for record in records}
        seen = set(session.get("shown_ids", []))
        # ponytail: 검색 상한이 100건이라 뺄 활동이 97개를 넘으면 남은 활동을 다 채우지 못한다. 카탈로그 162개에서는 드물다.
        limit = 3 + len(done | seen)

        def search(text):
            return asyncio.to_thread(
                self.retrieval.search, text, session["hobby_key"], session["constraints"], limit, scope)

        budget.consume('retrieval')
        found, starter = await search(query), False
        # 추천을 바랐는데 주제가 맞는 활동이 없으면 빈손으로 두지 않고, 조건에 맞는 활동을 짧은 순서로 보여 준다.
        # 이번 턴의 검색 횟수를 다 썼으면 대신 보여 주지 않는다. 한도 초과로 방금 찾은 결과까지 버리지 않기 위해서다.
        # 이번 검색의 추론이 실패해 비었으면 대신 보여 주지 않는다. 실패를 가리면 호출한 쪽이 거절을 판단할 수 없다(설계 4.6절).
        # 불러오지 못한 모델의 사유(model_errors와 같은 값)는 이번 검색의 실패가 아니다. 그때는 거절을 검색 전에 정한다.
        persistent = self.retrieval.model_errors
        inference_failed = any(found["diagnostics"].get(key) not in (None, persistent[model])
                               for key, model in zip(MODEL_ERROR_KEYS, ("embedding", "reranker")))
        if (not found["items"] and may_fall_back and scope == "activities" and not inference_failed
                and found["diagnostics"].get("eligible_documents")
                and budget.counts['retrieval'] < budget.limits['retrieval']):
            budget.consume('retrieval')
            found, starter = await search(""), True
        browsing = found["diagnostics"]["search_mode"] == "catalog_browse"
        skip = (seen if other else set()) | (done if other or browsing else set())
        return [item for item in found["items"] if item["activity_id"] not in skip][:3], found, starter

    async def respond(self, request, seconds: float | None = None):
        """같은 세션의 요청을 순서대로 실행하고 재전송은 캐시된 결과를 반환합니다.

        로컬 시연은 단일 서버 프로세스로 실행합니다. 기록 중복은 별도로 DB UNIQUE
        제약을 사용하므로 재전송과 프로세스 재시작 후에도 완료 수가 늘어나지 않습니다.
        seconds는 이 턴의 제한 시간입니다. 생략하면 TurnBudget의 기본값(60초)입니다.
        """
        if request.session_id:
            # 저장소 호출은 스레드에서 실행한다. SQLite 잠금 대기가 다른 사용자의 요청까지 멈추지 않게 한다.
            session = await asyncio.to_thread(self.store.get_session, request.session_id)
        else:
            # 같은 request_id의 재전송이면 그 요청이 만든 세션을 다시 쓴다. 새 세션을 또 만들지 않는다.
            # 무작위 UUID일 때만 찾는다. 'demo-1'이나 '1' 같은 값은 다른 사용자의 새 대화와 겹치고,
            # 겹치면 남의 대화에 이어 붙거나 첫 메시지가 409로 거부된다.
            # 찾기와 만들기는 저장소의 한 트랜잭션이라 같은 요청이 동시에 와도 세션이 하나다.
            if globally_unique(request.request_id):
                session = await asyncio.to_thread(self.store.session_for_origin, request.request_id)
            else:
                session = await asyncio.to_thread(self.store.create_session)
        sid = session["session_id"]
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            session = await asyncio.to_thread(self.store.get_session, sid)
            # 보낸 필드만으로 지문을 만든다. 조건을 생략한 요청(유지)과 null로 보낸 요청(해제)은 뜻이 다르다.
            payload = request.model_dump(exclude={"request_id"}, exclude_unset=True)
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            cached = await asyncio.to_thread(self.store.cached_request, sid, request.request_id, fingerprint)
            if cached:
                cached["diagnostics"] = {
                    **cached["diagnostics"],
                    "replayed": True,
                    "llm_calls": 0,
                    'decision_calls':0, 'retrieval_calls':0, 'generation_calls':0, 'verification_calls':0,
                    'agent_calls':0, 'usage':{},
                }
                return cached
            return await self._respond_locked(request, session, fingerprint, seconds)

    def _drop_pending(self, session: dict) -> None:
        """대기 중인 완료 확인과 해결·중단 제안을 버립니다(제안은 묻기 전 상태로 되돌림). 설정이 꺼져 있으면 아무것도 하지 않습니다."""
        if not self.agent_enabled:
            return
        confirmation.clear(session)
        self.dialogue.drop_proposal(session)

    async def _respond_locked(self, request, session, fingerprint, seconds=None):
        sid = session["session_id"]
        budget = TurnBudget() if seconds is None else TurnBudget(seconds)
        started = time.perf_counter()
        text = request.message
        user_message_id = str(uuid.uuid4())
        if request.activity_id:
            self.select(session, request.activity_id)
        selected = (
            self.activity(session["selected_activity_id"])
            if session["selected_activity_id"]
            else None
        )
        if request.current_step is not None:
            if not selected or request.current_step > len(selected["steps"]):
                raise StoreError(422, "선택 활동의 단계 범위를 확인해 주세요.")
            session["current_step"] = request.current_step
        changed = bool(request.constraints and request.constraints.model_fields_set)
        if request.constraints:
            session["constraints"].update(
                request.constraints.model_dump(exclude_unset=True)
            )
        response = {
            "message_id": str(uuid.uuid4()),
            "session_id": sid,
            "mode": "direct",
            "text": "",
            "activities": [],
            "sources": [],
            "actions": [],
            "selected_activity_id": session["selected_activity_id"],
            "current_step": session["current_step"],
            "execution_id": session["execution_id"],
            "diagnostics": {
                "intent": "unknown",
                "search_performed": False,
                "search_mode": "none",
                "llm_calls": 0,
                "llm_status": "not_needed",
                "latency_ms": 0,
                "trace_id": str(uuid.uuid4()),
                "decided_by": "rules",
                "agent_action": None,
                "agent_reason": None,
            },
        }
        diag = response["diagnostics"]
        try:
            async with asyncio.timeout(budget.remaining_seconds()):
                rid = request.request_id or str(uuid.uuid4())
                if not self.agent_enabled:
                    # 설정을 켰을 때 남은 해결·중단 제안과 완료 확인은 분류 전에 버린다. 꺼진 서버는 그런 확인을 만들지 않는다.
                    self.dialogue.drop_proposal(session)
                    confirmation.clear(session)
                context = context_for(session, selected, changed, self.dialogue.interprets_turns())
                intent = classify(text, context)
                diag["intent_category"] = intent.category if intent else "unknown"
                diag["intent_rule"] = intent.rule if intent else None
                turn = Turn(text, session, response, selected, budget, rid, user_message_id, intent, context, changed)
                # 알레르기 판정 요청은 에이전트에 넘기지 않는다(설계 4.7절).
                action = (await agent.decide(self, turn)
                          if agent.available(self) and not (intent and intent.rule == "allergy") else None)
                if action:
                    diag.update(decided_by="agent", agent_action=action.action, intent_rule=None,
                                intent_category=agent.category_for(action))
                    decided_calls = budget.llm_calls  # 판단 호출까지의 횟수
                    if action.action != "answer_confirmation":
                        self._drop_pending(session)  # 대기 확인은 바로 다음 턴에만 유효하다(설계 4.4절)
                    await execute(self, turn, action)
                    if diag["llm_status"] == "not_needed" and budget.llm_calls == decided_calls:
                        diag["llm_status"] = "decided"
                else:
                    if turn.provider_failure:
                        # Claude가 답하지 못한 턴은 키가 없을 때와 같은 맥락(모델 해석 없음)으로 다시 분류한다(설계 4.7절).
                        turn.context = context_for(session, selected, changed, False)
                        turn.intent = intent = classify(text, turn.context)
                        diag.update(intent_category=intent.category if intent else "unknown",
                                    intent_rule=intent.rule if intent else None)
                    decided_calls = budget.llm_calls
                    # 모든 말은 분류를 거친다. 정하지 못한 말(None)은 모름 처리로 간다.
                    await run_rules(self, turn)
                    # 쓸 수 없는 판단 뒤 규칙 경로가 Claude를 불렀는데 상태를 적지 않았으면 규칙 경로의 값으로 둔다.
                    if (diag["agent_reason"] and diag["llm_status"] == "invalid_decision"
                            and budget.llm_calls > decided_calls):
                        diag["llm_status"] = "not_needed"
        except (TimeoutError, BudgetExceeded):
            # 예외로 끝난 턴 뒤에도 대기 확인이 다음 턴의 '네'를 받지 않게 한다. 이 턴이 만든 확인은
            # 만든 뒤 기다리는 호출이 없어 여기까지 오지 않는다.
            self._drop_pending(session)
            self.dialogue._stop(response, "budget_exhausted")
        except ValueError as error:
            if str(error) != 'context_limit':
                raise
            self._drop_pending(session)
            self.dialogue._stop(response, "context_limit")
        response.pop("handoff", None)
        if response["activities"] and not response["actions"]:
            response["actions"] = [
                {
                    "type": "open_activity",
                    "activity_id": x["activity_id"],
                    "label": x["title"],
                }
                for x in response["activities"]
            ]
        response.update(
            selected_activity_id=session["selected_activity_id"],
            current_step=session["current_step"],
            execution_id=session["execution_id"],
        )
        diag.update(budget.diagnostics())
        diag.pop("query_aliases", None)
        state = normalize_dialogue(session["dialogue"], session)
        response.update(issue_id=state.issue_id, issue_phase=state.phase)
        diag["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        await asyncio.to_thread(
            self.store.save_reply, session, request.request_id, fingerprint, response, text, user_message_id
        )
        return response
