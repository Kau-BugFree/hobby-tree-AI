"""Catalog connections for material export tests, isolated per test and database."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from item_recommender.catalog_store import PostgresCatalogStore, SQLiteCatalogStore


@pytest.fixture(params=["sqlite", "postgres"])
def material_catalog_factory(request, tmp_path):
    """Open independent connections so the CLI can close its own store after each call."""
    if request.param == "sqlite":

        def open_sqlite(*, read_only=False):
            return SQLiteCatalogStore(tmp_path / "catalog.sqlite3", read_only=read_only)

        db = open_sqlite()
        try:
            db.initialize()
        finally:
            db.close()
        yield open_sqlite
        return

    dsn = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test DSN not configured")
    schema = "export_test_" + uuid4().hex

    class ScopedStore(PostgresCatalogStore):
        def _table(self, name):
            return f"{schema}.{name}"

    def open_postgres(*, read_only=False):
        return ScopedStore(dsn, read_only=read_only)

    sql = (Path(__file__).resolve().parents[1] / "sql/001_catalog.sql").read_text()
    db = open_postgres()
    try:
        with db._transaction() as conn:
            conn.execute(sql.replace("item_recommender", schema))
        try:
            yield open_postgres
        finally:
            with db._transaction() as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")
    finally:
        db.close()
