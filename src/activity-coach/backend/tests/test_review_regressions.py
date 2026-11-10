"""Regressions found by the 2026-11-07 code review, using the real API/store/retrieval."""
import pytest
from fastapi.testclient import TestClient
from test_api import app_factory, create_session, select_activity, message
from test_dialogue import Planner, decision, ask_plan
from test_answer_policy import evidence, policy
from app.dialogue_models import Requirement
from app.turn_router import coaching_requirements


def dialogue(client, sid):
    return client.get(f'/sessions/{sid}').json()['dialogue']


def test_trouble_after_resolution_offers_new_issue_and_keeps_its_text(app_factory):
    llm = Planner([decision('abstain'), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q')
        resolved = message(client, sid, '해결됐어', 'resolve')
        offer = message(client, sid, '이번엔 반죽이 잘 안돼', 'next')
        assert offer['mode'] == 'clarify' and offer['clarification']['purpose'] == 'transition'
        assert offer['issue_id'] == resolved['issue_id'] and offer['issue_phase'] == 'resolved'
        accepted = message(client, sid, '예', 'accept')
        state = dialogue(client, sid)
        assert accepted['issue_id'] != resolved['issue_id']
        assert state['observations'][0]['quote'] == '이번엔 반죽이 잘 안돼'


def test_repeated_ambiguous_transition_explains_explicit_new_issue(app_factory):
    with TestClient(app_factory(llm=Planner([decision('abstain')]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q')
        message(client, sid, '다른 문제인가?', 'ask')
        message(client, sid, '아니요', 'decline')
        reply = message(client, sid, '다른 문제인가?', 'again')
        assert reply['diagnostics']['stop_reason'] == 'ambiguous_intent'
        assert '새 문제' in reply['text']


@pytest.mark.parametrize('pending', [True, False])
def test_self_assessment_request_is_not_an_observation(app_factory, pending):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid = create_session(client); select_activity(client, sid)
        if pending:
            message(client, sid, '계량이 잘 안돼', 'q')
        reply = message(client, sid, '내 실력 평가해줘', 'assess')
        assert reply['clarification']['purpose'] == 'reflection'
        assert '내 실력 평가해줘' not in [o['quote'] for o in dialogue(client, sid)['observations']]


def test_step_in_answer_to_pending_question_becomes_issue_step(app_factory):
    llm = Planner([ask_plan(), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q')
        message(client, sid, '3단계에서 넘쳤어', 'a')
        assert dialogue(client, sid)['step_number'] == 3
        assert llm.contexts[-1]['current_step'] == 3


@pytest.mark.parametrize('text,kind', [
    ('계량스푼은 어떻게 잡아?', 'procedure'),
    ('계량이 잘 안 되는데 어떻게 해?', 'remedy'),
    ('어떻게 고칠지 알려줘', 'remedy'),
])
def test_how_question_is_procedure_unless_fixing_a_problem(text, kind):
    assert [p['kind'] for p in coaching_requirements(text)] == [kind]


def test_abstain_is_reported_as_conflicting_evidence(app_factory):
    with TestClient(app_factory(llm=Planner([decision('abstain')]))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '계량이 잘 안돼', 'q')
        assert reply['diagnostics']['stop_reason'] == 'conflicting_evidence'
        assert reply['metacognition']['stop_reason'] == 'conflicting_evidence'


def test_retrieve_without_query_is_execution_failure(app_factory):
    with TestClient(app_factory(llm=Planner([decision('retrieve')]))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '계량이 잘 안돼', 'q')
        assert reply['diagnostics']['stop_reason'] == 'execution_failure'
        assert reply['diagnostics']['stop_detail'] == 'empty_or_repeated_query'


def test_retrieve_without_missing_target_is_execution_failure(app_factory, monkeypatch):
    plan = decision('retrieve'); plan['query'] = '계량'
    app = app_factory(llm=Planner([plan])); retrieval = app.state.retrieval
    bundle = retrieval.knowledge_for_activity('cook-0-2')
    retrieval.knowledge['extra'] = {**retrieval.knowledge['cook-0-2:steps'], 'knowledge_id': 'extra'}
    monkeypatch.setattr(retrieval, 'knowledge_for_activity', lambda aid: bundle)
    with TestClient(app) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '계량이 잘 안돼', 'q')
        assert reply['diagnostics']['stop_reason'] == 'execution_failure'
        assert reply['diagnostics']['stop_detail'] == 'missing_target_requirement'


def test_oversized_planner_input_is_context_limit(app_factory):
    class TooLong(Planner):
        async def decide(self, question, context, evidence):
            return {'decision': None, 'status': 'context_too_long', 'llm_calls': 0, 'usage': {}}
    with TestClient(app_factory(llm=TooLong([]))) as client:
        sid = create_session(client); select_activity(client, sid)
        assert message(client, sid, '계량이 잘 안돼', 'q')['diagnostics']['stop_reason'] == 'context_limit'


@pytest.mark.parametrize('question,requirements,disclaimer', [
    ('2단계를 쉽게 설명해줘', [], False),
    ('1번이랑 2번 비교해줘', [], False),
    ('왜 드레싱이 짜?', [], True),
    ('계량 기준', [Requirement(id='r', text='해결', kind='remedy', source_message_id='m')], True),
])
def test_cause_disclaimer_only_for_cause_or_remedy_requests(question, requirements, disclaimer):
    prepared = policy().prepare(question, [evidence()], {'allowed_activity_ids': ['a']}, requirements)
    text = policy().render_reference(prepared)['text']
    assert ('원인을 확정할 검토 자료는 없어요' in text) is disclaimer
    assert '참고 안내' in text


def test_certainty_text_lists_review_status_and_open_requests(app_factory):
    with TestClient(app_factory(llm=Planner([decision('abstain')]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 왜 잘 안돼?', 'q')
        reply = message(client, sid, '확실해?', 'sure')
        assert '사람 검토 전' in reply['text']
        assert '계량이 왜 잘 안돼?' in reply['text']
        assert '원인 확정' in reply['text']
