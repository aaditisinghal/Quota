"""Connections. Only WritePath holds a writable connection; everyone else opens read-only."""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def memory_uri(name: str = "memdb1") -> str:
    """Shared-cache in-memory DB: every connection to this URI sees the same database."""
    return f"file:{name}?mode=memory&cache=shared"


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if path.startswith("file:"):
        uri = path
    else:
        uri = Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if readonly:
        # `mode=ro` cannot be combined with `mode=memory`, so in-memory readers rely on query_only.
        conn.execute("PRAGMA query_only = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
