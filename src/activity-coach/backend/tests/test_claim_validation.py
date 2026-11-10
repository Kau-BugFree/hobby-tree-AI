import asyncio
import pytest
from test_answer_policy import policy, evidence
from app.turn_budget import TurnBudget


class Claims:
    configured = True

    def __init__(self, verdict='supported', quote=None, text='큰술 15ml.'):
        self.verdict, self.quote, self.text = verdict, quote, text
        self.generations = self.verifications = 0
        self.contexts=[]

    async def explain_claims(self, question, rows, context):
        self.generations+=1; self.contexts.append(context)
        return {'claims':[{'text':self.text, 'evidence_ids':['e'],
                'quotes':[{'evidence_id':'e','quote':self.quote or rows[0]['excerpt']}]}],
                'status':'received','llm_calls':1,'usage':{'input_tokens':2}}

    async def verify_claims(self, claims, rows, context):
        self.verifications+=1; self.contexts.append(context)
        return {'verdicts':[{'claim_index':0,'verdict':self.verdict,'evidence_ids':['e']}],
                'status':'received','llm_calls':1,'usage':{'input_tokens':2}}


def answer(llm, row=None):
    row=row or evidence(); row['review_status']='reviewed'
    return asyncio.run(policy().answer('설명해줘', [row], {'allowed_activity_ids':['a']}, [], llm, TurnBudget()))


def test_reviewed_supported_claims_are_assembled_after_check():
    llm=Claims(); result=answer(llm)
    assert result['mode']=='generated' and result['text']=='큰술 15ml.'
    assert result['validation']['semantic_support']=='model_assessed'
    assert result['llm_calls']==2 and result['usage']['input_tokens']==4


@pytest.mark.parametrize('text', ['큰술 5ml, 작은술 15ml.', '차가운 것이 뜨겁다.'])
def test_semantic_rejection_never_returns_unverified_text(text):
    llm=Claims('contradicted',text=text); result=answer(llm)
    assert result['mode']=='reference' and text not in result['text']
    assert llm.generations==llm.verifications==2
    assert result['validation']['semantic_support']=='failed'


def test_fabricated_quote_rejected_before_semantic_check():
    llm=Claims(quote='원문에 없는 인용'); result=answer(llm)
    assert result['mode']=='reference' and llm.verifications==0


def test_unknown_applicability_does_not_generate():
    row=evidence(); row['applies_when']='차가운 경우'
    llm=Claims(); result=answer(llm,row)
    assert result['mode']=='reference' and llm.generations==0


def test_verifier_failure_falls_back():
    class Broken(Claims):
        async def verify_claims(self,*args):
            return {'status':'timeout','verdicts':None,'llm_calls':1}
    assert answer(Broken())['mode']=='reference'


class Unavailable(Claims):
    """생성 호출이 공급자 오류로 끝나는 대역."""

    def __init__(self, failure):
        super().__init__()
        self.failure = failure

    async def explain_claims(self, question, rows, context):
        self.generations += 1
        return {'claims': None, 'status': self.failure, 'llm_calls': 1, 'usage': {}}


@pytest.mark.parametrize('failure', ['timeout', 'rate_limited', 'authentication_error', 'blocked'])
def test_provider_failure_keeps_the_source_text_and_does_not_retry(failure):
    """공급자 오류는 모델이 답을 판단한 결과가 아니다. 키 없는 배포와 같은 원문 안내를 유지한다."""
    llm = Unavailable(failure); row = evidence()
    result = answer(llm, row)
    assert result['mode'] == 'reference' and row['excerpt'] in result['text']
    assert result['status'] == 'generation_unavailable' and result['validation_errors'] == [failure]
    assert llm.generations == 1


def test_verifier_provider_failure_keeps_the_source_text_without_the_unverified_answer():
    class Broken(Claims):
        async def verify_claims(self, *args):
            return {'status': 'timeout', 'verdicts': None, 'llm_calls': 1}
    llm = Broken(text='검증하지 못한 문장.'); row = evidence()
    result = answer(llm, row)
    assert row['excerpt'] in result['text'] and '검증하지 못한 문장.' not in result['text']
    assert result['status'] == 'generation_unavailable' and llm.generations == 1


def test_exhausted_budget_keeps_the_source_text():
    budget = TurnBudget(); budget.counts['generation'] = budget.limits['generation']
    row = evidence(); row['review_status'] = 'reviewed'
    result = asyncio.run(policy().answer('설명해줘', [row], {'allowed_activity_ids': ['a']}, [], Claims(), budget))
    assert row['excerpt'] in result['text'] and result['status'] == 'generation_unavailable'
    assert result['metacognition']['stop_reason'] == 'budget_exhausted'


def test_rejected_answer_is_withheld_and_cites_nothing():
    """검증에서 떨어진 답은 보류하고, 보류한 응답은 답변·인용으로 세지 않도록 상태를 구분한다."""
    result = answer(Claims('insufficient'))
    assert result['status'] == 'withheld' and '보류' in result['text']
    assert result['cited_evidence_ids'] == [] and result['cited_activity_ids'] == []


def test_repair_receives_rejected_claim_and_verdict():
    class Repair(Claims):
        async def explain_claims(self, question, rows, context):
            if self.generations:
                rejected = context.get('previous_claims', [])
                verdicts = context.get('previous_verdicts', [])
                if rejected and rejected[0]['text'] == '큰술 5ml, 작은술 15ml.' and verdicts[0]['verdict'] == 'contradicted':
                    self.text='큰술 15ml.'
            return await super().explain_claims(question, rows, context)
        async def verify_claims(self, claims, rows, context):
            self.verdict='supported' if claims[0]['text']=='큰술 15ml.' else 'contradicted'
            return await super().verify_claims(claims, rows, context)
    result=answer(Repair(text='큰술 5ml, 작은술 15ml.'))
    assert result['mode']=='generated' and result['text']=='큰술 15ml.'


@pytest.mark.parametrize('question', ['큰술과 작은술 계량은 어떻게 해?', '큰술 용량 알려줘'])
def test_source_only_facts_use_the_same_claim_and_semantic_checks(question):
    row = evidence()
    result = asyncio.run(policy().answer(question, [row], {'allowed_activity_ids': ['a']},
        [], Claims(), TurnBudget()))
    assert result['mode'] == 'generated' and result['text'] == '큰술 15ml.'
    assert result['validation']['semantic_support'] == 'model_assessed'
    assert result['llm_calls'] == 2
    assert result['sources'] == [row]
    assert result['metacognition']['source_review'] == {'e': 'source_only'}


@pytest.mark.parametrize('question', ['왜 계량에 실패했어?', '계량 실패를 어떻게 해결해?', '계량 원문 보여줘',
                                     '계량이 잘못됐는데 고치는 법 알려줘'])
def test_source_only_causes_remedies_and_explicit_originals_stay_extractive(question):
    llm = Claims()
    row = evidence()
    result = asyncio.run(policy().answer(question, [row], {'allowed_activity_ids': ['a']},
        [], llm, TurnBudget()))
    assert result['mode'] == 'reference' and row['excerpt'] in result['text']
    assert result['llm_calls'] == 0


def test_source_only_unknown_condition_stays_reference():
    row = evidence()
    row['applies_when'] = '차가운 상태'
    result = asyncio.run(policy().answer('큰술 계량은 어떻게 해?', [row],
        {'allowed_activity_ids': ['a']}, [], Claims(), TurnBudget()))
    assert result['mode'] == 'reference' and '차가운 상태' in result['text']
    assert result['llm_calls'] == 0


def test_source_only_without_key_keeps_complete_original_without_calls():
    llm = Claims()
    llm.configured = False
    row = evidence()
    result = asyncio.run(policy().answer('큰술 계량은 어떻게 해?', [row],
        {'allowed_activity_ids': ['a']}, [], llm, TurnBudget()))
    assert result['mode'] == 'reference' and row['excerpt'] in result['text']
    assert result['llm_calls'] == 0


def test_source_only_rejects_swapped_quantities_after_semantic_check():
    row = evidence()
    result = asyncio.run(policy().answer('큰술과 작은술은 어떻게 계량해?', [row],
        {'allowed_activity_ids': ['a']}, [], Claims('contradicted', text='큰술 5ml, 작은술 15ml.'), TurnBudget()))
    assert result['mode'] == 'reference'
    assert '큰술 5ml, 작은술 15ml.' not in result['text']
    assert result['validation']['semantic_support'] == 'failed'
