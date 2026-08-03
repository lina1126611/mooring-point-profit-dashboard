"""SQLite 연결 및 스키마 초기화."""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = PROJECT_ROOT / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"
DEFAULT_DB_PATH = DB_DIR / "mooring.db"


def connect(
    db_path: str | Path = DEFAULT_DB_PATH,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """SQLite 연결을 연다. ':memory:' 도 허용(테스트용).

    Streamlit 은 요청마다 다른 스레드에서 돌 수 있으므로 UI 쪽에서는
    check_same_thread=False 로 연다.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def open_app_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """앱용 연결 — 스키마가 없으면 만들어 두고 돌려준다."""
    conn = connect(db_path, check_same_thread=False)
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """db/schema.sql 을 실행해 테이블을 생성한다(멱등)."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
