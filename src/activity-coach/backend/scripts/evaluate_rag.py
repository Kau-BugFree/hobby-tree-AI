"""고정 질문 세트로 RAG 검색 정확도와 Claude 답변의 근거성을 함께 측정한다.

backend 폴더에서 실행한다.
    .venv\\Scripts\\python -m scripts.evaluate_rag            # 검색 + Claude (질문 수만큼 과금)
    .venv\\Scripts\\python -m scripts.evaluate_rag --no-llm   # 검색만, 과금 없음
결과는 var/eval/rag_eval_<UTC 시각>.json에 저장하고 요약 표를 출력한다.
Hit@k는 정답 활동이 상위 k개 안에 있는지, 정답 인용은 Claude가 정답 활동을 근거로 인용했는지다.
expected_ids가 빈 목록인 문항은 '근거가 없어야 하는' 질문이다. 아무것도 가져오지 않아야 맞다(거절률).
규칙 검증 통과는 사실 정확도·유용성을 대신하지 않으므로 보고서의 answer는 사람이 함께 읽어야 한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 스크립트 디렉터리에서 직접 실행해도 app 패키지를 찾도록 backend를 추가한다.
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_CASES = BACKEND / "data" / "eval_cases.jsonl"
GENERATED = {"generated", "generated_repaired"}
ANSWERED = GENERATED | {'reference'}
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens")


def load_cases(path: Path) -> list[dict[str, Any]]:
    """JSONL 질문 세트를 읽는다. expected_ids는 목록이어야 하며 빈 목록은 '근거 없음'이 정답이다."""
    cases = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for case in cases:
        if not case.get("question") or not isinstance(case.get("expected_ids"), list):
            raise ValueError(f"question과 expected_ids 목록이 필요합니다: {case.get('case_id')}")
    return cases


def score_case(case: dict[str, Any], result: dict[str, Any], top_k: int, use_llm: bool) -> dict[str, Any]:
    """질문 하나의 검색 적중·정답 인용·답변 생성 여부와 검토용 원자료를 묶는다."""
    ids = [item["activity_id"] for item in result["retrieval"]["items"]]
    expected = set(case["expected_ids"])
    positive = bool(expected)
    diagnostics = result["retrieval"]["diagnostics"]
    return {
        "case_id": case.get("case_id"), "question": case["question"], "expected_ids": case["expected_ids"],
        "retrieved_ids": ids,
        "hit_at_1": (bool(ids) and ids[0] in expected) if positive else None,
        "hit_at_k": bool(expected.intersection(ids[:top_k])) if positive else None,
        # 근거가 없어야 하는 질문은 아무것도 가져오지 않아야 맞다.
        "correct_rejection": None if positive else not ids,
        "cited_expected": bool(expected.intersection(result["cited_activity_ids"])) if use_llm and positive else None,
        "answered": result["status"] in ANSWERED if use_llm and positive else None,
        "generated": result['status'] in GENERATED if use_llm and positive else None,
        "status": result["status"], "answer": result["answer"],
        "cited_activity_ids": result["cited_activity_ids"], "validation_errors": result["validation_errors"],
        "search_mode": diagnostics.get("search_mode"), "reranker": diagnostics.get("reranker"),
        "ranking": diagnostics.get("ranking", []), "llm": result["llm"], "latency_ms": result["latency_ms"],
    }


def _rate(values: list[bool | None]) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(known) / len(known), 4) if known else None


def summarize(rows: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    """질문별 결과를 비율과 실제 호출 수·토큰 합계로 요약한다."""
    negatives = sum(1 for row in rows if row["correct_rejection"] is not None)
    return {
        "case_count": len(rows), "positive_count": len(rows) - negatives, "negative_count": negatives,
        "top_k": top_k,
        "hit_at_1": _rate([row["hit_at_1"] for row in rows]),
        "hit_at_k": _rate([row["hit_at_k"] for row in rows]),
        "rejection_rate": _rate([row["correct_rejection"] for row in rows]),
        "cited_expected_rate": _rate([row["cited_expected"] for row in rows]),
        "answered_rate": _rate([row["answered"] for row in rows]),
        "generated_rate": _rate([row.get('generated') for row in rows]),
        "llm_calls": sum(row["llm"]["calls"] for row in rows),
        "usage": {key: sum(row["llm"]["usage"].get(key, 0) for row in rows) for key in USAGE_KEYS},
    }


async def run_evaluation(asker: Any, cases: list[dict[str, Any]], top_k: int = 3,
                         use_llm: bool = True) -> dict[str, Any]:
    """질문을 순서대로 실행한다. GPU 모델과 API 요청 제한을 고려해 병렬로 보내지 않는다."""
    rows = []
    for case in cases:
        result = await asker.answer(case["question"], case.get("hobby_key", "cooking"),
                                    case.get("constraints") or {}, top_k, use_llm)
        rows.append(score_case(case, result, top_k, use_llm))
    return {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "use_llm": use_llm,
            "top_k": top_k, "summary": summarize(rows, top_k), "cases": rows}


def build_asker() -> Any:
    """서버와 같은 설정으로 실제 검색 모델과 Claude 클라이언트를 만든다."""
    from app.ask import RagAsk
    from app.claude import ClaudeService
    from app.config import Settings
    from app.retrieval import Retrieval

    settings = Settings.from_env()
    return RagAsk(Retrieval(settings), ClaudeService(settings))


def _mark(value: bool | None) -> str:
    return "-" if value is None else "O" if value else "X"


def print_table(report: dict[str, Any]) -> None:
    """질문별 적중·거절·인용·상태와 전체 요약을 콘솔 표로 출력한다."""
    print(f"{'case':<22} Hit@1 Hit@k 거절  인용  {'status':<20} question")
    for row in report["cases"]:
        print(f"{str(row['case_id']):<22} {_mark(row['hit_at_1']):^5} {_mark(row['hit_at_k']):^5} "
              f"{_mark(row['correct_rejection']):^4}  {_mark(row['cited_expected']):^4}  "
              f"{row['status']:<20} {row['question']}")
    summary = report["summary"]
    print(f"\nHit@1 {summary['hit_at_1']}  Hit@{summary['top_k']} {summary['hit_at_k']}  "
          f"거절률 {summary['rejection_rate']} (음성 {summary['negative_count']}건)  "
          f"정답 인용 {summary['cited_expected_rate']}  답변 생성 {summary['answered_rate']}  "
          f"LLM 호출 {summary['llm_calls']}  토큰 {summary['usage']}")


def main(argv: list[str] | None = None, asker: Any = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 검색 정확도와 Claude 답변 근거성 평가")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="질문 세트 JSONL")
    parser.add_argument("--top-k", type=int, default=3, choices=range(1, 6), help="근거로 넘길 상위 활동 수")
    parser.add_argument("--no-llm", action="store_true", help="검색만 평가하고 Claude를 호출하지 않음")
    parser.add_argument("--output", type=Path, help="보고서 경로 (기본: var/eval/rag_eval_<시각>.json)")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    report = asyncio.run(run_evaluation(asker or build_asker(), cases, args.top_k, not args.no_llm))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or BACKEND / "var" / "eval" / f"rag_eval_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_table(report)
    print(f"\n보고서: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
