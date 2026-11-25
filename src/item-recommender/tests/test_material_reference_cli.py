"""Use the actual CLI with an isolated catalog and the BE fixture already in Git."""

import json
import sqlite3
from pathlib import Path

from item_recommender import cli
from item_recommender.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "item-material-export-v1.json"
ARGS = [
    "preview-material-references",
    "--file",
    str(FIXTURE),
    "--source-id",
    "synthetic-hobbytree",
    "--source-epoch",
    "11111111-1111-4111-8111-111111111111",
]


def test_cli_preview_twice_without_applying(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(cli.Settings, "from_env", lambda: Settings(catalog_db_path=db_path))
    assert cli.main(["init-db"]) == 0
    capsys.readouterr()
    for _ in range(2):
        assert cli.main(ARGS) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "PREVIEW"
        assert (report["referenceCount"], report["uniqueQueryCount"]) == (4, 3)
        assert set(report["insertKeys"]) == {'계량 "스푼"', "strasse", "책갈피"}
    with sqlite3.connect(db_path) as connection:
        for table in ("material_source_refs", "catalog_queries", "refresh_jobs"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_preview_does_not_initialize_missing_db(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(cli.Settings, "from_env", lambda: Settings(catalog_db_path=db_path))
    assert cli.main(ARGS) != 0
    assert not db_path.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "COMMAND_FAILED"
