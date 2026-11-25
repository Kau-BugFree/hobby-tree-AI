"""Test source ownership and preview boundaries against persisted catalog data."""

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from item_recommender.catalog_store import PostgresCatalogStore, SQLiteCatalogStore
from item_recommender.material_export import read_reference_snapshot

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/fixtures/item-material-export-v1.json"
SOURCE = "synthetic-hobbytree"
EPOCH = "11111111-1111-4111-8111-111111111111"
OTHER_EPOCH = "77777777-7777-4777-8777-777777777777"


def read_input(tmp_path, items):
    doc = json.loads(SAMPLE.read_bytes())
    rows = [
        {
            "materialId": str(material_id),
            "activityId": str(material_id + 10),
            "hobbyId": "1",
            "activityContentVersion": "2",
            "name": name,
        }
        for material_id, name in items
    ]
    doc["activityCount"] = doc["materialCount"] = len(rows)
    doc["payload"]["materials"] = rows

    def canonical(obj):
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    doc["payloadSha256"] = hashlib.sha256(canonical(doc["payload"])).hexdigest()
    path = tmp_path / "next.json"
    path.write_bytes(canonical(doc) + b"\n")
    return read_reference_snapshot(path, SOURCE, EPOCH)


def seed_existing_catalog(store):
    # Reproduce the catalog_queries table and rows present before upgrading.
    with store._transaction() as conn:
        queries = store._table("catalog_queries")
        store._execute(
            conn,
            f"""
            CREATE TABLE {queries} (
                query_key text PRIMARY KEY, query_text text NOT NULL, active integer NOT NULL,
                origin text NOT NULL, created_at double precision NOT NULL,
                updated_at double precision NOT NULL
            )
        """,
        )
        for key, active, origin in [
            ("계량스푼", 1, "BE_MATERIAL"),
            ("피크", 1, "BE_MATERIAL"),
            ("타원본", 1, "BE_MATERIAL"),
            ("타계보", 1, "BE_MATERIAL"),
            ("과거자동등록", 1, "BE_MATERIAL"),
            ("재공개", 0, "BE_MATERIAL"),
            ("독서등", 1, "MANUAL"),
            ("수동 유지", 1, "MANUAL"),
        ]:
            store._execute(
                conn,
                f"INSERT INTO {queries} VALUES (?, ?, ?, ?, 10, 20)",
                (key, key, active, origin),
            )


def persisted_rows(store, table, order):
    with store._transaction(write=False) as conn:
        rows = store._execute(
            conn, f"SELECT * FROM {store._table(table)} ORDER BY {order}"
        ).fetchall()
    return [dict(row) for row in rows]


def exercise_upgrade_and_preview(store, initialize, tmp_path):
    seed_existing_catalog(store)
    original = persisted_rows(store, "catalog_queries", "query_key")
    initialize()
    assert persisted_rows(store, "catalog_queries", "query_key") == original
    refs_order = "source_id, source_epoch, material_id"
    assert persisted_rows(store, "material_source_refs", refs_order) == []
    # Simulate the last accepted reference set; applying a snapshot belongs to step 3.
    with store._transaction() as conn:
        for source, epoch, mid, key, raw in [
            (SOURCE, EPOCH, 101, "계량스푼", " 계량스푼 "),
            (SOURCE, EPOCH, 102, "계량스푼", "계량스푼"),
            (SOURCE, EPOCH, 201, "피크", "피크"),
            (SOURCE, EPOCH, 301, "타원본", "타원본"),
            (SOURCE, EPOCH, 302, "타계보", "타계보"),
            (SOURCE, EPOCH, 303, "수동 유지", "수동 유지"),
            ("other-source", EPOCH, 301, "타원본", "타원본"),
            (SOURCE, OTHER_EPOCH, 302, "타계보", "타계보"),
        ]:
            store._execute(
                conn,
                f"""
                INSERT INTO {store._table("material_source_refs")}
                (source_id, source_epoch, material_id, activity_id, hobby_id,
                 activity_content_version, raw_name, query_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (source, epoch, mid, mid + 10, 1, 1, raw, key),
            )

    original_refs = persisted_rows(store, "material_source_refs", refs_order)
    initialize()
    assert len(original_refs) == 8
    assert persisted_rows(store, "material_source_refs", refs_order) == original_refs

    # One spoon activity was hidden, the pick was renamed, and two activities now use it.
    snapshot = read_input(
        tmp_path,
        [
            (102, "계량스푼"),
            (201, "기타 피크"),
            (202, "기타　피크"),
            (401, "재공개"),
            (402, "독서등"),
        ],
    )
    plan = store.preview_material_references(snapshot)
    assert plan["status"] == "PREVIEW"
    assert (plan["referenceCount"], plan["uniqueQueryCount"]) == (5, 4)
    assert plan["insertKeys"] == ["기타 피크"]
    assert plan["reactivateKeys"] == ["재공개"]
    assert set(plan["keepKeys"]) == {"계량스푼", "독서등", "수동 유지", "타원본", "타계보"}
    assert plan["deactivationCandidates"] == ["피크"]

    no_spoon = store.preview_material_references(read_input(tmp_path, [(402, "독서등")]))
    assert set(no_spoon["deactivationCandidates"]) == {"계량스푼", "피크"}
    empty = store.preview_material_references(read_input(tmp_path, []))
    assert empty["status"] == "HOLD_EMPTY"
    assert empty["deactivationCandidates"] == []
    assert persisted_rows(store, "catalog_queries", "query_key") == original
    assert persisted_rows(store, "material_source_refs", refs_order) == original_refs
    assert persisted_rows(store, "refresh_jobs", "query_key") == []


def test_sqlite_reference_preview(tmp_path):
    store = SQLiteCatalogStore(tmp_path / "catalog.sqlite3")
    try:
        exercise_upgrade_and_preview(store, store.initialize, tmp_path)
    finally:
        store.close()


def test_postgres_reference_preview(tmp_path):
    dsn = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN not configured")
    psycopg = pytest.importorskip("psycopg")
    schema = "ref_test_" + uuid.uuid4().hex

    class ScopedStore(PostgresCatalogStore):
        def _table(self, name):
            return f"{schema}.{name}"

    store = ScopedStore(dsn)
    migration = (ROOT / "sql/001_catalog.sql").read_text().replace("item_recommender", schema)

    def initialize():
        with store._transaction() as conn:
            conn.execute(migration)

    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            exercise_upgrade_and_preview(store, initialize, tmp_path)
        finally:
            store.close()
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
