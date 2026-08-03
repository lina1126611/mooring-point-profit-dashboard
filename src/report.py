"""리포트 조립 — 대시보드/엑셀 출력용 데이터 생성.

대표에게 답해야 할 질문: "현장에서 남았는데 왜 최종적으로 안 남았나?"
→ 공헌이익과 진짜 영업이익을 나란히 놓고, 그 차이를 항목별로 분해해 보여준다.

app.py 는 여기서 나온 표를 그리기만 한다. 금액을 만드는 식은 전부 이 아래에 둔다.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd

from src import finance, profit
from src.classify import classification_stats
from src.rules import FIXED, VARIABLE

BRIDGE_COLUMNS = ["단계", "금액", "시작", "끝", "유형"]
TX_COLUMNS = ["일자", "거래처", "적요", "계정과목", "원가행태", "거래유형", "금액", "원본파일"]

# 진짜이익률이 이 아래면 브리핑 상단에 경고로 띄운다.
RISK_THRESHOLD = 0.10


def profit_table(conn: sqlite3.Connection) -> pd.DataFrame:
    """프로젝트별 2단 손익 표.

    컬럼: 프로젝트, 매출, 변동비, 공헌이익, 공헌이익률,
          배부고정비, 맨데이, 진짜영업이익, 진짜이익률
    """
    return profit.summary_frame(conn)


# ===============================================================
# 워터폴 (브릿지)
# ===============================================================


def profit_bridge(conn: sqlite3.Connection, project_id: int | None = None) -> pd.DataFrame:
    """매출 → 진짜 영업이익 워터폴 데이터.

    project_id 가 None 이면 전사 합계.
    컬럼: 단계 / 금액(부호 있음) / 시작 / 끝 / 유형('합계' | '차감')

    '시작'과 '끝'은 막대의 아래위 값이다. 차트가 이 두 값만 쓰면 되도록
    누적 계산을 여기서 끝내 둔다 (UI에 계산식을 두지 않기 위함).
    """
    if project_id is None:
        rows = profit.compute_all(conn)
        revenue = sum(p.revenue for p in rows)
        variable = sum(p.variable_cost for p in rows)
        fixed = sum(p.fixed_charge for p in rows)
        manday = sum(p.manday_cost for p in rows)
    else:
        p = profit.compute_project_profit(conn, project_id)
        revenue, variable, fixed, manday = p.revenue, p.variable_cost, p.fixed_charge, p.manday_cost

    margin = revenue - variable
    operating = margin - fixed - manday

    steps = [
        ("매출", revenue, 0, revenue, "합계"),
        ("변동비", -variable, margin, revenue, "차감"),
        ("공헌이익", margin, 0, margin, "합계"),
        ("고정비", -fixed, margin - fixed, margin, "차감"),
        ("맨데이 인건비", -manday, operating, margin - fixed, "차감"),
        ("진짜 영업이익", operating, 0, operating, "합계"),
    ]
    return pd.DataFrame(steps, columns=BRIDGE_COLUMNS)


# ===============================================================
# 기간 · 이번 달
# ===============================================================


def latest_period(conn: sqlite3.Connection) -> tuple[int, int]:
    """원장에 거래가 있는 가장 최근 (연, 월).

    데모/실사용 모두에서 '이번 달'은 달력이 아니라 **원장이 끝난 달**이다.
    달력 기준으로 잡으면 아직 전표가 안 들어온 달에 0원이 찍힌다.
    거래가 없으면 오늘 날짜를 쓴다.
    """
    row = conn.execute(
        "SELECT MAX(date) AS d FROM transactions WHERE date IS NOT NULL"
    ).fetchone()
    if not row or not row["d"]:
        today = date.today()
        return today.year, today.month
    return int(row["d"][:4]), int(row["d"][5:7])


def monthly_revenue(conn: sqlite3.Connection, year: int, month: int) -> int:
    """특정 월의 매출 합계."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE tx_type = '매출' AND substr(date, 1, 7) = ?",
        (f"{year:04d}-{month:02d}",),
    ).fetchone()
    return int(row["s"])


def monthly_cost(conn: sqlite3.Connection, year: int, month: int, behavior: str) -> int:
    """특정 월의 원가 합계 (변동/고정)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE cost_behavior = ? AND substr(date, 1, 7) = ?",
        (behavior, f"{year:04d}-{month:02d}"),
    ).fetchone()
    return int(row["s"])


def monthly_outflow(conn: sqlite3.Connection, year: int, month: int) -> int:
    """그 달에 나갈 고정성 지출 = 고정비 마스터 월액 + 그 달 이자(일할)."""
    return finance.monthly_fixed_cost(conn) + finance.month_interest(conn, year, month)


def upcoming_fixed_costs(
    conn: sqlite3.Connection,
    months: int = 3,
    year: int | None = None,
    month: int | None = None,
) -> pd.DataFrame:
    """다음 달부터 N개월 고정비 지출 예정표. 컬럼: 연월 / 고정비 / 이자 / 합계."""
    if year is None or month is None:
        year, month = latest_period(conn)
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return finance.fixed_cost_schedule(conn, year, month, months=months)


# ===============================================================
# 경고
# ===============================================================


def risk_projects(
    conn: sqlite3.Connection,
    threshold: float = RISK_THRESHOLD,
) -> pd.DataFrame:
    """진짜이익률이 기준 미만이거나 적자인 프로젝트. 나쁜 순으로 정렬.

    1단(공헌이익)만 보면 멀쩡한데 2단에서 무너지는 현장을 골라내는 것이 목적이라
    '공헌이익률'과 '낙폭'을 함께 담는다.
    """
    rows = [
        {
            "프로젝트": p.project_name,
            "매출": p.revenue,
            "공헌이익률": p.contribution_margin_rate,
            "진짜이익률": p.operating_profit_rate,
            "낙폭": p.contribution_margin_rate - p.operating_profit_rate,
            "진짜영업이익": p.operating_profit,
            "적자": p.operating_profit < 0,
        }
        for p in profit.compute_all(conn)
        if p.revenue > 0 and (p.operating_profit < 0 or p.operating_profit_rate < threshold)
    ]
    df = pd.DataFrame(rows, columns=[
        "프로젝트", "매출", "공헌이익률", "진짜이익률", "낙폭", "진짜영업이익", "적자",
    ])
    return df.sort_values("진짜이익률").reset_index(drop=True)


# ===============================================================
# 전사 요약
# ===============================================================


def company_summary(conn: sqlite3.Connection) -> dict:
    """전사 요약 지표 (총매출, 총공헌이익, 총영업이익, 손익분기 매출 등).

    profit.company_totals 에 브리핑 화면이 필요로 하는 '이번 달' 값과
    미분류 현황을 얹는다.
    """
    totals = dict(profit.company_totals(conn))
    year, month = latest_period(conn)
    stats = classification_stats(conn)
    orphan = profit.orphan_variable_cost(conn)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)

    totals.update({
        "기준연": year,
        "기준월": month,
        "기준월표기": f"{year:04d}-{month:02d}",
        "전월표기": f"{prev_year:04d}-{prev_month:02d}",
        "이번달매출": monthly_revenue(conn, year, month),
        # 프로젝트 수주형은 기성 청구가 특정 월에 몰린다. 그래서 '이번 달 매출 vs
        # 이번 달 변동비'는 손익처럼 보이지만 손익이 아니다. 비교 대상은 전월 매출.
        "전월매출": monthly_revenue(conn, prev_year, prev_month),
        "이번달변동비": monthly_cost(conn, year, month, VARIABLE),
        "이번달고정비거래": monthly_cost(conn, year, month, FIXED),
        "이번달지출예정": monthly_outflow(conn, year, month),
        "다음달지출예정": monthly_outflow(
            conn, *((year + 1, 1) if month == 12 else (year, month + 1))
        ),
        "미분류건수": stats["unclassified"],
        "미분류금액": stats["unclassified_amount"],
        "미분류비율": stats["unclassified_pct"],
        # 분류는 됐지만 현장에 안 붙어 원가에서 빠지는 변동비. 미분류와 원인은
        # 다르지만 '이익이 과대표시된다'는 결과는 같아서 나란히 노출한다.
        "미귀속변동비건수": orphan["건수"],
        "미귀속변동비금액": orphan["금액"],
        "고정비내역": finance.fixed_cost_breakdown(conn),
    })
    return totals


# ===============================================================
# 거래 내역
# ===============================================================


def project_transactions(
    conn: sqlite3.Connection,
    project_id: int,
    accounts: list[str] | None = None,
    behaviors: list[str] | None = None,
) -> pd.DataFrame:
    """프로젝트의 거래 내역. 계정과목·원가행태로 걸러 볼 수 있다."""
    sql = (
        "SELECT date AS 일자, vendor AS 거래처, description AS 적요, "
        "       account AS 계정과목, cost_behavior AS 원가행태, tx_type AS 거래유형, "
        "       amount AS 금액, source_file AS 원본파일 "
        "FROM transactions WHERE project_id = ?"
    )
    params: list = [project_id]
    if accounts:
        sql += f" AND account IN ({','.join('?' * len(accounts))})"
        params += list(accounts)
    if behaviors:
        sql += f" AND cost_behavior IN ({','.join('?' * len(behaviors))})"
        params += list(behaviors)
    sql += " ORDER BY date, id"

    df = pd.read_sql_query(sql, conn, params=params)
    return df if not df.empty else pd.DataFrame(columns=TX_COLUMNS)


def project_accounts(conn: sqlite3.Connection, project_id: int) -> list[str]:
    """그 프로젝트에 실제로 나타난 계정과목 (필터 선택지용)."""
    return [
        r["account"]
        for r in conn.execute(
            "SELECT DISTINCT account FROM transactions "
            "WHERE project_id = ? AND account IS NOT NULL ORDER BY account",
            (project_id,),
        )
    ]


# ===============================================================
# 엑셀 내보내기
# ===============================================================


def export_excel(conn: sqlite3.Connection, path: str) -> None:
    """리포트를 엑셀로 내보낸다. 시트: 요약 / 프로젝트별 / 브릿지 / 지출예정."""
    summary = company_summary(conn)
    flat = {k: v for k, v in summary.items() if not isinstance(v, dict)}

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([{"항목": k, "값": v} for k, v in flat.items()]).to_excel(
            writer, sheet_name="요약", index=False
        )
        profit_table(conn).to_excel(writer, sheet_name="프로젝트별", index=False)
        profit_bridge(conn).to_excel(writer, sheet_name="브릿지", index=False)
        upcoming_fixed_costs(conn, months=12).to_excel(
            writer, sheet_name="지출예정", index=False
        )
