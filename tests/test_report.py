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


# ===============================================================
# 경영 리포트 — 차이 분해
# ===============================================================


def test_profit_gap_identity(demo):
    """항등식: 공헌이익 − 고정비 − 이자 − 직접고정비 − 맨데이 = 진짜 영업이익.

    리포트 2번 항목이 통째로 이 식이므로, 여기가 깨지면 리포트가 거짓말을 한다.
    """
    g = report.profit_gap(demo.conn)
    assert (
        g["공헌이익"] - g["고정비"] - g["이자"] - g["직접고정비"] - g["맨데이인건비"]
        == g["진짜영업이익"]
    )


def test_profit_gap_matches_engine(demo):
    """분해 항목이 손익 엔진 값과 같다."""
    from src import profit

    t = profit.company_totals(demo.conn)
    g = report.profit_gap(demo.conn)
    assert g["공헌이익"] == t["공헌이익"]
    assert g["진짜영업이익"] == t["진짜영업이익"]
    assert g["맨데이인건비"] == t["맨데이인건비"]
    assert g["직접고정비"] == t["직접고정비"]
    # 고정비 + 이자 = 배부고정비 (이자는 고정비 풀에 들어가 함께 배부된다)
    assert g["고정비"] + g["이자"] == t["배부고정비"]


def test_profit_gap_total_equals_difference(demo):
    """차감합계 = 1단 − 2단. 이 둘이 어긋나면 어딘가 항목이 빠진 것이다."""
    g = report.profit_gap(demo.conn)
    assert g["차감합계"] == g["공헌이익"] - g["진짜영업이익"]


def test_profit_gap_empty_db(conn):
    g = report.profit_gap(conn)
    assert g["공헌이익"] == 0 and g["진짜영업이익"] == 0 and g["차감합계"] == 0


# ===============================================================
# 주의 현장 + 원인 후보
# ===============================================================


def test_weak_projects_worst_first_and_capped(conn):
    for name, rev, var in [("좋음", 100_000_000, 10_000_000),
                           ("보통", 100_000_000, 70_000_000),
                           ("나쁨", 100_000_000, 92_000_000),
                           ("적자", 100_000_000, 120_000_000)]:
        pid = add_project(conn, name, "2026-01-01", "2026-01-31")
        add_tx(conn, "2026-01-10", rev, "매출", "해당없음", project_id=pid)
        add_tx(conn, "2026-01-11", var, "매입", "변동", project_id=pid)

    weak = report.weak_projects(conn, top_n=3)
    assert [w["프로젝트"] for w in weak] == ["적자", "나쁨", "보통"]
    assert weak[0]["적자"] is True


def test_weak_projects_flags_high_variable_ratio(conn):
    """변동비율이 전사 평균보다 뚜렷이 높으면 원인 후보로 잡힌다."""
    a = add_project(conn, "변동비과다", "2026-01-01", "2026-01-31")
    b = add_project(conn, "정상", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=a)
    add_tx(conn, "2026-01-11", 95_000_000, "매입", "변동", project_id=a)
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=b)
    add_tx(conn, "2026-01-11", 10_000_000, "매입", "변동", project_id=b)

    worst = report.weak_projects(conn)[0]
    assert worst["프로젝트"] == "변동비과다"
    assert any("변동비율 높음" in c for c in worst["원인후보"])


def test_weak_projects_flags_manday_overload(conn):
    a = add_project(conn, "맨데이과다", "2026-01-01", "2026-01-31")
    b = add_project(conn, "정상", "2026-01-01", "2026-01-31")
    for pid in (a, b):
        add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=pid)
        add_tx(conn, "2026-01-11", 30_000_000, "매입", "변동", project_id=pid)
    add_manday(conn, a, "구조설계", 10, 20, 250_000)   # 50,000,000

    worst = report.weak_projects(conn)[0]
    assert worst["프로젝트"] == "맨데이과다"
    assert any("맨데이 과다" in c for c in worst["원인후보"])


def test_weak_projects_ignores_zero_revenue(conn):
    """아직 기성 청구 전인 현장을 최악으로 올리지 않는다."""
    add_project(conn, "착수전", "2026-01-01", "2026-12-31")
    add_tx(conn, "2026-01-10", 1_000_000, "매입", "변동")
    assert report.weak_projects(conn) == []


def test_weak_projects_empty_db(conn):
    assert report.weak_projects(conn) == []


# ===============================================================
# 리포트 조립
# ===============================================================


def test_build_report_has_all_four_sections(demo):
    md = report.build_report(demo.conn)
    for heading in ("## 1.", "## 2. 현장이익과 최종이익의 차이",
                    "## 3. 주의가 필요한 현장", "## 4. 확인이 필요한 항목"):
        assert heading in md, heading


def test_build_report_numbers_match_dashboard(demo):
    """[검증] 리포트에 찍힌 금액이 대시보드가 쓰는 값과 문자열 단위로 일치한다.

    리포트와 대시보드가 같은 함수를 쓰는지 확인하는 것이 목적이다.
    한쪽만 고쳐서 두 화면이 어긋나는 사고를 막는다.
    """
    md = report.build_report(demo.conn)
    s = report.company_summary(demo.conn)
    g = report.profit_gap(demo.conn)

    for value in (s["공헌이익"], s["진짜영업이익"], s["이번달매출"],
                  g["고정비"], g["이자"], g["맨데이인건비"]):
        assert f"{value:,}원" in md, value

    assert f"{s['공헌이익률'] * 100:.1f}%" in md
    assert f"{s['진짜이익률'] * 100:.1f}%" in md


def test_build_report_empty_db_does_not_crash(conn):
    md = report.build_report(conn)
    assert "경영 리포트" in md
    assert "해당 현장이 없습니다." in md


def test_build_report_warns_when_no_mandays(conn):
    """맨데이가 0이면 이익이 과대표시된다는 경고가 붙는다."""
    pid = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=pid)
    assert "맨데이 인건비가 0원입니다" in report.build_report(conn)


def test_build_report_flags_unclassified(conn):
    pid = add_project(conn, "A", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=pid)
    add_tx(conn, "2026-01-11", 5_000_000, "경비", "해당없음", account="미분류")
    md = report.build_report(conn)
    assert "미분류 거래 1건" in md
    assert "5,000,000원" in md


# ===============================================================
# 직원 공유용 마스킹
# ===============================================================


def test_share_mode_masks_amounts_but_keeps_ratios(demo):
    """금액은 가리고 비율은 남긴다.

    금액을 다 지우면 현장 담당자가 자기 현장 문제를 판단할 근거까지 사라지므로
    비율은 의도적으로 남긴다.
    """
    s = report.company_summary(demo.conn)
    md = report.build_report(demo.conn, share=True)

    assert report.REPORT_MASK in md
    assert f"{s['공헌이익']:,}원" not in md
    assert f"{s['진짜영업이익']:,}원" not in md
    # 비율은 그대로
    assert f"{s['공헌이익률'] * 100:.1f}%" in md
    assert f"{s['진짜이익률'] * 100:.1f}%" in md


def test_share_mode_leaks_no_absolute_amounts(demo):
    """마스킹 누락 회귀 — 공유용 리포트에 '원' 단위 금액이 하나도 없어야 한다."""
    import re

    md = report.build_report(demo.conn, share=True)
    leaked = re.findall(r"[\d,]{4,}원", md)
    assert leaked == [], leaked


def test_share_mode_labels_itself(demo):
    md = report.build_report(demo.conn, share=True)
    assert "직원 공유용" in md


# ===============================================================
# [2단계] AI 코멘트 — 선택 의존성
# ===============================================================


def test_ai_comment_returns_none_without_api_key(demo, monkeypatch):
    """키가 없으면 조용히 None. 예외를 던지지 않는다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert report.ai_comment("리포트", report.profit_table(demo.conn)) is None


def test_report_with_comment_falls_back_silently(demo, monkeypatch):
    """키가 없어도 1단계 리포트는 온전히 나온다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    md, has_comment = report.build_report_with_comment(demo.conn)
    assert has_comment is False
    assert "## 5. 경영 코멘트" not in md
    assert md == report.build_report(demo.conn)


def test_report_with_comment_appends_when_available(demo, monkeypatch):
    """코멘트가 생성되면 5번 절로 붙는다. (API 호출은 대체한다)"""
    monkeypatch.setattr(report, "ai_comment", lambda md, table: "1문단.\n\n2문단.\n\n3문단.")
    md, has_comment = report.build_report_with_comment(demo.conn)
    assert has_comment is True
    assert "## 5. 경영 코멘트 (AI)" in md
    assert "1문단." in md


def test_ai_comment_swallows_api_errors(demo, monkeypatch):
    """API가 실패해도 None을 돌려주고 리포트를 막지 않는다."""
    import sys
    import types

    fake = types.ModuleType("anthropic")

    class _Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("network down")

    fake.Anthropic = _Boom
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    assert report.ai_comment("리포트", report.profit_table(demo.conn)) is None


def test_ai_comment_uses_haiku_model(demo, monkeypatch):
    """모델 ID 회귀 — 스펙이 지정한 haiku 모델을 쓴다."""
    import sys
    import types

    captured = {}
    fake = types.ModuleType("anthropic")

    class _Msg:
        def __init__(self, text):
            self.content = [types.SimpleNamespace(type="text", text=text)]

    class _Client:
        def __init__(self, **kwargs):
            self.messages = types.SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            captured.update(kwargs)
            return _Msg("코멘트")

    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    assert report.ai_comment("리포트", report.profit_table(demo.conn)) == "코멘트"
    assert captured["model"] == "claude-haiku-4-5"
    # haiku 4.5 는 effort 파라미터를 지원하지 않는다 — 보내면 에러가 난다.
    assert "output_config" not in captured


def test_share_mode_mask_does_not_break_markdown(demo):
    """마스크가 마크다운 강조 기호와 충돌하지 않는다.

    별표 마스크를 쓰면 `**금액**` 이 `*****` 가 되어 볼드가 깨진다.
    (실제로 그렇게 깨졌던 자리)
    """
    md = report.build_report(demo.conn, share=True)
    assert "*" not in report.REPORT_MASK
    assert "****" not in md


def test_ai_error_reason_is_recorded_for_missing_package(demo, monkeypatch):
    """SDK 가 없으면 이유가 남아 나중에 원인을 알 수 있다."""
    import builtins

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    real_import = builtins.__import__

    def _no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_anthropic)
    assert report.ai_comment("리포트", report.profit_table(demo.conn)) is None
    assert "pip install anthropic" in report.last_ai_error


def test_ai_error_reason_is_recorded_for_bad_response_shape(demo, monkeypatch):
    """응답 구조가 예상과 다르면 '응답 파싱 실패' 로 남는다.

    이 경로는 실제 API 로 검증된 적이 없어서, 깨졌을 때 단서가 필요하다.
    """
    import sys
    import types

    fake = types.ModuleType("anthropic")

    class _Client:
        def __init__(self, **kwargs):
            # content 가 리스트가 아닌 응답 — 파싱이 깨지는 형태
            self.messages = types.SimpleNamespace(
                create=lambda **kw: types.SimpleNamespace(content=None)
            )

    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    assert report.ai_comment("리포트", report.profit_table(demo.conn)) is None
    assert "응답 파싱 실패" in report.last_ai_error


def test_ai_error_cleared_on_success(demo, monkeypatch):
    """성공하면 이전 실패 사유가 남아 있지 않다."""
    import sys
    import types

    fake = types.ModuleType("anthropic")

    class _Client:
        def __init__(self, **kwargs):
            self.messages = types.SimpleNamespace(
                create=lambda **kw: types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text="코멘트")]
                )
            )

    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    report.last_ai_error = "이전 실패"
    assert report.ai_comment("리포트", report.profit_table(demo.conn)) == "코멘트"
    assert report.last_ai_error is None


def test_report_labels_profit_as_pre_tax(demo):
    """2단 이익은 '세전'임을 리포트에 명시한다.

    이자를 고정비 풀에 넣어 배부하므로 회계상 영업이익이 아니라 세전이익에
    가깝고, 법인세는 발생과 납부 시점이 달라 곧 쓸 수 있는 현금도 아니다.
    """
    md = report.build_report(demo.conn)
    assert "세전" in md
    assert "법인세" in md


def test_share_report_also_labels_pre_tax(demo):
    """직원 공유용에서도 세전 표기는 남는다 (금액만 가린다)."""
    assert "세전" in report.build_report(demo.conn, share=True)
