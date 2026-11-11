"""질문·문제 보고가 선택·시작·완료 같은 상태 변경이나 추천 검색으로 처리되지 않는지 검증합니다.

2026-11-10 코드 리뷰(V01~V05)에서 재현한 발화입니다. 실제 API·모델 호출은 없습니다.
"""
import pytest
from fastapi.testclient import TestClient

from test_api import app_factory, client, create_session, message, recommend, select_activity  # noqa: F401
from test_dialogue import Planner, decision

RECOMMEND = {'recommend', 'update_constraints'}


def state(client, sid):
    return client.get(f'/sessions/{sid}').json()


def record_count(client, sid):
    return client.get('/records', params={'session_id': sid}).json()['total']


def open_answered_issue(client, sid):
    """키 없는 흐름으로 문제를 열고 원문 안내까지 받은 상태를 만듭니다."""
    message(client, sid, '계량이 잘 안돼', 'open')
    answered = message(client, sid, '모르겠어요', 'answer')
    assert answered['issue_phase'] == 'answer_provided'
    return state(client, sid)['dialogue']['issue_id']


def clarify_plan(question):
    plan = decision('clarify')
    plan.update(question=question, options=['예', '아니요'])
    return plan


# V01: 선택
@pytest.mark.parametrize('text', ['2번 뒤집어도 좋아?', '3번 섞으면 거품이 보여'])
def test_count_phrase_is_not_candidate_selection(client, text):
    """'2번 뒤집어도'의 2번은 횟수입니다. 선택 활동을 바꾸지 않습니다."""
    sid = create_session(client)
    ids = recommend(client, sid)
    message(client, sid, '첫 번째 걸로 할게', 'pick')
    reply = message(client, sid, text, 'count')
    assert reply['diagnostics']['intent'] != 'select'
    assert reply['selected_activity_id'] == ids[0]


@pytest.mark.parametrize('text', ['2번이 좋아', '두 번째가 더 좋아 보여', '2번 보여줘', '2번 활동으로 할래',
                                  '2번 걸로 해주세요', '2번 걸로 해볼게요', '2번 걸로 시작할게요',
                                  '2번으로 하고 싶어요', '좋아 2번'])
def test_choice_words_right_after_ordinal_still_select(client, text):
    sid = create_session(client)
    ids = recommend(client, sid)
    reply = message(client, sid, text, 'pick')
    assert reply['diagnostics']['intent'] == 'select'
    assert reply['selected_activity_id'] == ids[1]


def test_count_question_is_explained_from_the_selected_activity(client):
    """'2번 뒤집어도 좋아?'의 2번은 후보 번호가 아닙니다. 설명 근거는 선택한 활동입니다."""
    sid = create_session(client)
    ids = recommend(client, sid)
    message(client, sid, '첫 번째 걸로 할게', 'pick')
    reply = message(client, sid, '2번 뒤집어도 좋아?', 'count')
    assert reply['diagnostics']['intent'] == 'explain'
    assert [source['activity_id'] for source in reply['sources']] == [ids[0]]


@pytest.mark.parametrize('text', ['2번 설명해줘', '두 번째 거 어때?', '2번은 어떤 요리야?'])
def test_candidate_question_is_explained_from_that_candidate(client, text):
    """후보를 가리켜 묻는 말은 선택 활동이 있어도 그 후보의 자료로 설명합니다."""
    sid = create_session(client)
    ids = recommend(client, sid)
    message(client, sid, '첫 번째 걸로 할게', 'pick')
    reply = message(client, sid, text, 'candidate')
    assert reply['diagnostics']['intent'] == 'explain'
    assert [source['activity_id'] for source in reply['sources']] == [ids[1]]


# V01: 시작
@pytest.mark.parametrize('text', ['어떻게 진행할지 모르겠어요', '버터가 녹기 시작해요'])
def test_start_word_inside_report_does_not_start_execution(client, text):
    sid = create_session(client); select_activity(client, sid)
    issue = open_answered_issue(client, sid)
    reply = message(client, sid, text, 'start-like')
    assert reply['diagnostics']['intent'] != 'start'
    assert reply['execution_id'] is None
    assert state(client, sid)['dialogue']['issue_id'] == issue


@pytest.mark.parametrize('text', ['어렵지만 시작할게요', '이제 만들기 시작할게요'])
def test_start_declaration_after_difficulty_or_gerund_still_starts(client, text):
    """어려움을 먼저 말했거나 '~기 시작할게'로 말해도, 하겠다는 뜻이 분명하면 시작입니다."""
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'start')
    assert reply['diagnostics']['intent'] == 'start'
    assert reply['execution_id']


def test_trouble_after_start_word_is_coached(client):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, '시작할 때부터 막혔어요', 'stuck')
    assert reply['diagnostics']['intent'] == 'coaching'
    assert reply['execution_id'] is None


# V01: 완료
def test_completion_after_past_difficulty_is_not_a_trouble_report(client):
    """'어려웠는데 완성했어요'는 문제 보고가 아닙니다. 완료 처리 분기가 받고 새 문제를 열지 않습니다."""
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, '처음엔 어려웠는데 완성했어요', 'done')
    assert reply['diagnostics']['intent'] == 'complete'
    assert state(client, sid)['dialogue']['issue_id'] is None


@pytest.mark.parametrize('text', ['이제 다 끝났나요', '다 했나'])
def test_completion_question_does_not_record(client, text):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'q-done')
    assert reply['diagnostics']['intent'] != 'complete'
    assert record_count(client, sid) == 0


@pytest.mark.parametrize('text', ['다 했는데 안 돼요', '끝냈는데 망했어'])
def test_trouble_after_completion_word_is_coached(client, text):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'trouble')
    assert reply['diagnostics']['intent'] == 'coaching'
    assert record_count(client, sid) == 0


# V01: 부정 표현
@pytest.mark.parametrize('text', ['시작부터 못 하겠어요. 너무 어려워요', '완료 못 했는데 반죽이 안 돼'])
def test_negation_inside_trouble_report_is_coached(client, text):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'neg')
    assert reply['diagnostics']['intent'] == 'coaching'
    assert state(client, sid)['dialogue']['issue_id']


def test_pending_answer_about_not_having_started_is_recorded(app_factory):
    llm = Planner([clarify_plan('반죽을 시작했나요?'), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q1')
        reply = message(client, sid, '아직 시작 못 했어요', 'q2')
        assert reply['diagnostics']['intent'] == 'coaching'
        assert len(state(client, sid)['dialogue']['observations']) == 2


# V02: 실패 보고·대체 재료 질문
def test_failure_report_mentioning_recipe_is_coached(client):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, '레시피대로 했는데 망했어', 'r')
    assert reply['diagnostics']['intent'] == 'coaching'
    assert not reply['diagnostics']['search_performed']


def test_substitute_ingredient_question_is_about_selected_activity(client):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, '설탕 대신 다른 거 넣어도 돼?', 'sub')
    assert reply['diagnostics']['intent'] == 'explain'
    assert state(client, sid)['candidate_ids'] == []


class RecordingInterpreter(Planner):
    """규칙에 걸리지 않은 말을 문제 보고로 해석하는 모델 대역."""
    configured = True

    def __init__(self):
        super().__init__([decision('abstain')])
        self.interpreted = []

    async def interpret_turn(self, text, context):
        self.interpreted.append(text)
        return {'decision': {'route': 'dialogue', 'transition': 'continue', 'observation_updates': [{'quote': text}]},
                'status': 'planned', 'llm_calls': 1, 'usage': {}}


def test_wish_to_give_up_is_interpreted_not_searched(app_factory):
    llm = RecordingInterpreter()
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        text = '반죽이 너무 질어서 포기하고 싶어'
        reply = message(client, sid, text, 'want')
        assert llm.interpreted == [text]
        assert reply['diagnostics']['intent'] == 'coaching'
        assert not reply['diagnostics']['search_performed']


def test_doubt_about_result_is_coached(client):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, '이게 먹을 만한지 모르겠어', 'doubt')
    assert reply['diagnostics']['intent'] == 'coaching'
    assert not reply['diagnostics']['search_performed']


@pytest.mark.parametrize('text', ['망했어. 다른 요리 추천해줘', '김치찌개가 먹고 싶어', '망했어. 김치찌개 레시피 알려줘'])
def test_explicit_dish_request_still_recommends(client, text):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'dish')
    assert reply['diagnostics']['intent'] in RECOMMEND
    assert reply['diagnostics']['search_performed']


@pytest.mark.parametrize('text', ['쿠키를 만들어보고 싶어요', '빵을 굽고 싶어', '간단한 요리를 하고 싶어요'])
def test_wish_to_cook_something_else_recommends(client, text):
    """'만들어보고 싶어요'처럼 요리를 하고 싶다는 말은 선택 활동의 상태 보고가 아니라 추천 요청입니다."""
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'wish')
    assert reply['diagnostics']['intent'] in RECOMMEND
    assert reply['diagnostics']['search_performed']


# V03: 주제 밖 단어
@pytest.mark.parametrize('text', ['주식이 망했어요', '러닝은 너무 어려워'])
def test_complaint_about_an_off_topic_subject_stays_out_of_scope(client, text):
    """주제 밖 낱말이 주어이고 다른 내용이 없는 말은 선택 활동의 문제 보고가 아닙니다."""
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'off')
    assert reply['diagnostics']['intent'] == 'out_of_scope'
    assert state(client, sid)['dialogue']['issue_id'] is None


def test_off_topic_word_from_the_selected_activity_is_in_scope(client):
    """단계에 '주식과 곁들임'이 나오는 활동에서는 '주식'이 주제 이탈이 아닙니다."""
    sid = create_session(client); select_activity(client, sid, 'coo-6-1')
    reply = message(client, sid, '주식은 뭘로 골라야 해?', 'staple')
    assert reply['diagnostics']['intent'] == 'explain'


@pytest.mark.parametrize('text', ['날씨가 습해서 반죽이 안 돼', '커피를 넣었더니 망했어'])
def test_trouble_report_with_off_topic_word_is_coached(client, text):
    sid = create_session(client); select_activity(client, sid)
    reply = message(client, sid, text, 'n01')
    assert reply['diagnostics']['intent'] == 'coaching'
    assert state(client, sid)['dialogue']['issue_id']


def test_pending_answer_with_off_topic_word_is_recorded(app_factory):
    llm = Planner([clarify_plan('주변이 습했나요?'), decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 잘 안돼', 'q1')
        reply = message(client, sid, '네 날씨가 많이 습했어요', 'q2')
        assert reply['diagnostics']['intent'] == 'coaching'
        assert len(state(client, sid)['dialogue']['observations']) == 2


def test_follow_up_observation_with_off_topic_word_is_recorded(app_factory):
    """답변을 받은 뒤 덧붙인 관찰('날씨가 습한 날이었어요')도 주제 이탈이 아닙니다."""
    with TestClient(app_factory(llm=Planner([decision('abstain'), decision('abstain')]))) as client:
        sid = create_session(client); select_activity(client, sid)
        assert message(client, sid, '계량이 헷갈려.', 'problem')['issue_phase'] == 'answer_provided'
        reply = message(client, sid, '날씨가 습한 날이었어요', 'follow-up')
        assert reply['diagnostics']['intent'] == 'coaching'
        assert state(client, sid)['dialogue']['observations'][-1]['quote'] == '날씨가 습한 날이었어요'


def test_off_topic_completion_with_open_issue_stays_out_of_scope(app_factory):
    """문제가 열려 있어도 '러닝 끝냈어'는 선택해 둔 요리 활동의 완료나 관찰이 아닙니다."""
    with TestClient(app_factory(llm=Planner([decision('abstain')]))) as client:
        sid = create_session(client); select_activity(client, sid)
        message(client, sid, '계량이 헷갈려.', 'problem')
        reply = message(client, sid, '러닝 끝냈어', 'running')
        assert reply['diagnostics']['intent'] == 'out_of_scope'
        assert record_count(client, sid) == 0


# V04: 설명 분기
@pytest.mark.parametrize('text', ['추운데 따뜻한 국물 요리 추천해줘', '쉽게 만들 수 있는 요리 추천해줘'])
def test_recommendation_request_with_explain_like_word_recommends(client, text):
    sid = create_session(client)
    reply = message(client, sid, text, 'q4')
    assert reply['diagnostics']['intent'] in RECOMMEND
    assert reply['diagnostics']['search_performed']


# V05: 키 없음·모델 해석 실패
def test_keyless_dish_name_with_answered_issue_recommends(client):
    sid = create_session(client); select_activity(client, sid)
    open_answered_issue(client, sid)
    reply = message(client, sid, '김치볶음밥', 'dish')
    assert reply['diagnostics']['intent'] in RECOMMEND
    assert reply['diagnostics']['search_performed']


class FailingInterpreter(Planner):
    """모델 해석이 공급자 오류로 끝나는 경우."""
    configured = True

    async def interpret_turn(self, text, context):
        return {'decision': None, 'status': 'provider_error', 'llm_calls': 1, 'usage': {}}


def test_failed_interpretation_does_not_open_issue(app_factory):
    with TestClient(app_factory(llm=FailingInterpreter([]))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '김치볶음밥', 'dish')
        dialogue = state(client, sid)['dialogue']
        assert dialogue['issue_id'] is None and not dialogue['observations']
        assert reply['mode'] == 'insufficient_evidence'
        assert reply['diagnostics']['stop_reason'] == 'execution_failure'
        assert reply['diagnostics']['stop_detail'] == 'provider_error'


def test_oversized_context_in_interpretation_reports_context_limit(app_factory):
    """맥락 초과는 다시 보내도 같은 결과입니다. '잠시 후 다시'가 아니라 맥락 한도로 안내합니다."""
    class TooLong(Planner):
        configured = True

        async def interpret_turn(self, text, context):
            return {'decision': None, 'status': 'context_too_long', 'llm_calls': 0, 'usage': {}}

    with TestClient(app_factory(llm=TooLong([]))) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '김치볶음밥', 'dish')
        assert reply['diagnostics']['stop_reason'] == 'context_limit'
        assert '잠시 후' not in reply['text']
        assert state(client, sid)['dialogue']['issue_id'] is None
