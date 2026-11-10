"""V2 integration: real API/store/retriever; only paid LLM is scripted."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_api import app_factory, create_session, message, select_activity, FakeLLM


def decision(action, **kwargs):
    return dict(next_action=action, query="", depends_on=[], question="", options=[],
                evidence_ids=[], reset_issue=False, **kwargs)


class Planner(FakeLLM):
    def __init__(self, plans):
        super().__init__()
        self.plans = iter(plans)
        self.contexts = []

    async def decide(self, question, context, evidence):
        self.contexts.append(copy.deepcopy(context))
        return {"decision": next(self.plans), "status": "planned", "llm_calls": 1,
                "usage": {"input_tokens": 1, "output_tokens": 1}}

    async def explain(self, question, evidence, context, validate=True):
        result = await super().explain(question, evidence, context, validate)
        result["cited_activity_ids"] = [e["activity_id"] for e in evidence]
        return result


def ask_plan():
    result = decision("clarify")
    result.update(question="계량스푼의 눈금을 확인했나요?", options=["예", "아니요", "모르겠어요"])
    return result


def test_pending_yes_survives_restart_and_replay(app_factory):
    llm = Planner([ask_plan(), decision("abstain")])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client)
        select_activity(client, sid)
        first = message(client, sid, "계량이 잘 안돼", "q1")
        assert first["mode"] == "clarify"
        assert first["clarification"]["options"] == ["예", "아니요", "모르겠어요"]
        replay = message(client, sid, "계량이 잘 안돼", "q1")
        assert replay["message_id"] == first["message_id"]
    with TestClient(app_factory(llm=llm)) as client:
        answer = message(client, sid, "응", "q2")
        assert answer["diagnostics"]["intent"] == "coaching"
        assert llm.contexts[-1]["dialogue"]["observations"][-1]["answer"] == "예"
        state = client.get(f"/sessions/{sid}").json()
        assert state["dialogue"]["pending_question"] is None
        assert state["execution_id"] is None


def test_option_number_is_not_activity_selection(app_factory):
    llm = Planner([ask_plan(), decision("abstain")])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        result = message(client, sid, "1번", "q2")
        assert result["selected_activity_id"] == "cook-0-2"
        assert llm.contexts[-1]["dialogue"]["observations"][-1]["answer"] == "예"


def test_complete_bundle_skips_redundant_hops(app_factory):
    first = decision("retrieve"); first["query"] = "가루와 액체를 각각 알맞은 방법으로 계량했다"
    second = decision("retrieve"); second.update(query="계량스푼 수평", depends_on=["cook-0-2:checks"])
    final = decision("answer"); final["evidence_ids"] = ["cook-0-2:checks", "cook-0-2:steps"]
    llm = Planner([first, second, final])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        result = message(client, sid, "계량이 어려워", "hop")
        assert result["diagnostics"]["intent"] == "coaching"
        assert result["diagnostics"]["retrieval_hops"] == 1
        assert len(result["diagnostics"]["hop_trace"]) == 1
        assert result["mode"] == "reference"
        assert set(result["cited_evidence_ids"]) == {"cook-0-2:checks", "cook-0-2:steps", "cook-0-2:safety_notes"}


@pytest.mark.parametrize("plan", [decision("answer"), decision("retrieve", **{})])
def test_invalid_plan_fails_closed(app_factory, plan):
    llm = Planner([plan])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        result = message(client, sid, "잘 모르겠어", "bad")
        assert result["mode"] == "insufficient_evidence"
        assert result["execution_id"] is None


def test_no_key_asks_activity_without_claiming_generated(app_factory):
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        result = message(client, sid, "요리가 어려워", "missing")
        assert result["mode"] == "clarify"
        assert result["diagnostics"]["llm_calls"] == 0
        assert client.get(f"/sessions/{sid}").json()["dialogue"]["pending_question"]


def test_select_clears_pending_issue(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        state = select_activity(client, sid, "cook-0-3")
        assert state["dialogue"]["pending_question"] is None
        assert state["dialogue"]["observations"] == []


def test_catalog_knowledge_is_valid_and_provenance_is_exact():
    from pathlib import Path
    from app.knowledge import load_knowledge
    path = Path(__file__).resolve().parents[1] / "data"
    rows = load_knowledge(path)
    assert len(rows) >= 2
    assert all(row["review_status"] == "source_only" for row in rows)
    assert any(row["related_knowledge_ids"] for row in rows)


@pytest.mark.parametrize("answer", ["1번으로 할게", "다 했어", "아니요", "모르겠어요", "2단계요", "도구가 없어"])
def test_pending_reply_never_performs_activity_action(app_factory, answer):
    llm = Planner([ask_plan(), decision("abstain")])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        result = message(client, sid, answer, "q2")
        assert result["diagnostics"]["intent"] == "coaching"
        assert result["execution_id"] is None
        assert client.get('/records', params={'session_id': sid}).json()['total'] == 0


def test_explicit_new_problem_archives_previous_observations(app_factory):
    llm = Planner([ask_plan(), decision("abstain"), decision("abstain")])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        message(client, sid, "응", "q2")
        previous = client.get(f'/sessions/{sid}').json()['dialogue']['issue_id']
        message(client, sid, "새 문제야. 보관이 어려워", "q3")
        assert len(llm.contexts[-1]["dialogue"]["observations"]) == 1
        assert client.get(f'/sessions/{sid}').json()['dialogue']['issue_id'] != previous
        with client.app.state.store.connection() as db:
            saved = json.loads(db.execute('SELECT payload FROM dialogue_issues WHERE issue_id=?', (previous,)).fetchone()[0])
        assert saved['phase'] == 'abandoned' and len(saved['observations']) == 2


def test_negative_answer_to_pending_question_keeps_clarification_budget(app_factory):
    """'아니 …'로 시작하는 확인 질문 답변은 정정이 아니므로 질문 맥락과 한도를 유지합니다."""
    llm = Planner([ask_plan(), ask_plan()])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        result = message(client, sid, "아니 눈금은 안 봤어", "q2")
        assert llm.contexts[-1]["dialogue"]["observations"][-1]["question"] == "계량스푼의 눈금을 확인했나요?"
        assert result["diagnostics"]["stop_detail"] == "clarification_limit_or_duplicate"


def test_material_request_during_pending_question_is_answered_directly(app_factory):
    """확인 질문 대기 중에도 '준비물 알려줘'는 준비물 조회로 처리하고 확인 질문은 남겨 둡니다."""
    llm = Planner([ask_plan(), decision("abstain")])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        result = message(client, sid, "준비물 알려줘", "q2")
        assert result["diagnostics"]["intent"] == "activity_lookup"
        assert result["text"].startswith("준비물:")
        assert len(llm.contexts) == 1
        pending = client.get(f"/sessions/{sid}").json()["dialogue"]["pending_question"]
        assert pending["text"] == "계량스푼의 눈금을 확인했나요?"


def test_unknown_evidence_cannot_be_used(app_factory):
    plan = decision("answer"); plan['evidence_ids'] = ['invented']
    with TestClient(app_factory(llm=Planner([plan]))) as client:
        sid = create_session(client); select_activity(client, sid)
        result = message(client, sid, "잘 모르겠어", "bad-ref")
        assert result['diagnostics']['stop_detail'] == 'unknown_evidence_reference'


def test_repeated_question_stops(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan(), ask_plan()]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "잘 모르겠어", "q1")
        result = message(client, sid, "모르겠어요", "q2")
        assert result['diagnostics']['stop_detail'] == 'clarification_limit_or_duplicate'


def test_source_only_does_not_generate_unreviewed_causes(app_factory):
    search = decision('retrieve'); search['query'] = '계량스푼 수평'
    answer = decision('answer'); answer['evidence_ids'] = ['cook-0-2:steps']
    llm = Planner([search, answer])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        result = message(client, sid, '왜 잘 안돼', 'source')
        assert result['mode'] == 'reference'
        assert not llm.calls
        assert '원인' in result['text']


def test_activity_selection_question_accepts_candidate_number(app_factory):
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        recommendation = message(client, sid, '요리 활동 추천해줘', 'r')
        message(client, sid, '잘 모르겠어', 'q')
        selected = message(client, sid, '1번', 's')
        assert selected['selected_activity_id'] == recommendation['activities'][0]['activity_id']


def test_problem_step_is_not_progress_update(app_factory):
    llm = Planner([ask_plan()])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        result = message(client, sid, '2단계가 잘 안돼', 'step')
        assert result['diagnostics']['intent'] == 'coaching'
        assert result['current_step'] == 1
        assert llm.contexts[0]['current_step'] == 2


def test_model_reset_cannot_reset_clarification_budget(app_factory):
    plan = ask_plan(); plan['reset_issue'] = True
    with TestClient(app_factory(llm=Planner([plan, plan]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '잘 모르겠어', 'q')
        result = message(client, sid, '아니요', 'a')
        # 같은 질문을 다시 묻지 않고 한도도 늘지 않는다. 막다른 응답 대신 원문을 안내한다.
        assert result['mode'] == 'reference' and result['diagnostics']['clarification_count'] == 1
        assert result['diagnostics']['stop_detail'] == 'clarification_limit_or_duplicate'


@pytest.mark.parametrize('plans,reason', [
    ([{'next_action':'illegal'}], 'invalid_decision'),
    ([decision('retrieve', **{})], 'empty_or_repeated_query'),
])
def test_bad_model_action_stops(app_factory, plans, reason):
    with TestClient(app_factory(llm=Planner(plans))) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'잘 모르겠어','q')
        assert result['diagnostics']['stop_detail']==reason


def test_retrieval_retry_and_repeated_query_are_bounded(app_factory):
    plans=[]
    for query in ['양자역학', '우주여행']:
        plan=decision('retrieve'); plan['query']=query; plans.append(plan)
    with TestClient(app_factory(llm=Planner(plans))) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'잘 모르겠어','q')
        assert result['diagnostics']['stop_reason']=='missing_knowledge'
        assert result['diagnostics']['retrieval_retries']==0


def test_complete_bundle_needs_no_artificial_dependency(app_factory):
    first=decision('retrieve'); first['query']='계량스푼 수평'
    second=decision('retrieve'); second['query']='가루 액체'
    with TestClient(app_factory(llm=Planner([first,second]))) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'잘 모르겠어','q')
        assert result['diagnostics']['retrieval_calls']==1


def test_planner_sees_current_hops_and_search_without_progress(app_factory):
    first = decision('retrieve'); first['query'] = '계량스푼 수평'
    second = decision('retrieve'); second.update(query='가루 액체', depends_on=['cook-0-2:steps'])
    third = decision('retrieve'); third.update(query='간장 계량', depends_on=['cook-0-2:steps'])
    final = decision('answer'); final['evidence_ids'] = ['cook-0-2:steps']
    llm = Planner([first, second, third, final])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        result = message(client, sid, '계량이 어려워', 'progress')
        assert result['mode'] == 'reference'
        progress = llm.contexts[-1]['workflow']
        assert progress['retrieval_hops'] == 1
        assert progress['remaining_hops'] == 2
        assert progress['all_activity_evidence_loaded'] is True
        assert progress['last_search'] is None



@pytest.mark.parametrize('status,mode', [('generated','generated'), ('validation_failed','reference')])
def test_reviewed_answer_generation_and_validation_failure(app_factory, status, mode):
    from test_claim_validation import Claims
    class Reviewed(Planner, Claims):
        def __init__(self):
            final=decision('answer'); final['evidence_ids']=['cook-0-2:steps']
            Planner.__init__(self,[final]); Claims.__init__(self)
        async def explain_claims(self, question, evidence, context):
            self.calls.append({'context':context})
            row=evidence[0]
            return {'claims':[{'text':row['excerpt'],'evidence_ids':[row['evidence_id']],
                              'quotes':[{'evidence_id':row['evidence_id'],'quote':row['excerpt']}]}], 'llm_calls':1}
        async def verify_claims(self, claims, evidence, context):
            return {'verdicts':[{'claim_index':0,'verdict':'supported' if status=='generated' else 'insufficient',
                                'evidence_ids':[evidence[0]['evidence_id']]}], 'llm_calls':1}
    llm=Reviewed()
    app=app_factory(llm=llm)
    # Test-only review metadata; never alters bundled source-only data.
    app.state.retrieval.knowledge['cook-0-2:steps']['review_status']='reviewed'
    app.state.retrieval.knowledge['cook-0-2:steps']['applies_when']=''
    with TestClient(app) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'2단계가 잘 안돼','q')
        assert result['mode']==mode
        assert len(llm.calls)==(1 if status=='generated' else 2)
        assert llm.calls[0]['context']['current_step']==2


def test_timeout_is_saved_as_safe_reply(app_factory, monkeypatch):
    import asyncio
    import app.dialogue as module
    original_timeout=asyncio.timeout
    monkeypatch.setattr(module.asyncio,'timeout',lambda _: original_timeout(0.001))
    class Slow(Planner):
        async def decide(self,*args):
            await asyncio.sleep(1)
    with TestClient(app_factory(llm=Slow([]))) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'잘 모르겠어','q')
        assert result['diagnostics']['stop_reason']=='budget_exhausted'
        assert client.get(f'/sessions/{sid}').json()['dialogue']['pending_question'] is None


def test_old_session_payload_gets_dialogue_defaults(app_factory):
    app=app_factory()
    store=app.state.store
    session=store.create_session(); session.pop('dialogue')
    store.save_session(session)
    assert store.get_session(session['session_id'])['dialogue']['status']=='idle'


def test_no_key_selected_asks_once_then_shows_source(app_factory):
    with TestClient(app_factory()) as client:
        sid=create_session(client); select_activity(client,sid)
        first=message(client,sid,'잘 모르겠어','q1')
        assert first['mode']=='clarify'
        second=message(client,sid,'모르겠어요','q2')
        assert second['mode']=='reference' and second['sources']
        assert second['diagnostics']['llm_calls']==0


def test_three_call_budget_includes_bundle_and_two_extra_searches(app_factory, monkeypatch):
    class Missing(Planner):
        async def decide(self, text, context, evidence):
            rid=context['dialogue']['requirements'][0]['id']
            self.contexts.append(context)
            return {'decision':{'next_action':'retrieve','query':f'query {len(self.contexts)}',
                'target_requirement_ids':[rid], 'coverage':[{'requirement_id':rid,'status':'missing_knowledge'}]},
                'status':'planned','llm_calls':1}
    app=app_factory(llm=Missing([])); retrieval=app.state.retrieval
    original_bundle=retrieval.knowledge_for_activity
    bundle=original_bundle('cook-0-2')
    for i in range(4):
        retrieval.knowledge[f'node{i}']={**retrieval.knowledge['cook-0-2:steps'], 'knowledge_id':f'node{i}'}
    monkeypatch.setattr(retrieval,'knowledge_for_activity',lambda aid:bundle)
    calls=[]
    def search(*args):
        calls.append(args)
        return {'items':[{'knowledge_id':f'node{len(calls)-1}'}],'diagnostics':{'search_mode':'test'}}
    monkeypatch.setattr(retrieval,'search',search)
    with TestClient(app) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'왜 잘 안돼','q')
        assert len(calls)==2
        assert result['diagnostics']['retrieval_calls']==3
        assert result['diagnostics']['stop_reason']=='missing_knowledge'
        assert result['mode']=='reference'


def test_new_execution_starts_at_first_step_without_old_question(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid = create_session(client); select_activity(client, sid)
        client.post(f"/sessions/{sid}/executions", json={"activity_id": "cook-0-2", "request_id": "run-1"})
        message(client, sid, "다음 단계", "n1"); message(client, sid, "다음 단계", "n2")
        assert message(client, sid, "계량이 잘 안돼", "q1")["mode"] == "clarify"
        client.post(f"/sessions/{sid}/executions", json={"activity_id": "cook-0-2", "request_id": "run-2"})
        state = client.get(f"/sessions/{sid}").json()
        assert state["current_step"] == 1
        assert state["dialogue"]["pending_question"] is None
        assert state["dialogue"]["observations"] == []


def test_new_dish_request_during_clarification_searches(app_factory):
    llm = Planner([ask_plan()])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "계량이 잘 안돼", "q1")
        result = message(client, sid, "된장찌개 레시피 알려줘", "dish")
        assert result["diagnostics"]["intent"] == "recommend"
        assert result["diagnostics"]["search_performed"] is True
        assert len(llm.contexts) == 1
        # 새 후보를 받은 뒤의 번호는 이전 확인 질문의 답이 아니라 후보 선택이다.
        picked = message(client, sid, "1번", "pick")
        assert picked["diagnostics"]["intent"] == "select"
        assert picked["selected_activity_id"] == result["activities"][0]["activity_id"]


@pytest.mark.parametrize("text", ["활동 시작하지 마", "다시 시작하지 말아줘", "활동 시작 취소"])
def test_negated_start_keeps_coaching_record(app_factory, text):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, "계량이 잘 안돼", "q1")
        before = client.get(f"/sessions/{sid}").json()["dialogue"]
        result = message(client, sid, text, "no-start")
        assert "현재 진행 상태를 유지" in result["text"]
        assert client.get(f"/sessions/{sid}").json()["dialogue"] == before


def test_repeated_query_does_not_repeat_retrieval(app_factory):
    plan=decision('retrieve'); plan['query']='계량스푼 수평'
    repeated={**plan,'depends_on':['cook-0-2:steps']}
    with TestClient(app_factory(llm=Planner([plan,repeated]))) as client:
        sid=create_session(client); select_activity(client,sid)
        result=message(client,sid,'잘 모르겠어','q')
        assert result['diagnostics']['retrieval_hops']==1
        assert result['diagnostics']['stop_reason']=='missing_knowledge'
