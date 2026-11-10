"""RAG 검증 스크립트의 지표 계산과 보고서 저장을 실제 모델·API 없이 검사한다."""

import asyncio
import json

from scripts.evaluate_rag import DEFAULT_CASES, load_cases, main, run_evaluation


class FakeAsker:
    """질문별로 정해 둔 검색·생성 결과를 돌려주는 RagAsk 대역."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def answer(self, question, hobby_key="cooking", constraints=None, top_k=3, use_llm=True):
        self.calls.append({"question": question, "constraints": constraints, "top_k": top_k, "use_llm": use_llm})
        ids, cited, status = self.results[question]
        return {"question": question, "answer": "답변" if status == "generated" else None, "status": status,
                "cited_activity_ids": cited, "validation_errors": [],
                "retrieval": {"items": [{"rank": i, "activity_id": aid, "title": aid} for i, aid in enumerate(ids, 1)],
                              "diagnostics": {"search_mode": "hybrid_bm25_dense_rrf", "reranker": "cross_encoder"}},
                "evidence": [], "llm": {"model": "fake", "calls": 1 if use_llm else 0,
                                        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0}},
                "latency_ms": {"retrieval": 1.0, "llm": 2.0, "total": 3.0}}


CASES = [
    {"case_id": "a", "question": "질문A", "expected_ids": ["x"], "constraints": {"max_minutes": 15}},
    {"case_id": "b", "question": "질문B", "expected_ids": ["x"], "constraints": {}},
]


def test_scores_retrieval_hits_and_answer_citations():
    """Hit@1·Hit@k·정답 인용·답변 생성 비율을 질문별로 계산한다."""
    asker = FakeAsker({"질문A": (["x", "y", "z"], ["x"], "generated"),
                       "질문B": (["y", "x"], ["y"], "validation_failed")})
    report = asyncio.run(run_evaluation(asker, CASES, top_k=3, use_llm=True))
    first, second = report["cases"]
    assert (first["hit_at_1"], first["hit_at_k"], first["cited_expected"], first["answered"]) == (True, True, True, True)
    assert (second["hit_at_1"], second["hit_at_k"], second["cited_expected"], second["answered"]) == (False, True, False, False)
    summary = report["summary"]
    assert summary == {"case_count": 2, "positive_count": 2, "negative_count": 0, "top_k": 3,
                       "hit_at_1": 0.5, "hit_at_k": 1.0, "rejection_rate": None,
                       "cited_expected_rate": 0.5, "answered_rate": 0.5, "generated_rate":0.5, "llm_calls": 2,
                       "usage": {"input_tokens": 20, "output_tokens": 10, "cache_read_input_tokens": 0}}


def test_negative_cases_score_rejection_separately():
    """정답이 빈 목록인 문항은 근거를 하나도 가져오지 않아야 맞고, 적중률 분모에서 빠진다."""
    cases = CASES[:1] + [{"case_id": "n1", "question": "무관1", "expected_ids": []},
                         {"case_id": "n2", "question": "무관2", "expected_ids": []}]
    asker = FakeAsker({"질문A": (["x"], ["x"], "generated"),
                       "무관1": ([], [], "no_evidence"), "무관2": (["y"], ["y"], "generated")})
    report = asyncio.run(run_evaluation(asker, cases, top_k=3, use_llm=True))
    rows = {row["case_id"]: row for row in report["cases"]}
    assert rows["n1"]["correct_rejection"] is True and rows["n1"]["hit_at_1"] is None
    assert rows["n2"]["correct_rejection"] is False
    assert rows["a"]["correct_rejection"] is None
    summary = report["summary"]
    assert (summary["positive_count"], summary["negative_count"]) == (1, 2)
    assert summary["hit_at_1"] == 1.0
    assert summary["rejection_rate"] == 0.5
    assert summary["answered_rate"] == 1.0
    assert asker.calls[0]["constraints"] == {"max_minutes": 15}


def test_retrieval_only_mode_skips_answer_metrics():
    """--no-llm 모드는 생성 API를 쓰지 않고 답변 지표를 비워 둔다."""
    asker = FakeAsker({"질문A": (["x"], [], "skipped"), "질문B": (["y"], [], "skipped")})
    report = asyncio.run(run_evaluation(asker, CASES, top_k=3, use_llm=False))
    assert all(call["use_llm"] is False for call in asker.calls)
    assert report["summary"]["hit_at_1"] == 0.5
    assert report["summary"]["answered_rate"] is None
    assert report["summary"]["cited_expected_rate"] is None
    assert report["summary"]["llm_calls"] == 0


def test_default_cases_have_fixed_expected_ids():
    """기본 질문 세트는 실행 전에 고정한 정답(또는 '근거 없음')과 조건 없는 질문을 함께 갖는다."""
    cases = load_cases(DEFAULT_CASES)
    positives = [case for case in cases if case["expected_ids"]]
    negatives = [case for case in cases if not case["expected_ids"]]
    assert len(cases) == 32 and len(positives) == 27 and len(negatives) == 5
    assert sum(1 for case in positives if not case.get("constraints")) >= 18
    assert len({case["case_id"] for case in cases}) == 32


def test_main_writes_report_and_prints_summary(tmp_path, capsys):
    """CLI가 보고서 JSON을 저장하고 요약 표를 출력한다."""
    cases = tmp_path / "cases.jsonl"
    cases.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in CASES), encoding="utf-8")
    asker = FakeAsker({"질문A": (["x"], [], "skipped"), "질문B": (["x"], [], "skipped")})
    output = tmp_path / "report.json"
    assert main(["--cases", str(cases), "--no-llm", "--output", str(output)], asker=asker) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["summary"]["hit_at_1"] == 1.0
    assert saved["use_llm"] is False
    printed = capsys.readouterr().out
    assert "Hit@1" in printed and "질문A" in printed
