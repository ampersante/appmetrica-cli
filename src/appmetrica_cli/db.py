"""Read-only connections to the analytics database rebuilt by ``AppStore.build()``."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from appmetrica_cli.storage import AppStore, StorageError


def connect_read_only(store: AppStore) -> duckdb.DuckDBPyConnection:
    if not store.db_path.exists():
        raise StorageError(
            "analytics database has not been built for this app; run build() after sync"
        )
    return duckdb.connect(str(store.db_path), read_only=True)


@contextmanager
def read_only(store: AppStore) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect_read_only(store)
    try:
        yield con
    finally:
        con.close()
