"""마스터 데이터 CRUD — 차입금 / 월 고정비 / 맨데이 / 표준일당.

UI(app.py)에서 등록·수정하는 값들이다. SQL을 UI에 흩뿌리지 않으려고 여기 모은다.
금액은 정수(원)로 저장한다. 검증(음수 금지 등)도 여기서 한다 — UI를 갈아끼워도
데이터가 상하지 않아야 하므로.
"""

from __future__ import annotations

import json
import sqlite3

from src import db as db_module
from src.finance import monthly_interest

DAILY_RATES_KEY = "daily_rates"

# 표준일당 초기값 — 대표·설계팀장과 합의된 값으로 설정 화면에서 고친다.
DEFAULT_DAILY_RATES: dict[str, int] = {
    "설계팀장": 420_000,
    "구조설계": 350_000,
    "기본설계": 320_000,
    "CAD 작도": 240_000,
    "현장기술지원": 300_000,
}


def _positive(name: str, value) -> int:
    number = int(value)
    if number < 0:
        raise ValueError(f"{name}은(는) 음수일 수 없다: {number}")
    return number


# ===============================================================
# 차입금
# ===============================================================


def list_loans(conn: sqlite3.Connection) -> list[dict]:
    """차입금 목록. 각 행에 월 이자(monthly_interest)를 얹어서 돌려준다."""
    rows = conn.execute(
        "SELECT id, name, principal, annual_rate, start_date, end_date "
        "FROM loans ORDER BY id"
    ).fetchall()
    return [
        {**dict(r), "monthly_interest": monthly_interest(r["principal"], r["annual_rate"])}
        for r in rows
    ]


def add_loan(
    conn: sqlite3.Connection,
    name: str,
    principal: int,
    annual_rate: float,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    """차입금을 등록하고 id 를 돌려준다. annual_rate 는 소수 표기(4.5% → 0.045)."""
    if not str(name).strip():
        raise ValueError("차입금 이름이 비었다")
    if annual_rate < 0:
        raise ValueError(f"연이율은 음수일 수 없다: {annual_rate}")
    cur = conn.execute(
        "INSERT INTO loans (name, principal, annual_rate, start_date, end_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (name.strip(), _positive("원금", principal), float(annual_rate),
         start_date or None, end_date or None),
    )
    conn.commit()
    return cur.lastrowid


def update_loan(conn: sqlite3.Connection, loan_id: int, **fields) -> None:
    """차입금 일부 필드 수정. 허용 필드 외에는 무시하지 않고 에러를 낸다."""
    allowed = {"name", "principal", "annual_rate", "start_date", "end_date"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"수정할 수 없는 필드: {', '.join(sorted(unknown))}")
    if not fields:
        return
    if "principal" in fields:
        fields["principal"] = _positive("원금", fields["principal"])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE loans SET {assignments} WHERE id = ?", (*fields.values(), loan_id))
    conn.commit()


def delete_loan(conn: sqlite3.Connection, loan_id: int) -> None:
    conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
    conn.commit()


# ===============================================================
# 월 고정비
# ===============================================================


def list_fixed_costs(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, monthly_amount, category FROM fixed_costs ORDER BY monthly_amount DESC"
        )
    ]


def add_fixed_cost(
    conn: sqlite3.Connection,
    name: str,
    monthly_amount: int,
    category: str | None = None,
) -> int:
    if not str(name).strip():
        raise ValueError("고정비 항목명이 비었다")
    cur = conn.execute(
        "INSERT INTO fixed_costs (name, monthly_amount, category) VALUES (?, ?, ?)",
        (name.strip(), _positive("월 금액", monthly_amount), (category or "").strip() or None),
    )
    conn.commit()
    return cur.lastrowid


def update_fixed_cost(conn: sqlite3.Connection, fixed_cost_id: int, **fields) -> None:
    allowed = {"name", "monthly_amount", "category"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"수정할 수 없는 필드: {', '.join(sorted(unknown))}")
    if not fields:
        return
    if "monthly_amount" in fields:
        fields["monthly_amount"] = _positive("월 금액", fields["monthly_amount"])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE fixed_costs SET {assignments} WHERE id = ?", (*fields.values(), fixed_cost_id)
    )
    conn.commit()


def delete_fixed_cost(conn: sqlite3.Connection, fixed_cost_id: int) -> None:
    conn.execute("DELETE FROM fixed_costs WHERE id = ?", (fixed_cost_id,))
    conn.commit()


# ===============================================================
# 맨데이
# ===============================================================


def list_mandays(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """프로젝트의 맨데이 투입 내역. 행마다 인건비를 계산해 붙인다."""
    rows = conn.execute(
        "SELECT id, role, headcount, days, daily_rate FROM mandays "
        "WHERE project_id = ? ORDER BY id",
        (project_id,),
    ).fetchall()
    return [
        {**dict(r), "cost": int(round(r["headcount"] * r["days"] * r["daily_rate"]))}
        for r in rows
    ]


def add_manday(
    conn: sqlite3.Connection,
    project_id: int,
    role: str,
    headcount: int,
    days: float,
    daily_rate: int,
) -> int:
    """맨데이 1건 등록. days 는 0.5일 단위를 허용한다."""
    if not str(role).strip():
        raise ValueError("역할이 비었다")
    if float(days) < 0:
        raise ValueError(f"투입일수는 음수일 수 없다: {days}")
    cur = conn.execute(
        "INSERT INTO mandays (project_id, role, headcount, days, daily_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, role.strip(), _positive("투입인원", headcount),
         float(days), _positive("일단가", daily_rate)),
    )
    conn.commit()
    return cur.lastrowid


def delete_manday(conn: sqlite3.Connection, manday_id: int) -> None:
    conn.execute("DELETE FROM mandays WHERE id = ?", (manday_id,))
    conn.commit()


# ===============================================================
# 표준일당 — settings 에 JSON 으로 보관한다
#   역할별 단가는 자주 바뀌고 개수도 적어서 테이블을 새로 파지 않았다.
# ===============================================================


def get_daily_rates(conn: sqlite3.Connection) -> dict[str, int]:
    raw = db_module.get_setting(conn, DAILY_RATES_KEY)
    if not raw:
        return dict(DEFAULT_DAILY_RATES)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_DAILY_RATES)
    return {str(k): int(v) for k, v in loaded.items()}


def set_daily_rate(conn: sqlite3.Connection, role: str, rate: int) -> dict[str, int]:
    if not str(role).strip():
        raise ValueError("역할이 비었다")
    rates = get_daily_rates(conn)
    rates[role.strip()] = _positive("표준일당", rate)
    db_module.set_setting(conn, DAILY_RATES_KEY, json.dumps(rates, ensure_ascii=False))
    return rates


def delete_daily_rate(conn: sqlite3.Connection, role: str) -> dict[str, int]:
    rates = get_daily_rates(conn)
    rates.pop(role, None)
    db_module.set_setting(conn, DAILY_RATES_KEY, json.dumps(rates, ensure_ascii=False))
    return rates
