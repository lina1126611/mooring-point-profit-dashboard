"""이자비용 및 고정비 배부.

고정비 배부의 철칙: 배부액 합계 == 배부 대상 총액.
반올림 잔차는 버리지 말고 특정 대상에 몰아주어 합계를 보존한다. (CLAUDE.md 규칙 1)

이자 이중계상 주의:
    같은 이자가 loans 테이블(자동계산)과 transactions 원장(이자비용 계정)에
    동시에 존재할 수 있다. 둘 다 더하면 고정비가 부풀어 진짜 영업이익이
    실제보다 나쁘게 나온다. 어느 쪽을 쓸지는 settings.interest_source 로 택일한다.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date
from decimal import ROUND_DOWN, Decimal

import pandas as pd

from src import db as db_module
from src.rules import FIXED, INTEREST_ACCOUNT, VARIABLE

# 배부기준 (settings.allocation_basis)
BASIS_REVENUE = "revenue"              # 매출액 비례
BASIS_VARIABLE_COST = "variable_cost"  # 변동비 비례
BASIS_MANDAY = "manday"                # 투입 맨데이 비례
BASIS_DURATION = "duration"            # 프로젝트 기간(개월) 비례
BASIS_EQUAL = "equal"                  # 균등 배부
DEFAULT_BASIS = BASIS_REVENUE

ALL_BASES = (BASIS_REVENUE, BASIS_VARIABLE_COST, BASIS_MANDAY, BASIS_DURATION, BASIS_EQUAL)

BASIS_LABELS = {
    BASIS_REVENUE: "매출액 비례",
    BASIS_VARIABLE_COST: "변동비 비례",
    BASIS_MANDAY: "투입 맨데이 비례",
    BASIS_DURATION: "프로젝트 기간(개월) 비례",
    BASIS_EQUAL: "균등 배부",
}

# 이자 출처 (settings.interest_source) — 이중계상 방지용 택일 스위치
INTEREST_FROM_LOANS = "loans"                # loans 자동계산을 쓰고 원장 이자비용은 뺀다
INTEREST_FROM_TRANSACTIONS = "transactions"  # 원장 이자비용을 쓰고 loans 는 안 더한다
DEFAULT_INTEREST_SOURCE = INTEREST_FROM_LOANS

DAYS_IN_YEAR = 365  # 여신 실무 관행(365일 기준 일할)


# ===============================================================
# 금액 헬퍼 — 원 단위 정수화
# ===============================================================


def _floor_won(value: Decimal) -> int:
    """원 미만 절사. 이자는 절사가 은행 관행이다."""
    return int(value.to_integral_value(rounding=ROUND_DOWN))


def _parse_date(value: str | None) -> date | None:
    """'YYYY-MM-DD' → date. 값이 없거나 형식이 깨졌으면 None."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ===============================================================
# 이자 계산
# ===============================================================


def daily_interest(principal: int, annual_rate: float, days: int) -> int:
    """일할 이자 = 원금 × 연이율 × 경과일수 / 365. 원 미만 절사.

    예) 500,000,000 × 0.065 × 30 / 365 = 2,671,232.87... → 2,671,232원
    """
    if days < 0:
        raise ValueError(f"경과일수는 음수일 수 없다: {days}")
    if principal <= 0 or annual_rate <= 0 or days == 0:
        return 0
    amount = (
        Decimal(int(principal))
        * Decimal(str(annual_rate))
        * Decimal(int(days))
        / Decimal(DAYS_IN_YEAR)
    )
    return _floor_won(amount)


def monthly_interest(principal: int, annual_rate: float) -> int:
    """월 이자 = 원금 × 연이율 ÷ 12. 원 미만 절사.

    달마다 일수가 다른 것을 무시한 근사값. 기간 손익의 고정비 풀처럼
    '몇 개월치'로 뭉뚱그릴 때 쓴다. 특정 월의 실제 이자는
    monthly_interest_by_loan() 이 일할로 계산한다.
    """
    if principal <= 0 or annual_rate <= 0:
        return 0
    return _floor_won(Decimal(int(principal)) * Decimal(str(annual_rate)) / Decimal(12))


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """해당 월의 (1일, 말일)."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def loan_days_in_month(
    start: str | None,
    end: str | None,
    year: int,
    month: int,
) -> int:
    """그 달에 대출이 살아 있던 일수. 시작일·종료일 모두 포함해서 센다.

    기간이 비어 있으면 상시 차입으로 보고 만월로 센다(원금이 늘 깔려 있는
    한도 대출이 흔하다).
    """
    first, last = month_bounds(year, month)
    begin = max(first, _parse_date(start) or first)
    finish = min(last, _parse_date(end) or last)
    if finish < begin:
        return 0
    return (finish - begin).days + 1


def monthly_interest_by_loan(conn: sqlite3.Connection, year: int, month: int) -> dict[int, int]:
    """특정 월의 대출별 이자(일할). {loan_id: 이자}"""
    out: dict[int, int] = {}
    for row in conn.execute(
        "SELECT id, principal, annual_rate, start_date, end_date FROM loans ORDER BY id"
    ):
        days = loan_days_in_month(row["start_date"], row["end_date"], year, month)
        out[row["id"]] = daily_interest(row["principal"], row["annual_rate"], days)
    return out


def month_interest(conn: sqlite3.Connection, year: int, month: int) -> int:
    """특정 월의 전 차입금 이자 합계(일할)."""
    return sum(monthly_interest_by_loan(conn, year, month).values())


def loans_monthly_interest(conn: sqlite3.Connection) -> int:
    """전 차입금의 월 이자 합계(월할 근사). 고정비 풀 산출에 쓴다."""
    return sum(
        monthly_interest(row["principal"], row["annual_rate"])
        for row in conn.execute("SELECT principal, annual_rate FROM loans")
    )


def total_interest(conn: sqlite3.Connection, months: int) -> int:
    """전 차입금의 기간 이자 합계 = 월 이자 합계 × 개월수."""
    return loans_monthly_interest(conn) * int(months)


# ===============================================================
# 월별 고정비 지출 예정표
# ===============================================================


def monthly_fixed_cost(conn: sqlite3.Connection) -> int:
    """fixed_costs 월액 합계."""
    row = conn.execute("SELECT COALESCE(SUM(monthly_amount), 0) AS s FROM fixed_costs").fetchone()
    return int(row["s"])


def fixed_cost_schedule(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    months: int = 12,
) -> pd.DataFrame:
    """향후 N개월 고정비 지출 예정표. 컬럼: 연월 / 고정비 / 이자 / 합계.

    이자는 미래 지출이라 원장에 없으므로 언제나 loans 기준 일할로 계산한다.
    (settings.interest_source 와 무관)
    """
    monthly = monthly_fixed_cost(conn)
    rows = []
    y, m = year, month
    for _ in range(months):
        interest = month_interest(conn, y, m)
        rows.append(
            {
                "연월": f"{y:04d}-{m:02d}",
                "고정비": monthly,
                "이자": interest,
                "합계": monthly + interest,
            }
        )
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return pd.DataFrame(rows, columns=["연월", "고정비", "이자", "합계"])


# ===============================================================
# 분석 기간
# ===============================================================


def analysis_months(conn: sqlite3.Connection) -> int:
    """원장에 거래가 존재하는 기간의 개월 수(첫 달~마지막 달, 양끝 포함).

    fixed_costs 는 '월액'이므로 기간 손익에 태우려면 개월수가 필요하다.
    거래가 없어도 0으로 나누는 일이 없도록 최소 1을 돌려준다.
    """
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi FROM transactions WHERE date IS NOT NULL"
    ).fetchone()
    lo, hi = _parse_date(row["lo"]), _parse_date(row["hi"])
    if lo is None or hi is None:
        return 1
    return max(1, (hi.year - lo.year) * 12 + (hi.month - lo.month) + 1)


# ===============================================================
# 고정비 풀
# ===============================================================


def interest_source(conn: sqlite3.Connection) -> str:
    """이자 출처 설정. 알 수 없는 값이면 기본값(loans)."""
    value = db_module.get_setting(conn, "interest_source", DEFAULT_INTEREST_SOURCE)
    return value if value in (INTEREST_FROM_LOANS, INTEREST_FROM_TRANSACTIONS) else (
        DEFAULT_INTEREST_SOURCE
    )


def _ledger_fixed_sum(conn: sqlite3.Connection, project_clause: str, params: tuple) -> int:
    """원장의 고정비 합계. 이자 출처가 loans 면 이자비용 행을 뺀다(이중계상 방지)."""
    sql = (
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        f"WHERE cost_behavior = ? AND {project_clause}"
    )
    args: tuple = (FIXED, *params)
    if interest_source(conn) == INTEREST_FROM_LOANS:
        sql += " AND COALESCE(account, '') <> ?"
        args = (*args, INTEREST_ACCOUNT)
    return int(conn.execute(sql, args).fetchone()["s"])


def common_fixed_cost(conn: sqlite3.Connection) -> int:
    """공통 고정비 = 원장의 고정비 중 프로젝트에 귀속되지 않은 것(배부 대상)."""
    return _ledger_fixed_sum(conn, "project_id IS NULL", ())


def direct_fixed_cost(conn: sqlite3.Connection, project_id: int) -> int:
    """프로젝트에 직접 귀속된 고정비. 배부하지 않고 그 현장에 바로 부과한다."""
    return _ledger_fixed_sum(conn, "project_id = ?", (project_id,))


def fixed_cost_breakdown(conn: sqlite3.Connection, months: int | None = None) -> dict:
    """고정비 풀의 항목별 내역. 대표에게 '왜 안 남았는지'를 설명하는 재료."""
    months = analysis_months(conn) if months is None else int(months)
    master = monthly_fixed_cost(conn) * months
    interest = total_interest(conn, months) if interest_source(conn) == INTEREST_FROM_LOANS else 0
    common = common_fixed_cost(conn)
    return {
        "개월수": months,
        "월고정비월액": monthly_fixed_cost(conn),
        "월고정비": master,      # 월액 × 개월수
        "이자비용": interest,    # loans 자동계산분 (원장을 쓰는 설정이면 0 → 공통고정비에 포함)
        "공통고정비": common,    # 원장의 미귀속 고정비
        "합계": master + interest + common,
        "이자출처": interest_source(conn),
    }


def total_fixed_cost(conn: sqlite3.Connection, months: int | None = None) -> int:
    """배부 대상 고정비 총액.

    = fixed_costs 월액 × 개월수
    + 차입금 이자 (settings.interest_source = 'loans' 일 때만)
    + transactions 중 cost_behavior='고정' 이면서 project_id IS NULL 인 공통비
      (이자 출처가 loans 면 원장의 이자비용 행은 제외 — 이중계상 방지)
    """
    return fixed_cost_breakdown(conn, months)["합계"]


# ===============================================================
# 배부
# ===============================================================


def allocate(total: int, weights: dict[int, float]) -> dict[int, int]:
    """total 을 weights 비율로 배부한다.

    - 반환값의 합계는 반드시 total 과 정확히 일치해야 한다.
      (내림 배부 후 잔차를 가중치가 가장 큰 대상에 더한다)
    - 가중치 합이 0이면 균등 배부.
    - 음수 가중치는 0으로 본다.
    - weights 가 비면 빈 dict.
    """
    if not weights:
        return {}

    clean = {key: max(0.0, float(w)) for key, w in weights.items()}
    denominator = sum(clean.values())
    if denominator <= 0:
        clean = {key: 1.0 for key in clean}
        denominator = float(len(clean))

    total = int(total)
    out = {key: int(total * w / denominator) for key, w in clean.items()}

    # 잔차는 버리지 않는다 — 가중치가 가장 큰 대상(동률이면 id 가 작은 쪽)에 몰아준다.
    residual = total - sum(out.values())
    if residual:
        head = sorted(clean, key=lambda k: (-clean[k], k))[0]
        out[head] += residual
    return out


def project_duration_months(start: str | None, end: str | None) -> int:
    """프로젝트 기간(개월). 착수월과 준공월을 모두 포함해서 센다.

    날짜가 비어 있으면 0 (배부 가중치 없음). 전 프로젝트가 0이면
    allocate() 가 균등 배부로 넘긴다.
    """
    lo, hi = _parse_date(start), _parse_date(end)
    if lo is None or hi is None:
        return 0
    return max(0, (hi.year - lo.year) * 12 + (hi.month - lo.month) + 1)


def allocation_weights(conn: sqlite3.Connection, basis: str) -> dict[int, float]:
    """배부기준에 따른 프로젝트별 가중치를 만든다. {project_id: weight}"""
    if basis not in ALL_BASES:
        raise ValueError(f"알 수 없는 배부기준: {basis!r} (가능: {', '.join(ALL_BASES)})")

    rows = conn.execute("SELECT id, start_date, end_date FROM projects ORDER BY id").fetchall()
    weights = {row["id"]: 0.0 for row in rows}
    if not weights:
        return {}

    if basis == BASIS_EQUAL:
        return {pid: 1.0 for pid in weights}

    if basis == BASIS_DURATION:
        return {
            row["id"]: float(project_duration_months(row["start_date"], row["end_date"]))
            for row in rows
        }

    if basis == BASIS_MANDAY:
        query = (
            "SELECT project_id AS pid, COALESCE(SUM(headcount * days * daily_rate), 0) AS w "
            "FROM mandays GROUP BY project_id"
        )
        params: tuple = ()
    elif basis == BASIS_REVENUE:
        query = (
            "SELECT project_id AS pid, COALESCE(SUM(amount), 0) AS w FROM transactions "
            "WHERE tx_type = '매출' AND project_id IS NOT NULL GROUP BY project_id"
        )
        params = ()
    else:  # BASIS_VARIABLE_COST
        query = (
            "SELECT project_id AS pid, COALESCE(SUM(amount), 0) AS w FROM transactions "
            "WHERE cost_behavior = ? AND project_id IS NOT NULL GROUP BY project_id"
        )
        params = (VARIABLE,)

    for row in conn.execute(query, params):
        if row["pid"] in weights:
            weights[row["pid"]] = float(row["w"])
    return weights


def allocate_fixed_costs(
    conn: sqlite3.Connection,
    months: int | None = None,
    basis: str | None = None,
) -> dict[int, int]:
    """설정된 배부기준으로 고정비를 프로젝트에 배부한다. {project_id: 배부액}"""
    basis = basis or db_module.get_setting(conn, "allocation_basis", DEFAULT_BASIS)
    return allocate(total_fixed_cost(conn, months), allocation_weights(conn, basis))
