"""Issue-scoped coaching: bundle retrieval, bounded decisions and shared policy."""
import asyncio
import hashlib
import json
import re
import uuid

from .answer_policy import AnswerPolicy, evidence_for, knowledge_evidence
from .dialogue_models import Decision, RequirementCoverage
from .dialogue_state import apply_transition, normalize_dialogue, serialize_dialogue, build_issue_context, new_issue_confirmed
from .turn_budget import TurnBudget, BudgetExceeded
from .turn_router import TurnRouter
from .metacognition import build_metacognition, reflection_response, stop_message


TROUBLE = re.compile(r"어려|어렵|모르겠|안\s*돼|안되|안\s*되|실패|망했|망해|찢어|붙어|막혔|막혀|헷갈|잘못")
CORRECTION = re.compile(r"^(아니[,， ]|정정|그게\s*아니)|다른\s*문제")
INTERRUPT = re.compile(r"활동\s*(?:완료|시작|선택)|다시\s*시작|다음\s*단계|이전\s*단계|추천|다른\s*요리|취소|중단")
# '~하면 안 돼?'는 실패가 아니라 해도 되는지 묻는 말이다.
PERMISSION_QUESTION = re.compile(r"면\s*안\s*(?:돼요?\s*[?？]|되(?:나요?|니)|될까요?)")
TRANSITION_QUESTION = '새 문제로 시작할까요? 기존 문제의 관찰은 따로 보관합니다.'
TRANSITION_GUIDE = '새 문제로 시작하려면 “새 문제”라고 함께 적어 주세요. 기존 문제의 관찰은 따로 보관합니다.'
NO_MODEL_QUESTION = '어느 단계에서 어떤 모습으로 잘 안 되었나요?'


def is_trouble(text: str) -> bool:
    return bool(TROUBLE.search(text)) and not PERMISSION_QUESTION.search(text)


class DialogueCoach:
    def __init__(self, retrieval, llm):
        self.retrieval, self.llm = retrieval, llm
        self.policy = AnswerPolicy()
        self.allow_additional_retrieval = True  # Evaluation changes only this capability.

    def accepts(self, text: str, session: dict) -> bool:
        state=normalize_dialogue(session['dialogue'],session)
        return bool(is_trouble(text) or state.pending_question and session['selected_activity_id']
            or re.search(r'해결됐|해결되었|(?:새|새로운)\s*문제|다른\s*문제|문제.*그만',text)
            or state.issue_id and re.search(r'^(아니|정정|사실|그럼|그러면|그래도|이건|그건)|왜|어떻게',text)
            or state.issue_id and state.phase in {'open','answer_provided'}
                and not re.search(r'추천|레시피|설명|쉽게|비교|차이|뜻|고마|감사|안녕|알겠|오케이|^(응|네|좋아요?)[.! ]*$',text))

    @staticmethod
    def _stop(response: dict, reason: str, detail: str | None = None) -> None:
        response.update(mode='insufficient_evidence',text=stop_message(reason))
        response['diagnostics']['stop_reason']=reason
        if detail:
            response['diagnostics']['stop_detail']=detail

    @staticmethod
    def _question(state, response: dict, question: str, options: list[str]) -> None:
        if (not question or any(len(o)>80 for o in options) or question in state.asked_questions
                or state.clarification_count>=2):
            DialogueCoach._stop(response,'missing_observation','clarification_limit_or_duplicate')
            return
        pending={'question_id':str(uuid.uuid4()),'text':question,'options':options,
                 'purpose':'clarification','accepts_free_text':True}
        state.pending_question=pending; state.phase='awaiting_observation'
        state.asked_questions.append(question); state.clarification_count+=1
        response.update(mode='clarify',text=question,clarification=pending)

    @classmethod
    def _propose_transition(cls, state, response: dict, text: str, message_id: str, interpretation) -> None:
        """Ask once per issue before starting a new one; past the limit, explain the explicit phrase."""
        if TRANSITION_QUESTION in state.asked_questions or state.clarification_count>=2:
            cls._stop(response,'ambiguous_intent','transition_limit')
            response['text']=TRANSITION_GUIDE
            return
        previous_phase, previous_question = state.phase, state.pending_question
        cls._question(state,response,TRANSITION_QUESTION,['예','아니요'])
        state.pending_question.update(purpose='transition',proposed_goal=text,source_message_id=message_id,
                                      proposed_interpretation=interpretation.model_dump(),
                                      previous_phase=previous_phase,previous_question=previous_question)
        if previous_phase in {'resolved','abandoned'}:
            state.phase=previous_phase

    async def respond(self, text: str, session: dict, response: dict, message_id: str,
                      problem_step=None, interpretation=None, budget=None) -> None:
        budget=budget or TurnBudget()
        state=normalize_dialogue(session['dialogue'],session)
        diag=response['diagnostics']; diag.update(intent='coaching',retrieval_hops=0,retrieval_retries=0,hop_trace=[])
        evidence=[]
        try:
            async with asyncio.timeout(budget.remaining_seconds()):
                interpretation=interpretation or await TurnRouter(self.llm).interpret(text,session,state,budget)
                diag.update(route=interpretation.route,transition=interpretation.transition)
                if interpretation.route not in {'dialogue','reflection'}:
                    self._stop(response,'ambiguous_intent')
                    return
                if interpretation.transition=='new_issue' and state.issue_id and not new_issue_confirmed(state,text,interpretation.transition_quote):
                    self._propose_transition(state,response,text,message_id,interpretation)
                    return
                if (state.pending_question or {}).get('purpose')=='transition' and interpretation.transition=='continue':
                    if re.fullmatch(r'아니|아니요|2번?',text.strip()):
                        previous=state.pending_question
                        state.phase=previous.get('previous_phase','open')
                        state.pending_question=previous.get('previous_question')
                        response.update(mode='direct',text='기존 문제를 유지했어요. 새 문제라면 새 문제라고 명시해 주세요.')
                    else:
                        self._stop(response,'ambiguous_intent')
                    return
                # A closed issue is never reopened implicitly: a new question or trouble needs consent.
                if (state.phase in {'resolved','abandoned'} and interpretation.route=='dialogue'
                        and interpretation.transition not in {'new_issue','resolve','abandon'}):
                    self._propose_transition(state,response,text,message_id,interpretation)
                    return
                if interpretation.route=='reflection' and state.pending_question and state.pending_question.get('purpose')=='reflection':
                    pass  # reflection_response consumes the pending answer with its question snapshot.
                else:
                    state,archives=apply_transition(state,interpretation,message_id,text,session['selected_activity_id'],session['execution_id'])
                    session.setdefault('_issue_snapshots',[]).extend(serialize_dialogue(s) for s in archives)
                if problem_step is not None:
                    state.step_number=problem_step
                if interpretation.route=='reflection':
                    result=reflection_response(state,self.retrieval.get(session['selected_activity_id']) if session['selected_activity_id'] else {},text,message_id)
                    state=result.pop('state'); response.update(result); diag['intent']='self_assessment'
                elif state.phase in {'resolved','abandoned'}:
                    response.update(mode='reference',text='문제에 대한 사용자 보고를 보관했어요. 활동 수행 기록은 별도 명시적 명령으로 관리합니다.')
                elif interpretation.transition=='correct' and not any(o.source_message_id==message_id for o in state.observations):
                    self._stop(response,'ambiguous_intent')
                else:
                    evidence=await self._run(text,session,state,response,budget)
        except (BudgetExceeded,TimeoutError):
            self._stop(response,'budget_exhausted')
        except ValueError as error:
            self._stop(response,'context_limit' if str(error)=='context_limit' else 'execution_failure')
        finally:
            if response['mode'] in {'generated','reference'} and state.phase not in {'resolved','abandoned'} and not state.pending_question:
                state.phase='answer_provided'
            state.hop_trace=diag['hop_trace']
            session['dialogue']=serialize_dialogue(state)
            response.update(issue_id=state.issue_id,issue_phase=state.phase)
            response.setdefault('metacognition',build_metacognition(state,evidence,diag.get('stop_reason')))
            diag.update(budget.diagnostics(),issue_id=state.issue_id,clarification_count=state.clarification_count)

    async def _run(self, text, session, state, response, budget) -> list:
        aid=state.activity_id
        if not aid:
            self._question(state,response,'어떤 요리 활동의 어느 부분에서 막혔나요? 활동 이름이나 번호로 먼저 선택해 주세요.',[])
            return []
        budget.consume('retrieval')
        rows=await asyncio.to_thread(self.retrieval.knowledge_for_activity,aid)
        if not rows:
            activity=self.retrieval.get(aid)
            rows=[evidence_for(activity)] if activity else []
        evidence={e['evidence_id']:e for e in rows}
        state.evidence_ids=list(evidence)
        state.coverage=self.policy.assess_coverage(state.requirements,state.coverage,rows,state.observations)
        diag=response['diagnostics']; diag['retrieval_hops']=1
        diag['hop_trace']=[{'hop':1,'depends_on':[],'evidence_ids':list(evidence)}]
        all_ids={r['knowledge_id'] for r in self.retrieval.knowledge.values() if aid in r['activity_ids'] and r['review_status']!='draft'}
        no_progress=0; previous_supported=None; last_search=None
        while True:
            context=build_issue_context(state,session)
            context['workflow']={'retrieval_hops':budget.counts['retrieval'],'remaining_hops':3-budget.counts['retrieval'],
                'retrieval_retries':no_progress,'remaining_decisions':6-budget.counts['decision'],
                'hop_trace':diag['hop_trace'],'last_search':last_search,
                'all_activity_evidence_loaded':all_ids<=evidence.keys()}
            if not hasattr(self.llm,'decide'):
                await self._answer(text,session,state,response,evidence,budget)
                return list(evidence.values())
            result=await budget.model_call('decision',self.llm.decide,text,context,list(evidence.values()))
            if not result.get('decision'):
                if result.get('status')=='not_configured':
                    # Without a model: one bounded question, then the source text (which needs no model).
                    if NO_MODEL_QUESTION in state.asked_questions or state.clarification_count>=2:
                        await self._answer(text,session,state,response,evidence,budget)
                    else:
                        self._question(state,response,NO_MODEL_QUESTION,[])
                elif result.get('status')=='context_too_long':
                    self._stop(response,'context_limit','context_too_long')
                else:
                    self._stop(response,'execution_failure',result.get('status','invalid_decision'))
                return list(evidence.values())
            try:
                plan=Decision.model_validate(result['decision'])
            except ValueError:
                self._stop(response,'execution_failure','invalid_decision'); return list(evidence.values())
            if not set(plan.depends_on+plan.evidence_ids)<=evidence.keys():
                self._stop(response,'execution_failure','unknown_evidence_reference'); return list(evidence.values())
            self._coverage(state,plan,evidence)
            supported=sum(c.status=='supported' for c in state.coverage)
            if previous_supported is not None:
                gain=supported-previous_supported
                diag['hop_trace'][-1]['coverage_gain']=gain
                no_progress = no_progress+1 if gain<=0 else 0
                diag['retrieval_retries']=max(0,no_progress-1)
            if plan.next_action=='clarify':
                self._question(state,response,plan.question,plan.options); return list(evidence.values())
            if plan.next_action=='answer':
                if not plan.evidence_ids:
                    self._stop(response,'missing_knowledge','answer_without_evidence')
                else:
                    await self._answer(text,session,state,response,{k:evidence[k] for k in plan.evidence_ids},budget)
                return list(evidence.values())
            if plan.next_action=='abstain':  # The decide contract reserves abstain for conflicting evidence.
                await self._answer(text,session,state,response,evidence,budget,reason='conflicting_evidence')
                return list(evidence.values())
            if not plan.query:  # Planner contract violations are processing failures, not user ambiguity.
                self._stop(response,'execution_failure','empty_or_repeated_query'); return list(evidence.values())
            if all_ids<=evidence.keys() or not self.allow_additional_retrieval or no_progress>=2:
                await self._answer(text,session,state,response,evidence,budget,reason='missing_knowledge')
                return list(evidence.values())
            missing={c.requirement_id for c in state.coverage if c.status=='missing_knowledge'}
            if not plan.target_requirement_ids or not set(plan.target_requirement_ids)<=missing:
                self._stop(response,'execution_failure','missing_target_requirement'); return list(evidence.values())
            fingerprint=self._search_key(plan.query,state)
            cached=next((s for s in state.searched_queries if s['key']==fingerprint),None)
            if cached:
                await self._answer(text,session,state,response,evidence,budget,reason='missing_knowledge')
                diag['search_reused']=True; return list(evidence.values())
            if len(state.searched_queries)>=16:
                raise ValueError('context_limit')
            budget.consume('retrieval')
            found=await asyncio.to_thread(self.retrieval.search,plan.query,session['hobby_key'],{'activity_id':aid},3,'knowledge')
            incoming={r['knowledge_id']:self.retrieval.knowledge.get(r['knowledge_id']) for r in found['items']}
            incoming={k:knowledge_evidence(r,aid) for k,r in incoming.items() if r and r['review_status']!='draft' and aid in r['activity_ids']}
            new_ids=list(incoming.keys()-evidence.keys())
            state.searched_queries.append({'key':fingerprint,'query':plan.query,'evidence_ids':list(incoming)})
            if len(evidence.keys()|incoming.keys())>10:
                raise ValueError('context_limit')
            evidence.update(incoming); state.evidence_ids=list(evidence)
            previous_supported=supported; last_search={'new_evidence_ids':new_ids}
            diag['retrieval_hops']=budget.counts['retrieval']
            diag['hop_trace'].append({'hop':budget.counts['retrieval'],'depends_on':plan.depends_on,'evidence_ids':new_ids})

    @staticmethod
    def _search_key(query, state) -> str:
        facts=sorted({(o.quote,o.answer_text or o.quote,o.status,(o.question_snapshot or {}).get('text',''))
                      for o in state.observations if o.status!='superseded'})
        return hashlib.sha256(json.dumps([' '.join(query.casefold().split()),facts],ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _coverage(state, plan, evidence) -> None:
        known={r.id for r in state.requirements}; previous={c.requirement_id:c for c in state.coverage}
        for c in plan.coverage:
            if c.requirement_id not in known or not set(c.evidence_ids)<=evidence.keys():
                continue
            if c.status=='supported' and (not c.quotes or not c.evidence_ids
                    or {q.evidence_id for q in c.quotes}!=set(c.evidence_ids)
                    or any(q.quote not in evidence[q.evidence_id]['excerpt'] for q in c.quotes)):
                continue
            previous[c.requirement_id]=c
        state.coverage=[previous.get(r.id,RequirementCoverage(requirement_id=r.id)) for r in state.requirements]
        state.coverage=AnswerPolicy.assess_coverage(state.requirements,state.coverage,list(evidence.values()),state.observations)

    async def _answer(self,text,session,state,response,evidence,budget,reason=None):
        state.coverage=self.policy.assess_coverage(state.requirements,state.coverage,list(evidence.values()),state.observations)
        result=await self.policy.answer(text,list(evidence.values()),build_issue_context(state,session),state.requirements,self.llm,budget)
        response.update({k:v for k,v in result.items() if k not in {'llm_calls','usage','status'}})
        stop=result['metacognition']['stop_reason'] or reason
        if stop:
            response['diagnostics']['stop_reason']=stop
            response['metacognition']['stop_reason']=stop

