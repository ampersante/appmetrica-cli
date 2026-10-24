"""Read-only SQL sandbox over the rebuilt analytics database.

Exactly one SELECT/WITH statement is accepted. PRAGMA is checked by its first
keyword because DuckDB rewrites it to a SELECT internally. The connection is
read-only with external access disabled and a locked configuration, so the
statement cannot read files, attach databases or change settings. Queries are
interrupted after ``timeout`` seconds and results are capped at 10,000 rows.

Error messages carry only the user's SQL and the error description; they never
include paths, configuration or environment values.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

import duckdb

from appmetrica_cli.storage import AppStore

DEFAULT_TIMEOUT = 60.0
ROW_CAP = 10_000
_ALLOWED_KEYWORDS = {"SELECT", "WITH"}

_CONFIG = {
    "enable_external_access": "false",
    "lock_configuration": "true",
    "memory_limit": "1GB",
    "threads": "2",
}


class SandboxError(Exception):
    """Base error; ``sql`` is the statement the caller submitted."""

    def __init__(self, message: str, sql: str = ""):
        self.message = message
        self.sql = sql
        super().__init__(message)


class SqlRejected(SandboxError):
    """The statement is not a single read-only SELECT/WITH."""


class QueryTimeout(SandboxError):
    """The query was interrupted after the time limit."""


class QueryFailed(SandboxError):
    """DuckDB could not parse or execute the statement."""


@dataclass(frozen=True)
class SandboxResult:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    truncated: bool


def run_query(
    store: AppStore,
    sql: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_rows: int = ROW_CAP,
) -> SandboxResult:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    _validate(sql)

    if not store.db_path.exists():
        raise SandboxError("analytics database has not been built for this app")
    try:
        con = duckdb.connect(str(store.db_path), read_only=True, config=dict(_CONFIG))
    except duckdb.Error:
        # The message could contain the database path, so it is not forwarded.
        raise SandboxError("cannot open the analytics database read-only") from None

    fired = threading.Event()

    def _interrupt() -> None:
        fired.set()
        con.interrupt()

    timer = threading.Timer(timeout, _interrupt)
    timer.daemon = True
    timer.start()
    try:
        cursor = con.execute(sql)
        columns = tuple(column[0] for column in (cursor.description or ()))
        rows = cursor.fetchmany(max_rows + 1)
    except duckdb.InterruptException:
        raise QueryTimeout(f"query exceeded the {timeout:g} s limit", sql) from None
    except duckdb.Error as e:
        if fired.is_set():
            raise QueryTimeout(f"query exceeded the {timeout:g} s limit", sql) from None
        raise QueryFailed(f"query failed: {e}", sql) from None
    finally:
        timer.cancel()
        try:
            con.close()
        except duckdb.Error:
            pass
    truncated = len(rows) > max_rows
    return SandboxResult(columns=columns, rows=tuple(rows[:max_rows]), truncated=truncated)


def _validate(sql: str) -> None:
    if not sql or not sql.strip():
        raise SqlRejected("SQL statement is empty", sql)
    keyword = _first_keyword(sql)
    if keyword is None or keyword.upper() not in _ALLOWED_KEYWORDS:
        raise SqlRejected("only a single SELECT or WITH statement is allowed", sql)
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise QueryFailed(f"query failed: {e}", sql) from None
    if len(statements) != 1:
        raise SqlRejected(
            f"exactly one statement is allowed, got {len(statements)}", sql
        )
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SqlRejected("only a single SELECT or WITH statement is allowed", sql)


def _first_keyword(sql: str) -> str | None:
    """First keyword outside leading whitespace and SQL comments."""
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char.isspace() or char == "\ufeff":
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            if newline < 0:
                return None
            index = newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        break
    match = re.match(r"[A-Za-z_]+", sql[index:])
    return match.group(0) if match else None
