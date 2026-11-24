"""Material synchronization against isolated synthetic PostgreSQL tables."""

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_database():
    dsn = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN not configured")
    psycopg = pytest.importorskip("psycopg")
    schema = "sync_test_" + uuid.uuid4().hex
    migration = (ROOT / "sql/001_catalog.sql").read_text(encoding="utf-8")
    migration = migration.replace("item_recommender", schema)
    sync = (ROOT / "sql/003_sync_activity_materials.sql").read_text(encoding="utf-8")
    sync = sync.replace("item_recommender.", f"{schema}.").replace("public.", f"{schema}.")
    sync = "\n".join(
        line for line in sync.splitlines() if line.strip() not in {"BEGIN;", "COMMIT;"}
    )
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        with connection.transaction(force_rollback=True):
            connection.execute(migration)
            connection.execute(
                f"""
                CREATE TABLE {schema}.hobbies (
                    hobby_id bigint PRIMARY KEY,
                    is_supported boolean NOT NULL
                );
                CREATE TABLE {schema}.hobby_stages (
                    hobby_stage_id bigint PRIMARY KEY,
                    hobby_id bigint REFERENCES {schema}.hobbies(hobby_id)
                );
                CREATE TABLE {schema}.activities (
                    activity_id bigint PRIMARY KEY,
                    hobby_stage_id bigint
                        REFERENCES {schema}.hobby_stages(hobby_stage_id),
                    review_status text NOT NULL
                );
                CREATE TABLE {schema}.activity_materials (
                    activity_id bigint
                        REFERENCES {schema}.activities(activity_id),
                    material_name text NOT NULL
                );
                """
            )
            yield connection, schema, sync


def add_material(connection, schema, activity_id, supported=True, review_status="PUBLISHED"):
    connection.execute(f"INSERT INTO {schema}.hobbies VALUES (%s, %s)", (activity_id, supported))
    connection.execute(
        f"INSERT INTO {schema}.hobby_stages VALUES (%s, %s)", (activity_id, activity_id)
    )
    connection.execute(
        f"INSERT INTO {schema}.activities VALUES (%s, %s, %s)",
        (activity_id, activity_id, review_status),
    )
    connection.execute(
        f"INSERT INTO {schema}.activity_materials VALUES (%s, %s)", (activity_id, "계량스푼")
    )


@pytest.mark.parametrize(
    ("supported", "review_status", "expected_count"),
    [
        (True, "PUBLISHED", 1),
        (False, "PUBLISHED", 0),
        (True, "DRAFT", 0),
        (False, "DRAFT", 0),
    ],
)
def test_sync_registers_only_supported_published_materials(
    sync_database, supported, review_status, expected_count
):
    connection, schema, sync = sync_database
    add_material(connection, schema, 1, supported, review_status)
    connection.execute(sync)
    rows = connection.execute(
        f"SELECT query_key, active, origin FROM {schema}.catalog_queries"
    ).fetchall()
    assert len(rows) == expected_count
    if expected_count:
        assert rows == [("계량스푼", 1, "BE_MATERIAL")]


@pytest.mark.parametrize(
    ("supported", "review_status", "shared", "origin", "expected_active"),
    [
        (False, "PUBLISHED", False, "BE_MATERIAL", 0),
        (True, "DRAFT", False, "BE_MATERIAL", 0),
        (False, "PUBLISHED", True, "BE_MATERIAL", 1),
        (False, "PUBLISHED", False, "MANUAL", 1),
        (True, "PUBLISHED", False, "BE_MATERIAL", 1),
    ],
)
def test_sync_deactivation_preserves_shared_and_manual_queries(
    sync_database, supported, review_status, shared, origin, expected_active
):
    connection, schema, sync = sync_database
    add_material(connection, schema, 1)
    connection.execute(sync)
    # Start with a genuinely active imported query, then change its eligibility.
    assert connection.execute(f"SELECT active FROM {schema}.catalog_queries").fetchone() == (1,)
    connection.execute(f"UPDATE {schema}.catalog_queries SET origin = %s", (origin,))
    connection.execute(
        f"UPDATE {schema}.hobbies SET is_supported = %s WHERE hobby_id = 1", (supported,)
    )
    connection.execute(
        f"UPDATE {schema}.activities SET review_status = %s WHERE activity_id = 1",
        (review_status,),
    )
    if shared:
        add_material(connection, schema, 2)
    connection.execute(sync)
    rows = connection.execute(
        f"SELECT query_key, active, origin FROM {schema}.catalog_queries"
    ).fetchall()
    assert rows == [("계량스푼", expected_active, origin)]
    # Repeat the sync to check that eligibility and origin remain stable.
    connection.execute(sync)
    assert (
        connection.execute(
            f"SELECT query_key, active, origin FROM {schema}.catalog_queries"
        ).fetchall()
        == rows
    )


def test_sync_reactivates_query_when_hobby_support_returns(sync_database):
    connection, schema, sync = sync_database
    add_material(connection, schema, 1)
    connection.execute(sync)
    for supported, expected in [(False, 0), (True, 1)]:
        connection.execute(
            f"UPDATE {schema}.hobbies SET is_supported = %s WHERE hobby_id = 1", (supported,)
        )
        connection.execute(sync)
        assert connection.execute(
            f"SELECT active, origin FROM {schema}.catalog_queries"
        ).fetchall() == [(expected, "BE_MATERIAL")]


@pytest.mark.parametrize("origin", ["MANUAL", "BE_MATERIAL"])
def test_python_import_origin_survives_sql_sync_and_controls_deactivation(sync_database, origin):
    from item_recommender.catalog_store import PostgresCatalogStore

    connection, schema, sync = sync_database

    class IsolatedStore(PostgresCatalogStore):
        def _table(self, name):
            return f"{schema}.{name}"

    # Use the fixture transaction; no external connection or real BE tables.
    store = IsolatedStore("")
    store._register(connection, ["계량스푼"], 1.0, origin=origin)
    add_material(connection, schema, 1)
    connection.execute(sync)
    assert connection.execute(
        f"SELECT active, origin FROM {schema}.catalog_queries"
    ).fetchall() == [(1, origin)]
    connection.execute(f"UPDATE {schema}.hobbies SET is_supported = false")
    connection.execute(sync)
    expected = 1 if origin == "MANUAL" else 0
    assert connection.execute(
        f"SELECT active, origin FROM {schema}.catalog_queries"
    ).fetchall() == [(expected, origin)]
