"""Exercise real SDK JSON request/response without network or keys."""
import asyncio
import json
import anthropic
import httpx2
import pytest
from app.claude import ClaudeService
from test_claude import settings, message, execute


def run(handler, config=None, context=None):
    async def call():
        async with anthropic.AsyncAnthropic(api_key="test", max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))) as client:
            return await ClaudeService(config or settings(), client=client).decide(
                "잘 모르겠어", context or {"dialogue": {"pending_question": None}}, [])
    return asyncio.run(call())


def test_decision_schema_uses_supported_wire_constraints():
    def handler(request):
        payload = json.loads(request.content)
        schema = payload['output_config']['format']['schema']
        assert 'maxLength' not in schema['properties']['query']
        assert schema['additionalProperties'] is False
        assert 'next_action' in schema['required']
        return httpx2.Response(200, json=message(text=json.dumps({'next_action':'abstain'})))
    result = run(handler)
    assert result['decision']['next_action'] == 'abstain'
    assert result['llm_calls'] == 1


@pytest.mark.parametrize('raw', ['not json', '{"next_action":"complete"}',
    '{"next_action":"retrieve","query":"' + 'x'*1001 + '"}'])
def test_invalid_decision_is_not_executed(raw):
    result = run(lambda _: httpx2.Response(200, json=message(text=raw)))
    assert result['decision'] is None
    assert result['status'] == 'invalid_decision'


def test_planner_missing_key_and_context_bound():
    def fail(_):
        raise AssertionError('must not call provider')
    assert run(fail, settings(anthropic_api_key=''))['status'] == 'not_configured'
    assert run(fail, context={'text':'x'*48001})['status'] == 'context_too_long'


@pytest.mark.parametrize('status', [401, 429, 500])
def test_provider_failure_is_safe(status):
    result = run(lambda _: httpx2.Response(status, json={'error':{'type':'api_error','message':'secret'}}))
    assert result['decision'] is None
    assert 'secret' not in json.dumps(result)


def test_malformed_success_response_is_a_provider_error_not_a_crash():
    """필드가 빠진 200 응답은 예외로 번져 HTTP 500이 되지 않고 공급자 오류로 처리한다."""
    result = run(lambda _: httpx2.Response(200, json={'id': 'msg_1', 'type': 'message'}))
    assert result['decision'] is None and result['status'] == 'provider_error'


def test_long_evidence_keeps_applicability_conditions():
    evidence=[{'activity_id':'e1','title':'검토 자료','kind':'remedy','excerpt':'가'*6000,
               'applies_when':'활동 2단계에서만 적용'}]
    def handler(request):
        payload=json.loads(request.content)
        inputs=json.loads(payload['messages'][0]['content'])
        assert inputs['evidence'][0]['applies_when']=='활동 2단계에서만 적용'
        return httpx2.Response(200,json=message('근거 안내입니다.',ids=['e1']))
    assert execute(handler,evidence=evidence)['status']=='generated'


def test_new_structured_methods_preserve_all_context_and_disable_retries():
    seen=[]
    async def call():
        def handler(request):
            payload=json.loads(request.content); inputs=json.loads(payload['messages'][0]['content'])
            seen.append(inputs)
            if 'claims' in inputs:
                raw={'verdicts':[{'claim_index':0,'verdict':'supported','evidence_ids':['e']}]}
            elif 'evidence' in inputs:
                raw={'claims':[{'text':'큰술 15ml','evidence_ids':['e'],'quotes':[{'evidence_id':'e','quote':'큰술 15ml'}]}]}
            else:
                raw={'route':'dialogue','transition':'continue'}
            return httpx2.Response(200,json=message(text=json.dumps(raw)))
        async with anthropic.AsyncAnthropic(api_key='test',max_retries=4,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))) as client:
            service=ClaudeService(settings(),client=client)
            context={'dialogue':{'observations':[{'quote':str(i)} for i in range(8)]}}
            assert (await service.interpret_turn('왜?',context))['decision']['route']=='dialogue'
            generated=await service.explain_claims('설명',[],context)
            assert generated['claims']
            assert (await service.verify_claims(generated['claims'],[],context))['verdicts']
            assert all(len(item['context']['dialogue']['observations'])==8 for item in seen)
    asyncio.run(call())
