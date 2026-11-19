"""Claude 없이 하는 자료 조회와 원문 추리기(설계 4.5절, 6절 4·5번)를 실제 API로 확인합니다.

관련도는 가짜 재순위 모델이 정한다. 질문 낱말이 든 문서는 기준(-2.0) 위, 아니면 아래 점수를 받는다.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ambiguity_routing import Interpreter
from test_answer_policy import evidence, policy
from test_api import FakeLLM, app_factory, create_session, message, select_activity  # noqa: F401
from test_claim_validation import Claims
from test_no_model_paths import FailingLLM
from test_retrieval import FakeEncoder

from app.answer_policy import REFERENCE_TOP_K, UNAVAILABLE_PREFIX
from app.config import Settings
from app.main import create_app
from app.retrieval import Retrieval
from app.turn_budget import TurnBudget

HEAD = "설명은 생성하지 못했지만 관련 자료를 찾았어요."
TAIL = "전체 원문이 필요하면 “원문 보여줘”라고 요청해 주세요."
DISH = "계량스푼 간장 드레싱"   # 요청 표현 없는 주제어. classify가 None이다.


class StubReranker:
    """질문 낱말(두 글자 이상)이 들어 있는 문서에 높은 점수를 주는 가짜 재순위 모델."""
    name = "stub"

    def load(self) -> None:
        pass

    def score(self, query: str, documents: list[str]) -> list[float]:
        words = [word for word in query.split() if len(word) > 1]   # '더' 같은 한 글자는 어디에나 있다
        return [5.0 if any(word in doc for word in words) else -9.0 for doc in documents]


class UnrelatedReranker(StubReranker):
    """모든 문서를 기준 아래로 매기는 가짜 재순위 모델(낱말은 맞아도 관련 없음)."""

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [-9.0] * len(documents)


class BrokenReranker(StubReranker):
    def score(self, query: str, documents: list[str]) -> list[float]:
        raise RuntimeError("CUDA out of memory")


def make_settings(tmp_path: Path, *, reranker: bool = False) -> Settings:
    return Settings(data_dir=Path(__file__).resolve().parents[1] / "data", index_dir=tmp_path / "index",
                    database_path=tmp_path / "coach.sqlite", embedding_enabled=False, reranker_enabled=reranker,
                    anthropic_api_key="", allowed_hosts=("testserver",))


def reranked_app(tmp_path: Path, reranker: object | None = None, llm: object | None = None, api_key: str = ""):
    settings = replace(make_settings(tmp_path, reranker=True), anthropic_api_key=api_key)
    retrieval = Retrieval(settings, reranker=reranker or StubReranker())
    return create_app(settings, retrieval=retrieval, llm=llm)


def test_unknown_text_without_model_shows_related_material_read_only(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client)
        listed = message(client, sid, "20분 안에 되는 요리 추천해줘", "r1")
        before = client.get(f"/sessions/{sid}").json()
        reply = message(client, sid, DISH, "u1")
        assert reply["text"].startswith(HEAD) and reply["sources"]
        assert reply["diagnostics"]["intent"] == "evidence_lookup"
        assert reply["mode"] == "reference" and reply["diagnostics"]["search_performed"] is True
        assert not reply["activities"] and not reply["actions"]
        after = client.get(f"/sessions/{sid}").json()
        for key in ("candidate_ids", "selected_activity_id", "constraints", "dialogue"):
            assert after[key] == before[key]
        assert listed["activities"]


def test_related_activities_are_bullets_not_a_numbered_list(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client)
        reply = message(client, sid, DISH, "b1")
        body = reply["text"][len(HEAD):].strip().split("\n")
        assert body and all(line.startswith("- ") for line in body)
        assert [line[2:] for line in body] == [s["title"] for s in reply["sources"]]


def test_unknown_text_with_no_relevant_material_asks_back(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path, UnrelatedReranker())) as client:
        sid = create_session(client)
        reply = message(client, sid, DISH, "u2")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["intent"] == "ambiguous"
        assert not reply["sources"]


def test_keyword_only_mode_asks_back_without_searching(app_factory) -> None:
    with TestClient(app_factory()) as client:   # 재순위가 설정으로 꺼져 있다
        sid = create_session(client)
        reply = message(client, sid, DISH, "k1")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["intent"] == "ambiguous"
        assert reply["diagnostics"]["search_performed"] is False


def test_reply_without_a_topic_is_asked_back(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client)
        reply = message(client, sid, "예", "y1")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["search_performed"] is False


@pytest.mark.parametrize("text", ["2번으로 하고 바로 시작할게", "1번만 더 할게"])
def test_number_after_a_list_is_asked_back(tmp_path: Path, text: str) -> None:
    # 후보 목록이 있을 때 번호를 말한 말은 검색 전에 거른다. '2번으로'는 candidate_number,
    # '1번만'(ORDINAL은 횟수로 봄)은 NUMBER_MENTION이 잡는다.
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client)
        listed = message(client, sid, "20분 안에 되는 요리 추천해줘", "n1")
        assert listed["activities"]
        reply = message(client, sid, text, "n2")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["intent"] == "ambiguous"
        assert reply["selected_activity_id"] is None


@pytest.mark.parametrize("text", ["2번으로 하고 바로 시작할게", "1번만 더 할게"])
def test_number_after_a_list_is_not_searched(tmp_path: Path, text: str) -> None:
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client)
        message(client, sid, "20분 안에 되는 요리 추천해줘", "c1")
        reply = message(client, sid, text, "c2")
        assert reply["diagnostics"]["search_performed"] is False


def test_deictic_without_selection_is_not_searched(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client)
        reply = message(client, sid, "이거 어떻게 해?", "u3")
        assert reply["mode"] == "clarify"
        assert reply["diagnostics"]["search_performed"] is False


def test_rank_evidence_keeps_rows_above_threshold_in_score_order(tmp_path: Path) -> None:
    service = Retrieval(make_settings(tmp_path, reranker=True), reranker=StubReranker())
    rows = [{"evidence_id": "a", "excerpt": "설탕은 불을 끄고 넣는다"},
            {"evidence_id": "b", "excerpt": "간장은 15ml"}]
    ranked, error = service.rank_evidence("설탕", rows)
    assert [r["evidence_id"] for r in ranked] == ["a"] and error is None
    assert [r["evidence_id"] for r in rows] == ["a", "b"]
    assert service.rank_evidence("설탕", []) == ([], None)


def test_rank_evidence_is_none_when_reranker_disabled(tmp_path: Path) -> None:
    service = Retrieval(make_settings(tmp_path))
    assert service.rank_evidence("설탕", [{"evidence_id": "a", "excerpt": "설탕"}]) == (None, None)


def test_rank_evidence_failure_is_returned_with_none(tmp_path: Path) -> None:
    service = Retrieval(make_settings(tmp_path, reranker=True), reranker=BrokenReranker())
    assert service.rank_evidence("설탕", [{"evidence_id": "a", "excerpt": "설탕"}]) == (None, "reranker_inference_failed")


class MarkedFailureReranker(StubReranker):
    """질문에 '실패'가 들어 있을 때만 추론이 실패하는 재순위 모델."""

    def score(self, query: str, documents: list[str]) -> list[float]:
        if "실패" in query:
            raise RuntimeError("CUDA out of memory")
        return super().score(query, documents)


@pytest.mark.parametrize("order", [("실패", "설탕"), ("설탕", "실패")])
def test_rank_evidence_reason_belongs_to_each_call(tmp_path: Path, order: tuple[str, str]) -> None:
    # 사유를 인스턴스에 남기면 다른 세션의 호출이 덮어쓴다. 호출마다 자기 결과를 돌려받아야 한다.
    service = Retrieval(make_settings(tmp_path, reranker=True), reranker=MarkedFailureReranker())
    rows = [{"evidence_id": "a", "excerpt": "설탕은 불을 끄고 넣는다"}]
    results = {query: service.rank_evidence(query, rows) for query in order}
    assert results["실패"] == (None, "reranker_inference_failed")
    assert results["설탕"] == (rows, None)
    assert not hasattr(service, "last_rank_error")


def test_unjudged_state_sentence_looks_in_the_selected_activity_first(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path)) as client:
        sid = create_session(client); select_activity(client, sid)
        # 문제 낱말도 과거형 어미도 없는 상태 문장. '간장이 너무 짜요'는 cook-0-2 자료에서 찾지 못해 바꿨다.
        reply = message(client, sid, "설탕이 아직 덜 녹은 상태예요", "u4")
        assert reply["diagnostics"]["intent"] == "evidence_lookup"
        assert reply["text"].startswith(HEAD) and reply["sources"]
        assert all(source["activity_id"] == "cook-0-2" for source in reply["sources"])
        session = client.get(f"/sessions/{sid}").json()
        assert session["dialogue"]["observations"] == []


def test_handoff_after_provider_failure_looks_up_material(tmp_path: Path) -> None:
    app = reranked_app(tmp_path, llm=FailingLLM("timeout"), api_key="x")
    with TestClient(app) as client:
        sid = create_session(client)
        reply = message(client, sid, DISH, "h1")
        assert reply["diagnostics"]["intent"] == "evidence_lookup"
        assert reply["diagnostics"]["llm_status"] == "timeout"
        assert reply["text"].startswith(HEAD)


def test_working_model_unclear_is_asked_back_without_search(tmp_path: Path) -> None:
    with TestClient(reranked_app(tmp_path, llm=Interpreter("lookup"), api_key="x")) as client:
        sid = create_session(client)
        reply = message(client, sid, DISH, "w1")
        assert reply["diagnostics"]["intent"] == "ambiguous" and reply["mode"] == "clarify"
        assert reply["diagnostics"]["search_performed"] is False


def test_source_text_without_model_shows_the_most_relevant_passages(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, reranker=True)
    retrieval = Retrieval(settings, reranker=StubReranker())
    with TestClient(create_app(settings, retrieval=retrieval)) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, "설탕이 녹지 않아 망했어요", "t1")   # 규칙이 문제 보고로 정하는 말
        assert reply["mode"] == "reference"
        assert 0 < len(reply["sources"]) <= REFERENCE_TOP_K
        assert [s["evidence_id"] for s in reply["sources"]] == ["cook-0-2:steps"]
        assert reply["text"].endswith(TAIL)
        full = message(client, sid, "원문 보여줘", "t2")
        # 원문 요청은 추리지 않는다: 활동 원문 전체(준비물·모든 단계)가 그대로 나온다.
        assert [s["evidence_id"] for s in full["sources"]] == ["cook-0-2:source"]
        assert all(step in full["text"] for step in retrieval.get("cook-0-2")["steps"])
        assert TAIL not in full["text"]


def _rows() -> list[dict]:
    first, second = evidence(), evidence()
    second.update(evidence_id="e2", excerpt="뜨거운 물에 주의해요")
    return [first, second]


def _policy_answer(question: str, rows: list[dict], llm: object, ranked: list[dict] | None) -> dict:
    return asyncio.run(policy().answer(question, rows, {"allowed_activity_ids": ["a"]}, [], llm, TurnBudget(),
                                       ranked=ranked))


def test_ranked_rows_narrow_the_source_text_only() -> None:
    rows = _rows()
    narrowed = _policy_answer("설명해줘", rows, object(), [rows[1]])
    assert narrowed["cited_evidence_ids"] == ["e2"] and narrowed["text"].endswith(TAIL)
    assert "큰술" not in narrowed["text"]
    unranked = _policy_answer("설명해줘", rows, object(), None)
    assert unranked["cited_evidence_ids"] == ["e", "e2"] and TAIL not in unranked["text"]
    original = _policy_answer("원문 보여줘", rows, object(), [rows[1]])
    assert original["cited_evidence_ids"] == ["e", "e2"] and TAIL not in original["text"]


def test_ranked_rows_keep_rank_order_and_always_add_the_tail() -> None:
    rows = _rows()
    result = _policy_answer("설명해줘", rows, object(), [rows[1], rows[0]])
    assert result["cited_evidence_ids"] == ["e2", "e"] and result["text"].endswith(TAIL)
    assert result["text"].index("뜨거운 물") < result["text"].index("큰술")


def test_empty_ranking_leaves_the_source_text_unchanged() -> None:
    rows = _rows()
    assert _policy_answer("설명해줘", rows, object(), []) == _policy_answer("설명해줘", rows, object(), None)


def test_ranked_rows_never_change_a_generated_answer() -> None:
    rows = _rows()
    for row in rows:
        row["review_status"] = "reviewed"
    result = _policy_answer("설명해줘", rows, Claims(), [rows[1]])
    assert result["mode"] == "generated" and result["text"] == "큰술 15ml."
    assert [s["evidence_id"] for s in result["sources"]] == ["e", "e2"]


def test_provider_failure_shows_the_narrowed_source_text_with_the_prefix() -> None:
    rows = _rows()
    for row in rows:
        row["review_status"] = "reviewed"
    result = _policy_answer("설명해줘", rows, FailingLLM("timeout"), [rows[1]])
    assert result["text"].startswith(UNAVAILABLE_PREFIX) and result["text"].endswith(TAIL)
    assert [s["evidence_id"] for s in result["sources"]] == ["e2"]


# 설계 4.6절: Claude도 검색 모델도 못 쓰면 검색이 필요한 요청을 거절한다.
REFUSAL = "지금은 자료를 찾을 수 없어 답변을 드리기 어려워요. 잠시 후 다시 시도해 주세요."
# 거절한 턴에서 바뀌면 안 되는 세션 값
SEARCH_STATE = ("constraints", "search_scope", "search_query", "unmet_query", "candidate_ids", "shown_ids",
                "candidate_selection_pending", "last_recommendation")


class UnloadableReranker(StubReranker):
    def load(self) -> None:
        raise RuntimeError("model file missing")


class SwitchableReranker(StubReranker):
    """broken을 켜면 그때부터 추론이 실패하는 재순위 모델."""
    broken = False

    def score(self, query: str, documents: list[str]) -> list[float]:
        if self.broken:
            raise RuntimeError("CUDA out of memory")
        return super().score(query, documents)


def broken_app(tmp_path: Path, *, llm: object | None = None, api_key: str = "", reranker: object | None = None):
    """재순위 모델이 켜져 있지만 쓸 수 없는 앱."""
    return reranked_app(tmp_path, reranker or BrokenReranker(), llm, api_key)


def assert_refused(reply: dict) -> None:
    assert reply["text"] == REFUSAL
    assert reply["diagnostics"]["stop_reason"] == "service_unavailable"
    assert reply["activities"] == [] and reply["sources"] == []
    assert reply["diagnostics"]["search_models"]


def test_no_model_and_broken_search_model_refuses_recommendation(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path)) as client:                      # 키 없음
        sid = create_session(client)
        reply = message(client, sid, "20분 안에 되는 요리 추천해줘", "x1")
        assert_refused(reply)
        assert reply["diagnostics"]["search_models"] == ["reranker_inference_failed"]
        assert reply["diagnostics"]["intent"] == "recommend"   # 검색 뒤 거절도 조건을 되돌렸다
        session = client.get(f"/sessions/{sid}").json()
        assert session["candidate_ids"] == [] and session["constraints"].get("max_minutes") is None


def test_broken_search_model_still_serves_requests_that_need_no_search(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client); select_activity(client, sid)
        assert message(client, sid, "준비물 알려줘", "x2")["diagnostics"]["intent"] == "activity_lookup"
        assert message(client, sid, "시작할게", "x3")["mode"] == "action"
        assert message(client, sid, "안녕", "x4")["diagnostics"]["intent"] == "greeting"


@pytest.mark.parametrize("before,text,intent", [
    ("시작할게", "활동 완료했어", "complete"),
    (None, "완료 목록 보여줘", "records"),
    (None, "확실해?", "metacognition"),
    (None, "뭘 도와줄 수 있어?", "capabilities"),
    (None, "날씨 어때?", "out_of_scope"),
])
def test_broken_search_model_serves_other_requests_that_need_no_search(tmp_path: Path, before: str | None,
                                                                      text: str, intent: str) -> None:
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client); select_activity(client, sid)
        if before:
            assert message(client, sid, before, "m0")["mode"] == "action"
        reply = message(client, sid, text, "m1")
        assert reply["diagnostics"]["intent"] == intent and reply["text"] != REFUSAL
        assert reply["diagnostics"].get("stop_reason") != "service_unavailable"
        if intent == "complete":
            assert reply["mode"] == "action" and reply["record"]["created"] is True


def test_model_available_and_broken_search_model_keeps_keyword_search(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path, llm=FakeLLM(), api_key="x")) as client:
        sid = create_session(client)
        reply = message(client, sid, "20분 안에 되는 요리 추천해줘", "x5")
        assert reply["text"] != REFUSAL and reply["activities"]
        assert reply["diagnostics"]["reranker_error"] == "reranker_inference_failed"


def test_search_models_disabled_by_settings_is_not_a_refusal(app_factory) -> None:
    with TestClient(app_factory()) as client:                              # 기존 테스트 설정
        sid = create_session(client)
        assert message(client, sid, "20분 안에 되는 요리 추천해줘", "x6")["activities"]


def test_unloadable_reranker_refuses_recommendation_before_searching(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path, reranker=UnloadableReranker())) as client:
        sid = create_session(client)
        reply = message(client, sid, "20분 안에 되는 요리 추천해줘", "x7")
        assert_refused(reply)
        assert reply["diagnostics"]["search_performed"] is False
        assert reply["diagnostics"]["search_models"] == ["reranker_model_unavailable"]
        assert reply["diagnostics"]["intent"] == "recommend"   # 말한 조건(20분)은 반영하지 않았다


@pytest.mark.parametrize("llm,api_key", [(None, ""), (FailingLLM("timeout"), "x")], ids=["no_key", "provider_failure"])
def test_broken_reranker_refuses_problem_report_on_selected_activity(tmp_path: Path, llm: object | None,
                                                                    api_key: str) -> None:
    # 설계 4.6절: 거절한 턴은 대화 상태를 바꾸지 않는다. 다시 보내도 관찰이 쌓이지 않는다.
    with TestClient(broken_app(tmp_path, llm=llm, api_key=api_key)) as client:
        sid = create_session(client); select_activity(client, sid)
        before = client.get(f"/sessions/{sid}").json()["dialogue"]
        for request_id in ("x8", "x8-again"):
            reply = message(client, sid, "반죽이 자꾸 찢어져요", request_id)
            assert_refused(reply)
            assert reply["issue_id"] is None and reply["issue_phase"] == before["phase"]
            dialogue = client.get(f"/sessions/{sid}").json()["dialogue"]
            assert dialogue == before
            assert dialogue["issue_id"] is None and dialogue["observations"] == []


def test_broken_reranker_refuses_unclassified_dish_sentence(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client)
        reply = message(client, sid, DISH, "x9")
        assert_refused(reply)
        assert reply["diagnostics"]["intent"] != "ambiguous"


def test_broken_reranker_still_asks_back_a_reply_without_topic(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client)
        reply = message(client, sid, "예", "x10")
        assert reply["mode"] == "clarify" and reply["diagnostics"]["search_performed"] is False
        assert reply["diagnostics"].get("stop_reason") != "service_unavailable"


def test_refused_recommendation_leaves_the_session_as_it_was(tmp_path: Path) -> None:
    reranker = SwitchableReranker()
    with TestClient(broken_app(tmp_path, reranker=reranker)) as client:
        sid = create_session(client)
        listed = message(client, sid, "계란 요리 추천해줘", "x11")   # 모델이 되는 동안 받은 목록
        assert listed["activities"]
        before = client.get(f"/sessions/{sid}").json()
        reranker.broken = True
        assert_refused(message(client, sid, "20분 안에 되는 요리 추천해줘", "x12"))
        after = client.get(f"/sessions/{sid}").json()
        assert after["constraints"].get("max_minutes") is None
        for key in SEARCH_STATE:
            assert after.get(key) == before.get(key), key


class QueryFailingEncoder(FakeEncoder):
    """문서 벡터는 만들지만 질문 임베딩은 늘 실패하는 임베딩 모델."""

    def encode_query(self, text: str) -> object:
        raise RuntimeError("CUDA out of memory")


NO_MATCH = "쿠스쿠스 타진 추천해줘"   # 어느 문서 낱말과도 맞지 않아 BM25 후보가 없다


def test_embedding_failure_is_not_hidden_by_the_starter_list(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), embedding_enabled=True)
    app = create_app(settings, retrieval=Retrieval(settings, encoder=QueryFailingEncoder()))
    with TestClient(app) as client:
        sid = create_session(client)
        reply = message(client, sid, NO_MATCH, "e1")
        assert_refused(reply)
        assert reply["diagnostics"]["search_models"] == ["embedding_inference_failed"]
        assert reply["diagnostics"]["search_mode"] == "bm25"   # 입문 목록(catalog_browse)으로 바꾸지 않았다


def test_broken_reranker_refuses_search_backed_explanation(tmp_path: Path) -> None:
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client)
        reply = message(client, sid, "간장 드레싱 어떻게 만들어?", "e3")
        assert reply["diagnostics"]["intent"] == "explain"
        assert_refused(reply)


def test_broken_reranker_refuses_explaining_the_selected_activity(tmp_path: Path) -> None:
    # 검색 없이 선택 활동의 원문을 추리다 재순위가 실패해도 거절한다(_answer와 같은 판단).
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, "지금 단계 설명해줘", "e4")
        assert reply["diagnostics"]["intent"] == "explain" and reply["diagnostics"]["search_performed"] is False
        assert_refused(reply)
        assert reply["diagnostics"]["search_models"] == ["reranker_inference_failed"]


def test_broken_reranker_still_shows_the_requested_original_text(tmp_path: Path) -> None:
    # 원문 요청은 저장된 원문을 읽을 뿐 추리지 않는다. 재순위가 고장이어도 거절하지 않는다.
    with TestClient(broken_app(tmp_path)) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, "원문 보여줘", "o1")
        assert reply["text"] != REFUSAL and reply["diagnostics"].get("stop_reason") != "service_unavailable"
        assert reply["mode"] == "reference" and [s["evidence_id"] for s in reply["sources"]] == ["cook-0-2:source"]
        steps = Retrieval(make_settings(tmp_path)).get("cook-0-2")["steps"]
        assert all(step in reply["text"] for step in steps)


def test_broken_reranker_still_shows_the_original_requested_with_a_report(tmp_path: Path) -> None:
    # 문제 보고에 원문 요청을 붙이면 대화 처리기가 받는다. 공급자도 실패한 턴에 그 경로도 원문을 추리지 않아 거절하지 않는다.
    with TestClient(broken_app(tmp_path, llm=FailingLLM("timeout"), api_key="x")) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, "반죽이 찢어졌어. 원문 보여줘", "o2")
        assert reply["diagnostics"]["intent"] == "coaching" and reply["diagnostics"]["llm_status"] == "timeout"
        assert reply["text"] != REFUSAL and reply["diagnostics"].get("stop_reason") != "service_unavailable"
        assert reply["mode"] == "reference" and reply["sources"]


def test_unloaded_model_with_claude_up_keeps_the_starter_list(tmp_path: Path) -> None:
    # 불러오지 못한 모델의 사유는 이번 검색의 실패가 아니다. Claude가 되면 입문 목록을 이전처럼 보여 준다.
    settings = replace(make_settings(tmp_path), embedding_enabled=True, anthropic_api_key="x")
    app = create_app(settings, retrieval=Retrieval(settings, encoder=FakeEncoder(fail=True)), llm=FakeLLM())
    with TestClient(app) as client:
        sid = create_session(client)
        reply = message(client, sid, NO_MATCH, "e5")
        assert reply["diagnostics"]["embedding_error"] == "embedding_model_unavailable"
        assert "요청과 직접 맞는 활동은 찾지 못했어요. 지금 조건에서 시간이 짧은 활동부터 보여드릴게요." in reply["text"]
        assert reply["activities"] and reply["diagnostics"]["starter_fallback"] is True
