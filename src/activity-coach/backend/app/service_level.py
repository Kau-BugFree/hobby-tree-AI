"""서비스 단계(설계 4.4·4.6절): Claude와 검색 모델을 쓸 수 있는지 보고, 둘 다 못 쓰면 검색이 필요한 요청을 거절합니다."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .answer_policy import PROVIDER_FAILURES

if TYPE_CHECKING:
    from .coach import Coach
    from .action_executors import Turn
    from .retrieval import Retrieval

# search() 진단에서 이번 검색의 모델 실패를 적는 키. 설정으로 끈 모델은 None이다.
MODEL_ERROR_KEYS = ("embedding_error", "reranker_error")


def claude_down(coach: Coach, turn: Turn) -> bool:
    """키가 없거나 이 턴에서 공급자 호출이 실패했으면 True.

    원문 안내·자료 조회로 내려갈지 정한다. 턴 해석(interpret_turn)을 못 하는 llm도 없는 것으로 본다.
    """
    return bool(turn.provider_failure) or coach.dialogue.model_down(turn.response["diagnostics"])


def claude_unavailable(llm: object, diagnostics: dict) -> bool:
    """거절 판단용 'Claude가 없다': 키가 없거나 이 턴의 호출이 실패했다.

    추천처럼 Claude를 부르지 않는 요청은 키가 있으면 되는 것으로 본다(설계 4.6절).
    claude_down과 달리 interpret_turn 유무는 보지 않는다. 실제 ClaudeService에서는 둘이 같다.
    """
    return not getattr(llm, "configured", True) or diagnostics.get("llm_status") in PROVIDER_FAILURES


def broken_models(retrieval: Retrieval) -> list[str]:
    """설정으로 켰는데 불러오지 못한 모델의 사유. 설정으로 끈 모델은 고장이 아니다."""
    errors = retrieval.model_errors
    return [error for error, enabled in ((errors["embedding"], retrieval.embedding_enabled),
                                         (errors["reranker"], retrieval.reranker_enabled)) if enabled and error]


def refuse(response: dict, reasons: list[str]) -> None:
    """거절 응답을 적습니다. 찾은 활동·근거는 보여 주지 않습니다."""
    from .dialogue import DialogueCoach  # dialogue가 이 모듈을 가져오므로 순환을 피해 여기서 가져온다
    DialogueCoach._stop(response, "service_unavailable", ",".join(reasons))
    response.update(activities=[], sources=[])
    response["diagnostics"]["search_models"] = reasons


def search_refused(coach: Coach, turn: Turn, found: dict | None = None) -> bool:
    """Claude가 없고 검색 모델이 고장이면(불러오지 못함, 또는 이 검색 found의 추론 실패) 거절을 적고 True."""
    if not claude_unavailable(coach.llm, turn.response["diagnostics"]):
        return False
    reasons = broken_models(coach.retrieval)
    if found is not None:
        reasons += [found["diagnostics"][key] for key in MODEL_ERROR_KEYS if found["diagnostics"].get(key)]
    if not reasons:
        return False
    refuse(turn.response, list(dict.fromkeys(reasons)))
    return True


def rank_refused(llm: object, error: str | None, response: dict) -> bool:
    """rank_evidence가 돌려준 실패 사유 error가 있고 Claude도 없으면 거절을 적고 True."""
    if not error or not claude_unavailable(llm, response["diagnostics"]):
        return False
    refuse(response, [error])
    return True
