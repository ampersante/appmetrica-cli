from __future__ import annotations

import pytest

from appmetrica_cli.sandbox import (
    QueryFailed,
    QueryTimeout,
    SandboxError,
    SqlRejected,
    run_query,
)
from tests.helpers import build, epoch, event_row, make_store


@pytest.fixture
def store(tmp_path):
    store = make_store(tmp_path)
    build(store, events=[event_row(1, epoch(2026, 9, 15, 10))])
    return store


def test_select_returns_columns_and_rows(store):
    result = run_query(store, "SELECT 1 AS one, 'x' AS two")
    assert result.columns == ("one", "two")
    assert result.rows == ((1, "x"),)
    assert result.truncated is False


def test_with_select_is_allowed(store):
    result = run_query(store, "WITH x AS (SELECT 42 AS n) SELECT n FROM x")
    assert result.rows == ((42,),)


def test_result_cap_keeps_10000_rows(store):
    result = run_query(store, "SELECT * FROM range(10005)")
    assert len(result.rows) == 10_000
    assert result.rows[0] == (0,)
    assert result.truncated is True

    exact = run_query(store, "SELECT * FROM range(10000)")
    assert len(exact.rows) == 10_000
    assert exact.truncated is False


@pytest.mark.parametrize(
    "sql",
    [
        "COPY (SELECT 1) TO '/tmp/out.csv'",
        "ATTACH ':memory:' AS other",
        "PRAGMA database_list",
        "/* preamble */ PRAGMA database_list",
        "CALL pragma_version()",
        "INSERT INTO events VALUES ('x')",
        "UPDATE events SET event_name = 'x'",
        "DELETE FROM events",
        "CREATE TABLE x (a INTEGER)",
        "DROP TABLE events",
        "SET threads = 1",
        "INSTALL httpfs",
        "LOAD httpfs",
        "EXPLAIN SELECT 1",
        "WITH x AS (SELECT 1) DELETE FROM events",
    ],
)
def test_non_select_statements_are_rejected(store, sql):
    with pytest.raises(SqlRejected):
        run_query(store, sql)


@pytest.mark.parametrize("sql", ["", "   \n\t", "-- only a comment", "/* only a comment */"])
def test_empty_input_is_rejected(store, sql):
    with pytest.raises(SqlRejected):
        run_query(store, sql)


def test_multiple_statements_are_rejected(store):
    with pytest.raises(SqlRejected, match="exactly one statement"):
        run_query(store, "SELECT 1; SELECT 2")


def test_read_only_configuration(store):
    result = run_query(
        store,
        "SELECT current_setting('enable_external_access'), current_setting('threads')",
    )
    assert result.rows == ((False, 2),)

    with pytest.raises(QueryFailed, match="disabled by configuration"):
        run_query(store, "SELECT * FROM read_csv('/etc/hosts')")

    with pytest.raises(QueryFailed):
        run_query(store, "SELECT * FROM missing_table")


def test_cartesian_query_times_out(store):
    with pytest.raises(QueryTimeout, match="limit"):
        run_query(
            store,
            "SELECT sum(a.range * b.range) "
            "FROM range(1, 1000000) a, range(1, 1000000) b",
            timeout=0.1,
        )


def test_errors_do_not_leak_paths(store, tmp_path):
    with pytest.raises(SqlRejected) as rejected:
        run_query(store, "COPY (SELECT 1) TO '/tmp/out.csv'")
    assert str(tmp_path) not in str(rejected.value)

    with pytest.raises(QueryFailed) as failed:
        run_query(store, "SELECT * FROM missing_table")
    assert str(tmp_path) not in str(failed.value)

    with pytest.raises(QueryTimeout) as timed_out:
        run_query(
            store,
            "SELECT sum(a.range * b.range) "
            "FROM range(1, 1000000) a, range(1, 1000000) b",
            timeout=0.05,
        )
    assert str(tmp_path) not in str(timed_out.value)


def test_missing_database_is_loud(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(SandboxError, match="not been built"):
        run_query(store, "SELECT 1")


def test_sandbox_result_is_immutable(store):
    result = run_query(store, "SELECT 1")
    with pytest.raises(TypeError):
        result.rows[0] = (2,)


def test_invalid_limits(store):
    with pytest.raises(ValueError):
        run_query(store, "SELECT 1", timeout=0)
    with pytest.raises(ValueError):
        run_query(store, "SELECT 1", max_rows=0)


def test_parser_errors_are_query_failures(store):
    with pytest.raises(QueryFailed):
        run_query(store, "SELECT FROM WHERE")
