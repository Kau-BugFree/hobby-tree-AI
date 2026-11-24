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
            create_backend_tables(connection, schema)
            yield connection, schema, sync


def create_backend_tables(connection, schema):
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


def sync_statements(schema):
    """Keep file transaction boundaries and send each statement separately.

    Sending the entire script in one execute() can create an implicit transaction
    even without BEGIN, hiding a missing transaction boundary. This narrow loader
    supports this file's four statements only, not arbitrary SQL or PL/pgSQL.
    """
    script = (ROOT / "sql/003_sync_activity_materials.sql").read_text(encoding="utf-8")
    script = script.replace("item_recommender.", f"{schema}.").replace("public.", f"{schema}.")
    statements, pending = [], []
    for line in script.splitlines():
        if not line.strip() or line.lstrip().startswith("--"):
            continue
        pending.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(pending).strip())
            pending = []
    assert not pending, "Unterminated sync SQL statement"
    assert len(statements) == 4, "Update the test loader if sync SQL structure changes"
    assert statements[0] == "BEGIN;"
    assert statements[1].startswith("WITH material_queries AS (")
    assert statements[2].startswith(f"UPDATE {schema}.catalog_queries q")
    assert statements[3] == "COMMIT;"
    return statements


def execute_committed_sync(dsn, schema, *, omit_boundaries=False):
    import psycopg

    statements = sync_statements(schema)
    if omit_boundaries:
        statements = statements[1:-1]  # Negative control only; never changes the SQL file.
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement)


def committed_catalog_rows(dsn, schema):
    import psycopg

    # A separate connection proves what was persisted, not just transaction-local state.
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        return connection.execute(
            f"SELECT query_key, query_text, active, origin, created_at, updated_at "
            f"FROM {schema}.catalog_queries ORDER BY query_key"
        ).fetchall()


@pytest.fixture
def committed_sync_database():
    dsn = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN not configured")
    psycopg = pytest.importorskip("psycopg")
    schema = "sync_atomic_test_" + uuid.uuid4().hex
    migration = (ROOT / "sql/001_catalog.sql").read_text(encoding="utf-8")
    migration = migration.replace("item_recommender", schema)
    try:
        # Commit the baseline before the tested sync. No fixture-wide rollback wrapper.
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute(migration)
            create_backend_tables(connection, schema)
            add_material(connection, schema, 1)
            add_material(connection, schema, 2)
            connection.execute(
                f"UPDATE {schema}.activity_materials SET material_name = %s WHERE activity_id = 2",
                ("거품기",),
            )
            for name, active, origin in [
                ("거품기", 0, "BE_MATERIAL"),
                ("프라이팬", 1, "BE_MATERIAL"),
                ("오븐장갑", 1, "MANUAL"),
            ]:
                connection.execute(
                    f"INSERT INTO {schema}.catalog_queries "
                    "(query_key, query_text, active, origin, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, 100, 200)",
                    (name, name, active, origin),
                )
            connection.execute(
                f"""
                CREATE FUNCTION {schema}.fail_deactivation() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM {schema}.catalog_queries
                        WHERE query_key = '계량스푼' AND active = 1
                    ) OR NOT EXISTS (
                        SELECT 1 FROM {schema}.catalog_queries
                        WHERE query_key = '거품기' AND active = 1
                    ) THEN
                        RAISE EXCEPTION 'registration_did_not_run';
                    END IF;
                    RAISE EXCEPTION 'injected_deactivation_failure';
                END;
                $$;
                CREATE TRIGGER fail_deactivation BEFORE UPDATE
                ON {schema}.catalog_queries FOR EACH ROW
                WHEN (NEW.active = 0)
                EXECUTE FUNCTION {schema}.fail_deactivation();
                """
            )
        yield dsn, schema
    finally:
        # Drop only this fixture's generated schema, including its failure trigger.
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def expect_deactivation_failure(dsn, schema, *, omit_boundaries=False):
    from psycopg.errors import RaiseException

    with pytest.raises(RaiseException, match="injected_deactivation_failure"):
        execute_committed_sync(dsn, schema, omit_boundaries=omit_boundaries)


def test_sync_failure_rolls_back_insert_reactivation_and_timestamps(committed_sync_database):
    dsn, schema = committed_sync_database
    before = committed_catalog_rows(dsn, schema)
    expect_deactivation_failure(dsn, schema)
    assert committed_catalog_rows(dsn, schema) == before


def test_sync_can_retry_and_commit_after_failure(committed_sync_database):
    import psycopg

    dsn, schema = committed_sync_database
    before = {row[0]: row for row in committed_catalog_rows(dsn, schema)}
    expect_deactivation_failure(dsn, schema)
    assert {row[0]: row for row in committed_catalog_rows(dsn, schema)} == before
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        connection.execute(f"DROP TRIGGER fail_deactivation ON {schema}.catalog_queries")
    execute_committed_sync(dsn, schema)
    after = {row[0]: row for row in committed_catalog_rows(dsn, schema)}
    expected = {
        "계량스푼": (1, "BE_MATERIAL"),
        "거품기": (1, "BE_MATERIAL"),
        "프라이팬": (0, "BE_MATERIAL"),
        "오븐장갑": (1, "MANUAL"),
    }
    assert {key: row[2:4] for key, row in after.items()} == expected
    assert after["오븐장갑"] == before["오븐장갑"]
    for key in ("거품기", "프라이팬"):
        assert after[key][4] == before[key][4]
        assert after[key][5] > before[key][5]
    execute_committed_sync(dsn, schema)
    repeated = committed_catalog_rows(dsn, schema)
    assert {row[0]: row[2:4] for row in repeated} == expected
    assert next(row for row in repeated if row[0] == "오븐장갑") == before["오븐장갑"]


def test_sync_negative_control_detects_missing_transaction_boundaries(committed_sync_database):
    dsn, schema = committed_sync_database
    before = {row[0]: row for row in committed_catalog_rows(dsn, schema)}
    expect_deactivation_failure(dsn, schema, omit_boundaries=True)
    after = {row[0]: row for row in committed_catalog_rows(dsn, schema)}
    # Without BEGIN/COMMIT, the prior registration committed despite the later failure.
    assert after != before
    assert "계량스푼" not in before
    assert after["계량스푼"][2:4] == (1, "BE_MATERIAL")
    assert before["거품기"][2] == 0 and after["거품기"][2] == 1
    assert after["거품기"][5] > before["거품기"][5]
    assert after["프라이팬"] == before["프라이팬"]
    assert after["오븐장갑"] == before["오븐장갑"]
