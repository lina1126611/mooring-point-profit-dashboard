"""2단 손익 엔진 테스트 — 이 프로젝트에서 가장 중요한 테스트.

    [1단] 공헌이익      = 매출 − 변동비
    [2단] 진짜 영업이익 = 공헌이익 − 배부고정비 − 맨데이 인건비

명세 검증 케이스(수기 계산):
    매출 100,000,000 / 변동비 60,000,000        → 공헌이익 40,000,000 (40%)
    고정비 풀 20,000,000 중 매출비례 배부 몫     → 10,000,000
    맨데이 3명 × 20일 × 250,000                  → 15,000,000
    진짜 영업이익 = 40,000,000 − 10,000,000 − 15,000,000 = 15,000,000 (15%)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import db as db_module
from src import finance, profit
from tests.test_finance import add_fixed_cost, add_loan, add_project, add_tx


def add_manday(conn, project_id, role, headcount, days, daily_rate) -> None:
    conn.execute(
        "INSERT INTO mandays (project_id, role, headcount, days, daily_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, role, headcount, days, daily_rate),
    )
    conn.commit()


# ===============================================================
# 명세 검증 시나리오
#   A: 매출 1억, 변동비 6천만, 맨데이 3×20×250,000
#   B: 매출 1억 (배부 분모를 만들기 위한 짝)
#   고정비 풀 2천만 → 매출 비례로 A에 1천만
# ===============================================================


@pytest.fixture
def spec_db(conn):
    a = add_project(conn, "A현장", "2026-01-01", "2026-01-31")
    b = add_project(conn, "B현장", "2026-01-01", "2026-01-31")

    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", account="공사매출", project_id=a)
    add_tx(conn, "2026-01-15", 60_000_000, "매입", "변동", account="외주가공비", project_id=a)
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", account="공사매출", project_id=b)

    add_fixed_cost(conn, "본사 임차료", 20_000_000)      # 1개월치 = 고정비 풀 2천만
    add_manday(conn, a, "구조설계", 3, 20, 250_000)      # 15,000,000

    return SimpleNamespace(conn=conn, a=a, b=b)


def test_spec_case_stage1_contribution_margin(spec_db):
    p = profit.compute_project_profit(spec_db.conn, spec_db.a)
    assert p.revenue == 100_000_000
    assert p.variable_cost == 60_000_000
    assert p.contribution_margin == 40_000_000
    assert p.contribution_margin_rate == pytest.approx(0.40)


def test_spec_case_stage2_operating_profit(spec_db):
    p = profit.compute_project_profit(spec_db.conn, spec_db.a)
    assert p.allocated_fixed == 10_000_000      # 2천만 × (1억/2억)
    assert p.manday_cost == 15_000_000          # 3 × 20 × 250,000
    assert p.operating_profit == 15_000_000
    assert p.operating_profit_rate == pytest.approx(0.15)


def test_spec_case_gap_is_explained(spec_db):
    """대표에게 답해야 하는 숫자: 1단과 2단의 차이 = 배부고정비 + 맨데이."""
    p = profit.compute_project_profit(spec_db.conn, spec_db.a)
    assert p.gap == 25_000_000
    assert p.gap == p.allocated_fixed + p.direct_fixed + p.manday_cost


def test_spec_case_partner_project(spec_db):
    """B현장: 변동비 0이라 공헌이익률 100%지만, 고정비 배부 후엔 90%."""
    p = profit.compute_project_profit(spec_db.conn, spec_db.b)
    assert p.contribution_margin == 100_000_000
    assert p.allocated_fixed == 10_000_000
    assert p.manday_cost == 0
    assert p.operating_profit == 90_000_000


# ===============================================================
# 1단 — 매출 / 변동비 / 공헌이익
# ===============================================================


def test_project_revenue_counts_only_sales_rows(conn):
    a = add_project(conn, "A")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-11", 30_000_000, "매입", "변동", project_id=a)
    assert profit.project_revenue(conn, a) == 100_000_000


def test_project_revenue_excludes_other_projects(conn):
    a = add_project(conn, "A")
    b = add_project(conn, "B")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-10", 555_000_000, "매출", "해당없음", project_id=b)
    add_tx(conn, "2026-01-10", 777_000_000, "매출", "해당없음", project_id=None)
    assert profit.project_revenue(conn, a) == 100_000_000


def test_project_variable_cost_only_variable_behavior(conn):
    a = add_project(conn, "A")
    add_tx(conn, "2026-01-11", 30_000_000, "매입", "변동", project_id=a)
    add_tx(conn, "2026-01-12", 5_000_000, "경비", "변동", project_id=a)
    add_tx(conn, "2026-01-13", 9_000_000, "경비", "고정", project_id=a)      # 고정 → 제외
    add_tx(conn, "2026-01-14", 7_000_000, "매입", "해당없음", project_id=a)  # 미분류 → 제외
    assert profit.project_variable_cost(conn, a) == 35_000_000


def test_project_with_no_data_is_all_zero(conn):
    a = add_project(conn, "빈 현장")
    p = profit.compute_project_profit(conn, a)
    assert (p.revenue, p.variable_cost, p.contribution_margin) == (0, 0, 0)
    assert p.contribution_margin_rate == 0.0     # 0으로 나누지 않는다
    assert p.operating_profit_rate == 0.0


@pytest.mark.parametrize(
    "revenue,variable,expected",
    [
        (100_000_000, 60_000_000, 40_000_000),
        (0, 0, 0),
        (50_000_000, 80_000_000, -30_000_000),   # 적자 현장도 그대로 보여준다
    ],
)
def test_contribution_margin(revenue, variable, expected):
    assert profit.contribution_margin(revenue, variable) == expected


def test_operating_profit_formula():
    assert profit.operating_profit(40_000_000, 10_000_000, 15_000_000) == 15_000_000


# ===============================================================
# 맨데이 인건비 — ERP에 안 잡히는 원가
# ===============================================================


def test_manday_cost_sums_rows(conn):
    a = add_project(conn, "A")
    add_manday(conn, a, "설계팀장", 1, 10, 420_000)   # 4,200,000
    add_manday(conn, a, "CAD", 2, 15, 240_000)        # 7,200,000
    assert profit.manday_cost(conn, a) == 11_400_000


def test_manday_cost_half_day_rounds_half_up(conn):
    """0.5일 단위 허용. 원 단위 반올림은 사사오입(내림 아님)."""
    a = add_project(conn, "A")
    add_manday(conn, a, "감리", 1, 0.5, 300_001)      # 150,000.5 → 150,001
    assert profit.manday_cost(conn, a) == 150_001


def test_manday_cost_no_rows(conn):
    a = add_project(conn, "A")
    assert profit.manday_cost(conn, a) == 0


# ===============================================================
# 직접 귀속 고정비 — 배부가 아니라 그 현장에 바로 부과
# ===============================================================


def test_direct_fixed_cost_is_charged_to_project(conn):
    a = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-12", 3_000_000, "경비", "고정", account="보험료", project_id=a)

    p = profit.compute_project_profit(conn, a)
    assert p.direct_fixed == 3_000_000
    assert p.allocated_fixed == 0                     # 배부 풀은 비어 있다
    assert p.operating_profit == 97_000_000


# ===============================================================
# 손익 항등식 — 회귀 테스트로 고정 (CLAUDE.md 규칙 1)
# ===============================================================


def test_profit_identity_holds_per_project(spec_db):
    for p in profit.compute_all(spec_db.conn):
        assert p.contribution_margin - p.allocated_fixed - p.direct_fixed - p.manday_cost == (
            p.operating_profit
        )


def test_allocated_total_equals_fixed_pool(spec_db):
    """배부 총액 == 고정비 풀. 반올림 잔차가 새어 나가면 안 된다."""
    profits = profit.compute_all(spec_db.conn)
    pool = finance.total_fixed_cost(spec_db.conn)
    assert sum(p.allocated_fixed for p in profits) == pool


def test_company_totals_are_sum_of_projects(spec_db):
    profits = profit.compute_all(spec_db.conn)
    t = profit.company_totals(spec_db.conn)
    assert t["매출"] == sum(p.revenue for p in profits) == 200_000_000
    assert t["변동비"] == sum(p.variable_cost for p in profits) == 60_000_000
    assert t["공헌이익"] == 140_000_000
    assert t["공헌이익률"] == pytest.approx(0.70)
    assert t["맨데이인건비"] == 15_000_000
    assert t["진짜영업이익"] == 105_000_000


def test_identity_holds_with_all_bases(spec_db):
    """배부기준을 바꿔도 항등식과 배부 총액은 유지된다."""
    pool = finance.total_fixed_cost(spec_db.conn)
    for basis in (
        finance.BASIS_REVENUE,
        finance.BASIS_DURATION,
        finance.BASIS_VARIABLE_COST,
        finance.BASIS_MANDAY,
        finance.BASIS_EQUAL,
    ):
        db_module.set_setting(spec_db.conn, "allocation_basis", basis)
        profits = profit.compute_all(spec_db.conn)
        assert sum(p.allocated_fixed for p in profits) == pool, basis
        for p in profits:
            assert p.contribution_margin - p.gap == p.operating_profit, basis


def test_compute_all_empty_db(conn):
    assert profit.compute_all(conn) == []


def test_compute_project_profit_unknown_id(conn):
    with pytest.raises(LookupError):
        profit.compute_project_profit(conn, 999)


# ===============================================================
# BEP (손익분기 매출)
# ===============================================================


def test_bep_revenue_spec_case():
    """전사 고정비 20,000,000, 공헌이익률 40% → BEP 매출 50,000,000."""
    assert profit.bep_revenue(20_000_000, 0.40) == 50_000_000


@pytest.mark.parametrize(
    "fixed,rate,expected",
    [
        (0, 0.40, 0),
        (20_000_000, 1.0, 20_000_000),
        (20_000_000, 0.0, 0),      # 공헌이익률 0 → 계산 불가, 0으로 표시
        (20_000_000, -0.1, 0),     # 변동비가 매출을 넘으면 BEP 없음
        (10_000_000, 0.33, 30_303_031),   # 30,303,030.3 → 올림(그 매출이어야 흑자)
    ],
)
def test_bep_revenue_cases(fixed, rate, expected):
    assert profit.bep_revenue(fixed, rate) == expected


def test_company_bep_uses_all_period_costs(spec_db):
    """BEP 고정비 = 배부고정비 + 직접고정비 + 맨데이(수주와 무관하게 나가는 인건비)."""
    bep = profit.company_bep(spec_db.conn)
    # 고정비 20,000,000 + 맨데이 15,000,000 = 35,000,000, 전사 공헌이익률 70%
    assert bep == 50_000_000


# ===============================================================
# 요약표 DataFrame
# ===============================================================


EXPECTED_COLUMNS = [
    "프로젝트", "매출", "변동비", "공헌이익", "공헌이익률",
    "배부고정비", "맨데이", "진짜영업이익", "진짜이익률",
]


def test_summary_frame_columns(spec_db):
    df = profit.summary_frame(spec_db.conn)
    assert list(df.columns) == EXPECTED_COLUMNS


def test_summary_frame_values(spec_db):
    df = profit.summary_frame(spec_db.conn).set_index("프로젝트")
    row = df.loc["A현장"]
    assert row["매출"] == 100_000_000
    assert row["변동비"] == 60_000_000
    assert row["공헌이익"] == 40_000_000
    assert row["공헌이익률"] == pytest.approx(0.40)
    assert row["배부고정비"] == 10_000_000
    assert row["맨데이"] == 15_000_000
    assert row["진짜영업이익"] == 15_000_000
    assert row["진짜이익률"] == pytest.approx(0.15)


def test_summary_frame_row_arithmetic(spec_db):
    """표 안에서도 항등식이 성립해야 한다(표를 눈으로 검산할 수 있어야 함)."""
    df = profit.summary_frame(spec_db.conn)
    calc = df["공헌이익"] - df["배부고정비"] - df["맨데이"]
    assert (calc == df["진짜영업이익"]).all()


def test_summary_frame_empty_db(conn):
    df = profit.summary_frame(conn)
    assert list(df.columns) == EXPECTED_COLUMNS
    assert len(df) == 0


def test_summary_frame_includes_direct_fixed_in_allocated_column(conn):
    """직접 귀속 고정비도 '배부고정비' 열에 합산해 보여준다(표의 항등식 유지)."""
    a = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-12", 3_000_000, "경비", "고정", account="보험료", project_id=a)
    add_fixed_cost(conn, "임차료", 1_000_000)

    df = profit.summary_frame(conn)
    assert df.iloc[0]["배부고정비"] == 4_000_000       # 배부 1,000,000 + 직접 3,000,000
    assert df.iloc[0]["진짜영업이익"] == 96_000_000


# ===============================================================
# 이자까지 태운 통합 케이스 (loans → 고정비 풀 → 배부)
# ===============================================================


def test_end_to_end_with_loan_interest(conn):
    a = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-11", 40_000_000, "매입", "변동", project_id=a)
    add_loan(conn, "기업은행", 120_000_000, 0.10, None, None)   # 월 이자 1,000,000

    p = profit.compute_project_profit(conn, a)
    assert p.contribution_margin == 60_000_000
    assert p.allocated_fixed == 1_000_000        # 유일한 프로젝트 → 전액 배부
    assert p.operating_profit == 59_000_000


# ===============================================================
# 현장 미귀속 변동비 — 분류는 됐는데 원가에서 새는 자리
#
# 변동비 합산은 project_id 로 묶으므로, 현장이 비어 있는 '변동' 행은
# 변동비에서 빠진다. 고정비 배부 대상도 아니라 그쪽에서도 빠진다.
# 결과는 미분류와 같다 — 이익이 과대표시된다. 자동으로 어느 쪽에 밀어
# 넣지 않고, 금액을 합산해 UI에 노출하는 것까지가 이 함수의 책임이다.
# ===============================================================


def test_orphan_variable_cost_counts_project_less_variable_rows(conn):
    a = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-11", 40_000_000, "매입", "변동", project_id=a)   # 정상 (현장 있음)
    add_tx(conn, "2026-01-12", 3_000_000, "경비", "변동", project_id=None)  # 미귀속
    add_tx(conn, "2026-01-13", 1_000_000, "경비", "변동", project_id=None)  # 미귀속

    assert profit.orphan_variable_cost(conn) == {"건수": 2, "금액": 4_000_000}


def test_orphan_variable_cost_ignores_other_behaviors(conn):
    """'고정'(→공통고정비)과 '해당없음'(→미분류)은 각자 다른 곳에서 잡힌다."""
    add_tx(conn, "2026-01-12", 5_000_000, "경비", "고정", project_id=None)
    add_tx(conn, "2026-01-13", 7_000_000, "경비", "해당없음", project_id=None)

    assert profit.orphan_variable_cost(conn) == {"건수": 0, "금액": 0}


def test_orphan_variable_cost_empty_db(conn):
    assert profit.orphan_variable_cost(conn) == {"건수": 0, "금액": 0}


def test_orphan_variable_cost_lands_in_neither_line(conn):
    """회귀 고정 — 미귀속 변동비는 변동비에도 배부고정비에도 안 들어간다.

    이 테스트가 깨진다면 둘 중 하나다. 어느 쪽이든 의도한 변경인지 확인해야
    한다: (a) 미귀속 변동비를 어딘가에 태우도록 엔진을 고쳤거나,
    (b) 실수로 공통비 합산 범위가 넓어졌거나.
    """
    a = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-11", 40_000_000, "매입", "변동", project_id=a)

    before = profit.company_totals(conn)

    # 현장 없는 변동비를 얹어도 1단·2단 어느 숫자도 움직이지 않는다
    add_tx(conn, "2026-01-12", 9_000_000, "경비", "변동", project_id=None)
    after = profit.company_totals(conn)

    assert after["변동비"] == before["변동비"]
    assert after["공헌이익"] == before["공헌이익"]
    assert after["배부고정비"] == before["배부고정비"]
    assert after["진짜영업이익"] == before["진짜영업이익"]

    # 숫자가 안 움직인다는 것 자체가 위험 신호다. 그래서 별도로 집계해 노출한다.
    assert profit.orphan_variable_cost(conn)["금액"] == 9_000_000
