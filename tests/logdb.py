"""Helpers to generate WiCAN-style SQLite log databases for tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path


def make_log_db(
    path: Path,
    rows: Iterable[tuple[str, float, object]],
    *,
    with_ts_index: bool = False,
) -> Path:
    """Create a log database with the WiCAN schema from (name, ts, value) rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE param_info (Id INTEGER PRIMARY KEY, Name TEXT, Unit TEXT)"
    )
    conn.execute(
        "CREATE TABLE param_data (param_id INTEGER, timestamp INTEGER, value REAL)"
    )
    ids: dict[str, int] = {}
    for name, ts, value in rows:
        if name not in ids:
            ids[name] = len(ids) + 1
            conn.execute(
                "INSERT INTO param_info (Id, Name) VALUES (?, ?)", (ids[name], name)
            )
        conn.execute(
            "INSERT INTO param_data VALUES (?, ?, ?)", (ids[name], ts, value)
        )
    if with_ts_index:
        conn.execute("CREATE INDEX idx_ts ON param_data(timestamp)")
    conn.commit()
    conn.close()
    return path


def corrupt_index(path: Path, index_name: str = "idx_ts") -> None:
    """Overwrite the root page of an index with garbage."""
    conn = sqlite3.connect(path)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    root = conn.execute(
        "SELECT rootpage FROM sqlite_master WHERE name = ?", (index_name,)
    ).fetchone()[0]
    conn.close()
    with path.open("r+b") as f:
        f.seek((root - 1) * page_size)
        f.write(b"\xff" * page_size)
