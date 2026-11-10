"""RAG 검증 경로: 질문 하나를 검색 → 근거 구성 → Claude 답변 → 검증까지 대화 분기 없이 실행한다."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .answer_policy import AnswerPolicy, evidence_for
from .turn_budget import TurnBudget


def _elapsed_ms(start: float, end: float) -> float:
    return round((end - start) * 1000, 2)


class RagAsk:
    """검색 순위·근거·답변·지연을 한 결과로 묶는다. 세션이나 완료 기록은 저장하지 않는다."""

    def __init__(self, retrieval: Any, llm: Any):
        self.retrieval = retrieval
        self.llm = llm

    async def answer(self, question: str, hobby_key: str = "cooking", constraints: dict[str, Any] | None = None,
                     top_k: int = 3, use_llm: bool = True) -> dict[str, Any]:
        """use_llm=False면 검색만 수행해 생성 비용 없이 검색 정확도를 확인한다."""
        constraints = constraints or {}
        started = time.perf_counter()
        budget = TurnBudget()
        # 임베딩·리랭커 추론은 블로킹이므로 이벤트 루프 밖 스레드에서 실행한다.
        budget.consume('retrieval')
        try:
            async with asyncio.timeout(budget.remaining_seconds()):
                found = await asyncio.to_thread(self.retrieval.search, question, hobby_key, constraints, top_k, "activities")
        except TimeoutError:
            found = {'items':[], 'diagnostics':{'stop_reason':'budget_exhausted'}}
        retrieved = time.perf_counter()
        evidence = [evidence_for(item) for item in found["items"]]
        if not evidence or not use_llm:
            # 검색이 시간 안에 끝나지 않은 것은 '근거 없음'과 다르다.
            status = found["diagnostics"].get("stop_reason") or ("no_evidence" if not evidence else "skipped")
            result = {"text": None, "status": status, "llm_calls": 0,
                      "model": None, "usage": {}, "validation_errors": [], "cited_activity_ids": []}
        else:
            result = await AnswerPolicy().answer(question, evidence,
                {'constraints':constraints, 'allowed_activity_ids':[e['activity_id'] for e in evidence]}, [], self.llm, budget)
        finished = time.perf_counter()
        return {
            "question": question,
            "answer": result["text"],
            "status": result["status"],
            "cited_activity_ids": result.get("cited_activity_ids", []),
            "validation_errors": result.get("validation_errors", []),
            "validation": result.get('validation', {}),
            "metacognition": result.get('metacognition', {}),
            "retrieval": {
                "items": [{"rank": rank, "activity_id": item["activity_id"], "title": item["title"]}
                          for rank, item in enumerate(found["items"], 1)],
                "diagnostics": found["diagnostics"],
            },
            "evidence": evidence,
            "llm": {"model": result.get("model"), "calls": result["llm_calls"], "usage": result.get("usage", {})},
            "latency_ms": {"retrieval": _elapsed_ms(started, retrieved), "llm": _elapsed_ms(retrieved, finished),
                           "total": _elapsed_ms(started, finished)},
        }
