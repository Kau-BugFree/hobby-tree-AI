import importlib
from app.dialogue_models import DialogueState


def module():
    return importlib.import_module('app.metacognition')


def test_previous_recommendation_reason_uses_previous_constraints():
    snap={'message_id':'m','query':'달걀','scope':'activities','constraints':{'max_minutes':40},
          'candidate_ids':['a'],'ranking':[],'search_mode':'catalog_browse'}
    reason=module().recommendation_reason(snap,{'max_minutes':1},[{'activity_id':'a','title':'요리'}])
    assert '40분' in reason and '이전' in reason and '시간순' in reason
    assert '적용한 조건: 1분' not in reason


def test_legacy_recommendation_has_no_invented_provenance():
    assert '복원' in module().recommendation_reason(None,{},[{'activity_id':'a','title':'요리'}])


def test_failure_is_not_reported_as_missing_knowledge():
    assert module().stop_message('execution_failure',None) != module().stop_message('missing_knowledge',None)


def test_reflection_one_question_unknown_not_pass():
    state=DialogueState(issue_id='i',phase='open'); activity={'checks':['표면이 고른가?']}
    first=module().reflection_response(state,activity,'평가해줘','m1')
    assert first['state'].reflection_count==1 and first['state'].pending_question['purpose']=='reflection'
    second=module().reflection_response(first['state'],activity,'모르겠어요','m2')
    assert second['state'].outcome['quote']=='모르겠어요'
    assert second['state'].phase != 'resolved' and second['mode']!='clarify'
    assert '확인되지 않은' in second['text'] and '합격' not in second['text']


def test_reported_resolution_is_only_self_report():
    from app.dialogue_state import apply_transition
    from app.dialogue_models import TurnInterpretation
    state,_=apply_transition(DialogueState(issue_id='i',phase='open'),
        TurnInterpretation(route='dialogue',transition='resolve'),'m','해결됐어',None,None)
    assert state.phase=='resolved' and state.outcome['kind']=='self_report'
