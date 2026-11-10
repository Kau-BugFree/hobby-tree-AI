import asyncio
import importlib
from app.dialogue_models import Requirement
from app.turn_budget import TurnBudget


def evidence(**kwargs):
    return dict(evidence_id='e', activity_id='a', title='자료', kind='steps', excerpt='큰술 15ml, 작은술 5ml.',
                review_status='source_only', source_refs=['a/steps'], applies_when='', **kwargs)


def policy():
    return importlib.import_module('app.answer_policy').AnswerPolicy()


def test_source_only_never_calls_generator_or_verifier():
    result = asyncio.run(policy().answer('왜 실패했어?', [evidence()], {'allowed_activity_ids':['a']},
        [Requirement(id='r',text='왜?',kind='cause',source_message_id='m')], object(), TurnBudget()))
    assert result['mode'] == 'reference' and result['llm_calls'] == 0
    assert '확정' in result['text'] and result['metacognition']['unresolved']


def test_draft_and_wrong_activity_excluded():
    draft=evidence(); draft['review_status']='draft'
    prepared=policy().prepare('설명', [draft, evidence()], {'allowed_activity_ids':['other']}, [])
    assert prepared['evidence'] == []


def test_candidate_comparison_preserves_server_evidence():
    other=evidence(); other.update(evidence_id='e2',activity_id='b')
    prepared=policy().prepare('비교', [evidence(),other], {'allowed_activity_ids':['a','b']}, [])
    assert len(prepared['evidence']) == 2


def test_recipe_not_implicitly_reviewed():
    adapter=importlib.import_module('app.answer_policy').evidence_for
    result=adapter({'activity_id':'recipe:1','title':'요리','kind':'recipe','steps':['섞기'], 'quality':{'human_reviewed':True}})
    assert result['review_status'] == 'source_only'


def test_context_limit_preserves_complete_conditions():
    row=evidence(); row.update(review_status='reviewed',excerpt='원문'*4000,applies_when='차가운 상태')
    result=policy().prepare('설명', [row], {'allowed_activity_ids':['a']}, [])
    assert not result['allow_generation'] and result['stop_reason'] == 'context_limit'
    assert result['evidence'][0]['applies_when'] == '차가운 상태'


def test_negated_condition_is_not_confirmed():
    row=evidence(); row.update(review_status='reviewed',applies_when='차가운 상태')
    context={'allowed_activity_ids':['a'],'dialogue':{'observations':[{'quote':'차가운 상태가 아니야',
        'status':'reported','provenance':'user_quote'}]}}
    assert not policy().prepare('설명',[row],context,[])['allow_generation']


def test_claim_coverage_cannot_survive_missing_evidence():
    context={'allowed_activity_ids':['a'],'dialogue':{'coverage':[{'requirement_id':'r','status':'supported',
              'evidence_ids':['removed'],'quotes':[{'evidence_id':'removed','quote':'원문'}]}]}}
    prepared=policy().prepare('절차',[evidence()],context,[Requirement(id='r',text='절차',source_message_id='m')])
    assert not prepared['covered'] and prepared['unresolved']


def test_unrelated_reviewed_row_does_not_authorize_source_only_remedy():
    other=evidence(); other.update(evidence_id='reviewed',review_status='reviewed')
    context={'allowed_activity_ids':['a'],'dialogue':{'coverage':[{'requirement_id':'r','status':'supported',
        'evidence_ids':['e'],'quotes':[{'evidence_id':'e','quote':'큰술 15ml, 작은술 5ml.'}]}]}}
    prepared=policy().prepare('해결',[evidence(),other],context,[Requirement(id='r',text='해결',kind='remedy',source_message_id='m')])
    assert not prepared['covered']
    assert prepared['unresolved'][0]['reason']=='missing_knowledge'


def test_unconfirmed_condition_cannot_be_supported_coverage():
    row=evidence(); row.update(review_status='reviewed',applies_when='차가운 상태')
    context={'allowed_activity_ids':['a'],'dialogue':{'coverage':[{'requirement_id':'r','status':'supported',
        'evidence_ids':['e'],'quotes':[{'evidence_id':'e','quote':row['excerpt']}]}]}}
    prepared=policy().prepare('해결',[row],context,[Requirement(id='r',text='해결',kind='remedy',source_message_id='m')])
    assert not prepared['covered']
    assert prepared['unresolved'][0]['reason']=='missing_observation'

