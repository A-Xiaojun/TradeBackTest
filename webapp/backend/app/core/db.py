import sqlite3
from typing import Generator

from app.core.config import DB_FILE, SCHEMA_FILE


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(schema_sql)
