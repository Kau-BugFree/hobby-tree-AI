from fastapi.testclient import TestClient
from test_api import app_factory, create_session, select_activity, message
from test_dialogue import Planner, decision, ask_plan


def test_bundle_answers_without_second_retrieval(app_factory):
    plan=decision('answer'); plan['evidence_ids']=['cook-0-2:steps','cook-0-2:checks']
    llm=Planner([plan]); app=app_factory(llm=llm)
    with TestClient(app) as client:
        sid=create_session(client); select_activity(client,sid)
        reply=message(client,sid,'계량이 어려워','q')
        assert reply['diagnostics']['retrieval_calls']==1
        assert reply['issue_phase']=='answer_provided'
        assert len(llm.contexts[0]['dialogue']['evidence_ids'])==3
        assert reply['mode']=='reference'


def test_recommendation_failure_keeps_snapshot(app_factory):
    with TestClient(app_factory()) as client:
        sid=create_session(client)
        message(client,sid,'달걀 요리 활동 추천','r')
        old=client.get(f'/sessions/{sid}').json()['last_recommendation']
        message(client,sid,'1분 이내','none')
        assert client.get(f'/sessions/{sid}').json()['last_recommendation']==old
        reply=message(client,sid,'왜 추천했어?','reason')
        assert '이전' in reply['text'] and '적용한 조건: 1분' not in reply['text']


def test_reflection_answer_does_not_select_or_complete(app_factory):
    with TestClient(app_factory()) as client:
        sid=create_session(client); select_activity(client,sid)
        first=message(client,sid,'평가해줘','r')
        assert first['clarification']['purpose']=='reflection'
        reply=message(client,sid,'네','a')
        assert reply['execution_id'] is None and reply['selected_activity_id']=='cook-0-2'
        assert client.get(f'/sessions/{sid}').json()['dialogue']['outcome']['quote']=='네'


def test_diagnostics_contains_no_observation_quotes(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid=create_session(client); select_activity(client,sid)
        text='비밀관찰: 계량이 잘 안돼'
        reply=message(client,sid,text,'r')
        assert text not in str(reply['diagnostics'])


def test_replay_has_zero_operations_and_tokens(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'계량이 어려워','q')
        reply=message(client,sid,'계량이 어려워','q')
        assert all(reply['diagnostics'].get(k+'_calls',0)==0 for k in ['decision','retrieval','generation','verification'])
        assert reply['diagnostics'].get('usage',{})=={}


def test_lookup_during_issue_does_not_move_execution_step(app_factory):
    with TestClient(app_factory(llm=Planner([ask_plan()]))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'계량이 어려워','q')
        before=client.get(f'/sessions/{sid}').json()
        result=message(client,sid,'3단계 설명해줘','lookup')
        after=client.get(f'/sessions/{sid}').json()
        assert after['current_step']==before['current_step']
        assert after['dialogue']==before['dialogue']
        assert result['sources'][0]['excerpt'].startswith('3단계:')


def test_migrated_pending_question_restart_and_rollback(tmp_path, app_factory):
    import json,sqlite3
    from test_dialogue_state import legacy
    app=app_factory(llm=Planner([decision('abstain')]))
    store=app.state.store; session=store.create_session()
    session.update(selected_activity_id='cook-0-2',dialogue=legacy())
    with store.connection() as db:
        db.execute('UPDATE sessions SET payload=? WHERE id=?',(json.dumps(session),session['session_id']))
    with store.connection() as db:
        with sqlite3.connect(tmp_path/'legacy-backup.sqlite') as backup:
            db.backup(backup)
    with TestClient(app) as client:
        result=message(client,session['session_id'],'2번','migrate')
        state=client.get(f"/sessions/{session['session_id']}").json()['dialogue']
        assert state['version']==2 and state['observations'][-1]['answer_text']=='아니요'
        assert state['clarification_count']==2
    with sqlite3.connect(tmp_path/'legacy-backup.sqlite') as old:
        assert json.loads(old.execute('SELECT payload FROM sessions').fetchone()[0])['dialogue']['version']==1


def test_model_proposed_new_issue_requires_user_confirmation(app_factory):
    class ProposesNew(Planner):
        async def interpret_turn(self,text,context):
            return {'decision':{'route':'dialogue','transition':'new_issue','transition_quote':text},'llm_calls':1}
    llm=ProposesNew([decision('abstain'),decision('abstain')])
    with TestClient(app_factory(llm=llm)) as client:
        sid=create_session(client); select_activity(client,sid)
        first=message(client,sid,'계량이 어려워','q')
        proposed=message(client,sid,'정정할게. 이제 보관 이야기야','proposal')
        assert proposed['issue_id']==first['issue_id']
        assert proposed['clarification']['purpose']=='transition'
        accepted=message(client,sid,'예','accept')
        assert accepted['issue_id']!=first['issue_id']


def test_observation_after_answer_continues_existing_issue(app_factory):
    class Reports(Planner):
        async def interpret_turn(self,text,context):
            return {'decision':{'route':'dialogue','transition':'continue','observation_updates':[{'quote':text}]},'llm_calls':1}
    with TestClient(app_factory(llm=Reports([decision('abstain'),decision('abstain')]))) as client:
        sid=create_session(client); select_activity(client,sid)
        first=message(client,sid,'잘 안돼','q')
        second=message(client,sid,'표면은 갈색이고 안쪽은 축축해','report')
        assert second['issue_id']==first['issue_id'] and second['diagnostics']['intent']=='coaching'
        state=client.get(f'/sessions/{sid}').json()['dialogue']
        assert state['observations'][-1]['quote']=='표면은 갈색이고 안쪽은 축축해'


def test_declining_transition_preserves_resolved_issue(app_factory):
    class Proposes(Planner):
        async def interpret_turn(self,text,context):
            return {'decision':{'route':'dialogue','transition':'new_issue','transition_quote':text},'llm_calls':1}
    with TestClient(app_factory(llm=Proposes([decision('abstain')]))) as client:
        sid=create_session(client); select_activity(client,sid)
        message(client,sid,'잘 안돼','q')
        resolved=message(client,sid,'해결됐어','resolve')
        message(client,sid,'정정할게. 이제 보관 이야기야','proposal')
        reply=message(client,sid,'아니요','decline')
        assert reply['issue_id']==resolved['issue_id'] and reply['issue_phase']=='resolved'
