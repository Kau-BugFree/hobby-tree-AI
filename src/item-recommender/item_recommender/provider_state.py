"""Shared rolling quota and circuit state. No credentials, queries or products are stored."""

import math
import sqlite3
import threading
from functools import wraps
from pathlib import Path

from .domain import ProviderError


_LOCAL_TRANSACTION_LOCK = threading.Lock()


def _serialized(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        # Avoid a local thread thundering herd; SQLite still arbitrates between processes.
        with _LOCAL_TRANSACTION_LOCK:
            return operation(*args, **kwargs)

    return run


class ProviderState:
    def __init__(
        self,
        path: Path,
        namespace: str,
        limit: int,
        failure_threshold: int,
        reset_seconds: int,
        probe_seconds: float,
    ):
        self.path = Path(path)
        self.namespace = namespace
        self.limit = limit
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.probe_seconds = probe_seconds

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.2, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_calls "
                "(namespace TEXT NOT NULL, called_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS provider_calls_window "
                "ON provider_calls(namespace, called_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_circuits "
                "(namespace TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, "
                "open_until REAL NOT NULL DEFAULT 0, probe_until REAL NOT NULL DEFAULT 0, "
                "generation INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO provider_circuits(namespace) VALUES (?)",
                (self.namespace,),
            )
            return connection
        except BaseException:
            connection.close()
            raise

    @_serialized
    def reserve(self, now: float) -> int:
        """Atomically reserve one outbound attempt, allowing one half-open probe."""
        connection = None
        try:
            connection = self._connect()
            _, open_until, probe_until, generation = connection.execute(
                "SELECT failures, open_until, probe_until, generation "
                "FROM provider_circuits WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            blocked_until = max(open_until, probe_until)
            if blocked_until > now:
                raise ProviderError("PROVIDER_CIRCUIT_OPEN", max(1, math.ceil(blocked_until - now)))
            connection.execute("DELETE FROM provider_calls WHERE called_at <= ?", (now - 60,))
            count, oldest = connection.execute(
                "SELECT COUNT(*), MIN(called_at) FROM provider_calls WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            if count >= self.limit:
                raise ProviderError("PROVIDER_RATE_LIMITED", max(1, math.ceil(oldest + 60 - now)))
            if open_until:
                connection.execute(
                    "UPDATE provider_circuits SET probe_until = ? WHERE namespace = ?",
                    (now + self.probe_seconds, self.namespace),
                )
            connection.execute(
                "INSERT INTO provider_calls(namespace, called_at) VALUES (?, ?)",
                (self.namespace, now),
            )
            connection.commit()
            return generation
        except ProviderError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise ProviderError("PROVIDER_STATE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    @_serialized
    def success(self, generation: int) -> None:
        """An older successful request must not close a circuit opened by another request."""
        connection = None
        try:
            connection = self._connect()
            connection.execute(
                "UPDATE provider_circuits SET failures = 0, open_until = 0, probe_until = 0 "
                "WHERE namespace = ? AND generation = ?",
                (self.namespace, generation),
            )
            connection.commit()
        except (OSError, sqlite3.Error, ValueError):
            raise ProviderError("PROVIDER_STATE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    @_serialized
    def failure(self, now: float, block_seconds: int | None = None) -> None:
        connection = None
        try:
            connection = self._connect()
            failures, open_until = connection.execute(
                "SELECT failures, open_until FROM provider_circuits WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            failures += 1
            if block_seconds is not None or failures >= self.failure_threshold:
                open_until = max(open_until, now + (block_seconds or self.reset_seconds))
            connection.execute(
                "UPDATE provider_circuits SET failures = ?, open_until = ?, probe_until = 0, "
                "generation = generation + 1 WHERE namespace = ?",
                (failures, open_until, self.namespace),
            )
            connection.commit()
        except (OSError, sqlite3.Error, ValueError):
            raise ProviderError("PROVIDER_STATE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()
