"""검색의 제약·별칭·색인 버전 검증. 네트워크나 실제 GPU 모델을 호출하지 않는다.

임베딩·리랭커는 같은 인터페이스의 테스트 대역을 주입한다. 실제 BGE-m3-ko·KURE 경로는
scripts/evaluate_rag.py와 서버 실행으로 확인한다.
"""
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from app.retrieval import Retrieval, build_lexical_index, query_terms


def activity(identifier, title, minutes, tools=None, difficulty=None):
    """검색 규칙을 분명히 검증할 작은 자료를 만든다."""
    return {"activity_id": identifier, "hobby_key": "cooking", "kind": "activity", "language": "ko",
            "title": title, "description": title, "categories": ["공통"], "materials": [],
            "steps": [title + " 준비", title + " 완료"], "checks": [], "tools": tools or [],
            "difficulty": difficulty, "duration": {"total_minutes": minutes},
            "quality": {"status": "structural_pass", "flags": [], "human_reviewed": False}}


@pytest.fixture
def corpus(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    records = [
        activity("dressing", "간장 드레싱 만들기", 15, ["계량스푼", "작은 볼", "숟가락"]),
        activity("bread", "오븐으로 빵 만들기", 90, ["오븐", "볼"]),
        activity("unknown", "총시간 모르는 빵", None, ["오븐"]),
        activity("potato", "감자 팬 요리", 20, ["팬"]),
        activity("soup", "냄비 수프", 30, ["냄비"], difficulty=2),
    ]
    (data / "activities.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
    (data / "activity_aliases.json").write_text(json.dumps({"aliases": {"old-dressing": "dressing"}}))
    settings = SimpleNamespace(data_dir=data, index_dir=tmp_path / "index", embedding_enabled=False,
                               reranker_enabled=False, rerank_candidates=20)
    return settings, records


class FakeEncoder:
    """'드레싱' 문서와 질문을 같은 방향 벡터로 만드는 2차원 임베딩 대역."""

    name = "fake-encoder"

    def __init__(self, fail=False):
        self.fail = fail
        self.document_calls = 0

    def encode_documents(self, texts):
        self.document_calls += 1
        if self.fail:
            raise OSError("model files missing")
        return np.array([[1.0, 0.0] if "드레싱" in text else [0.0, 1.0] for text in texts], dtype="float32")

    def encode_query(self, text):
        return np.array([1.0, 0.0], dtype="float32")


class FakeReranker:
    """지정한 단어가 든 문서에 높은 점수를 주는 cross-encoder 대역."""

    name = "fake-reranker"

    def __init__(self, favorite, fail=False):
        self.favorite = favorite
        self.fail = fail
        self.calls = []

    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        if self.fail:
            raise RuntimeError("CUDA out of memory")
        return [5.0 if self.favorite in document else -5.0 for document in documents]


def test_alias_missing_id_and_specific_topic(corpus):
    """존재하지 않는 ID는 대체하지 않고 주제와 맞는 활동을 반환한다."""
    settings, _ = corpus
    service = Retrieval(settings)
    assert service.get("old-dressing")["activity_id"] == "dressing"
    assert service.get("missing") is None
    result = service.search("간장 드레싱 추천")
    assert result["items"][0]["activity_id"] == "dressing"
    assert result["diagnostics"]["search_mode"] == "bm25"


def test_numeric_filters_do_not_accept_unknown_values(corpus):
    """시간·난이도 미상 자료가 숫자 조건을 충족했다고 반환되는 것을 막는다."""
    settings, _ = corpus
    service = Retrieval(settings)
    short = service.search("요리 추천", constraints={"max_minutes": 20}, limit=10)
    assert {row["activity_id"] for row in short["items"]} == {"dressing", "potato"}
    difficulty = service.search("요리 추천", constraints={"max_difficulty": 2}, limit=10)
    assert [row["activity_id"] for row in difficulty["items"]] == ["soup"]
    budget = service.search("요리 추천", constraints={"budget_max": 10000})
    assert budget["items"] == []
    assert budget["diagnostics"]["missing_filter_fields"] == ["budget_max"]


def test_tools_constraints_apply_before_ranking(corpus):
    """동의어는 처리하되 필수 도구를 일부만 갖춘 경우와 금지 도구는 제외한다."""
    settings, _ = corpus
    service = Retrieval(settings)
    found = service.search("드레싱", constraints={"available_tools": ["계량스푼", "볼", "스푼"]})
    assert [row["activity_id"] for row in found["items"]] == ["dressing"]
    assert service.search("드레싱", constraints={"available_tools": ["볼"]})["items"] == []
    excluded = service.search("빵", constraints={"excluded_tools": ["오븐"]})
    assert excluded["items"] == []


def test_non_cooking_and_unknown_topics_are_empty(corpus):
    """다른 취미나 근거 없는 주제에 요리 결과를 억지로 붙이지 않는다."""
    settings, _ = corpus
    service = Retrieval(settings)
    assert service.search("러닝 추천")["items"] == []
    assert service.search("양자역학 강의")["items"] == []
    assert service.search("양자역학 설명")["items"] == []
    assert service.search("추천", hobby_key="reading")["items"] == []


def test_off_topic_word_inside_cooking_question_does_not_block_search(corpus):
    """'날씨가 추운데 수프 요리'처럼 요리 질문에 섞인 주제 밖 단어로 검색을 막지 않는다."""
    settings, _ = corpus
    found = Retrieval(settings).search("날씨가 추운데 수프 요리 추천")
    assert [row["activity_id"] for row in found["items"]] == ["soup"]


def test_empty_result_reports_why(corpus):
    """주제 밖 질문과 조건에 맞는 자료가 없는 경우를 진단에서 구분한다."""
    settings, _ = corpus
    service = Retrieval(settings)
    assert service.search("러닝 추천")["diagnostics"]["empty_reason"] == "outside_hobby_scope"
    assert service.search("추천", hobby_key="reading")["diagnostics"]["empty_reason"] == "no_eligible_documents"


def test_supplemental_scope_and_quarantine(corpus, tmp_path):
    """격리 자료는 색인하지 않고 보조 레시피를 앱 활동 목록과 분리한다."""
    settings, records = corpus
    service = Retrieval(settings)
    recipe = activity("external", "potato soup", None)
    recipe["kind"] = "recipe"
    recipe["quality"]["flags"] = ["tools_unknown"]
    bad = copy.deepcopy(recipe)
    bad["activity_id"] = "bad"
    bad["quality"]["status"] = "quarantined"
    file = tmp_path / "recipes.jsonl"
    file.write_text("\n".join(json.dumps(row) for row in [recipe, bad]))
    summary = build_lexical_index([file], settings.index_dir, "recipes")
    assert summary["count"] == 1 and summary["skipped"] == 1
    assert service.search("감자 수프", scope="recipes")["items"][0]["activity_id"] == "external"
    assert "external" not in [row["activity_id"] for row in service.list_activities()]
    assert service.search("감자", scope="recipes", constraints={"available_tools": []})["items"] == []


def test_dense_index_is_built_by_encoder_and_rebuilt_after_data_change(corpus):
    """주입한 임베딩으로 활동 벡터를 만들어 하이브리드 검색하고, 자료가 바뀌면 다시 만든다."""
    settings, records = corpus
    settings.embedding_enabled = True
    encoder = FakeEncoder()
    service = Retrieval(settings, encoder=encoder)
    assert encoder.document_calls == 1
    assert service.status()["dense_index_scopes"] == ["activities"]
    hybrid = service.search("드레싱", constraints={"max_minutes": 20})
    assert hybrid["diagnostics"]["search_mode"] == "hybrid_bm25_dense_rrf"
    assert hybrid["items"][0]["activity_id"] == "dressing"
    assert all(row["duration"]["total_minutes"] <= 20 for row in hybrid["items"])
    # 같은 자료·같은 모델이면 재시작해도 다시 임베딩하지 않는다.
    reused = FakeEncoder()
    Retrieval(settings, encoder=reused)
    assert reused.document_calls == 0
    records[0]["title"] = "다른 드레싱"
    (settings.data_dir / "activities.jsonl").write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    rebuilt = FakeEncoder()
    Retrieval(settings, encoder=rebuilt)
    assert rebuilt.document_calls == 1


def test_embedding_failure_does_not_claim_hybrid(corpus):
    """임베딩 모델 실패는 BM25로 내려가고 진단에 그대로 남는다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    service = Retrieval(settings, encoder=FakeEncoder(fail=True))
    result = service.search("드레싱")
    assert result["diagnostics"]["search_mode"] == "bm25"
    assert result["diagnostics"]["embedding_error"] == "embedding_model_unavailable"
    assert result["items"][0]["activity_id"] == "dressing"


class FlakyEncoder(FakeEncoder):
    """질문 임베딩이 첫 번째 호출에서만 실패하는 대역."""

    def __init__(self):
        super().__init__()
        self.query_calls = 0

    def encode_query(self, text):
        self.query_calls += 1
        if self.query_calls == 1:
            raise RuntimeError("CUDA out of memory")
        return super().encode_query(text)


def test_one_failed_query_embedding_does_not_mark_later_searches(corpus):
    """한 번의 추론 실패는 그 검색의 진단에만 남는다. 이후 검색과 상태를 계속 오류로 표시하지 않는다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    service = Retrieval(settings, encoder=FlakyEncoder())
    failed = service.search("드레싱")["diagnostics"]
    assert failed["embedding_error"] == "embedding_inference_failed" and failed["search_mode"] == "bm25"
    recovered = service.search("드레싱")["diagnostics"]
    assert recovered["embedding_error"] is None and recovered["search_mode"] == "hybrid_bm25_dense_rrf"
    assert service.status()["embedding_error"] is None


def test_fullwidth_and_decomposed_questions_keep_their_topic():
    """전각 영문과 분해형(NFD) 한글 질문도 정규화한 뒤 주제어를 뽑는다."""
    import unicodedata
    assert query_terms("ｐｏｔａｔｏ ｓｏｕｐ", "recipes")[0] == "potato soup"
    assert query_terms(unicodedata.normalize("NFD", "감자 수프"), "activities")[0] == "감자 수프"


def test_unreadable_activity_vector_manifest_is_rebuilt(corpus):
    """깨진 activities.dense.json은 모델 고장이 아니다. 벡터를 다시 만들고 임베딩을 유지한다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    Retrieval(settings, encoder=FakeEncoder())
    (settings.index_dir / "activities.dense.json").write_text("{broken", encoding="utf-8")
    encoder = FakeEncoder()
    status = Retrieval(settings, encoder=encoder).status()
    assert encoder.document_calls == 1
    assert status["embedding_error"] is None and status["index_errors"] == {}
    assert status["dense_index_scopes"] == ["activities"]


def test_unreadable_recipe_vector_manifest_falls_back_to_bm25_with_reason(corpus, tmp_path, caplog):
    """다시 만들 수 없는 레시피 벡터는 BM25로 내려가고, 원인을 상태와 로그에 남긴다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    Retrieval(settings, encoder=FakeEncoder())
    index_english_recipes(settings, tmp_path)
    (settings.index_dir / "recipes.dense.json").write_text("{broken", encoding="utf-8")
    service = Retrieval(settings, encoder=FakeEncoder())
    assert service.status()["index_errors"] == {"recipes": "dense_manifest_unreadable"}
    assert service.search("불고기", scope="recipes")["diagnostics"]["search_mode"] == "bm25"
    assert "recipes.dense.json" in caplog.text


def test_sqlite_error_while_loading_vectors_is_limited_to_that_scope(corpus, monkeypatch):
    """벡터를 연결하다 난 SQLite 오류가 서버 시작을 막지 않는다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    Retrieval(settings, encoder=FakeEncoder())
    original = Retrieval._current_manifest

    def failing_for_recipes(self, scope):
        if scope == "recipes":
            raise sqlite3.OperationalError("disk I/O error")
        return original(self, scope)

    monkeypatch.setattr(Retrieval, "_current_manifest", failing_for_recipes)
    status = Retrieval(settings, encoder=FakeEncoder()).status()
    assert status["index_errors"] == {"recipes": "dense_index_unavailable"}
    assert status["dense_index_scopes"] == ["activities"]


def write_knowledge(settings, review_status):
    """활동 'dressing'에 연결된 코칭 지식 한 건을 쓴다."""
    row = {"knowledge_id": "dressing:tip", "activity_ids": ["dressing"], "title": "계량 팁", "kind": "principle",
           "content": "드레싱 계량은 수평으로", "source_refs": ["manual"], "review_status": review_status}
    if review_status == "reviewed":
        row["reviewed_by"] = "tester"
    (settings.data_dir / "coaching_knowledge.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


def test_draft_only_knowledge_keeps_activity_embeddings(corpus):
    """검색할 지식이 모두 초안이면 지식 벡터를 만들지 않고 활동 하이브리드 검색은 유지한다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    write_knowledge(settings, "draft")
    service = Retrieval(settings, encoder=FakeEncoder())
    result = service.search("드레싱")
    assert result["diagnostics"]["search_mode"] == "hybrid_bm25_dense_rrf"
    assert service.status()["embedding_error"] is None


class KnowledgeFailingEncoder(FakeEncoder):
    """활동 문서는 임베딩하고 지식 문서에서만 실패하는 대역."""

    def encode_documents(self, texts):
        if any("수평으로" in text for text in texts):
            raise RuntimeError("knowledge batch failed")
        return super().encode_documents(texts)


def test_knowledge_embedding_failure_is_limited_to_knowledge(corpus):
    """지식 벡터 생성 실패는 지식 범위 오류로만 남고 활동 임베딩을 끄지 않는다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    write_knowledge(settings, "reviewed")
    service = Retrieval(settings, encoder=KnowledgeFailingEncoder())
    assert service.search("드레싱")["diagnostics"]["search_mode"] == "hybrid_bm25_dense_rrf"
    assert service.status()["embedding_error"] is None
    assert service.status()["index_errors"] == {"knowledge": "dense_index_unavailable"}


def test_unreadable_knowledge_vector_manifest_does_not_stop_startup(corpus):
    """깨진 knowledge.dense.json이 있어도 서버가 시작하고 지식 벡터를 다시 만든다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    write_knowledge(settings, "reviewed")
    Retrieval(settings, encoder=FakeEncoder())
    (settings.index_dir / "knowledge.dense.json").write_text("{broken", encoding="utf-8")
    status = Retrieval(settings, encoder=FakeEncoder()).status()
    assert status["dense_index_scopes"] == ["activities", "knowledge"]
    assert status["index_errors"] == {}


@pytest.mark.parametrize("favorite,expected", [("감자", "potato"), ("수프", "soup")])
def test_cross_encoder_decides_final_order_and_reports_scores(corpus, favorite, expected):
    """BM25 순서와 무관하게 리랭커 점수가 최종 순위를 정하고 단계별 점수를 진단에 남긴다."""
    settings, _ = corpus
    settings.reranker_enabled = True
    reranker = FakeReranker(favorite)
    service = Retrieval(settings, reranker=reranker)
    result = service.search("감자 팬이나 냄비 수프", limit=2)
    assert result["items"][0]["activity_id"] == expected
    assert result["diagnostics"]["reranker"] == "cross_encoder"
    assert result["diagnostics"]["reranker_model"] == "fake-reranker"
    query, documents = reranker.calls[0]
    assert query == "감자 팬이나 냄비 수프"
    assert len(documents) == len(result["diagnostics"]["ranking"]) == 2
    top = result["diagnostics"]["ranking"][0]
    assert top["activity_id"] == expected and top["final_rank"] == 1
    assert top["rerank_score"] == 5.0 and top["bm25_rank"] is not None


def test_reranker_floor_drops_irrelevant_candidates(corpus):
    """리랭커 하한보다 낮은 후보는 근거에서 빼고, 무엇을 뺐는지 진단에 남긴다."""
    settings, _ = corpus
    settings.reranker_enabled = True
    settings.rerank_min_score = -2.0
    service = Retrieval(settings, reranker=FakeReranker("감자"))
    result = service.search("감자 팬이나 냄비 수프", limit=2)
    assert [row["activity_id"] for row in result["items"]] == ["potato"]
    assert [row["passed"] for row in result["diagnostics"]["ranking"]] == [True, False]
    assert result["diagnostics"]["rerank_min_score"] == -2.0


def test_all_candidates_below_floor_return_no_evidence(corpus):
    """모든 후보가 하한 미만이면 억지로 근거를 채우지 않고 이유를 표시한다."""
    settings, _ = corpus
    settings.reranker_enabled = True
    settings.rerank_min_score = -2.0
    result = Retrieval(settings, reranker=FakeReranker("어디에도없음")).search("감자 팬이나 냄비 수프")
    assert result["items"] == []
    assert result["diagnostics"]["empty_reason"] == "below_rerank_threshold"


def test_topic_particle_is_removed_only_when_bare_noun_is_in_documents():
    """'밥은'처럼 문서에 없는 형태만 은/는/이/가를 떼고, '오이'처럼 문서에 있는 단어는 그대로 둔다."""
    assert query_terms("밥은 어떤게 맛있어", "activities", known_text="냄비밥짓기")[0].split()[0] == "밥"
    assert query_terms("오이 무침 알려줘", "activities", known_text="오이무침")[0].split()[0] == "오이"
    assert query_terms("밥은 어떤게 맛있어", "activities")[0].split()[0] == "밥은"


def test_question_with_topic_particle_finds_the_activity(corpus):
    """'수프는 어떻게 끓여?'처럼 주제 조사가 붙은 질문도 해당 활동을 찾는다."""
    settings, _ = corpus
    result = Retrieval(settings).search("수프는 어떻게 끓여?")
    assert result["items"][0]["activity_id"] == "soup"


def test_reranker_failure_keeps_rule_based_order(corpus):
    """리랭커 오류가 검색 실패나 가짜 리랭크 성공으로 숨겨지지 않는다."""
    settings, _ = corpus
    settings.reranker_enabled = True
    service = Retrieval(settings, reranker=FakeReranker("감자", fail=True))
    result = service.search("감자 팬이나 냄비 수프")
    assert result["items"]
    assert result["diagnostics"]["reranker"] == "rule_based"
    assert result["diagnostics"]["reranker_error"] == "reranker_inference_failed"
    assert all(row["rerank_score"] is None for row in result["diagnostics"]["ranking"])


def test_particles_and_compound_nouns_keep_the_topic(corpus):
    """조사·희망 표현이 붙어도 두 핵심 명사를 함께 가진 합성명사 제목을 먼저 찾는다."""
    settings, records = corpus
    records.extend([
        activity("brown-rice", "냄비현미밥", 45, ["냄비"]),
        activity("rice-ball", "현미 주먹밥", 20, ["냄비"]),
    ])
    records[-1]["steps"] = ["현미를 냄비로 데워 보고 손으로 뭉친다"]
    (settings.data_dir / "activities.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
    service = Retrieval(settings)
    subject, _ = query_terms("현미를 냄비로 익혀보고 싶어요", "activities")
    assert subject == "현미 냄비"
    result = service.search("현미를 냄비로 익혀보고 싶어요", constraints={"max_minutes": 60})
    assert result["items"][0]["activity_id"] == "brown-rice"


def test_korean_fragment_collisions_do_not_count_as_topic_matches(corpus):
    """페타/페타바이트 및 분리를/원리를 같은 글자 조각만 공유하는 문서를 거부한다."""
    settings, records = corpus
    cheese = activity("cheese", "페타 치즈 샐러드", 15)
    cheese["description"] = "소스의 분리를 관찰한다"
    records.append(cheese)
    (settings.data_dir / "activities.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
    service = Retrieval(settings)
    collision = service.search("페타바이트를 설명해줘")
    assert collision["items"] == []
    assert collision["diagnostics"]["lexical_rejected_without_topic"] >= 1
    assert service.search("유체역학의 원리를 알려줘")["items"] == []
    # 정상적인 음식 검색의 띄어쓰기 차이는 계속 허용한다.
    assert service.search("페타치즈 추천")["items"][0]["activity_id"] == "cheese"


def test_missing_topic_is_not_an_implicit_recommendation(corpus):
    """주제 없는 설명 요청은 비우고 명시적인 일반 추천 요청만 카탈로그로 돌린다."""
    settings, _ = corpus
    service = Retrieval(settings)
    empty = service.search("원리를 설명해주세요")
    assert empty["items"] == []
    assert empty["diagnostics"]["empty_reason"] == "missing_search_topic"
    assert service.search("15분 요리 추천", constraints={"max_minutes": 15})["items"]


def index_english_recipes(settings, tmp_path):
    """영어 원문 레시피 범위를 활동 색인 옆에 적재한다. 실제 레시피 자료와 같은 형식이다."""
    rows = []
    for identifier, title in [("bulgogi", "Beef Bulgogi"), ("kimchi-stew", "Kimchi Jjigae Stew"),
                              ("sprouts", "Bean Sprout Salad"), ("ramen", "Ramen Noodles")]:
        row = activity(identifier, title, 20)
        row.update(kind="recipe", language="en", description=None, steps=[title + " step"])
        rows.append(row)
    file = tmp_path / "recipes.jsonl"
    file.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    build_lexical_index([file], settings.index_dir, "recipes")


@pytest.mark.parametrize("query,expected", [("불고기", "bulgogi"), ("김치찌개 레시피", "kimchi-stew"),
                                            ("콩나물 무침", "sprouts"), ("라면 끓이기", "ramen")])
def test_korean_dish_name_finds_english_recipe(corpus, tmp_path, query, expected):
    """한국어 요리명은 별칭 사전을 거쳐 영어 원문 레시피를 찾는다."""
    settings, _ = corpus
    service = Retrieval(settings)
    index_english_recipes(settings, tmp_path)
    assert service.search(query, scope="recipes")["items"][0]["activity_id"] == expected


def test_recipe_reranker_scores_with_english_aliases(corpus, tmp_path):
    """영어 레시피는 한국어 질문만으로 재정렬 점수가 낮으므로 레시피 범위에서만 별칭을 함께 넘긴다."""
    settings, _ = corpus
    settings.reranker_enabled = True
    reranker = FakeReranker("Bulgogi")
    service = Retrieval(settings, reranker=reranker)
    index_english_recipes(settings, tmp_path)
    result = service.search("불고기 레시피", scope="recipes")
    assert reranker.calls[0][0] == "불고기 레시피 bulgogi"
    assert result["diagnostics"]["query_aliases"] == ["bulgogi"]
    assert result["items"][0]["activity_id"] == "bulgogi"


@pytest.mark.parametrize("query,present,absent", [
    ("초보라면 뭐가 좋아", None, "ramen"), ("라면을 끓이고 싶어", "ramen", None),
    ("오이스터 소스", "oyster", "cucumber"), ("오이 무침", "cucumber", None),
    ("애호박 볶음", "zucchini", "pumpkin"), ("타코야키", "takoyaki", "taco"),
])
def test_short_aliases_do_not_fire_inside_other_words(query, present, absent):
    """'초보라면'의 어미, '오이스터', '애호박', '타코야키' 안의 짧은 별칭은 다른 음식으로 바꾸지 않는다."""
    _, terms = query_terms(query, "recipes")
    if present:
        assert present in terms
    if absent:
        assert absent not in terms
