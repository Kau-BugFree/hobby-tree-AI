"""판단 형식(AgentDecision)이 Claude에서 받아들여지는지 실제 호출 한 번으로 확인합니다(설계 4.6절).

사용: python -B scripts/check_agent_schema.py
유료 호출입니다. 설정은 Settings.from_env()로 읽고 키는 출력하지 않습니다.
오류 내용을 봐야 하므로 ClaudeService._structured가 아니라 SDK를 직접 부릅니다.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import anthropic  # noqa: E402

from app.actions import AgentDecision, from_wire, parse_decision  # noqa: E402
from app.claude import AGENT_INSTRUCTION, FALLBACK_BETA, FALLBACK_MODELS  # noqa: E402
from app.config import Settings  # noqa: E402

TEXT = "두 번째 걸로 할게"
CONTEXT = {
    "selected": None,
    "candidates": [
        {"number": 1, "activity_id": "cook-0-1", "title": "계란 프라이"},
        {"number": 2, "activity_id": "cook-0-2", "title": "계량스푼으로 드레싱 만들기"},
        {"number": 3, "activity_id": "cook-0-3", "title": "주먹밥"},
    ],
    "constraints": {},
    "dialogue": {"issue_id": None, "phase": "idle", "pending_question": None, "observations": []},
    "pending_confirmation": None,
    "recent_messages": [],
}


async def check(settings: Settings) -> int:
    """요청을 한 번 보내고 결과를 출력합니다. 받아들여지고 행동이 읽히면 0, 아니면 1을 반환합니다."""
    schema = anthropic.transform_schema(AgentDecision)
    effort = settings.agent_effort or settings.claude_effort  # ClaudeService.choose_action과 같은 노력 수준
    print(f"model: {settings.claude_model}, effort: {effort}, "
          f"schema: {len(json.dumps(schema, ensure_ascii=False))} chars")
    started = time.perf_counter()
    async with anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=settings.claude_timeout_seconds,
                                        max_retries=0) as client:
        if settings.claude_model in FALLBACK_MODELS:
            create, extra = client.beta.messages.create, {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        else:
            create, extra = client.messages.create, {}
        try:
            response = await create(
                model=settings.claude_model,
                max_tokens=settings.claude_max_tokens,
                system=AGENT_INSTRUCTION,
                messages=[{"role": "user", "content": json.dumps({"text": TEXT, "context": CONTEXT}, ensure_ascii=False)}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                **extra,
            )
        except anthropic.APIStatusError as error:
            print(f"rejected: {type(error).__name__} {error.status_code}: {error.message}")
            return 1
        except anthropic.APIError as error:
            print(f"failed: {type(error).__name__}: {error}")
            return 1
    elapsed = time.perf_counter() - started
    raw = "".join(block.text for block in response.content if block.type == "text")
    print(f"stop_reason: {response.stop_reason}, seconds: {elapsed:.2f}")
    print(f"usage: input {response.usage.input_tokens}, output {response.usage.output_tokens}")
    print(f"raw: {raw}")
    try:
        action = parse_decision(from_wire(json.loads(raw).get("decision")))
    except (ValueError, AttributeError, TypeError):
        action = None
    print(f"parsed: {action!r}")
    return 0 if action is not None else 1


if __name__ == "__main__":
    loaded = Settings.from_env()
    if not loaded.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    sys.exit(asyncio.run(check(loaded)))
