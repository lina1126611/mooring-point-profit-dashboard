"""리포트 조립 + 마스터 CRUD 테스트.

브리핑 화면이 읽는 값은 전부 여기서 만들어진다. UI를 갈아끼워도
숫자가 흔들리지 않도록 계산식을 이 층에 고정한다. (CLAUDE.md 규칙 1)
"""

from __future__ import annotations

import pytest

from src import db as db_module
from src import finance, masters, report
from tests.test_finance import add_fixed_cost, add_loan, add_project, add_tx
from tests.test_profit import add_manday


@pytest.fixture
def demo(conn):
    """A현장(건전) / B현장(위험) 2건. 원장은 2026-01 ~ 2026-02."""
    a = add_project(conn, "A현장", "2026-01-01", "2026-02-28")
    b = add_project(conn, "B현장", "2026-01-01", "2026-02-28")

    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", account="공사매출", project_id=a)
    add_tx(conn, "2026-02-10", 100_000_000, "매출", "해당없음", account="공사매출", project_id=a)
    add_tx(conn, "2026-01-15", 60_000_000, "매입", "변동", account="외주가공비", project_id=a)

    add_tx(conn, "2026-02-10", 100_000_000, "매출", "해당없음", account="공사매출", project_id=b)
    add_tx(conn, "2026-02-15", 88_000_000, "매입", "변동", account="외주가공비", project_id=b)

    add_fixed_cost(conn, "본사 임차료", 10_000_000)       # 2개월 → 풀 20,000,000
    add_manday(conn, a, "구조설계", 3, 20, 250_000)        # 15,000,000
    return type("D", (), {"conn": conn, "a": a, "b": b})


# ===============================================================
# 워터폴
# ===============================================================


def test_bridge_columns_and_steps(demo):
    br = report.profit_bridge(demo.conn)
    assert list(br.columns) == report.BRIDGE_COLUMNS
    assert br["단계"].tolist() == [
        "매출", "변동비", "공헌이익", "고정비", "맨데이 인건비", "진짜 영업이익",
    ]


def test_bridge_bars_are_connected(demo):
    """차감 막대의 끝은 다음 막대의 시작과 이어져야 한다 — 끊기면 차트가 거짓말을 한다."""
    br = report.profit_bridge(demo.conn).set_index("단계")
    assert br.loc["변동비", "끝"] == br.loc["매출", "끝"]            # 매출 꼭대기에서 내려온다
    assert br.loc["변동비", "시작"] == br.loc["공헌이익", "끝"]      # 공헌이익에 착지
    assert br.loc["고정비", "끝"] == br.loc["공헌이익", "끝"]
    assert br.loc["맨데이 인건비", "끝"] == br.loc["고정비", "시작"]
    assert br.loc["맨데이 인건비", "시작"] == br.loc["진짜 영업이익", "끝"]


def test_bridge_bar_height_equals_amount(demo):
    br = report.profit_bridge(demo.conn)
    for _, row in br.iterrows():
        assert row["끝"] - row["시작"] == abs(row["금액"]), row["단계"]


def test_bridge_matches_engine(demo):
    """전사 브릿지의 시작과 끝이 손익 엔진 값과 같다."""
    from src import profit

    t = profit.company_totals(demo.conn)
    br = report.profit_bridge(demo.conn).set_index("단계")
    assert br.loc["매출", "금액"] == t["매출"]
    assert br.loc["공헌이익", "금액"] == t["공헌이익"]
    assert br.loc["진짜 영업이익", "금액"] == t["진짜영업이익"]


def test_bridge_for_single_project(demo):
    br = report.profit_bridge(demo.conn, demo.a).set_index("단계")
    assert br.loc["매출", "금액"] == 200_000_000
    assert br.loc["변동비", "금액"] == -60_000_000
    assert br.loc["공헌이익", "금액"] == 140_000_000
    assert br.loc["맨데이 인건비", "금액"] == -15_000_000
    # 140,000,000 − 배부고정비 − 15,000,000
    alloc = finance.allocate_fixed_costs(demo.conn)[demo.a]
    assert br.loc["진짜 영업이익", "금액"] == 140_000_000 - alloc - 15_000_000


def test_bridge_empty_db(conn):
    br = report.profit_bridge(conn)
    assert list(br.columns) == report.BRIDGE_COLUMNS
    assert (br["금액"] == 0).all()


# ===============================================================
# 기간 · 이번 달
# ===============================================================


def test_latest_period_is_last_month_in_ledger(demo):
    assert report.latest_period(demo.conn) == (2026, 2)


def test_latest_period_empty_db_falls_back_to_today(conn):
    from datetime import date

    today = date.today()
    assert report.latest_period(conn) == (today.year, today.month)


def test_monthly_revenue(demo):
    assert report.monthly_revenue(demo.conn, 2026, 1) == 100_000_000
    assert report.monthly_revenue(demo.conn, 2026, 2) == 200_000_000
    assert report.monthly_revenue(demo.conn, 2026, 3) == 0


def test_monthly_cost(demo):
    assert report.monthly_cost(demo.conn, 2026, 1, "변동") == 60_000_000
    assert report.monthly_cost(demo.conn, 2026, 2, "변동") == 88_000_000


def test_monthly_outflow_is_fixed_master_plus_interest(conn):
    add_fixed_cost(conn, "임차료", 5_000_000)
    add_loan(conn, "은행", 100_000_000, 0.06, None, None)
    # 2026-04(30일): 100,000,000 × 0.06 × 30/365 = 493,150
    assert report.monthly_outflow(conn, 2026, 4) == 5_000_000 + 493_150


def test_upcoming_fixed_costs_starts_next_month(demo):
    up = report.upcoming_fixed_costs(demo.conn, months=3)
    assert list(up.columns) == ["연월", "고정비", "이자", "합계"]
    assert up["연월"].tolist() == ["2026-03", "2026-04", "2026-05"]
    assert (up["고정비"] == 10_000_000).all()


def test_upcoming_fixed_costs_rolls_over_year(conn):
    add_fixed_cost(conn, "임차료", 1_000_000)
    up = report.upcoming_fixed_costs(conn, months=2, year=2026, month=12)
    assert up["연월"].tolist() == ["2027-01", "2027-02"]


# ===============================================================
# 경고
# ===============================================================


def test_risk_projects_flags_low_margin(demo):
    """B현장: 공헌이익 12% → 고정비·맨데이 얹으면 적자."""
    risk = report.risk_projects(demo.conn)
    assert "B현장" in risk["프로젝트"].tolist()
    assert "A현장" not in risk["프로젝트"].tolist()


def test_risk_projects_sorted_worst_first(conn):
    for name, rev, var in [("좋음", 100_000_000, 10_000_000),
                           ("보통", 100_000_000, 92_000_000),
                           ("적자", 100_000_000, 120_000_000)]:
        pid = add_project(conn, name, "2026-01-01", "2026-01-31")
        add_tx(conn, "2026-01-10", rev, "매출", "해당없음", project_id=pid)
        add_tx(conn, "2026-01-11", var, "매입", "변동", project_id=pid)

    risk = report.risk_projects(conn)
    assert risk["프로젝트"].tolist() == ["적자", "보통"]
    assert bool(risk.iloc[0]["적자"]) is True


def test_risk_projects_none_when_all_healthy(conn):
    pid = add_project(conn, "좋음", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=pid)
    add_tx(conn, "2026-01-11", 10_000_000, "매입", "변동", project_id=pid)
    assert report.risk_projects(conn).empty


def test_risk_projects_ignores_zero_revenue_projects(conn):
    """아직 기성 청구 전인 현장을 적자로 몰지 않는다."""
    add_project(conn, "착수전", "2026-01-01", "2026-12-31")
    add_tx(conn, "2026-01-10", 1_000_000, "매입", "변동")
    assert report.risk_projects(conn).empty


# ===============================================================
# 전사 요약
# ===============================================================


def test_company_summary_has_briefing_keys(demo):
    s = report.company_summary(demo.conn)
    for key in ("매출", "공헌이익", "진짜영업이익", "BEP매출", "기준월표기",
                "이번달매출", "전월매출", "이번달지출예정", "미분류금액", "고정비내역",
                "미귀속변동비건수", "미귀속변동비금액"):
        assert key in s, key
    assert s["기준월표기"] == "2026-02"
    assert s["이번달매출"] == 200_000_000
    assert s["전월표기"] == "2026-01"
    assert s["전월매출"] == 100_000_000


def test_company_summary_previous_month_crosses_year(conn):
    """1월이 기준월이면 전월은 작년 12월."""
    pid = add_project(conn, "A현장")
    add_tx(conn, "2025-12-20", 50_000_000, "매출", "해당없음", project_id=pid)
    add_tx(conn, "2026-01-20", 80_000_000, "매출", "해당없음", project_id=pid)

    s = report.company_summary(conn)
    assert s["기준월표기"] == "2026-01"
    assert s["전월표기"] == "2025-12"
    assert s["전월매출"] == 50_000_000


def test_company_summary_empty_db(conn):
    s = report.company_summary(conn)
    assert s["매출"] == 0 and s["진짜영업이익"] == 0
    assert s["BEP매출"] == 0


# ===============================================================
# 거래 내역
# ===============================================================


def test_project_transactions_filters(demo):
    all_rows = report.project_transactions(demo.conn, demo.a)
    assert len(all_rows) == 3
    assert list(all_rows.columns) == report.TX_COLUMNS

    only_var = report.project_transactions(demo.conn, demo.a, behaviors=["변동"])
    assert len(only_var) == 1
    assert only_var.iloc[0]["금액"] == 60_000_000

    by_account = report.project_transactions(demo.conn, demo.a, accounts=["공사매출"])
    assert len(by_account) == 2


def test_project_transactions_empty_keeps_columns(conn):
    pid = add_project(conn, "빈 현장")
    assert list(report.project_transactions(conn, pid).columns) == report.TX_COLUMNS


def test_project_accounts(demo):
    assert report.project_accounts(demo.conn, demo.a) == ["공사매출", "외주가공비"]


# ===============================================================
# 마스터 CRUD — 차입금
# ===============================================================


def test_add_loan_shows_monthly_interest(conn):
    masters.add_loan(conn, "기업은행", 500_000_000, 0.065, "2026-01-01", None)
    loans = masters.list_loans(conn)
    assert len(loans) == 1
    # 500,000,000 × 0.065 ÷ 12 = 2,708,333.33 → 절사
    assert loans[0]["monthly_interest"] == 2_708_333


def test_add_loan_rejects_bad_input(conn):
    with pytest.raises(ValueError):
        masters.add_loan(conn, "  ", 100, 0.05)
    with pytest.raises(ValueError):
        masters.add_loan(conn, "은행", -1, 0.05)
    with pytest.raises(ValueError):
        masters.add_loan(conn, "은행", 100, -0.05)


def test_update_and_delete_loan(conn):
    lid = masters.add_loan(conn, "기업은행", 100_000_000, 0.05)
    masters.update_loan(conn, lid, principal=200_000_000, annual_rate=0.06)
    row = masters.list_loans(conn)[0]
    assert row["principal"] == 200_000_000
    assert row["monthly_interest"] == 1_000_000      # 200,000,000 × 0.06 ÷ 12

    masters.delete_loan(conn, lid)
    assert masters.list_loans(conn) == []


def test_update_loan_rejects_unknown_field(conn):
    lid = masters.add_loan(conn, "은행", 100, 0.05)
    with pytest.raises(ValueError, match="수정할 수 없는"):
        masters.update_loan(conn, lid, 이자율=0.1)


def test_loan_registration_moves_the_fixed_pool(conn):
    """등록 즉시 고정비 풀과 지출 예정표에 반영돼야 한다."""
    add_tx(conn, "2026-01-10", 1_000, "매입", "변동")
    before = finance.total_fixed_cost(conn)
    masters.add_loan(conn, "기업은행", 120_000_000, 0.10, None, None)  # 월 1,000,000
    assert finance.total_fixed_cost(conn) == before + 1_000_000


# ===============================================================
# 마스터 CRUD — 고정비 / 맨데이 / 표준일당
# ===============================================================


def test_fixed_cost_crud(conn):
    fid = masters.add_fixed_cost(conn, "본사 임차료", 4_500_000, "임차료")
    assert masters.list_fixed_costs(conn)[0]["monthly_amount"] == 4_500_000
    assert finance.monthly_fixed_cost(conn) == 4_500_000

    masters.update_fixed_cost(conn, fid, monthly_amount=5_000_000)
    assert finance.monthly_fixed_cost(conn) == 5_000_000

    masters.delete_fixed_cost(conn, fid)
    assert masters.list_fixed_costs(conn) == []
    assert finance.monthly_fixed_cost(conn) == 0


def test_fixed_cost_rejects_negative(conn):
    with pytest.raises(ValueError):
        masters.add_fixed_cost(conn, "임차료", -1)


def test_manday_crud_and_cost(conn):
    pid = add_project(conn, "A현장")
    mid = masters.add_manday(conn, pid, "구조설계", 3, 20, 250_000)
    rows = masters.list_mandays(conn, pid)
    assert rows[0]["cost"] == 15_000_000

    from src import profit

    assert profit.manday_cost(conn, pid) == 15_000_000
    masters.delete_manday(conn, mid)
    assert profit.manday_cost(conn, pid) == 0


def test_manday_allows_half_days(conn):
    pid = add_project(conn, "A현장")
    masters.add_manday(conn, pid, "감리", 1, 0.5, 300_000)
    assert masters.list_mandays(conn, pid)[0]["cost"] == 150_000


def test_manday_rejects_bad_input(conn):
    pid = add_project(conn, "A현장")
    with pytest.raises(ValueError):
        masters.add_manday(conn, pid, "", 1, 1, 1000)
    with pytest.raises(ValueError):
        masters.add_manday(conn, pid, "구조설계", 1, -1, 1000)
    with pytest.raises(ValueError):
        masters.add_manday(conn, pid, "구조설계", 1, 1, -1000)


def test_daily_rates_default_and_override(conn):
    assert masters.get_daily_rates(conn) == masters.DEFAULT_DAILY_RATES

    masters.set_daily_rate(conn, "구조설계", 380_000)
    assert masters.get_daily_rates(conn)["구조설계"] == 380_000
    assert masters.get_daily_rates(conn)["설계팀장"] == 420_000  # 나머지는 그대로

    masters.set_daily_rate(conn, "특수잠수", 500_000)
    assert "특수잠수" in masters.get_daily_rates(conn)

    masters.delete_daily_rate(conn, "특수잠수")
    assert "특수잠수" not in masters.get_daily_rates(conn)


def test_daily_rates_survive_broken_setting(conn):
    """설정값이 깨져 있어도 화면이 죽지 않고 기본값으로 돌아간다."""
    db_module.set_setting(conn, masters.DAILY_RATES_KEY, "{이건 JSON이 아니다")
    assert masters.get_daily_rates(conn) == masters.DEFAULT_DAILY_RATES


def test_daily_rate_rejects_negative(conn):
    with pytest.raises(ValueError):
        masters.set_daily_rate(conn, "구조설계", -1)


# ===============================================================
# 엑셀 내보내기
# ===============================================================


def test_export_excel(demo, tmp_path):
    import openpyxl

    out = tmp_path / "리포트.xlsx"
    report.export_excel(demo.conn, str(out))
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["요약", "프로젝트별", "브릿지", "지출예정"]


def test_company_summary_surfaces_orphan_variable_cost(conn):
    """현장 미귀속 변동비가 요약에 노출된다 — 미분류와 같은 이유로 새는 금액."""
    pid = add_project(conn, "A현장")
    add_tx(conn, "2026-01-20", 100_000_000, "매출", "해당없음", project_id=pid)
    add_tx(conn, "2026-01-21", 6_000_000, "경비", "변동", project_id=None)

    s = report.company_summary(conn)
    assert s["미귀속변동비건수"] == 1
    assert s["미귀속변동비금액"] == 6_000_000
