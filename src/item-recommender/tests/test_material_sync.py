"""Registration eligibility using synthetic tables in an isolated test schema."""

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
    supported, review_status, expected_count
):
    dsn = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN not configured")

    psycopg = pytest.importorskip("psycopg")
    schema = "sync_test_" + uuid.uuid4().hex

    migration = (ROOT / "sql/001_catalog.sql").read_text(encoding="utf-8")
    migration = migration.replace("item_recommender", schema)

    sync = (ROOT / "sql/003_sync_activity_materials.sql").read_text(encoding="utf-8")
    sync = sync.replace("item_recommender.", f"{schema}.")
    sync = sync.replace("public.", f"{schema}.")
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
            connection.execute(f"INSERT INTO {schema}.hobbies VALUES (1, %s)", (supported,))
            connection.execute(f"INSERT INTO {schema}.hobby_stages VALUES (1, 1)")
            connection.execute(
                f"INSERT INTO {schema}.activities VALUES (1, 1, %s)",
                (review_status,),
            )
            connection.execute(
                f"INSERT INTO {schema}.activity_materials VALUES (1, %s)",
                ("계량스푼",),
            )

            connection.execute(sync)

            rows = connection.execute(
                f"SELECT query_key, active, origin FROM {schema}.catalog_queries"
            ).fetchall()
            assert len(rows) == expected_count
            if expected_count:
                assert rows == [("계량스푼", 1, "BE_MATERIAL")]
