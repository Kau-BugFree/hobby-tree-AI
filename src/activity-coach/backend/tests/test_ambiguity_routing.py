"""Keyword-free reports and vague turns: classify or ask, never guess a recommendation."""
import pytest
from fastapi.testclient import TestClient

from test_api import app_factory, create_session, message, select_activity  # noqa: F401
from test_dialogue import Planner, decision


def clarify(index):
    plan = decision('clarify')
    plan.update(question=f'확인 질문 {index}?', options=['예', '아니요'])
    return plan


class Interpreter(Planner):
    """Scripted intent model: one fixed route for every turn the rules leave open."""

    def __init__(self, route, plans=()):
        super().__init__(plans)
        self.route, self.interpreted = route, []

    async def interpret_turn(self, text, context):
        self.interpreted.append(text)
        updates = [{'quote': text}] if self.route == 'dialogue' else []
        return {'decision': {'route': self.route, 'transition': 'continue', 'observation_updates': updates},
                'status': 'planned', 'llm_calls': 1, 'usage': {}}


def session_of(client, sid):
    return client.get(f'/sessions/{sid}').json()


@pytest.mark.parametrize('text', ['탔어', '가루가 뭉쳤어', '물이 넘쳤어요', '덩어리가 남아 있어'])
def test_declarative_report_ending_is_an_observation(text):
    from app.turn_router import reported_observation
    assert reported_observation(text)


@pytest.mark.parametrize('text', ['알겠어', '네 알겠어요', '내일 시간 있어', '맛있겠어'])
def test_acknowledgement_or_present_tense_is_not_an_observation(text):
    """'겠'·'있'은 받침이 ㅆ이지만 과거 보고가 아닙니다."""
    from app.turn_router import reported_observation
    assert not reported_observation(text)


def test_acknowledgement_with_selected_activity_stays_a_greeting(app_factory):
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '알겠어', 'ack')
        assert reply['diagnostics']['intent'] == 'greeting'
        assert session_of(client, sid)['dialogue']['issue_id'] is None


@pytest.mark.parametrize('text', ['나 이거 먹고 싶어', '그거 10분 안에 돼?'])
def test_deictic_dish_request_without_activity_still_searches(app_factory, text):
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        reply = message(client, sid, text, 'dish')
        assert reply['diagnostics']['intent'] in {'recommend', 'update_constraints'}
        assert reply['diagnostics']['search_performed']


def test_hedge_word_inside_another_word_is_not_unknown():
    from app.dialogue_state import UNKNOWN
    assert not UNKNOWN.search('아마존에서 산 계량컵을 썼어')
    assert UNKNOWN.search('아마도') and UNKNOWN.search('아마 넣었을걸')


def test_ability_statement_is_not_an_observation():
    from app.turn_router import reported_observation
    assert not reported_observation('전자레인지로도 할 수 있어')


@pytest.mark.parametrize('text', ['맛이 없어', '너무 짜', '탔어', '반죽이 질어', '물이 넘쳐', '뭔가 이상해',
                                  '소금을 너무 많이 넣은 것 같아', '불 조절을 못하겠어'])
def test_keywordless_report_about_selected_activity_opens_issue_without_model(app_factory, text):
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, text, 'report')
        session = session_of(client, sid)
        assert reply['diagnostics']['intent'] == 'coaching' and reply['mode'] == 'clarify'
        assert not reply['diagnostics']['search_performed'] and session['candidate_ids'] == []
        assert session['dialogue']['observations'][0]['quote'] == text


@pytest.mark.parametrize('text', ['드레싱이 자꾸 분리돼', '계량스푼 눈금이 안 보여'])
def test_statement_ending_like_a_question_or_request_word_is_still_a_report(app_factory, text):
    """'분리돼'의 '돼'는 허락을 묻는 말이 아니고, '안 보여'의 '보여'는 보여 달라는 말이 아닙니다."""
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, text, 'report')
        assert reply['diagnostics']['intent'] == 'coaching'
        assert session_of(client, sid)['dialogue']['observations'][0]['quote'] == text


@pytest.mark.parametrize('text', ['이거 넣어도 돼', '이렇게 하면 돼', '원문 보여줘'])
def test_permission_question_and_show_request_stay_explanations(app_factory, text):
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, text, 'ask')
        assert reply['diagnostics']['intent'] == 'explain'
        assert session_of(client, sid)['dialogue']['issue_id'] is None


@pytest.mark.parametrize('text', ['3단계에서 설탕이 안 녹고 바닥에 가라앉아 있어',
                                  '가루 재료를 계량스푼에 수북하게 담았더니 양이 매번 달라', '재료를 다 넣었어'])
def test_report_mentioning_a_lookup_word_is_not_a_lookup(app_factory, text):
    """'3단계', '재료'라는 말이 들어 있어도 상태를 말하는 문장은 단계·준비물 조회가 아닙니다."""
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, text, 'report')
        session = session_of(client, sid)
        assert reply['diagnostics']['intent'] == 'coaching' and session['current_step'] == 1
        assert session['dialogue']['observations'][0]['quote'] == text


def test_step_named_in_a_report_becomes_the_issue_step_not_progress(app_factory):
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '3단계에서 설탕이 안 녹고 바닥에 가라앉아 있어', 'report')
        session = session_of(client, sid)
        assert session['dialogue']['step_number'] == 3 and session['current_step'] == 1


@pytest.mark.parametrize('text', ['재료를 알려줘', '3단계 알려줘', '준비물', '다음 단계', '재료가 뭐가 필요해', '재료를 정리해 줘'])
def test_lookup_request_is_still_answered_directly(app_factory, text):
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, text, 'lookup')
        assert reply['diagnostics']['intent'] == 'activity_lookup'
        assert session_of(client, sid)['dialogue']['issue_id'] is None


@pytest.mark.parametrize('text', ['김치볶음밥', '오이 무침', '계란말이 김밥'])
def test_dish_name_with_selected_activity_still_searches_without_model(app_factory, text):
    with TestClient(app_factory()) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, text, 'dish')
        assert reply['diagnostics']['intent'] == 'recommend' and reply['diagnostics']['search_performed']
        assert session_of(client, sid)['dialogue']['issue_id'] is None


def test_model_dialogue_route_opens_issue_for_keywordless_report(app_factory):
    llm = Interpreter('dialogue', [clarify(1)])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '맛이 없어', 'report')
        assert llm.interpreted == ['맛이 없어']
        assert reply['diagnostics']['intent'] == 'coaching' and reply['text'] == '확인 질문 1?'
        assert session_of(client, sid)['dialogue']['observations'][0]['quote'] == '맛이 없어'


def test_model_recommend_route_runs_the_search(app_factory):
    with TestClient(app_factory(llm=Interpreter('recommend'))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '맛이 없어', 'dish')
        assert reply['diagnostics']['intent'] == 'recommend' and reply['diagnostics']['search_performed']
        assert session_of(client, sid)['dialogue']['issue_id'] is None


def test_model_out_of_scope_route_declines(app_factory):
    with TestClient(app_factory(llm=Interpreter('out_of_scope'))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '맛이 없어', 'scope')
        assert reply['diagnostics']['intent'] == 'out_of_scope' and reply['mode'] == 'clarify'
        assert not reply['diagnostics']['search_performed']


@pytest.mark.parametrize('selected', [True, False])
def test_model_unclear_route_asks_what_the_user_needs(app_factory, selected):
    with TestClient(app_factory(llm=Interpreter('unclear'))) as client:
        sid = create_session(client)
        if selected:
            select_activity(client, sid)
        reply = message(client, sid, '음 저기', 'vague')
        assert reply['mode'] == 'clarify' and '어떤 도움' in reply['text']
        assert reply['diagnostics']['intent'] == 'ambiguous'
        assert reply['diagnostics']['stop_reason'] == 'ambiguous_intent'
        assert not reply['diagnostics']['search_performed']
        assert session_of(client, sid)['dialogue']['issue_id'] is None


def test_model_is_not_consulted_for_explicit_recommendation(app_factory):
    llm = Interpreter('dialogue')
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '간단한 요리 추천해줘', 'explicit')
        assert llm.interpreted == [] and reply['diagnostics']['intent'] == 'recommend'


def test_model_report_before_activity_selection_asks_which_activity(app_factory):
    with TestClient(app_factory(llm=Interpreter('dialogue'))) as client:
        sid = create_session(client)
        reply = message(client, sid, '맛이 없어', 'report')
        assert reply['mode'] == 'clarify' and '활동' in reply['text']
        assert not reply['diagnostics']['search_performed']


def test_model_recommend_route_inside_open_issue_runs_the_search(app_factory):
    llm = Interpreter('recommend', [decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'open')
        reply = message(client, sid, '음 저기', 'vague')
        assert llm.interpreted == ['음 저기']
        assert reply['diagnostics']['intent'] == 'recommend' and reply['diagnostics']['search_performed']
        assert reply['diagnostics'].get('stop_reason') != 'ambiguous_intent'


def test_model_unclear_route_inside_open_issue_keeps_observations(app_factory):
    llm = Interpreter('unclear', [decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'open')
        before = session_of(client, sid)['dialogue']['observations']
        reply = message(client, sid, '음 저기', 'vague')
        assert reply['mode'] == 'clarify' and '어떤 도움' in reply['text']
        assert session_of(client, sid)['dialogue']['observations'] == before


def test_deictic_question_without_activity_asks_which_activity(app_factory):
    with TestClient(app_factory()) as client:
        sid = create_session(client)
        reply = message(client, sid, '이거 어떻게 해?', 'deictic')
        assert reply['mode'] == 'clarify' and '활동' in reply['text']
        assert not reply['diagnostics']['search_performed'] and reply['sources'] == []


def test_exhausted_clarifications_fall_back_to_source_reference(app_factory):
    llm = Planner([clarify(1), clarify(2), clarify(3)])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q1')
        message(client, sid, '눈금은 확인했어', 'q2')
        third = message(client, sid, '수평은 안 맞췄어', 'q3')
        assert third['mode'] == 'reference' and third['cited_evidence_ids']
        assert third['diagnostics']['stop_reason'] == 'missing_observation'
        assert third['diagnostics']['clarification_count'] == 2


def test_planner_is_told_remaining_clarifications(app_factory):
    llm = Planner([clarify(1), clarify(2), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        for index, text in enumerate(['계량이 잘 안돼', '눈금은 확인했어', '수평은 안 맞췄어']):
            message(client, sid, text, f'q{index}')
        assert [c['workflow']['remaining_clarifications'] for c in llm.contexts] == [2, 1, 0]


@pytest.mark.parametrize('text', ['몰라', '글쎄', '아마도'])
def test_hedged_answer_to_pending_question_is_unknown(app_factory, text):
    llm = Planner([clarify(1), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q1')
        message(client, sid, text, 'q2')
        assert session_of(client, sid)['dialogue']['observations'][-1]['status'] == 'unknown'


def test_other_part_asks_before_replacing_open_issue(app_factory):
    llm = Planner([decision('abstain'), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        first = message(client, sid, '계량이 잘 안돼', 'q1')
        reply = message(client, sid, '다른 부분도 안돼', 'q2')
        assert reply['mode'] == 'clarify' and reply['clarification']['purpose'] == 'transition'
        assert reply['issue_id'] == first['issue_id']
