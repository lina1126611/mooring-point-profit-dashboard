"""이자비용 · 고정비 풀 · 배부 로직 테스트.

CLAUDE.md 규칙 1: 금액을 산출하는 함수는 테스트 없이 커밋하지 않는다.
특히 배부는 **배부액 합계 == 배부 대상 총액**을 반드시 검증한다(반올림 잔차 포함).
"""

from __future__ import annotations

import pytest

from src import db as db_module
from src import finance


# ===============================================================
# 테스트 데이터 헬퍼
# ===============================================================


def add_project(conn, name, start=None, end=None, contract=0) -> int:
    cur = conn.execute(
        "INSERT INTO projects (name, client, start_date, end_date, contract_amount) "
        "VALUES (?, '발주처', ?, ?, ?)",
        (name, start, end, contract),
    )
    conn.commit()
    return cur.lastrowid


def add_tx(conn, date, amount, tx_type, behavior, account=None, project_id=None) -> None:
    conn.execute(
        "INSERT INTO transactions (date, project_id, vendor, description, account, "
        "tx_type, amount, cost_behavior, source_file) "
        "VALUES (?, ?, '거래처', '적요', ?, ?, ?, ?, 'test.xlsx')",
        (date, project_id, account, tx_type, amount, behavior),
    )
    conn.commit()


def add_loan(conn, name, principal, rate, start=None, end=None) -> int:
    cur = conn.execute(
        "INSERT INTO loans (name, principal, annual_rate, start_date, end_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, principal, rate, start, end),
    )
    conn.commit()
    return cur.lastrowid


def add_fixed_cost(conn, name, monthly, category="기타") -> None:
    conn.execute(
        "INSERT INTO fixed_costs (name, monthly_amount, category) VALUES (?, ?, ?)",
        (name, monthly, category),
    )
    conn.commit()


# ===============================================================
# 이자 계산 — 일할
#   일할 이자 = 원금 × 연이율 × 경과일수 / 365
# ===============================================================


def test_daily_interest_spec_case():
    """명세 검증 케이스: 5억, 연 6.5%, 30일 → 2,671,232원.

    500,000,000 × 0.065 × 30 / 365 = 2,671,232.876...
    이자는 원 미만 절사(은행 관행)이므로 2,671,232원.
    """
    assert finance.daily_interest(500_000_000, 0.065, 30) == 2_671_232


@pytest.mark.parametrize(
    "principal,rate,days,expected",
    [
        (500_000_000, 0.065, 365, 32_500_000),  # 만 1년 = 원금 × 이율
        (500_000_000, 0.065, 0, 0),             # 경과일 0
        (0, 0.065, 30, 0),                      # 원금 0
        (500_000_000, 0.0, 30, 0),              # 무이자
        (100_000_000, 0.045, 31, 382_191),      # 100,000,000×0.045×31/365 = 382,191.78
    ],
)
def test_daily_interest_cases(principal, rate, days, expected):
    assert finance.daily_interest(principal, rate, days) == expected


def test_daily_interest_rejects_negative_days():
    with pytest.raises(ValueError):
        finance.daily_interest(100_000_000, 0.05, -1)


def test_monthly_interest():
    """월 이자 = 원금 × 연이율 ÷ 12 (원 미만 절사). 32,500,000/12 = 2,708,333.33"""
    assert finance.monthly_interest(500_000_000, 0.065) == 2_708_333
    assert finance.monthly_interest(0, 0.065) == 0


# ===============================================================
# 특정 월의 대출별 이자 (일할, 대출 기간 반영)
# ===============================================================


def test_monthly_interest_by_loan_full_and_partial_month(conn):
    """2026-03(31일). A는 만월, B는 3/16 실행이라 16일치만."""
    a = add_loan(conn, "A은행", 500_000_000, 0.065, "2025-01-01", "2027-12-31")
    b = add_loan(conn, "B은행", 300_000_000, 0.045, "2026-03-16", None)

    by_loan = finance.monthly_interest_by_loan(conn, 2026, 3)

    # 500,000,000 × 0.065 × 31/365 = 2,760,273.97
    assert by_loan[a] == 2_760_273
    # 300,000,000 × 0.045 × 16/365 = 591,780.82  (3/16~3/31 = 16일)
    assert by_loan[b] == 591_780


def test_monthly_interest_by_loan_outside_period_is_zero(conn):
    """상환 완료된 대출은 그 달 이자가 0."""
    ended = add_loan(conn, "상환완료", 500_000_000, 0.065, "2024-01-01", "2026-02-28")
    by_loan = finance.monthly_interest_by_loan(conn, 2026, 3)
    assert by_loan[ended] == 0


def test_monthly_interest_by_loan_without_dates_counts_full_month(conn):
    """기간이 비어 있으면 상시 차입으로 보고 만월 이자를 잡는다."""
    lid = add_loan(conn, "기간미상", 100_000_000, 0.06, None, None)
    by_loan = finance.monthly_interest_by_loan(conn, 2026, 4)  # 30일
    # 100,000,000 × 0.06 × 30/365 = 493,150.68
    assert by_loan[lid] == 493_150


def test_monthly_interest_by_loan_empty_db(conn):
    assert finance.monthly_interest_by_loan(conn, 2026, 3) == {}


# ===============================================================
# 월별 고정비 지출 예정표 (향후 12개월)
# ===============================================================


def test_fixed_cost_schedule_12_months(conn):
    add_fixed_cost(conn, "본사 임차료", 5_000_000)
    add_fixed_cost(conn, "관리직 급여", 3_000_000)
    add_loan(conn, "만기도래", 100_000_000, 0.06, "2026-01-01", "2026-09-30")

    sched = finance.fixed_cost_schedule(conn, 2026, 8, months=12)

    assert list(sched.columns) == ["연월", "고정비", "이자", "합계"]
    assert len(sched) == 12
    assert sched.iloc[0]["연월"] == "2026-08"
    assert sched.iloc[11]["연월"] == "2027-07"          # 해를 넘겨도 이어진다

    # 2026-08: 31일 → 100,000,000×0.06×31/365 = 509,589.04
    assert sched.iloc[0]["이자"] == 509_589
    # 2026-09: 30일 → 493,150.68 (9/30 만기, 당일 포함)
    assert sched.iloc[1]["이자"] == 493_150
    # 2026-10 이후: 상환 완료
    assert sched.iloc[2]["이자"] == 0

    assert (sched["고정비"] == 8_000_000).all()
    assert (sched["합계"] == sched["고정비"] + sched["이자"]).all()


def test_fixed_cost_schedule_empty_db(conn):
    sched = finance.fixed_cost_schedule(conn, 2026, 1, months=12)
    assert len(sched) == 12
    assert sched["합계"].sum() == 0


# ===============================================================
# 분석 기간 (거래가 존재하는 개월 수)
# ===============================================================


def test_analysis_months(conn):
    add_tx(conn, "2026-01-10", 1000, "매입", "변동")
    add_tx(conn, "2026-01-20", 1000, "매입", "변동")
    add_tx(conn, "2026-03-02", 1000, "매입", "변동")
    assert finance.analysis_months(conn) == 3  # 1월, 2월, 3월 (구간 전체)


def test_analysis_months_empty_db_is_one(conn):
    """거래가 없어도 0으로 나누지 않도록 최소 1개월."""
    assert finance.analysis_months(conn) == 1


# ===============================================================
# 고정비 풀 — 이중계상 방지가 핵심
# ===============================================================


def _pool_fixture(conn):
    add_fixed_cost(conn, "본사 임차료", 2_000_000)
    add_loan(conn, "기업은행", 100_000_000, 0.12, None, None)   # 월 이자 1,000,000
    add_tx(conn, "2026-01-05", 900_000, "경비", "고정", account="이자비용")
    add_tx(conn, "2026-01-06", 500_000, "경비", "고정", account="보험료")


def test_fixed_cost_pool_uses_loans_by_default(conn):
    """기본값 interest_source='loans' → transactions 의 이자비용 행은 제외한다."""
    _pool_fixture(conn)
    # 2,000,000(월고정비) + 1,000,000(loans 이자) + 500,000(공통 고정 거래, 이자 제외)
    assert finance.total_fixed_cost(conn, months=1) == 3_500_000


def test_fixed_cost_pool_can_use_ledger_interest(conn):
    """interest_source='transactions' → 원장의 이자비용을 쓰고 loans 는 더하지 않는다."""
    _pool_fixture(conn)
    db_module.set_setting(conn, "interest_source", finance.INTEREST_FROM_TRANSACTIONS)
    # 2,000,000 + 0(loans 미반영) + (900,000 + 500,000)
    assert finance.total_fixed_cost(conn, months=1) == 3_400_000


def test_fixed_cost_pool_no_double_counting_of_interest(conn):
    """어느 설정이든 이자가 두 번 잡히면 안 된다."""
    _pool_fixture(conn)
    from_loans = finance.total_fixed_cost(conn, months=1)
    db_module.set_setting(conn, "interest_source", finance.INTEREST_FROM_TRANSACTIONS)
    from_ledger = finance.total_fixed_cost(conn, months=1)
    both = 2_000_000 + 1_000_000 + 900_000 + 500_000
    assert from_loans < both and from_ledger < both


def test_fixed_cost_pool_scales_with_months(conn):
    _pool_fixture(conn)
    assert finance.total_fixed_cost(conn, months=2) == 2_000_000 * 2 + 1_000_000 * 2 + 500_000


def test_fixed_cost_pool_excludes_project_attributed_fixed(conn):
    """프로젝트에 직접 귀속된 고정비는 배부 풀에 넣지 않는다(직접 부과 대상)."""
    pid = add_project(conn, "가덕도")
    add_tx(conn, "2026-01-07", 700_000, "경비", "고정", account="보험료", project_id=pid)
    assert finance.total_fixed_cost(conn, months=1) == 0
    assert finance.direct_fixed_cost(conn, pid) == 700_000


def test_fixed_cost_breakdown_sums_to_total(conn):
    _pool_fixture(conn)
    bd = finance.fixed_cost_breakdown(conn, months=1)
    assert bd["월고정비"] + bd["이자비용"] + bd["공통고정비"] == bd["합계"]
    assert bd["합계"] == finance.total_fixed_cost(conn, months=1)


def test_fixed_cost_pool_empty_db(conn):
    assert finance.total_fixed_cost(conn, months=1) == 0


# ===============================================================
# 배부 — 합계 보존이 생명
# ===============================================================


def test_allocate_by_weight():
    assert finance.allocate(100_000_000, {1: 3.0, 2: 1.0}) == {1: 75_000_000, 2: 25_000_000}


def test_allocate_preserves_total_with_remainder():
    """3등분 불가능한 금액도 합계는 반드시 원본과 일치한다."""
    out = finance.allocate(10_000_000, {1: 1.0, 2: 1.0, 3: 1.0})
    assert sum(out.values()) == 10_000_000
    assert out == {1: 3_333_334, 2: 3_333_333, 3: 3_333_333}  # 잔차는 최대 가중치에


def test_allocate_remainder_goes_to_largest_weight():
    out = finance.allocate(100, {1: 1.0, 2: 2.0})
    assert sum(out.values()) == 100
    assert out[2] == 67  # 66.67 → 66 + 잔차 1


@pytest.mark.parametrize("total", [1, 7, 999_999_999, 1_000_000_007])
def test_allocate_sum_always_equals_total(total):
    out = finance.allocate(total, {1: 1.7, 2: 2.3, 3: 0.9, 4: 5.1})
    assert sum(out.values()) == total


def test_allocate_zero_weights_falls_back_to_equal():
    """가중치가 전부 0(예: 매출 미발생)이면 균등 배부. 합계는 유지."""
    out = finance.allocate(1_000, {1: 0.0, 2: 0.0, 3: 0.0})
    assert sum(out.values()) == 1_000
    assert out == {1: 334, 2: 333, 3: 333}


def test_allocate_empty_weights():
    assert finance.allocate(1_000, {}) == {}


def test_allocate_zero_total():
    assert finance.allocate(0, {1: 1.0, 2: 2.0}) == {1: 0, 2: 0}


def test_allocate_ignores_negative_weights():
    """음수 가중치는 0으로 본다(환불 등으로 매출이 음수인 프로젝트)."""
    out = finance.allocate(100, {1: -5.0, 2: 1.0})
    assert out == {1: 0, 2: 100}


# ===============================================================
# 배부기준
# ===============================================================


def test_allocation_weights_revenue(conn):
    a = add_project(conn, "A")
    b = add_project(conn, "B")
    add_tx(conn, "2026-01-10", 300_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=b)

    w = finance.allocation_weights(conn, finance.BASIS_REVENUE)
    assert w == {a: 300_000_000.0, b: 100_000_000.0}


def test_allocation_weights_duration_months(conn):
    """프로젝트 기간(개월) 비례 — 시작월과 종료월을 모두 포함해 센다."""
    a = add_project(conn, "A", "2026-01-01", "2026-03-31")  # 3개월
    b = add_project(conn, "B", "2026-01-15", "2026-01-31")  # 1개월
    c = add_project(conn, "C", None, None)                  # 기간 미상 → 0

    w = finance.allocation_weights(conn, finance.BASIS_DURATION)
    assert w == {a: 3.0, b: 1.0, c: 0.0}


def test_allocation_weights_equal(conn):
    a = add_project(conn, "A")
    b = add_project(conn, "B")
    assert finance.allocation_weights(conn, finance.BASIS_EQUAL) == {a: 1.0, b: 1.0}


def test_allocation_weights_unknown_basis_raises(conn):
    add_project(conn, "A")
    with pytest.raises(ValueError):
        finance.allocation_weights(conn, "복불복")


def test_allocate_fixed_costs_by_revenue(conn):
    a = add_project(conn, "A")
    b = add_project(conn, "B")
    add_tx(conn, "2026-01-10", 300_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=b)
    add_fixed_cost(conn, "임차료", 20_000_000)

    alloc = finance.allocate_fixed_costs(conn, months=1)
    assert alloc == {a: 15_000_000, b: 5_000_000}
    assert sum(alloc.values()) == finance.total_fixed_cost(conn, months=1)


def test_allocate_fixed_costs_by_duration(conn):
    a = add_project(conn, "A", "2026-01-01", "2026-03-31")  # 3개월
    b = add_project(conn, "B", "2026-01-01", "2026-01-31")  # 1개월
    add_tx(conn, "2026-01-10", 300_000_000, "매출", "해당없음", project_id=a)
    add_fixed_cost(conn, "임차료", 20_000_000)
    db_module.set_setting(conn, "allocation_basis", finance.BASIS_DURATION)

    alloc = finance.allocate_fixed_costs(conn, months=1)
    assert alloc == {a: 15_000_000, b: 5_000_000}
    assert sum(alloc.values()) == 20_000_000


def test_allocate_fixed_costs_total_is_preserved(conn):
    """배부기준을 무엇으로 바꾸든 배부 총액 == 고정비 풀."""
    a = add_project(conn, "A", "2026-01-01", "2026-05-31")
    b = add_project(conn, "B", "2026-02-01", "2026-03-31")
    c = add_project(conn, "C", "2026-01-01", "2026-12-31")
    add_tx(conn, "2026-01-10", 333_333_333, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-10", 111_111_111, "매출", "해당없음", project_id=b)
    add_tx(conn, "2026-01-11", 77_777_777, "매입", "변동", project_id=c)
    add_fixed_cost(conn, "임차료", 7_777_777)
    add_loan(conn, "은행", 123_456_789, 0.037, None, None)

    pool = finance.total_fixed_cost(conn)
    for basis in (
        finance.BASIS_REVENUE,
        finance.BASIS_DURATION,
        finance.BASIS_VARIABLE_COST,
        finance.BASIS_EQUAL,
        finance.BASIS_MANDAY,
    ):
        db_module.set_setting(conn, "allocation_basis", basis)
        alloc = finance.allocate_fixed_costs(conn)
        assert sum(alloc.values()) == pool, f"배부 합계 불일치: {basis}"
        assert set(alloc) == {a, b, c}


def test_allocate_fixed_costs_no_projects(conn):
    add_fixed_cost(conn, "임차료", 20_000_000)
    assert finance.allocate_fixed_costs(conn, months=1) == {}
