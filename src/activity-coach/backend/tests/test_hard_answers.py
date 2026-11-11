"""Hard-answer regressions: original intent, abstention and follow-up context."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.answer_policy import AnswerPolicy
from app.dialogue_models import DialogueState
from app.dialogue_state import build_issue_context
from app.turn_budget import TurnBudget
from test_answer_policy import evidence
from test_claim_validation import Claims
from test_api import app_factory, create_session, message, select_activity


@pytest.mark.parametrize('question', [
    '간장 없이 만들되 원문의 재료와 양은 바꾸지 말고 방법을 알려줘.',
    '원문을 참고해서 계량 방법을 설명해줘.',
    '원문을 보여주지 말고 계량 방법만 설명해줘.',
    '원문을 그대로 따르면서 간장 없이 만드는 방법을 알려줘.',
    '원문 보여주지 마. 계량 방법만 설명해줘.',
])
def test_source_mentions_do_not_force_original_dump(question):
    result = asyncio.run(AnswerPolicy().answer(question, [evidence()],
        {'allowed_activity_ids': ['a']}, [], Claims(), TurnBudget()))
    assert result['mode'] == 'generated'


@pytest.mark.parametrize('question', ['계량 원문 보여줘', '원문을 그대로 출력해줘', '원문만 읽어줘'])
def test_explicit_original_request_preserves_the_original(question):
    result = asyncio.run(AnswerPolicy().answer(question, [evidence()],
        {'allowed_activity_ids': ['a']}, [], Claims(), TurnBudget()))
    assert result['mode'] == 'reference' and result['llm_calls'] == 0
    assert evidence()['excerpt'] in result['text']


def test_assessment_explains_its_own_limit_without_listing_recipes():
    row = evidence()
    row['excerpt'] += '\n준비물: 간장, 설탕\n자기 점검: 가루를 깎아서 계량했다'
    result = asyncio.run(AnswerPolicy().answer('두 번 성공했으니 요리 실력이 중급이라고 평가해도 돼?',
        [row], {'allowed_activity_ids': ['a']}, [], Claims(), TurnBudget()))
    assert result['mode'] == 'reference' and result['llm_calls'] == 0
    assert '실력' in result['text'] and '확정할 수 없' in result['text']
    assert '원인을' not in result['text'] and '준비물:' not in result['text']
    assert result['sources'] == [row]


def test_rejected_answer_gives_a_bounded_abstention_and_keeps_sources():
    row = evidence()
    result = asyncio.run(AnswerPolicy().answer('밥숟가락도 15ml로 보면 돼?', [row],
        {'allowed_activity_ids': ['a']}, [], Claims('insufficient'), TurnBudget()))
    assert result['mode'] == 'reference'
    assert result['validation']['semantic_support'] == 'failed'
    assert '확인' in result['text'] and '보류' in result['text']
    assert row['excerpt'] not in result['text']
    assert result['sources'] == [row]


class ContextualClaims:
    configured = True
    model = 'test-grounded'

    async def explain_claims(self, question, rows, context):
        previous = [m['text'] for m in context.get('recent_messages', []) if m['role'] == 'user']
        if '그건' in question:
            text = '큰술은 작은술 3번입니다.' if previous == ['큰술과 작은술 계량은 어떻게 해?'] else '무엇을 비교하는지 확인이 필요합니다.'
        elif '쓰지 않고' in question:
            text = '원문은 간장을 사용하므로 간장을 제외하면서 원문 재료를 유지할 수는 없습니다.'
        else:
            text = '큰술은 15ml, 작은술은 5ml입니다.'
        row = rows[0]
        return {'claims': [{'text': text, 'evidence_ids': [row['evidence_id']],
            'quotes': [{'evidence_id': row['evidence_id'], 'quote': row['excerpt']}]}],
            'status': 'received', 'llm_calls': 1, 'usage': {}}

    async def verify_claims(self, claims, rows, context):
        return {'verdicts': [{'claim_index': i, 'verdict': 'supported', 'evidence_ids': c['evidence_ids']}
            for i, c in enumerate(claims)], 'status': 'received', 'llm_calls': 1, 'usage': {}}


def test_contradictory_material_request_reaches_answer_policy(app_factory):
    with TestClient(app_factory(llm=ContextualClaims())) as client:
        sid = create_session(client)
        select_activity(client, sid)
        reply = message(client, sid, '간장 드레싱을 간장은 전혀 쓰지 않고 만들고 싶어. 원문의 재료와 양은 하나도 바꾸지 말고 방법을 알려줘.', 'contradiction')
    assert reply['mode'] == 'generated'
    assert '유지할 수는 없' in reply['text']
    assert reply['execution_id'] is None and reply['current_step'] == 1


def test_followup_context_survives_restart_and_request_replay(app_factory):
    with TestClient(app_factory(llm=ContextualClaims())) as client:
        sid = create_session(client)
        select_activity(client, sid)
        first = message(client, sid, '큰술과 작은술 계량은 어떻게 해?', 'first')
        replay = message(client, sid, '큰술과 작은술 계량은 어떻게 해?', 'first')
        assert replay['message_id'] == first['message_id'] and replay['diagnostics']['llm_calls'] == 0
    with TestClient(app_factory(llm=ContextualClaims())) as restarted:
        reply = message(restarted, sid, '그건 작은술로 몇 번이야?', 'followup')
        assert reply['mode'] == 'generated' and '3번' in reply['text']
        state = restarted.get(f'/sessions/{sid}').json()
        assert len(state['messages']) == 4
        assert state['execution_id'] is None and state['current_step'] == 1


def test_returning_to_activity_does_not_restore_old_reference_context(app_factory):
    with TestClient(app_factory(llm=ContextualClaims())) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, '큰술과 작은술 계량은 어떻게 해?', 'first')
        select_activity(client, sid, 'cook-3-3')
        select_activity(client, sid, 'cook-0-2')
        reply = message(client, sid, '그건 작은술로 몇 번이야?', 'after-switch')
    assert '확인' in reply['text'] and '3번' not in reply['text']


def test_repeated_assessment_question_is_not_a_performance_report(app_factory):
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        select_activity(client, sid)
        question = '간장 드레싱을 두 번 성공했어. 이제 요리 실력이 중급이라고 평가해도 되지?'
        first = message(client, sid, question, 'assessment')
        before = client.get(f'/sessions/{sid}').json()['dialogue']
        repeated = message(client, sid, question, 'assessment-again')
        after = client.get(f'/sessions/{sid}').json()['dialogue']
    assert repeated['mode'] == first['mode'] == 'clarify'
    assert after['pending_question'] == before['pending_question']
    assert after['outcome'] == before['outcome']
    assert after['observations'] == before['observations']


@pytest.mark.parametrize('answer', [
    '예, 가루를 평평하게 깎아서 계량했어. 피드백해줘.',
    '가루를 깎아서 계량했어. 내 실력에 피드백해줘.',
    '모르겠어요. 피드백해줘.',
])
def test_performance_report_with_feedback_request_is_preserved(app_factory, answer):
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        select_activity(client, sid)
        message(client, sid, '내 실력을 평가해줘.', 'assessment')
        reply = message(client, sid, answer, 'report')
        state = client.get(f'/sessions/{sid}').json()['dialogue']
    assert reply['mode'] == 'reference' and state['pending_question'] is None
    assert state['outcome']['quote'] == answer
    assert state['observations'][-1]['quote'] == answer


@pytest.mark.parametrize('boundary', ['selected_activity_id', 'execution_id', 'issue_id'])
def test_recent_context_stops_at_activity_execution_or_issue_boundary(boundary):
    response = {'selected_activity_id': 'a', 'execution_id': None, 'issue_id': None}
    response[boundary] = 'previous'
    session = {'selected_activity_id': 'a', 'execution_id': None, 'messages': [
        {'role': 'user', 'text': 'old question'},
        {'role': 'assistant', 'text': 'old answer', 'response': response},
    ]}
    context = build_issue_context(DialogueState(), session)
    assert context.get('recent_messages', []) == []
