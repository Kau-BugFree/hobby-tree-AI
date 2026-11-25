"""Verify full-export decisions and persisted state on both catalog implementations."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier

import pytest

from item_recommender import cli
from item_recommender.catalog_store import SQLiteCatalogStore
from item_recommender.config import Settings
from test_material_reference_store import (
    EPOCH,
    OTHER_EPOCH,
    SAMPLE,
    SOURCE,
    persisted_rows,
    read_input,
)

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


@pytest.fixture
def store(material_catalog_factory):
    db = material_catalog_factory()
    try:
        yield db
    finally:
        db.close()


def state(db):
    return (
        persisted_rows(db, "catalog_queries", "query_key"),
        persisted_rows(db, "material_source_refs", "source_id, source_epoch, material_id"),
        persisted_rows(db, "material_export_receipts", "source_id, source_sequence"),
    )


def test_immutable_delivery_decisions(store, tmp_path):
    first = read_input(tmp_path, [(1, "바늘")])
    before = state(store)
    assert store.sync_material_export(first, NOW)["status"] == "APPLY"
    assert state(store) == before
    assert store.sync_material_export(first, NOW, apply=True)["status"] == "APPLIED"
    latest = replace(first, source_sequence=10)
    assert store.sync_material_export(latest, NOW, apply=True)["status"] == "APPLIED"
    baseline = state(store)
    assert len(baseline[2]) == 2
    for snapshot, expected in [
        (latest, "NOOP_REPLAY"),
        (first, "REJECT_STALE"),
        (
            replace(read_input(tmp_path, [(2, "다른 이름")]), source_sequence=10),
            "REJECT_SEQUENCE_CONFLICT",
        ),
        (replace(first, source_sequence=11, source_epoch=OTHER_EPOCH), "REJECT_EPOCH"),
        (replace(read_input(tmp_path, []), source_sequence=11), "HOLD_EMPTY"),
    ]:
        assert store.sync_material_export(snapshot, NOW, apply=True)["status"] == expected
        assert state(store) == baseline


def test_review_protects_manual_and_shared_queries(store, tmp_path):
    store.register_queries(["독서등"], NOW)
    first = read_input(
        tmp_path, [(1, " 독서등 "), (2, " 스푼 "), (3, "스푼"), (4, "피크"), (5, "책")]
    )
    store.sync_material_export(first, NOW, apply=True)
    other = replace(read_input(tmp_path, [(1, "책")]), source_id="other")
    store.sync_material_export(other, NOW, apply=True)
    second = replace(read_input(tmp_path, [(3, "스푼"), (4, "기타 피크")]), source_sequence=2)
    baseline = state(store)
    plan = store.sync_material_export(second, NOW)
    assert plan["status"] == "REVIEW_DEACTIVATION"
    assert plan["deactivationCandidates"] == ["피크"]
    assert store.sync_material_export(second, NOW, apply=True) == plan
    assert (
        store.sync_material_export(second, NOW, apply=True, approve_plan="wrong")["status"]
        == "REJECT_APPROVAL"
    )
    assert state(store) == baseline
    assert (
        store.sync_material_export(second, NOW, apply=True, approve_plan=plan["planSha256"])[
            "status"
        ]
        == "APPLIED"
    )
    queries, refs, receipts = state(store)
    assert {r["query_key"] for r in queries if r["active"]} == {"독서등", "스푼", "기타 피크", "책"}
    assert next(r for r in queries if r["query_key"] == "독서등")["origin"] == "MANUAL"
    assert {r["material_id"] for r in refs if r["source_id"] == SOURCE and r["is_current"]} == {
        3,
        4,
    }
    assert len(refs) == 6
    assert (
        next(r for r in receipts if r["source_sequence"] == 2)["approval_digest"]
        == plan["planSha256"]
    )
    third = replace(
        read_input(tmp_path, [(3, "스푼"), (4, "기타 피크"), (5, "책"), (6, "피크")]),
        source_sequence=3,
    )
    assert store.sync_material_export(third, NOW)["reactivateKeys"] == ["피크"]
    assert store.sync_material_export(third, NOW, apply=True)["status"] == "APPLIED"


def test_approval_expires_when_ownership_changes(store, tmp_path):
    first = read_input(tmp_path, [(1, "책"), (2, "피크")])
    store.sync_material_export(first, NOW, apply=True)
    second = replace(read_input(tmp_path, [(3, "스푼")]), source_sequence=2)
    preview = store.sync_material_export(second, NOW)
    store.register_queries(["책"], NOW)
    baseline = state(store)
    result = store.sync_material_export(second, NOW, apply=True, approve_plan=preview["planSha256"])
    assert result["status"] == "REJECT_APPROVAL"
    assert result["deactivationCandidates"] == ["피크"]
    assert result["planSha256"] != preview["planSha256"]
    assert state(store) == baseline


def test_receipt_failure_rolls_back_and_allows_retry(store, tmp_path):
    first = read_input(tmp_path, [(1, "스푼"), (2, "피크")])
    store.sync_material_export(first, NOW, apply=True)
    second = replace(read_input(tmp_path, [(1, "새 이름")]), source_sequence=2)
    token = store.sync_material_export(second, NOW)["planSha256"]
    baseline = state(store)
    table = store._table("material_export_receipts")
    with store._transaction() as conn:
        conn.execute(f"CREATE UNIQUE INDEX fail_second_receipt ON {table} (source_id)")
    import sqlite3

    if store._postgres:
        import psycopg

        error = psycopg.IntegrityError
    else:
        error = sqlite3.IntegrityError
    with pytest.raises(error):
        store.sync_material_export(second, NOW, apply=True, approve_plan=token)
    assert state(store) == baseline
    with store._transaction() as conn:
        conn.execute(f"DROP INDEX {store._table('fail_second_receipt')}")
    assert (
        store.sync_material_export(second, NOW, apply=True, approve_plan=token)["status"]
        == "APPLIED"
    )


def test_two_workers_accept_once(store, tmp_path):
    first = read_input(tmp_path, [(1, "스푼")])
    barrier = Barrier(2)
    peer = (
        type(store)(store._dsn)
        if store._postgres
        else SQLiteCatalogStore(tmp_path / "catalog.sqlite3")
    )

    def apply(db):
        barrier.wait(timeout=10)
        return db.sync_material_export(first, NOW, apply=True)["status"]

    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(apply, [store, peer])) == ["APPLIED", "NOOP_REPLAY"]
        assert len(state(store)[2]) == 1
    finally:
        peer.close()


def test_cli_sync_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli.Settings, "from_env", lambda: Settings(catalog_db_path=tmp_path / "cli.sqlite3")
    )

    def run(args):
        code = cli.main(args)
        return code, json.loads(capsys.readouterr().out)["status"]

    def args(path, *flags):
        return [
            "sync-material-export",
            "--source-id",
            SOURCE,
            "--source-epoch",
            EPOCH,
            "--file",
            str(path),
            *flags,
        ]

    assert run(["init-db"]) == (0, "INITIALIZED")
    read_input(tmp_path, [])
    assert run(args(tmp_path / "next.json", "--apply")) == (3, "HOLD_EMPTY")
    assert run(args(SAMPLE, "--approve-plan", "x")) == (2, "REJECT_APPROVAL")
    assert run(args(SAMPLE)) == (0, "APPLY")
    assert run(args(SAMPLE, "--apply")) == (0, "APPLIED")
    assert run(args(SAMPLE, "--apply")) == (0, "NOOP_REPLAY")
    assert run(args(tmp_path / "missing.json", "--apply")) == (2, "REJECT_FILE")


def test_sqlite_upgrade_preserves_previous_references(tmp_path):
    db = SQLiteCatalogStore(tmp_path / "old.sqlite3")
    try:
        db.initialize()
        first = read_input(tmp_path, [(1, "스푼")])
        db.sync_material_export(first, NOW, apply=True)
        with db._transaction() as conn:
            conn.execute("ALTER TABLE material_source_refs DROP COLUMN is_current")
            conn.execute("DROP TABLE material_export_receipts")
        db.initialize()
        db.initialize()
        queries, refs, receipts = state(db)
        assert len(queries) == len(refs) == 1
        assert refs[0]["is_current"] == 1
        assert receipts == []
    finally:
        db.close()
