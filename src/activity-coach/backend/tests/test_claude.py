"""실제 API 키·과금 없이 Claude 호출 정책과 실패 복구를 검사한다.

공식 anthropic SDK에 httpx2 MockTransport를 주입해 실제 요청 본문과 응답 파싱을 그대로 거친다.
"""

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.claude import ClaudeService, validate_answer


def settings(**overrides):
    """테스트 전용 가짜 키와 짧은 제한값을 만든다. 사용자 환경변수는 읽지 않는다."""
    values = dict(anthropic_api_key="unit-test-key", claude_model="claude-opus-5-5",
                  claude_effort="medium", claude_max_tokens=16000,
                  claude_timeout_seconds=5, claude_max_retries=0)
    values.update(overrides)
    return SimpleNamespace(**values)


EVIDENCE = [{"activity_id": "cook-0-2", "title": "드레싱 계량", "kind": "activity",
             "excerpt": "큰술은 15mL, 작은술은 5mL입니다. 설탕이 녹을 때까지 저어요."}]


def message(answer="큰술과 작은술의 차이를 확인한 뒤 설탕이 녹을 때까지 저어 주세요.", ids=None,
            stop_reason="end_turn", text=None, blocks=None):
    """Messages API 응답 모양의 최소 fixture를 만든다."""
    body_text = text if text is not None else json.dumps(
        {"answer": answer, "cited_activity_ids": ids if ids is not None else ["cook-0-2"]}, ensure_ascii=False)
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": (blocks or []) + [{"type": "text", "text": body_text}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}


def request_content(request):
    """요청 본문의 사용자 메시지(JSON 문자열)를 dict로 복원한다."""
    payload = json.loads(request.content)
    return json.loads(payload["messages"][0]["content"])


def execute(handler, config=None, evidence=EVIDENCE, **kwargs):
    """MockTransport를 주입한 SDK 클라이언트로 explain을 실행하고 클라이언트를 닫는다."""
    async def call():
        client = anthropic.AsyncAnthropic(
            api_key="unit-test-key", max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
        async with client:
            service = ClaudeService(config or settings(), client=client)
            return await service.explain("이 단계를 쉽게 설명해줘", evidence,
                                         {"selected_activity_id": "cook-0-2", "current_step": 3}, **kwargs)
    return asyncio.run(call())


def test_request_carries_model_effort_schema_fallback_and_header_key():
    """모델·effort·JSON 스키마·거절 대체를 본문에 싣고 키는 헤더로만 보낸다."""
    def handler(request):
        assert request.url.path == "/v1/messages"
        # SDK 2.x는 키를 Authorization 헤더로 보낸다. URL·본문에는 키가 없어야 한다.
        assert "unit-test-key" in request.headers["authorization"]
        assert "unit-test-key" not in str(request.url) + request.content.decode()
        assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
        payload = json.loads(request.content)
        assert payload["model"] == "claude-opus-5-5"
        assert payload["max_tokens"] == 16000
        assert payload["fallbacks"] == "default"
        assert payload["output_config"]["effort"] == "medium"
        schema = payload["output_config"]["format"]
        assert schema["type"] == "json_schema"
        assert set(schema["schema"]["required"]) == {"answer", "cited_activity_ids"}
        assert "신뢰되지 않은 데이터" in payload["system"]
        content = request_content(request)
        assert content["question"] == "이 단계를 쉽게 설명해줘"
        assert content["evidence"] == EVIDENCE
        return httpx2.Response(200, json=message())
    result = execute(handler)
    assert result["status"] == "generated"
    assert result["llm_calls"] == 1
    assert result["cited_activity_ids"] == ["cook-0-2"]
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 20
    assert "unit-test-key" not in json.dumps(result)


def test_haiku_request_omits_unsupported_server_fallback():
    """Haiku 5.5는 서버 측 거절 대체가 없으므로 fallbacks·beta 헤더 없이 일반 Messages API로 보낸다."""
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "claude-haiku-5-5"
        assert "fallbacks" not in payload
        assert "server-side-fallback" not in request.headers.get("anthropic-beta", "")
        assert payload["output_config"]["format"]["type"] == "json_schema"
        return httpx2.Response(200, json=message())
    result = execute(handler, settings(claude_model="claude-haiku-5-5"))
    assert result["status"] == "generated"
    assert result["model"] == "claude-haiku-5-5"


def test_missing_key_does_not_call_provider():
    """키가 없을 때 성공을 흉내 내지 않고 호출 0회·not_configured를 반환한다."""
    def fail(_request):
        raise AssertionError("must not call network")
    result = execute(fail, settings(anthropic_api_key=""))
    assert result["text"] is None
    assert result["status"] == "not_configured"
    assert result["llm_calls"] == 0


def test_thinking_blocks_are_not_used_as_answer():
    """적응형 thinking 블록이 앞에 와도 text 블록의 JSON만 답변으로 쓴다."""
    thinking = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    result = execute(lambda _request: httpx2.Response(200, json=message(blocks=thinking)))
    assert result["status"] == "generated"
    assert result["text"].startswith("큰술과 작은술")


def test_correction_once_retains_original_context():
    """새 수량·잘못된 인용은 한 번만 수정하고 최초 질문·근거를 유지한다."""
    requests = []
    def handler(request):
        requests.append(request_content(request))
        if len(requests) == 1:
            return httpx2.Response(200, json=message("설탕 99g을 넣으세요.", ["invented-id"]))
        return httpx2.Response(200, json=message("큰술은 15mL입니다."))
    result = execute(handler)
    assert result["status"] == "generated_repaired"
    assert result["llm_calls"] == 2
    assert result["usage"]["input_tokens"] == 20
    assert requests[1]["question"] == requests[0]["question"]
    assert requests[1]["evidence"] == requests[0]["evidence"]
    assert "unknown_citation" in requests[1]["correction"]["errors"]


def test_correction_names_values_missing_from_evidence():
    """수정 요청에 근거에 없는 숫자·수량 표현을 그대로 알려 같은 환산을 반복하지 않게 한다."""
    requests = []
    def handler(request):
        requests.append(request_content(request))
        if len(requests) == 1:
            return httpx2.Response(200, json=message("1큰술은 15mL, 1작은술은 5mL라서 큰술은 작은술 3개예요."))
        return httpx2.Response(200, json=message("큰술은 15mL, 작은술은 5mL입니다."))
    result = execute(handler)
    assert result["status"] == "generated_repaired"
    assert requests[1]["correction"].get("unsupported_values") == {
        "numbers": ["1", "3"], "measurements": ["1큰술", "1작은술", "3개"]}


def test_repeated_invalid_output_stops_at_two():
    """수정 후에도 근거 없는 수량이면 본문을 반환하지 않고 무한 재시도하지 않는다."""
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json=message("설탕 99g을 넣으세요."))
    result = execute(handler)
    assert len(calls) == result["llm_calls"] == 2
    assert result["status"] == "validation_failed"
    assert result["text"] is None
    assert "unsupported_measurement" in result["validation_errors"]


def test_json_failure_can_be_repaired_once():
    """모델 본문이 잘못된 JSON일 때 구조 오류를 포함한 한 번의 수정만 허용한다."""
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json=message(text="not JSON") if len(calls) == 1 else message())
    result = execute(handler)
    assert result["status"] == "generated_repaired"
    assert result["llm_calls"] == 2


@pytest.mark.parametrize("status,expected", [(401, "authentication_error"), (403, "authentication_error"),
                                             (429, "rate_limited"), (500, "provider_error"), (529, "provider_error")])
def test_http_failures_are_classified_without_leaking_body(status, expected):
    """인증·요청 제한·서버 실패를 분류하고 공급자의 에러 본문은 노출하지 않는다."""
    body = {"type": "error", "error": {"type": "api_error", "message": "unit-test-key"}}
    result = execute(lambda _request: httpx2.Response(status, json=body))
    assert result["status"] == expected
    assert result["llm_calls"] == 1
    assert result["text"] is None
    assert "unit-test-key" not in json.dumps(result)


def test_timeout_falls_back_without_text():
    """시간 초과는 상위 서비스가 직접 근거 응답으로 복귀할 수 있는 값으로 반환한다."""
    def handler(request):
        raise httpx2.ReadTimeout("timeout with secret unit-test-key", request=request)
    result = execute(handler)
    assert result["status"] == "timeout"
    assert result["llm_calls"] == 1
    assert "unit-test-key" not in json.dumps(result)


@pytest.mark.parametrize("stop_reason,expected", [("refusal", "blocked"), ("max_tokens", "truncated")])
def test_refusal_and_truncation_do_not_claim_or_repair(stop_reason, expected):
    """거절·출력 한도 도달은 생성 성공으로 표시하지 않고 수정 호출도 하지 않는다."""
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json=message(stop_reason=stop_reason))
    result = execute(handler)
    assert result["status"] == expected
    assert result["text"] is None
    assert len(calls) == result["llm_calls"] == 1


def test_validate_false_is_labeled_unvalidated():
    """검증을 끈 최초 답변은 검증 성공으로 잘못 표시하지 않는다."""
    result = execute(lambda _request: httpx2.Response(200, json=message("설탕 99g을 넣으세요.")), validate=False)
    assert result["status"] == "generated_unvalidated"
    assert result["llm_calls"] == 1


def test_missing_evidence_never_calls():
    """근거가 없으면 외부 요청을 만들지 않는다."""
    def fail(_request):
        raise AssertionError("must not call network")
    assert execute(fail, evidence=[])["status"] == "no_evidence"


def test_context_whitelist_excludes_secrets_and_oversized_question():
    """불필요한 세션·키 필드를 보내지 않고 긴 질문을 조용히 잘라 변형하지 않는다."""
    service = ClaudeService(settings())
    payload, error = service._bounded_inputs("설명", EVIDENCE, {"api_key": "secret", "session_token": "secret", "current_step": 2})
    assert error is None
    assert payload["context"] == {"current_step": 2}
    assert service._bounded_inputs("가" * 3001, EVIDENCE, {})[1] == "question_too_long"


def test_oversized_source_is_rejected_without_truncating_conditions():
    service=ClaudeService(settings())
    rows=[{**EVIDENCE[0],'excerpt':'가'*6001,'applies_when':'차가운 경우'}]
    assert service._bounded_inputs('설명',rows,{})==(None,'context_limit')


def test_measurements_are_grounded_in_cited_evidence_only():
    """단위의 단순 표기는 정규화하되 다른 수량·단위로 바꾸는 것은 허용하지 않는다."""
    assert not validate_answer({"answer": "큰술은 15ml입니다.", "cited_activity_ids": ["cook-0-2"]}, EVIDENCE)
    assert "unsupported_measurement" in validate_answer({"answer": "설탕 15g을 넣어요.", "cited_activity_ids": ["cook-0-2"]}, EVIDENCE)
    assert "missing_citation" in validate_answer({"answer": "잘 저어요.", "cited_activity_ids": []}, EVIDENCE)
    assert "unsupported_number" in validate_answer({"answer": "비용은 5000원입니다.", "cited_activity_ids": ["cook-0-2"]}, EVIDENCE)


def test_step_numbers_of_listed_evidence_are_supported():
    """'1. …' 목록으로 넘긴 단계 번호를 'N단계'로 말해도 근거 있는 숫자로 보고, 없는 단계는 거부한다."""
    evidence = [{"activity_id": "cook-1-4", "title": "냄비밥 짓기", "kind": "activity",
                 "excerpt": "준비물: 쌀\n1. 쌀을 씻어요\n2. 물을 부어요\n3. 끓여요\n4. 뜸을 들여요"}]
    assert not validate_answer({"answer": "4단계에서 뜸을 들여요.", "cited_activity_ids": ["cook-1-4"]}, evidence)
    assert not validate_answer({"answer": "1. 쌀을 씻어요\n2. 물을 부어요", "cited_activity_ids": ["cook-1-4"]}, evidence)
    assert "unsupported_number" in validate_answer({"answer": "5단계에서 뜸을 들여요.", "cited_activity_ids": ["cook-1-4"]}, evidence)


def test_previous_answer_reaches_model_within_limit():
    """직전 답변은 지시어 해석용으로 1,000자까지만 전달한다."""
    service = ClaudeService(settings())
    payload, _ = service._bounded_inputs("그거 확실해?", EVIDENCE, {"previous_answer": "직전 설명"})
    assert payload["context"]["previous_answer"] == "직전 설명"
    payload, _ = service._bounded_inputs("그거 확실해?", EVIDENCE, {"previous_answer": "가" * 1500})
    assert payload["context"]["previous_answer"] == "가" * 1000


def test_excluded_tools_reach_the_model():
    """검색에서 제외한 도구를 모델도 알도록 컨텍스트와 지시문에 포함한다."""
    from app.claude import SYSTEM_INSTRUCTION
    service = ClaudeService(settings())
    payload, error = service._bounded_inputs("설명", EVIDENCE, {"constraints": {"excluded_tools": ["오븐"], "max_minutes": 20}})
    assert error is None
    assert payload["context"]["constraints"]["excluded_tools"] == ["오븐"]
    assert "excluded_tools" in SYSTEM_INSTRUCTION


def test_fraction_measurement_keeps_its_value():
    """유니코드 반 컵을 2컵으로 오인하지 않도록 분수 비교를 확인한다."""
    evidence = [{"activity_id": "recipe-1", "title": "반 컵", "excerpt": "Use ½ cup of water.", "kind": "recipe"}]
    assert not validate_answer({"answer": "물 1/2컵을 사용합니다.", "cited_activity_ids": ["recipe-1"]}, evidence)
    assert "unsupported_measurement" in validate_answer({"answer": "물 2컵을 사용합니다.", "cited_activity_ids": ["recipe-1"]}, evidence)
