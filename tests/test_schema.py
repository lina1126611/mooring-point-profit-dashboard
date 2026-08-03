"""스키마 무결성 테스트.

계산 로직 테스트는 각 함수 구현 시 tests/test_profit.py, test_finance.py 등에
추가한다. (CLAUDE.md 규칙 1: 모든 금액 계산은 반드시 pytest로 검증한다)
"""

from __future__ import annotations

import sqlite3

import pytest

EXPECTED_TABLES = {
    "projects",
    "transactions",
    "loans",
    "fixed_costs",
    "mandays",
    "settings",
}


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_all_tables_created(conn):
    assert EXPECTED_TABLES <= table_names(conn)


def test_default_settings_seeded(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'allocation_basis'"
    ).fetchone()
    assert row["value"] == "revenue"


def test_tx_type_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (date, tx_type, amount) VALUES (?, ?, ?)",
            ("2026-01-01", "이상한값", 1000),
        )


def test_cost_behavior_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (date, tx_type, amount, cost_behavior) "
            "VALUES (?, ?, ?, ?)",
            ("2026-01-01", "매입", 1000, "반변동"),
        )


def test_transaction_allows_null_project(conn):
    """프로젝트 미귀속 공통비는 허용되어야 한다 (고정비 배부 대상)."""
    conn.execute(
        "INSERT INTO transactions (date, project_id, tx_type, amount, cost_behavior, account) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-01-01", None, "경비", 3_000_000, "고정", "임차료"),
    )
    row = conn.execute(
        "SELECT project_id, amount FROM transactions WHERE account = '임차료'"
    ).fetchone()
    assert row["project_id"] is None
    assert row["amount"] == 3_000_000


def test_manday_row_roundtrip(conn):
    conn.execute(
        "INSERT INTO projects (name, contract_amount) VALUES (?, ?)",
        ("부산항 계류시설 보강", 1_200_000_000),
    )
    pid = conn.execute("SELECT id FROM projects").fetchone()["id"]
    conn.execute(
        "INSERT INTO mandays (project_id, role, headcount, days, daily_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "구조설계", 2, 15.5, 350_000),
    )
    row = conn.execute("SELECT * FROM mandays").fetchone()
    assert row["headcount"] * row["days"] * row["daily_rate"] == pytest.approx(10_850_000)
