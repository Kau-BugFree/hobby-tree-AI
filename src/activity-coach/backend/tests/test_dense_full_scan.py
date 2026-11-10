"""조건으로 걸러진 문서가 없을 때 벡터 검색이 전체 행렬을 한 번에 훑는 경로를 검증한다.

실제 GPU 모델은 쓰지 않는다. 문서마다 유사도를 정해 둔 임베딩 대역으로 행과 ID의 대응을 확인한다.
"""
import json
import sqlite3

import numpy as np

from app import retrieval
from app.recipe_dense import build_recipe_dense
from app.retrieval import Retrieval, build_lexical_index
from test_retrieval import corpus, FakeEncoder


class GradedEncoder(FakeEncoder):
    """문서에 든 단어마다 정해 둔 유사도가 나오는 단위 벡터를 만든다. 질문 벡터는 [1, 0]이다."""

    max_length = 1024

    def __init__(self, grades, model_path=None):
        super().__init__()
        self.grades = grades
        self.model_path = model_path

    def encode_documents(self, texts):
        scores = [next((score for word, score in self.grades.items() if word in text), 0.0) for text in texts]
        return np.array([[score, (1 - score * score) ** 0.5] for score in scores], dtype="float32")


def dense_scores(result):
    """벡터 검색으로 들어온 후보의 ID별 유사도."""
    return {row["activity_id"]: row["dense_score"] for row in result["diagnostics"]["ranking"]
            if row["dense_score"] is not None}


def test_top_dense_keeps_highest_scores_and_breaks_boundary_ties_by_id():
    """경계 점수가 같은 문서는 ID 순으로 자른다. 조건별 순회 경로와 같은 후보가 나와야 한다."""
    scores = np.array([0.75, 0.5, 0.5, 0.5, 0.25], dtype="float32")
    ids = ["e", "d", "b", "c", "a"]
    assert retrieval.top_dense(scores, ids, 0.0, limit=3) == [(0.75, "e"), (0.5, "b"), (0.5, "c")]
    assert retrieval.top_dense(scores, ids, 0.0, limit=40) == [
        (0.75, "e"), (0.5, "b"), (0.5, "c"), (0.5, "d"), (0.25, "a")]


def test_top_dense_drops_scores_below_minimum():
    """상위 건수 안에 들어도 유사도 하한 미만은 후보로 쓰지 않는다."""
    scores = np.array([0.75, 0.5, 0.5, 0.5, 0.25], dtype="float32")
    assert retrieval.top_dense(scores, ["e", "d", "b", "c", "a"], 0.6, limit=3) == [(0.75, "e")]


def test_unfiltered_search_scores_the_whole_matrix(corpus):
    """조건이 없으면 전체 행렬에서 유사도를 구하고, 각 행의 점수를 그 행의 활동 ID에 붙인다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    service = Retrieval(settings, encoder=GradedEncoder({"드레싱": 1.0, "감자": 0.8, "수프": 0.6}))
    result = service.search("드레싱")
    assert result["diagnostics"].get("dense_scan") == "full_matrix"
    assert dense_scores(result) == {"dressing": 1.0, "potato": 0.8, "soup": 0.6}


def test_filtered_search_scores_only_eligible_documents(corpus):
    """시간 조건으로 문서가 빠지면 조건을 통과한 문서만 벡터 후보가 된다. 30분인 수프는 들어오지 않는다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    service = Retrieval(settings, encoder=GradedEncoder({"드레싱": 1.0, "감자": 0.8, "수프": 0.6}))
    result = service.search("드레싱", constraints={"max_minutes": 20})
    assert result["diagnostics"].get("dense_scan") == "filtered"
    assert dense_scores(result) == {"dressing": 1.0, "potato": 0.8}


def recipe_service(settings, tmp_path):
    """소요 시간이 다른 레시피 4건과 세대 벡터를 만든 검색 서비스.

    활동을 먼저 색인해 레시피의 rowid가 1부터 시작하지 않게 한다. ID 정렬 순서와 적재 순서도 다르다.
    """
    settings.embedding_enabled = True
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights").write_bytes(b"weights")
    encoder = GradedEncoder({"potato": 1.0, "tomato": 0.8, "garlic": 0.6}, model)
    Retrieval(settings, encoder=encoder)
    rows = [{"activity_id": identifier, "title": title, "steps": ["cook"], "duration": {"total_minutes": minutes}}
            for identifier, title, minutes in [("z-potato", "potato soup", 10), ("a-tomato", "tomato salad", 50),
                                               ("m-garlic", "garlic bread", 20), ("b-lemon", "lemon cake", 5)]]
    source = tmp_path / "recipes.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    build_lexical_index([source], settings.index_dir, "recipes")
    build_recipe_dense(settings.index_dir, encoder, expected_count=4)
    return Retrieval(settings, encoder=encoder)


def test_recipe_generation_rows_map_to_recipe_ids_in_load_order(corpus, tmp_path):
    """레시피 세대 벡터도 전체 행렬 경로를 쓴다. 행 순서는 ID 정렬 순서가 아니라 적재 순서다."""
    result = recipe_service(corpus[0], tmp_path).search("potato", scope="recipes")
    assert result["diagnostics"].get("dense_scan") == "full_matrix"
    assert dense_scores(result) == {"z-potato": 1.0, "a-tomato": 0.8, "m-garlic": 0.6}


def test_recipe_time_limit_keeps_only_eligible_rows(corpus, tmp_path):
    """시간 조건을 통과한 레시피의 행만 후보가 된다. 유사도가 높아도 50분인 토마토는 빠진다."""
    result = recipe_service(corpus[0], tmp_path).search("potato", scope="recipes", constraints={"max_minutes": 20})
    assert result["diagnostics"].get("dense_scan") == "filtered"
    assert dense_scores(result) == {"z-potato": 1.0, "m-garlic": 0.6}


def test_document_added_after_loading_never_lets_an_ineligible_vector_in(corpus, tmp_path):
    """서버가 벡터를 읽은 뒤 문서가 추가돼 조건 통과 수가 벡터 수와 같아져도 조건 밖 문서를 쓰지 않는다."""
    settings, _ = corpus
    service = recipe_service(settings, tmp_path)
    late = {"activity_id": "n-late", "title": "late pie", "steps": ["cook"], "duration": {"total_minutes": 10}}
    with sqlite3.connect(settings.index_dir / "retrieval.sqlite") as db:
        db.execute("INSERT INTO records (id,scope,hobby,total_minutes,tools_json,tools_known,quality,payload) "
                   "VALUES ('n-late','recipes','cooking',10,'[]',1,'restricted',?)", (json.dumps(late),))
    # 20분 이내 문서가 3건에서 4건이 되어 벡터 수(4)와 같다. 50분인 토마토는 여전히 조건 밖이다.
    result = service.search("potato", scope="recipes", constraints={"max_minutes": 20})
    assert dense_scores(result) == {"z-potato": 1.0, "m-garlic": 0.6}
    assert "a-tomato" not in [row["activity_id"] for row in result["items"]]


def test_vectors_are_not_loaded_when_documents_changed_without_reindexing(corpus):
    """색인 지문은 그대로인데 문서 수가 벡터 수와 다르면 그 범위의 벡터를 쓰지 않고 진단에 남긴다."""
    settings, _ = corpus
    settings.embedding_enabled = True
    encoder = GradedEncoder({"드레싱": 1.0})
    Retrieval(settings, encoder=encoder)
    with sqlite3.connect(settings.index_dir / "retrieval.sqlite") as db:
        db.execute("DELETE FROM records WHERE id='soup'")
    restarted = Retrieval(settings, encoder=encoder)
    assert restarted.status()["dense_index_scopes"] == []
    assert restarted.status()["index_errors"] == {"activities": "dense_index_unavailable"}
    assert restarted.search("드레싱")["diagnostics"]["search_mode"] == "bm25"
