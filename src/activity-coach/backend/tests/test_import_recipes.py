"""전처리 레시피 적재 명령 검증. 실제 52만 건 대신 같은 형식의 작은 gzip 자료를 쓴다."""
import gzip
import json
import sqlite3

import pytest

from app.retrieval import build_lexical_index
from scripts.import_recipes import RECIPE_FILES, main


def recipe(identifier, title, status="restricted"):
    return {"activity_id": identifier, "hobby_key": "cooking", "kind": "recipe", "language": "en",
            "title": title, "steps": [title + " step"], "tools": [], "duration": {"total_minutes": 10},
            "quality": {"status": status, "flags": ["tools_unknown"], "human_reviewed": False}}


def write_gz(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write("\n".join(json.dumps(row) for row in rows) + "\n")


def scope_ids(index_dir, scope):
    with sqlite3.connect(index_dir / "retrieval.sqlite") as db:
        return {row[0] for row in db.execute("SELECT id FROM records WHERE scope=?", (scope,))}


@pytest.fixture
def index_with_activity(tmp_path):
    """적재가 기존 활동 범위를 건드리지 않는지 보기 위해 활동 한 개를 먼저 색인한다."""
    activities = tmp_path / "activities.jsonl"
    activities.write_text(json.dumps({**recipe("cook-1", "rice"), "kind": "activity"}), encoding="utf-8")
    index_dir = tmp_path / "index"
    build_lexical_index([activities], index_dir)
    return index_dir


def test_imports_both_normalized_files_into_recipe_scope(tmp_path, index_with_activity):
    data = tmp_path / "data"
    data.mkdir()
    list_file, food_file = (data / name for name in RECIPE_FILES)
    write_gz(list_file, [recipe("recipe-a", "potato soup")])
    write_gz(food_file, [recipe("recipe-b", "soy dressing"), recipe("recipe-x", "bad", "quarantined")])

    summary = main([str(data), "--index-dir", str(index_with_activity)])

    assert summary["count"] == 2 and summary["skipped"] == 1 and summary["duplicate_ids"] == 0
    assert scope_ids(index_with_activity, "recipes") == {"recipe-a", "recipe-b"}
    assert scope_ids(index_with_activity, "activities") == {"cook-1"}


def test_missing_recipe_file_stops_before_replacing_index(tmp_path, index_with_activity):
    data = tmp_path / "data"
    data.mkdir()
    write_gz(data / RECIPE_FILES[0], [recipe("recipe-old", "old soup")])
    write_gz(data / RECIPE_FILES[1], [recipe("recipe-old2", "old dressing")])
    main([str(data), "--index-dir", str(index_with_activity)])
    (data / RECIPE_FILES[1]).unlink()

    with pytest.raises(SystemExit, match=RECIPE_FILES[1]):
        main([str(data), "--index-dir", str(index_with_activity)])

    assert scope_ids(index_with_activity, "recipes") == {"recipe-old", "recipe-old2"}
