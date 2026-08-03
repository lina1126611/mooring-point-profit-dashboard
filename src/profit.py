"""손익 엔진 — 2단 손익 구조.

    [1단] 공헌이익(현장이익) = 프로젝트 매출 − 변동비
    [2단] 진짜 영업이익      = 공헌이익 − 배부된 고정비 − 맨데이 인건비

이 모듈의 모든 함수는 pytest 검증 대상이다. (CLAUDE.md 규칙 1)
금액은 정수(원)로 다룬다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

import pandas as pd

from src import finance
from src.rules import VARIABLE

SUMMARY_COLUMNS = [
    "프로젝트", "매출", "변동비", "공헌이익", "공헌이익률",
    "배부고정비", "맨데이", "진짜영업이익", "진짜이익률",
]


@dataclass(frozen=True)
class ProjectProfit:
    """프로젝트 1건의 2단 손익 결과."""

    project_id: int
    project_name: str
    revenue: int             # 매출
    variable_cost: int       # 변동비
    contribution_margin: int # 1단 공헌이익 = revenue - variable_cost
    allocated_fixed: int     # 배부된 고정비 (공통 고정비 중 이 현장 몫)
    direct_fixed: int        # 이 현장에 직접 귀속된 고정비 (배부 대상이 아님)
    manday_cost: int         # 맨데이 인건비
    operating_profit: int    # 2단 진짜 영업이익

    @property
    def fixed_charge(self) -> int:
        """이 현장이 최종적으로 짊어진 고정비 = 배부액 + 직접귀속액."""
        return self.allocated_fixed + self.direct_fixed

    @property
    def contribution_margin_rate(self) -> float:
        """공헌이익률. 매출 0이면 0.0."""
        return self.contribution_margin / self.revenue if self.revenue else 0.0

    @property
    def operating_profit_rate(self) -> float:
        """진짜 영업이익률. 매출 0이면 0.0."""
        return self.operating_profit / self.revenue if self.revenue else 0.0

    @property
    def gap(self) -> int:
        """1단과 2단의 차이 = 대표가 궁금해하는 '왜 안 남았는지'의 총량."""
        return self.contribution_margin - self.operating_profit


def project_revenue(conn: sqlite3.Connection, project_id: int) -> int:
    """프로젝트 매출 합계."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE tx_type = '매출' AND project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row["s"])


def project_variable_cost(conn: sqlite3.Connection, project_id: int) -> int:
    """프로젝트 변동비 합계 (cost_behavior = '변동').

    미분류('해당없음')는 변동비에도 고정비에도 들어가지 않는다.
    그래서 미분류 총액을 UI에 반드시 노출해야 한다. (CLAUDE.md)
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE cost_behavior = ? AND project_id = ?",
        (VARIABLE, project_id),
    ).fetchone()
    return int(row["s"])


def orphan_variable_cost(conn: sqlite3.Connection) -> dict:
    """변동으로 분류됐지만 현장에 안 붙은 거래 (project_id IS NULL).

    이 행들은 어디에도 안 잡힌다 — 변동비 합산은 project_id 로 묶으므로
    빠지고, 고정비 배부 대상은 '고정' 행이므로 또 빠진다. 결과적으로 미분류와
    똑같이 원가에서 사라져 이익이 과대표시된다.

    분류만 보면 처리된 것처럼 보이는 탓에 미분류보다 발견이 늦다. 그래서
    금액을 합산해 UI에 따로 노출한다. 자동으로 어느 쪽에 밀어 넣지는 않는다 —
    현장 귀속을 채울지, 고정으로 볼지는 사람이 판단할 문제다.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE cost_behavior = ? AND project_id IS NULL",
        (VARIABLE,),
    ).fetchone()
    return {"건수": int(row["n"]), "금액": int(row["s"])}


def contribution_margin(revenue: int, variable_cost: int) -> int:
    """[1단] 공헌이익 = 매출 − 변동비."""
    return int(revenue) - int(variable_cost)


def manday_cost(conn: sqlite3.Connection, project_id: int) -> int:
    """맨데이 인건비 = Σ(headcount × days × daily_rate).

    ERP에 안 잡히는 원가. 이걸 빼야 이익률 과대표시가 걷힌다.
    days 는 0.5일 단위를 허용하므로 합계에서 원 단위 반올림(사사오입)한다.
    """
    total = Decimal(0)
    for row in conn.execute(
        "SELECT headcount, days, daily_rate FROM mandays WHERE project_id = ?",
        (project_id,),
    ):
        total += (
            Decimal(int(row["headcount"]))
            * Decimal(str(row["days"]))
            * Decimal(int(row["daily_rate"]))
        )
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def operating_profit(
    contribution_margin: int,
    allocated_fixed: int,
    manday_cost: int,
) -> int:
    """[2단] 진짜 영업이익 = 공헌이익 − 배부고정비 − 맨데이 인건비."""
    return int(contribution_margin) - int(allocated_fixed) - int(manday_cost)


def compute_all(conn: sqlite3.Connection) -> list[ProjectProfit]:
    """전 프로젝트 2단 손익. 고정비 배부는 전체를 한 번에 계산해야 하므로 여기서 수행."""
    projects = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
    if not projects:
        return []

    allocation = finance.allocate_fixed_costs(conn)

    results = []
    for row in projects:
        pid = row["id"]
        revenue = project_revenue(conn, pid)
        variable = project_variable_cost(conn, pid)
        margin = contribution_margin(revenue, variable)
        allocated = allocation.get(pid, 0)
        direct = finance.direct_fixed_cost(conn, pid)
        mandays = manday_cost(conn, pid)
        results.append(
            ProjectProfit(
                project_id=pid,
                project_name=row["name"],
                revenue=revenue,
                variable_cost=variable,
                contribution_margin=margin,
                allocated_fixed=allocated,
                direct_fixed=direct,
                manday_cost=mandays,
                operating_profit=operating_profit(margin, allocated + direct, mandays),
            )
        )
    return results


def compute_project_profit(conn: sqlite3.Connection, project_id: int) -> ProjectProfit:
    """프로젝트 1건의 2단 손익을 계산한다.

    배부액은 전 프로젝트를 함께 봐야 정해지므로 compute_all() 결과에서 꺼낸다.
    """
    for item in compute_all(conn):
        if item.project_id == project_id:
            return item
    raise LookupError(f"프로젝트를 찾을 수 없다: id={project_id}")


# ===============================================================
# 전사 지표
# ===============================================================


def bep_revenue(fixed_total: int, cm_rate: float) -> int:
    """BEP(손익분기) 매출액 = 총고정비 / 공헌이익률.

    공헌이익률이 0 이하면(변동비가 매출을 넘으면) 아무리 팔아도 흑자가
    안 되므로 BEP가 존재하지 않는다 → 0으로 표시한다.
    올림 처리: 그 매출을 '넘어야' 흑자이므로 내림하면 안 된다.
    """
    if fixed_total <= 0 or cm_rate <= 0:
        return 0
    value = Decimal(int(fixed_total)) / Decimal(str(cm_rate))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def company_totals(conn: sqlite3.Connection) -> dict:
    """전사 합계 — 1단과 2단을 나란히 놓고 차이를 항목별로 분해한다."""
    profits = compute_all(conn)

    revenue = sum(p.revenue for p in profits)
    variable = sum(p.variable_cost for p in profits)
    margin = sum(p.contribution_margin for p in profits)
    allocated = sum(p.allocated_fixed for p in profits)
    direct = sum(p.direct_fixed for p in profits)
    mandays = sum(p.manday_cost for p in profits)
    profit_total = sum(p.operating_profit for p in profits)
    cm_rate = margin / revenue if revenue else 0.0

    return {
        "프로젝트수": len(profits),
        "개월수": finance.analysis_months(conn),
        "매출": revenue,
        "변동비": variable,
        "공헌이익": margin,
        "공헌이익률": cm_rate,
        "배부고정비": allocated,
        "직접고정비": direct,
        "맨데이인건비": mandays,
        "진짜영업이익": profit_total,
        "진짜이익률": profit_total / revenue if revenue else 0.0,
        "차이": margin - profit_total,          # 1단 − 2단 = 안 남은 이유의 총량
        "BEP매출": bep_revenue(allocated + direct + mandays, cm_rate),
    }


def company_bep(conn: sqlite3.Connection) -> int:
    """전사 손익분기 매출액.

    분자의 '총고정비'에는 맨데이 인건비도 넣는다 — 설계 인력 급여는 수주가
    없어도 나가므로 성격이 고정비다. 이렇게 해야 BEP가 '진짜 영업이익 = 0'이
    되는 매출과 일치한다.
    """
    return company_totals(conn)["BEP매출"]


# ===============================================================
# 요약표
# ===============================================================


def summary_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """프로젝트별 2단 손익 요약표.

    '배부고정비' 열에는 직접 귀속된 고정비까지 합쳐 넣는다. 그래야 표 위에서
    공헌이익 − 배부고정비 − 맨데이 = 진짜영업이익 이 눈으로 검산된다.
    """
    rows = [
        {
            "프로젝트": p.project_name,
            "매출": p.revenue,
            "변동비": p.variable_cost,
            "공헌이익": p.contribution_margin,
            "공헌이익률": p.contribution_margin_rate,
            "배부고정비": p.fixed_charge,
            "맨데이": p.manday_cost,
            "진짜영업이익": p.operating_profit,
            "진짜이익률": p.operating_profit_rate,
        }
        for p in compute_all(conn)
    ]
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
