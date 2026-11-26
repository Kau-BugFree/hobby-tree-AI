"""Run real catalog integration tests against an isolated, temporary PostgreSQL server.

Install requirements-postgres-test.txt, then run this script with the same virtualenv.
No external database or product provider is contacted. Connection URLs are never printed.
"""

import contextlib
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    try:
        import pgserver
        import psycopg
    except ImportError:
        print("Install requirements-postgres-test.txt in this Python environment first.")
        return 2

    root = Path(__file__).resolve().parent.parent
    logging.getLogger("pgserver").setLevel(logging.CRITICAL)
    result = 2
    with tempfile.TemporaryDirectory(prefix="hobbytree-pg-test-") as directory:
        server = None
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                server = pgserver.get_server(Path(directory) / "data", cleanup_mode="stop")
                dsn = server.get_uri()
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                version = connection.execute("SHOW server_version").fetchone()[0]
                extension = connection.execute(
                    "SELECT installed_version FROM pg_available_extensions WHERE name = 'pg_cron'"
                ).fetchone()
            print(f"Temporary PostgreSQL {version}; no external database is used.")
            if extension is None:
                print("pg_cron is not bundled; cron activation remains unverified.")
            elif extension[0] is None:
                print("pg_cron is available but not activated; cron activation remains unverified.")
            else:
                print("pg_cron is installed; this runner does not install cron schedules.")

            environment = os.environ.copy()
            environment.pop("ADPICK_API_KEY", None)
            environment["CATALOG_TEST_DATABASE_URL"] = dsn
            environment["PYTEST_ADDOPTS"] = ""
            environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_postgres_catalog.py",
                    "tests/test_material_sync.py",
                    "tests/test_material_reference_store.py::test_postgres_reference_preview",
                    "tests/test_material_export_sync.py",
                    "tests/test_be_material_sync_contract.py::test_be_scenarios[postgres]",
                    "-q",
                    "--tb=short",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            output = (completed.stdout + completed.stderr).replace(dsn, "[temporary database]")
            output = re.sub(r"postgres(?:ql)?://[^\s'\"]+", "[temporary database]", output)
            print(output, end="" if output.endswith("\n") else "\n")
            result = completed.returncode
        except Exception as error:
            # pgserver/psycopg exceptions may contain a connection URL; report only the class.
            print(f"Temporary PostgreSQL test run failed ({type(error).__name__}).")
            print("The runner needs permission to start a local PostgreSQL socket.")
            result = 2
        finally:
            if server is not None:
                try:
                    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                        server.cleanup()
                    print("Temporary PostgreSQL stopped; test data removed.")
                except Exception as error:
                    print(f"Temporary PostgreSQL cleanup failed ({type(error).__name__}).")
                    result = 3
    return result


if __name__ == "__main__":
    raise SystemExit(main())
