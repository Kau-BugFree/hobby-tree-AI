import asyncio
import json

import anthropic
import httpx2

from scripts.evaluate_live_dialogue import TracedClaude, dependent_hops
from test_claude import message, settings


def test_live_multi_hop_check_requires_real_dependencies_and_new_evidence():
    first = {"depends_on": [], "evidence_ids": ["steps"]}
    assert dependent_hops([first, {"depends_on": ["steps"], "evidence_ids": ["checks"]}])
    assert not dependent_hops([first])
    assert not dependent_hops([first, {"depends_on": [], "evidence_ids": ["checks"]}])
    assert not dependent_hops([first, {"depends_on": ["invented"], "evidence_ids": ["checks"]}])
    assert not dependent_hops([first, {"depends_on": ["steps"], "evidence_ids": ["steps"]}])


def test_live_rag_check_accepts_a_verified_answer_and_rejects_a_withheld_one():
    """RAG 점검은 원문 안내와 검증된 생성 답변을 모두 받는다. 보류·실패는 답변으로 세지 않는다."""
    from scripts.evaluate_live_dialogue import rag_answer_cites

    assert rag_answer_cites({"status": "generated", "cited_activity_ids": ["cook-0-2"]}, "cook-0-2")
    assert rag_answer_cites({"status": "reference", "cited_activity_ids": ["cook-0-2", "cook-0-1"]}, "cook-0-2")
    assert not rag_answer_cites({"status": "withheld", "cited_activity_ids": []}, "cook-0-2")
    assert not rag_answer_cites({"status": "generation_unavailable", "cited_activity_ids": ["cook-0-2"]}, "cook-0-2")
    assert not rag_answer_cites({"status": "generated", "cited_activity_ids": ["cook-0-1"]}, "cook-0-2")


def test_live_counter_counts_planner_and_answer_requests():
    def handler(request):
        schema = json.loads(request.content)['output_config']['format']['schema']
        result = message(text='{"next_action":"abstain"}') if 'next_action' in schema['properties'] else message('근거 안내입니다.', ids=['a'])
        return httpx2.Response(200, json=result)

    async def run():
        async with anthropic.AsyncAnthropic(api_key='test', max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))) as client:
            llm = TracedClaude(settings())
            llm._client = client
            await llm.decide('질문', {'current_step': 1, 'dialogue': {'observations': []}}, [])
            await llm.explain('질문', [{'activity_id': 'a', 'excerpt': '근거 안내입니다.'}], {})
            assert llm.generation_calls == 2
            assert len(llm.decisions) == 1

    asyncio.run(run())
