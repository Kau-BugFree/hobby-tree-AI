import importlib
import json

import pytest

from app.store import Store, StoreError


def modules():
    return importlib.import_module('app.dialogue_state'), importlib.import_module('app.dialogue_models')


def legacy():
    return {'version': 1, 'status': 'awaiting_clarification', 'clarification_count': 2,
            'pending_question': {'question_id': 'q', 'text': '눈금?', 'options': ['예', '아니요']},
            'observations': [{'source_message_id': 'm0', 'raw_text': '2', 'question': '온도?', 'answer': '차가움'}]}


def test_legacy_pending_question_and_ids_survive_upgrade():
    state, _ = modules()
    session = {'session_id': 's', 'selected_activity_id': 'a', 'execution_id': None}
    one = state.normalize_dialogue(legacy(), session)
    two = state.normalize_dialogue(legacy(), session)
    assert one == two
    assert one.pending_question['question_id'] == 'q'
    assert one.pending_question['options'] == ['예', '아니요']
    assert one.clarification_count == 2
    assert one.observations[0].answer_text == '차가움'
    assert one.observations[0].provenance == 'legacy'


def test_partial_correction_preserves_other_observations():
    state, model = modules()
    current = model.DialogueState(issue_id='i', activity_id='a', phase='open', observations=[
        model.Observation(id=str(i), source_message_id='old', quote=str(i)) for i in range(5)])
    updated, archived = state.apply_transition(current, model.TurnInterpretation(route='dialogue', transition='correct',
        observation_updates=[{'quote': '뜨거워', 'supersedes': ['0'], 'status': 'reported'}]), 'm', '사실 뜨거워', 'a', None)
    assert current.observations[0].status == 'reported'
    assert updated.observations[0].status == 'superseded'
    assert all(o.status == 'reported' for o in updated.observations[1:])
    assert not archived


def test_option_answer_keeps_question_after_next_question():
    state, model = modules()
    current = model.DialogueState(issue_id='i', phase='awaiting_observation', pending_question={
        'question_id': 'q', 'text': '온도?', 'options': ['뜨거움', '차가움'], 'purpose': 'clarification'})
    updated, _ = state.apply_transition(current, model.TurnInterpretation(route='dialogue', transition='continue'),
                                      'm', '2번', 'a', None)
    updated.pending_question = {'question_id': 'q2', 'text': '색?', 'options': []}
    fact = updated.observations[-1]
    assert fact.question_snapshot['question_id'] == 'q'
    assert fact.selected_option_index == 1 and fact.answer_text == '차가움'
    assert fact.quote == '2번' and fact.source_message_id == 'm'


def test_answer_is_not_resolution():
    state, model = modules()
    serialized = state.serialize_dialogue(model.DialogueState(phase='answer_provided'))
    assert serialized['status'] == 'answered' and serialized['phase'] != 'resolved'


def test_unknown_future_version_is_not_overwritten(tmp_path):
    store = Store(tmp_path/'db')
    session = store.create_session()
    payload = json.dumps({**session, 'dialogue': {'version': 999}})
    with store.connection() as db:
        db.execute('UPDATE sessions SET payload=?', (payload,))
    with pytest.raises(StoreError, match='version'):
        store.get_session(session['session_id'])
    with store.connection() as db:
        assert db.execute('SELECT payload FROM sessions').fetchone()[0] == payload


def test_snapshot_reply_transaction_rolls_back_together(tmp_path):
    store = Store(tmp_path/'db'); session = store.create_session()
    before = store.get_session(session['session_id'])
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_trace BEFORE INSERT ON traces BEGIN SELECT RAISE(ABORT, 'forced'); END")
    with pytest.raises(Exception, match='forced'):
        store.save_reply(session, 'r', 'f', {'message_id': 'a', 'text': 'reply', 'diagnostics': {'trace_id': 't'}},
                         'hello', issue_snapshots=[{'issue_id': 'i', 'phase': 'open'}])
    assert store.get_session(session['session_id']) == before
    with store.connection() as db:
        for table in ['requests', 'traces', 'dialogue_issues']:
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


def test_session_saved_by_another_process_is_not_overwritten(tmp_path):
    """같은 DB를 쓰는 다른 프로세스가 먼저 저장했으면 덮어쓰지 않고 409로 알립니다."""
    mine_store, their_store = Store(tmp_path/'db'), Store(tmp_path/'db')
    sid = mine_store.create_session()['session_id']
    mine, theirs = mine_store.get_session(sid), their_store.get_session(sid)
    theirs['selected_activity_id'] = 'cook-0-2'
    their_store.save_session(theirs)
    mine['constraints']['max_minutes'] = 20
    with pytest.raises(StoreError) as error:
        mine_store.save_session(mine)
    assert error.value.status == 409
    saved = mine_store.get_session(sid)
    assert saved['selected_activity_id'] == 'cook-0-2' and saved['constraints']['max_minutes'] is None


def test_same_writer_can_save_a_session_repeatedly(tmp_path):
    store = Store(tmp_path/'db'); session = store.create_session()
    for minutes in (10, 20):
        session['constraints']['max_minutes'] = minutes
        store.save_session(session)
    assert store.get_session(session['session_id'])['constraints']['max_minutes'] == 20


def test_reply_is_not_saved_over_a_newer_session(tmp_path):
    """응답 저장도 같은 검사를 거칩니다. 충돌하면 재전송 캐시도 남기지 않습니다."""
    mine_store, their_store = Store(tmp_path/'db'), Store(tmp_path/'db')
    sid = mine_store.create_session()['session_id']
    mine, theirs = mine_store.get_session(sid), their_store.get_session(sid)
    their_store.save_session(theirs)
    with pytest.raises(StoreError):
        mine_store.save_reply(mine, 'r', 'f', {'message_id': 'a', 'text': 'reply', 'diagnostics': {'trace_id': 't'}}, 'hello')
    with mine_store.connection() as db:
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0


def test_origin_lookup_uses_an_index(tmp_path):
    """세션 없는 요청마다 모든 세션의 JSON을 훑지 않습니다."""
    from app.store import ORIGIN_LOOKUP
    store = Store(tmp_path/'db')
    with store.connection() as db:
        plan = db.execute('EXPLAIN QUERY PLAN ' + ORIGIN_LOOKUP, ('x',)).fetchall()
    assert any('sessions_origin' in row[-1] for row in plan)


def test_concurrent_sessionless_requests_share_one_session(tmp_path):
    """같은 첫 요청이 여러 worker에 동시에 와도 세션은 하나만 생깁니다."""
    from concurrent.futures import ThreadPoolExecutor
    stores = [Store(tmp_path/'db') for _ in range(4)]
    with ThreadPoolExecutor(8) as pool:
        ids = set(pool.map(lambda i: stores[i % 4].session_for_origin('origin-1')['session_id'], range(16)))
    assert len(ids) == 1
    with stores[0].connection() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1


def test_execution_is_not_created_when_the_session_cannot_be_saved(tmp_path):
    """실행 생성과 세션 저장은 함께 확정됩니다. 세션 저장이 거부되면 연결되지 않은 실행이 남지 않습니다."""
    mine_store, their_store = Store(tmp_path/'db'), Store(tmp_path/'db')
    sid = mine_store.create_session()['session_id']
    activity = {'activity_id': 'a', 'title': '활동', 'steps': ['하나']}

    def attach_to(session):
        def attach(execution):
            session['execution_id'] = execution['execution_id']
            return session
        return attach

    stale = mine_store.get_session(sid)
    their_store.save_session(their_store.get_session(sid))
    with pytest.raises(StoreError):
        mine_store.start_execution(sid, activity, 'start-1', attach_to(stale))
    with mine_store.connection() as db:
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 0
    execution, created = mine_store.start_execution(sid, activity, 'start-1', attach_to(mine_store.get_session(sid)))
    assert created and mine_store.get_session(sid)['execution_id'] == execution['execution_id']


def test_unquoted_model_facts_and_unconfirmed_reset_are_rejected():
    state, model = modules()
    current = model.DialogueState(issue_id='old', phase='open', clarification_count=2)
    updated, archive = state.apply_transition(current, model.TurnInterpretation(route='dialogue', transition='new_issue',
        transition_quote='왜?', observation_updates=[{'quote': '거품이 남', 'status': 'reported'}]), 'm', '왜?', 'a', None)
    assert updated.issue_id == 'old' and updated.clarification_count == 2
    assert not archive and not any(o.quote == '거품이 남' for o in updated.observations)


def test_negated_new_issue_does_not_reset_budget():
    state,model=modules()
    current=model.DialogueState(issue_id='i',phase='open',clarification_count=2)
    updated,archive=state.apply_transition(current,model.TurnInterpretation(route='dialogue',transition='new_issue',
        transition_quote='새 문제'), 'm','새 문제가 아니야',None,None)
    assert updated.issue_id=='i' and updated.clarification_count==2 and not archive


def test_correction_invalidates_old_supported_coverage():
    state,model=modules()
    current=model.DialogueState(issue_id='i',phase='open',observations=[model.Observation(id='old',source_message_id='m',quote='차가움')],
        coverage=[model.RequirementCoverage(requirement_id='r',status='supported',evidence_ids=['e'])])
    updated,_=state.apply_transition(current,model.TurnInterpretation(route='dialogue',transition='correct',
        observation_updates=[{'quote':'뜨거움','supersedes':['old']}]),'m2','뜨거움',None,None)
    assert not any(c.status=='supported' for c in updated.coverage)


def test_same_facts_reuse_query_and_corrected_facts_change_key():
    from app.dialogue import DialogueCoach
    _,model=modules()
    one=model.DialogueState(observations=[model.Observation(id='1',source_message_id='m',quote='차가움')])
    two=one.model_copy(deep=True); two.observations[0].id='2'
    assert DialogueCoach._search_key(' 물   계량 ',one)==DialogueCoach._search_key('물 계량',two)
    two.observations[0].quote='뜨거움'
    assert DialogueCoach._search_key('물 계량',one)!=DialogueCoach._search_key('물 계량',two)


def test_explicit_new_issue_can_contain_a_question_and_negated_abandon_is_ignored():
    state,model=modules()
    current=model.DialogueState(issue_id='i',phase='open',clarification_count=2)
    updated,archives=state.apply_transition(current,model.TurnInterpretation(route='dialogue',transition='new_issue',
        transition_quote='새 문제야.'),'m','새 문제야. 팬에 왜 붙어?',None,None)
    assert updated.issue_id!='i' and updated.clarification_count==0 and archives
    kept,_=state.apply_transition(current,model.TurnInterpretation(route='dialogue',transition='abandon'),
                                  'm','문제 그만하지 마',None,None)
    assert kept.phase!='abandoned'


def test_confirmed_proposal_retains_original_quote_requirement_and_consent():
    state,model=modules()
    pending={'question_id':'q','text':'새 문제로 시작할까요?','options':['예','아니요'],'purpose':'transition',
        'proposed_goal':'차가운 물을 썼는데 왜 팬에 붙어?','source_message_id':'m1',
        'proposed_interpretation':{'route':'dialogue','transition':'new_issue','requirement_proposals':[
            {'text':'왜 팬에 붙어?','kind':'cause','source_quote':'왜 팬에 붙어?'}]}}
    current=model.DialogueState(issue_id='i',phase='awaiting_observation',pending_question=pending)
    updated,_=state.apply_transition(current,model.TurnInterpretation(route='dialogue',transition='new_issue'),'m2','예',None,None)
    assert updated.requirements[0].kind=='cause' and updated.requirements[0].source_message_id=='m1'
    assert updated.observations[0].quote==pending['proposed_goal'] and updated.observations[0].source_message_id=='m1'
    consent=updated.observations[-1]
    assert consent.question_snapshot['question_id']=='q' and consent.selected_option_index==0 and consent.source_message_id=='m2'
