"""Regressions observed in the actual Claude/GPU run, using the real API/store."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from test_api import app_factory, create_session, select_activity, message
from test_dialogue import Planner, decision, ask_plan
from app.dialogue_models import DialogueState, Requirement, TurnInterpretation
from app.dialogue_state import apply_transition
from app.turn_router import TurnRouter
from app.turn_budget import TurnBudget


class ReportsSupported(Planner):
    async def decide(self, question, context, evidence):
        row = evidence[0]
        return {'decision': {**decision('answer'), 'evidence_ids': [row['evidence_id']],
            'coverage': [{'requirement_id': r['id'], 'status': 'supported',
                'evidence_ids': [row['evidence_id']],
                'quotes': [{'evidence_id': row['evidence_id'], 'quote': row['excerpt']}]}
                for r in context['dialogue']['requirements']]}, 'llm_calls': 1}


def test_policy_coverage_survives_persistence_and_certainty(app_factory):
    with TestClient(app_factory(llm=ReportsSupported([]))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '계량이 잘 안돼. 어느 관찰을 확인하면 좋을까?', 'first')
        stored = client.get(f'/sessions/{sid}').json()['dialogue']
        assert stored['coverage'][0]['status'] == 'missing_knowledge'
        assert not reply['metacognition']['supported']
    with TestClient(app_factory()) as client:
        meta = message(client, sid, '확실해?', 'certainty')['metacognition']
        assert meta['unresolved'] == reply['metacognition']['unresolved']
        assert not meta['supported']


def test_certainty_reconciles_old_supported_snapshot(app_factory):
    app = app_factory()
    session = app.state.store.create_session()
    session['selected_activity_id'] = 'cook-0-2'
    session['dialogue'].update(issue_id='old', activity_id='cook-0-2', phase='answer_provided',
        requirements=[{'id':'r','text':'왜 실패했어?','kind':'cause','source_message_id':'m'}],
        coverage=[{'requirement_id':'r','status':'supported','evidence_ids':['cook-0-2:steps'],
                   'quotes':[{'evidence_id':'cook-0-2:steps','quote':'물로 큰술(15ml)과 작은술(5ml)의 차이를 먼저 확인해요'}]}])
    app.state.store.save_session(session)
    with TestClient(app) as client:
        reply = message(client, session['session_id'], '확실해?', 'meta')
        assert not reply['metacognition']['supported']
        stored = client.get(f"/sessions/{session['session_id']}").json()['dialogue']
        assert stored['coverage'][0]['status'] == 'missing_knowledge'


@pytest.mark.parametrize('pending', [True, False])
@pytest.mark.parametrize('procedure_text', ['원문 확인 기준','원문 절차','작업 순서'])
def test_compound_request_tracks_every_part_with_or_without_pending(app_factory, pending, procedure_text):
    llm = Planner([ask_plan() if pending else decision('abstain'), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid, 'cn-0-1')
        message(client, sid, '중식 양념 소량 계량의 과정이 헷갈려.', 'first')
        reply = message(client, sid, f'원인과 해결 방법, {procedure_text}를 모두 설명해줘.', 'compound')
        state = client.get(f'/sessions/{sid}').json()['dialogue']
        assert {'cause','remedy','procedure'} <= {r['kind'] for r in state['requirements']}
        ids = {r['id'] for r in state['requirements'] if r['kind'] in {'cause','remedy'}}
        assert ids <= {r['requirement_id'] for r in reply['metacognition']['unresolved']}
        assert reply['diagnostics']['intent'] == 'coaching'


def test_title_containing_comparison_still_opens_problem(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid = create_session(client); select_activity(client, sid, 'cook-0-12')
        reply = message(client, sid, '깨 으깨 향 비교하기에서 막혔어.', 'problem')
        assert reply['issue_id'] and reply['mode'] == 'clarify'
        assert reply['diagnostics'].get('stop_reason') is None


def test_comparison_request_without_trouble_remains_lookup():
    result = asyncio.run(TurnRouter(None).interpret('1번이랑 2번 비교해줘', {}, DialogueState(), TurnBudget()))
    assert result.route == 'lookup'


def test_requirement_capacity_fails_explicitly_instead_of_dropping():
    state = DialogueState(issue_id='i', requirements=[Requirement(id=str(i), text=str(i), source_message_id='m') for i in range(8)])
    interpretation = TurnInterpretation(route='dialogue', transition='continue',
        requirement_proposals=[{'text':'새 요청','kind':'lookup','source_quote':'새 요청'}])
    with pytest.raises(ValueError, match='context_limit'):
        apply_transition(state, interpretation, 'new', '새 요청', 'a', None)


@pytest.mark.parametrize('pending', [True, False])
@pytest.mark.parametrize('question', ['2단계는 어떻게 하는지 알려줘','작업 순서는 어떻게 되는지 알려줘'])
def test_stage_how_question_preserves_existing_problem(app_factory, pending, question):
    llm=Planner([ask_plan() if pending else decision('abstain'), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'계량이 잘 안돼','problem')
        before=client.get(f'/sessions/{sid}').json()['dialogue']
        result=message(client,sid,question,'stage')
        after=client.get(f'/sessions/{sid}').json()['dialogue']
        assert result['diagnostics']['intent']=='explain'
        if question.startswith('2단계'):
            assert result['sources'][0]['excerpt'].startswith('2단계:')
        assert after==before
