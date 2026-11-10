import asyncio
import importlib
from app.dialogue_models import DialogueState, Observation


def interpret(text, state=None, llm=None):
    router = importlib.import_module('app.turn_router').TurnRouter(llm)
    budget = importlib.import_module('app.turn_budget').TurnBudget()
    return asyncio.run(router.interpret(text, {'selected_activity_id': 'a', 'current_step': 3},
                                       state or DialogueState(issue_id='i', activity_id='a', phase='open'), budget))


def test_followup_paraphrases_keep_same_issue():
    assert interpret('그럼 왜 그래?').route == interpret('왜 그래?').route == 'dialogue'
    assert interpret('왜 그래?').transition == 'continue'


def test_unknown_answer_is_not_false_fact():
    result = interpret('모르겠어요', DialogueState(issue_id='i', pending_question={'text':'온도?', 'options':[]}))
    assert result.observation_updates[0].status == 'unknown'


def test_ambiguous_correction_preserves_state():
    result = interpret('아니 그게 아니야')
    assert result.transition == 'correct' and not result.observation_updates


def test_model_action_does_not_start_execution():
    class LLM:
        async def interpret_turn(self, *args):
            return {'decision': {'route':'action'}, 'status':'planned', 'llm_calls':1}
    assert interpret('음 저기', llm=LLM()).route != 'action'


def test_fourth_older_fact_reaches_common_context():
    from app.dialogue_state import build_issue_context
    state = DialogueState(observations=[Observation(id=str(i),source_message_id='m',quote=f'관찰{i}') for i in range(6)])
    assert len(build_issue_context(state, {})['dialogue']['observations']) == 6


def test_lookup_does_not_mutate_issue():
    assert interpret('준비물 알려줘').route == 'lookup'
    assert interpret('1번이랑 2번 비교해줘').route == 'lookup'


def test_negated_command_is_never_new_issue():
    assert interpret('다시 시작하지 마').transition == 'none'


def test_explicit_correction_during_pending_preserves_question_and_targets_fact():
    from app.dialogue_state import apply_transition
    class LLM:
        async def interpret_turn(self,*args):
            return {'decision':{'route':'dialogue','transition':'correct','observation_updates':[
                {'quote':'뜨거운 물','supersedes':['old']}]},'llm_calls':1}
    state=DialogueState(issue_id='i',activity_id='a',phase='awaiting_observation',
        observations=[Observation(id='old',source_message_id='m',quote='찬물')],
        pending_question={'question_id':'q','text':'저었나요?','options':['예','아니요']})
    text='정정할게요. 찬물이 아니라 뜨거운 물을 썼어요'
    result=interpret(text,state,LLM())
    updated,_=apply_transition(state,result,'m2',text,'a',None)
    assert result.transition=='correct' and updated.observations[0].status=='superseded'
    assert updated.pending_question==state.pending_question


def test_explicit_transition_precedes_pending_reflection():
    state=DialogueState(issue_id='i',activity_id='a',phase='awaiting_observation',pending_question={
        'question_id':'q','text':'결과는?','options':[],'purpose':'reflection'})
    for text,transition in [('새 문제야. 팬에 붙어','new_issue'),('해결됐어','resolve'),('이 문제 그만할래','abandon')]:
        result=interpret(text,state)
        assert result.route=='dialogue' and result.transition==transition


def test_explicit_transition_precedes_pending_confirmation():
    state=DialogueState(issue_id='i',phase='awaiting_observation',pending_question={
        'question_id':'q','text':'새 문제?','options':['예','아니요'],'purpose':'transition'})
    for text,transition in [('새 문제야. 왜 붙어?','new_issue'),('해결됐어','resolve'),('이 문제 그만할래','abandon')]:
        assert interpret(text,state).transition==transition
