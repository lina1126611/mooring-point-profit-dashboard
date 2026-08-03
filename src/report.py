"""리포트 조립 — 대시보드/엑셀 출력용 데이터 생성.

대표에게 답해야 할 질문: "현장에서 남았는데 왜 최종적으로 안 남았나?"
→ 공헌이익과 진짜 영업이익을 나란히 놓고, 그 차이를 항목별로 분해해 보여준다.

app.py 는 여기서 나온 표를 그리기만 한다. 금액을 만드는 식은 전부 이 아래에 둔다.
"""

from __future__ import annotations

import os
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


# ===============================================================
# 경영 리포트
#
# 2단계 구조:
#   [1단계] 템플릿 기반 — API 키 없이 항상 동작한다. 리포트의 모든 숫자는
#           대시보드와 같은 함수(profit / finance)에서 나오므로 두 화면이
#           어긋날 수 없다.
#   [2단계] ANTHROPIC_API_KEY 가 있으면 위 리포트를 컨텍스트로 넘겨
#           경영 코멘트를 덧붙인다. 없으면 조용히 1단계만 반환한다.
# ===============================================================

# 별표(*)를 쓰면 마크다운 강조 기호와 붙어 `**금액**` 이 `*****` 로 깨진다.
REPORT_MASK = "(비공개)"


def _won(value) -> str:
    return f"{int(round(value or 0)):,}원"


def _pct(value) -> str:
    return f"{value * 100:.1f}%"


def _delta_phrase(now: int, before: int) -> str:
    """전월 대비 증감을 사람이 읽는 문장으로. 0 으로 나누는 자리를 피한다."""
    diff = int(now) - int(before)
    if before == 0:
        return "전월 실적이 없어 비교할 수 없습니다" if diff else "전월과 같이 실적이 없습니다"
    if diff == 0:
        return "전월과 같습니다"
    rate = abs(diff) / before * 100
    direction = "증가" if diff > 0 else "감소"
    return f"전월 대비 {_won(abs(diff))} {direction} ({rate:.1f}%)"


def profit_gap(conn: sqlite3.Connection) -> dict:
    """공헌이익 → 진짜 영업이익 사이에서 빠져나간 금액을 항목별로 분해한다.

    배부고정비는 고정비 풀(월고정비 + 공통고정비 + 이자)을 배부한 것이라
    총액이 풀과 같다. 그래서 이자분을 따로 떼어 표시할 수 있다.

    항등식: 공헌이익 − 고정비 − 이자 − 직접고정비 − 맨데이 = 진짜 영업이익
    """
    totals = profit.company_totals(conn)
    breakdown = finance.fixed_cost_breakdown(conn)

    interest = int(breakdown["이자비용"])
    # 배부된 총액에서 이자분을 뺀 나머지가 순수 고정비 배부액이다.
    fixed = int(totals["배부고정비"]) - interest
    direct = int(totals["직접고정비"])
    mandays = int(totals["맨데이인건비"])

    return {
        "공헌이익": int(totals["공헌이익"]),
        "고정비": fixed,
        "이자": interest,
        "직접고정비": direct,
        "맨데이인건비": mandays,
        "진짜영업이익": int(totals["진짜영업이익"]),
        "차감합계": fixed + interest + direct + mandays,
        "이자출처": breakdown["이자출처"],
    }


# 원인 후보 판정 — 전사 평균의 이 배수를 넘으면 해당 항목을 원인으로 본다.
# 1.0(평균)으로 잡으면 절반이 걸려 신호가 죽으므로 여유를 둔다.
CAUSE_MULTIPLIER = 1.2


def weak_projects(conn: sqlite3.Connection, top_n: int = 3) -> list[dict]:
    """진짜이익률 하위 N개와 원인 후보.

    원인은 단정하지 않고 '이 비율이 전사 평균보다 뚜렷이 높다'는 사실만
    제시한다. 실제 원인은 현장 사정을 아는 사람이 판단할 문제다.
    """
    projects = [p for p in profit.compute_all(conn) if p.revenue > 0]
    if not projects:
        return []

    total_revenue = sum(p.revenue for p in projects)
    if total_revenue == 0:
        return []

    avg_variable = sum(p.variable_cost for p in projects) / total_revenue
    avg_manday = sum(p.manday_cost for p in projects) / total_revenue
    avg_fixed = sum(p.allocated_fixed + p.direct_fixed for p in projects) / total_revenue

    rows = []
    for p in sorted(projects, key=lambda x: x.operating_profit_rate)[:top_n]:
        variable_rate = p.variable_cost / p.revenue
        manday_rate = p.manday_cost / p.revenue
        fixed_rate = (p.allocated_fixed + p.direct_fixed) / p.revenue

        causes = []
        if variable_rate > avg_variable * CAUSE_MULTIPLIER:
            causes.append(f"변동비율 높음 ({_pct(variable_rate)}, 전사 {_pct(avg_variable)})")
        if manday_rate > avg_manday * CAUSE_MULTIPLIER:
            causes.append(f"맨데이 과다 ({_pct(manday_rate)}, 전사 {_pct(avg_manday)})")
        if fixed_rate > avg_fixed * CAUSE_MULTIPLIER:
            causes.append(f"고정비 배부 큼 ({_pct(fixed_rate)}, 전사 {_pct(avg_fixed)})")

        rows.append({
            "프로젝트": p.project_name,
            "매출": p.revenue,
            "공헌이익률": p.contribution_margin_rate,
            "진짜이익률": p.operating_profit_rate,
            "진짜영업이익": p.operating_profit,
            "적자": p.operating_profit < 0,
            "원인후보": causes,
        })
    return rows


def _money_fn(share: bool):
    """직원 공유용이면 절대 금액을 가린다."""
    return (lambda v: REPORT_MASK) if share else _won


def _section_summary(summary: dict, share: bool) -> list[str]:
    money = _money_fn(share)
    lines = [f"## 1. {summary['기준월표기']} 요약", ""]

    if share:
        lines += ["> 직원 공유용 — 절대 금액은 가렸고 비율과 증감만 남겼습니다.", ""]

    delta = REPORT_MASK if share else _delta_phrase(
        summary["이번달매출"], summary["전월매출"]
    )
    lines += [
        f"- **매출**: {money(summary['이번달매출'])} — {delta}",
        f"- **전사 공헌이익 (1단)**: {money(summary['공헌이익'])}"
        f" (이익률 {_pct(summary['공헌이익률'])})",
        f"- **진짜 영업이익 (2단, 세전)**: {money(summary['진짜영업이익'])}"
        f" (이익률 {_pct(summary['진짜이익률'])})",
    ]
    return lines


def _section_gap(gap: dict, share: bool) -> list[str]:
    money = _money_fn(share)
    lines = [
        "",
        "## 2. 현장이익과 최종이익의 차이",
        "",
        f"전사 공헌이익 {money(gap['공헌이익'])} 에서 아래 항목이 차감되어 "
        f"최종 {money(gap['진짜영업이익'])} 이 남았습니다.",
        "",
        "| 항목 | 금액 |",
        "|---|---:|",
        f"| 공헌이익 (1단) | {money(gap['공헌이익'])} |",
        f"| − 고정비 배부 | {money(gap['고정비'])} |",
        f"| − 이자비용 | {money(gap['이자'])} |",
    ]
    if gap["직접고정비"]:
        lines.append(f"| − 현장 직접 고정비 | {money(gap['직접고정비'])} |")
    lines += [
        f"| − 맨데이 인건비 | {money(gap['맨데이인건비'])} |",
        f"| **= 진짜 영업이익 (2단, 세전)** | **{money(gap['진짜영업이익'])}** |",
        "",
        f"차이의 총량은 {money(gap['차감합계'])} 입니다. "
        "ERP 화면에서는 보이지 않던 부분이 이 금액입니다.",
        "",
        "> **세전 금액입니다.** 회계 기준으로 영업이익은 이자비용을 빼지 않지만"
        "(이자는 영업외비용), 위 계산은 이자를 고정비 풀에 넣어 배부하므로"
        " 영업이익보다 세전이익에 가깝습니다. 법인세는 포함돼 있지 않으며,"
        " 발생은 당기이지만 납부는 이듬해입니다 — 이 금액이 곧 쓸 수 있는 현금은 아닙니다.",
    ]
    if gap["맨데이인건비"] == 0:
        lines += [
            "",
            "> 맨데이 인건비가 0원입니다. 설계 인력 투입이 입력되지 않았다면 "
            "이익이 실제보다 크게 잡혀 있습니다.",
        ]
    return lines


def _section_weak(weak: list[dict], share: bool) -> list[str]:
    money = _money_fn(share)
    lines = ["", "## 3. 주의가 필요한 현장", ""]
    if not weak:
        lines.append("해당 현장이 없습니다.")
        return lines

    for i, w in enumerate(weak, 1):
        head = f"{i}. **{w['프로젝트']}** — 진짜이익률 {_pct(w['진짜이익률'])}"
        if w["적자"]:
            head += " **(적자)**"
        lines.append(head)
        lines.append(
            f"   - 공헌이익률 {_pct(w['공헌이익률'])} 로는 남는 것처럼 보이지만, "
            f"최종 {money(w['진짜영업이익'])} 입니다."
        )
        if w["원인후보"]:
            lines.append(f"   - 원인 후보: {' / '.join(w['원인후보'])}")
        else:
            lines.append(
                "   - 뚜렷한 단일 원인은 없습니다. 매출 규모 대비 고정비 배부가 "
                "누적된 형태로 보입니다."
            )
    return lines


def _section_check(summary: dict, share: bool) -> list[str]:
    money = _money_fn(share)
    lines = ["", "## 4. 확인이 필요한 항목", ""]
    clean = True

    if summary["미분류건수"]:
        clean = False
        lines.append(
            f"- **미분류 거래 {summary['미분류건수']}건 · {money(summary['미분류금액'])}** "
            f"(전체의 {summary['미분류비율']:.1f}%) — 변동비에도 고정비에도 들어가 있지 "
            "않습니다. 분류하면 진짜 영업이익은 그만큼 더 줄어듭니다."
        )
    if summary.get("미귀속변동비건수"):
        clean = False
        lines.append(
            f"- **현장 미귀속 변동비 {summary['미귀속변동비건수']}건 · "
            f"{money(summary['미귀속변동비금액'])}** — 변동으로 분류됐지만 현장이 비어 있어 "
            "어느 현장 원가에도 안 잡힙니다."
        )
    if clean:
        lines.append("- 미분류 거래 없음. 분류 상태는 깨끗합니다.")
    return lines


def build_report(conn: sqlite3.Connection, share: bool = False) -> str:
    """템플릿 기반 경영 리포트 (마크다운).

    share=True 는 '직원 공유용' — 절대 금액을 가리고 비율만 남긴다.
    비율을 남기는 이유: 금액을 다 지우면 현장 담당자가 자기 현장의 문제를
    판단할 근거까지 사라진다.
    """
    summary = company_summary(conn)
    gap = profit_gap(conn)
    weak = weak_projects(conn)

    title = "# 경영 리포트" + (" (직원 공유용)" if share else "")
    lines = [
        title,
        "",
        f"기준월 **{summary['기준월표기']}** · 분석기간 {summary['개월수']}개월 · "
        f"프로젝트 {summary['프로젝트수']}건",
        "",
    ]
    lines += _section_summary(summary, share)
    lines += _section_gap(gap, share)
    lines += _section_weak(weak, share)
    lines += _section_check(summary, share)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------
# [2단계] LLM 경영 코멘트 — 선택
#
# 키가 없거나 SDK 가 없으면 None 을 돌려주고 조용히 넘어간다.
# 리포트 본문은 1단계에서 이미 완성돼 있으므로, 여기서 실패해도
# 사용자가 보는 결과가 비지 않는다.
# ---------------------------------------------------------------

AI_MODEL = "claude-haiku-4-5"
AI_SYSTEM = (
    "당신은 수주형 엔지니어링 기업의 경영 자문입니다. "
    "제공된 손익 리포트와 현장별 표만 근거로 삼아 한국어로 3문단의 코멘트를 쓰세요.\n"
    "- 1문단: 이번 달 손익에서 가장 중요한 사실 한 가지\n"
    "- 2문단: 공헌이익과 진짜 영업이익의 차이가 시사하는 것\n"
    "- 3문단: 다음 달에 확인하거나 조치할 것\n"
    "제공되지 않은 수치를 지어내지 마세요. 표에 있는 숫자만 인용하세요. "
    "마크다운 제목은 쓰지 말고 문단만 쓰세요."
)


"""마지막 AI 코멘트 시도가 실패한 이유. 성공했거나 시도조차 안 했으면 None.

이 경로는 실제 API 로 검증된 적이 없다(키·SDK 가 없는 환경에서 개발됨).
그래서 실패를 조용히 삼키기만 하면 나중에 키를 넣었을 때 원인을 알 방법이
없어진다. UI 가 이유를 띄울 수 있도록 마지막 실패 사유를 남긴다.
"""
last_ai_error: str | None = None


def ai_comment(report_md: str, table: pd.DataFrame) -> str | None:
    """ANTHROPIC_API_KEY 가 있으면 경영 코멘트 3문단을 생성한다.

    키·SDK·네트워크 중 하나라도 없으면 None. 리포트는 이미 완성돼 있으므로
    여기서 실패해도 사용자가 보는 결과는 온전하다. 실패 사유는
    `report.last_ai_error` 에 남는다.
    """
    global last_ai_error
    last_ai_error = None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        # anthropic 은 선택 의존성이다. requirements.txt 에 넣지 않는다.
        last_ai_error = "anthropic 패키지가 없습니다. `pip install anthropic` 후 다시 시도하세요."
        return None

    # to_markdown() 은 tabulate 를 요구한다. 선택 기능 하나 때문에 의존성을
    # 늘리지 않도록 CSV 로 넘긴다 — 모델이 읽는 데는 지장이 없다.
    context = f"{report_md}\n\n## 현장별 요약표 (CSV)\n\n{table.to_csv(index=False)}"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=1500,
            system=AI_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
    except Exception as e:
        # 코멘트는 부가 기능이다. 실패해도 리포트 출력을 막지 않는다.
        last_ai_error = f"{type(e).__name__}: {e}"
        return None

    try:
        text = "".join(b.text for b in response.content if b.type == "text").strip()
    except (AttributeError, TypeError) as e:
        # 응답 구조가 예상과 다른 경우. 이 경로는 실제 API 로 검증된 적이 없다.
        last_ai_error = f"응답 파싱 실패 ({type(e).__name__}: {e})"
        return None

    if not text:
        last_ai_error = f"빈 응답 (stop_reason={getattr(response, 'stop_reason', '?')})"
        return None
    return text


def build_report_with_comment(
    conn: sqlite3.Connection, share: bool = False
) -> tuple[str, bool]:
    """리포트 + (가능하면) AI 코멘트. (마크다운, 코멘트포함여부) 를 돌려준다."""
    report = build_report(conn, share=share)
    comment = ai_comment(report, profit_table(conn))
    if not comment:
        return report, False
    return f"{report}\n## 5. 경영 코멘트 (AI)\n\n{comment}\n", True
