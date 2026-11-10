"""Failures from the frozen paired evaluation, plus wording/negation boundaries."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from test_api import app_factory, create_session, select_activity, message
from test_dialogue import Planner, decision
from app.dialogue_models import DialogueState, Observation, Requirement, TurnInterpretation
from app.dialogue_state import apply_transition
from app.turn_router import TurnRouter, coaching_requirements
from app.turn_budget import TurnBudget


@pytest.mark.parametrize('text', [
    '계량 중인데 물 온도가 차가웠어.', '가루를 계량하면 항상 수북하게 올라와.',
    '샐러드 채소에 물기가 많이 남아 있어.', '반죽 표면에 작은 덩어리가 남아 있어요.',
    '큰술과 작은술 구분이 헷갈려.', '소금 말고 물을 넣었어.', '요약이 잘못됐어.',
])
def test_initial_report_opens_issue_without_recommendation(app_factory, text):
    with TestClient(app_factory(llm=Planner([decision('abstain')]))) as client:
        sid=create_session(client); select_activity(client,sid)
        reply=message(client,sid,text,'report')
        state=client.get(f'/sessions/{sid}').json()
        assert reply['diagnostics']['intent']=='coaching'
        assert state['dialogue']['issue_id']
        assert state['dialogue']['observations'][0]['quote']==text
        assert state['execution_id'] is None


def test_information_request_is_not_reported_fact_or_pending_answer():
    state=DialogueState(issue_id='i',phase='awaiting_observation',
        pending_question={'text':'온도는?', 'options':[], 'purpose':'clarification'})
    text='보관 장소와 기간을 원문에서 알려줘.'
    intent=asyncio.run(TurnRouter(None).interpret(text,{},state,TurnBudget()))
    after,_=apply_transition(state,intent,'m',text,'a',None)
    assert not after.observations
    assert after.pending_question==state.pending_question


def test_withdrawn_requirement_is_removed_without_losing_observations():
    state=DialogueState(issue_id='i',phase='open',
        observations=[Observation(id='o',source_message_id='m',quote='계량이 잘 안돼.')],
        requirements=[Requirement(id='c',source_message_id='m',text='왜 그래?',kind='cause'),
                      Requirement(id='p',source_message_id='m',text='계량 순서',kind='procedure')])
    text='원인 설명은 취소할게. 원인 말고 가루와 액체를 계량하는 원문 방법만 알려줘.'
    intent=asyncio.run(TurnRouter(None).interpret(text,{},state,TurnBudget()))
    after,_=apply_transition(state,intent,'m2',text,'a',None)
    assert all(r.kind!='cause' for r in after.requirements)
    assert after.observations==state.observations
    assert any(r.kind=='procedure' for r in after.requirements)
    assert not any(p['kind']=='cause' for p in coaching_requirements(text))


def test_lookup_followup_answers_and_preserves_issue(app_factory):
    with TestClient(app_factory(llm=Planner([decision('abstain')]*4))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'가루를 계량하면 항상 수북하게 올라와.','old')
        old=client.get(f'/sessions/{sid}').json()['dialogue']
        message(client,sid,'새 문제야. 이제 남은 드레싱 보관이 헷갈려.','new')
        current=client.get(f'/sessions/{sid}').json()['dialogue']
        reply=message(client,sid,'보관 장소와 기간을 원문에서 알려줘.','lookup')
        after=client.get(f'/sessions/{sid}').json()['dialogue']
        assert old['issue_id'] and current['issue_id']!=old['issue_id']
        assert after['issue_id']==current['issue_id']
        assert reply['diagnostics'].get('stop_reason')!='ambiguous_intent'
        assert not any(o['quote']==old['observations'][0]['quote'] for o in after['observations'])


@pytest.mark.parametrize('text', [
    '이 문제는 해결됐어. 활동 전체를 완료했다는 뜻은 아니야.',
    '문제는 해결되었어. 아직 활동은 끝내지 않았어.',
])
def test_issue_resolution_is_separate_from_activity_completion(app_factory,text):
    with TestClient(app_factory(llm=Planner([decision('abstain')]*3))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'큰술과 작은술 구분이 헷갈려.','problem')
        reply=message(client,sid,text,'resolved')
        state=client.get(f'/sessions/{sid}').json()
        assert state['dialogue']['phase']=='resolved'
        assert state['execution_id'] is None and 'record' not in reply
        assert state['current_step']==1


def test_correction_then_summary_uses_only_active_user_reports(app_factory):
    class Corrector(Planner):
        async def interpret_turn(self,text,context):
            old=context['dialogue']['observations'][0]['id']
            return {'decision':{'route':'dialogue','transition':'correct','observation_updates':[
                {'quote':'물 온도는 모르겠어.', 'status':'unknown', 'supersedes':[old]}]}, 'llm_calls':1}
    with TestClient(app_factory(llm=Corrector([decision('abstain')]*4))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'계량 중인데 물 온도가 차가웠어.','report')
        message(client,sid,'정정할게. 차갑다는 말은 잘못했어. 물 온도는 모르겠어.','correct')
        before=client.get(f'/sessions/{sid}').json()['dialogue']
        reply=message(client,sid,'지금 확실한 관찰과 모르는 점을 구분해서 말해줘.','summary')
        after=client.get(f'/sessions/{sid}').json()['dialogue']
        assert before['observations']==after['observations']
        assert any(o['status']=='unknown' and '온도' in o['quote'] for o in after['observations'])
        assert '물 온도는 모르겠어' in reply['text'] and '차가웠어' not in reply['text']
        assert reply['diagnostics']['intent']!='self_assessment'


@pytest.mark.parametrize('text', ['이 문제는 아직 해결되지 않았어.', '문제가 해결됐어?', '해결됐다는 뜻은 아니야.'])
def test_negated_or_question_resolution_keeps_issue_open(text):
    state=DialogueState(issue_id='i',phase='open')
    intent=asyncio.run(TurnRouter(None).interpret(text,{},state,TurnBudget()))
    after,_=apply_transition(state,intent,'m',text,'a',None)
    assert after.phase!='resolved'


@pytest.mark.parametrize('text,quote', [
    ('계량이 잘 안돼. 원문의 방법을 알려줘.', '계량이 잘 안돼.'),
    ('물 온도가 차가웠어. 보관 장소를 알려줘.', '물 온도가 차가웠어.'),
])
def test_mixed_report_and_request_only_records_report(text,quote):
    intent=asyncio.run(TurnRouter(None).interpret(text,{},DialogueState(),TurnBudget()))
    after,_=apply_transition(DialogueState(),intent,'m',text,'a',None)
    assert [o.quote for o in after.observations]==[quote]


def test_negated_withdrawal_keeps_requirement():
    state=DialogueState(issue_id='i',phase='open',requirements=[
        Requirement(id='c',source_message_id='m',text='왜 그래?',kind='cause')])
    text='원인 설명은 취소하지 마.'
    intent=asyncio.run(TurnRouter(None).interpret(text,{},state,TurnBudget()))
    after,_=apply_transition(state,intent,'m2',text,'a',None)
    assert any(r.id=='c' for r in after.requirements)


@pytest.mark.parametrize('text,expected', [
    ('원인은 알려주고 해결 방법은 제외해줘.', {'remedy'}),
    ('원인은 취소하고 해결 방법을 알려줘.', {'cause'}),
    ('원인과 해결 방법은 취소할게.', {'cause','remedy'}),
])
def test_withdrawal_only_targets_its_own_requests(text,expected):
    from app.turn_router import withdrawn_requirements
    assert withdrawn_requirements(text)==expected


def test_unrelated_resolution_does_not_close_cooking_issue(app_factory):
    with TestClient(app_factory(llm=Planner([decision('abstain')]))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'계량이 헷갈려.','problem')
        reply=message(client,sid,'러닝 문제는 해결됐어.','other')
        assert reply['diagnostics']['intent']=='out_of_scope'
        assert client.get(f'/sessions/{sid}').json()['dialogue']['phase']!='resolved'
