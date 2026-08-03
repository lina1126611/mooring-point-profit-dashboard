"""db/mooring.db 의 실제 데이터로 2단 손익 요약표를 콘솔에 출력한다.

    python scripts/show_profit.py

출력:
  ① 전사 요약 — 1단 공헌이익 vs 2단 진짜 영업이익
  ② 고정비 풀 구성 (이자 이중계상 방지 확인)
  ③ 프로젝트별 2단 손익 요약표
  ④ 배부기준별 비교 (매출액 비례 / 프로젝트 기간 비례)
  ⑤ 수기 검산 — 매출 1위 프로젝트를 원장 합계로 되짚어 대조
  ⑥ 향후 12개월 고정비 지출 예정표
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src import db as db_module  # noqa: E402
from src import finance, profit  # noqa: E402
from src.rules import FIXED, VARIABLE  # noqa: E402

DB_PATH = PROJECT_ROOT / "db" / "mooring.db"


def won(n) -> str:
    return f"{int(round(n or 0)):,}원"


def pct(x) -> str:
    return f"{x * 100:.1f}%"


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> None:
    if not DB_PATH.exists():
        sys.exit("db/mooring.db 가 없다. 먼저 python scripts/load_sample_data.py 를 실행할 것.")

    conn = db_module.connect(DB_PATH)
    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 200)

    totals = profit.company_totals(conn)
    months = totals["개월수"]

    # ---------------------------------------------------------
    rule(f"① 전사 요약  (분석기간 {months}개월, 프로젝트 {totals['프로젝트수']}건)")
    print(f"  매출액                    {won(totals['매출']):>20s}")
    print(f"  (−) 변동비                {won(totals['변동비']):>20s}")
    print(f"  {'-' * 48}")
    print(f"  [1단] 공헌이익 (현장이익) {won(totals['공헌이익']):>20s}   {pct(totals['공헌이익률'])}")
    print(f"  (−) 배부 고정비           {won(totals['배부고정비']):>20s}")
    print(f"  (−) 직접귀속 고정비       {won(totals['직접고정비']):>20s}")
    print(f"  (−) 맨데이 인건비         {won(totals['맨데이인건비']):>20s}")
    print(f"  {'-' * 48}")
    print(f"  [2단] 진짜 영업이익       {won(totals['진짜영업이익']):>20s}   {pct(totals['진짜이익률'])}")
    print()
    print(f"  ▶ 현장에선 {pct(totals['공헌이익률'])} 남았는데 회사엔 {pct(totals['진짜이익률'])} 남았다.")
    print(f"    사라진 금액 {won(totals['차이'])} = 고정비 "
          f"{won(totals['배부고정비'] + totals['직접고정비'])} + 맨데이 {won(totals['맨데이인건비'])}")
    print(f"  ▶ BEP(손익분기) 매출액: {won(totals['BEP매출'])}  "
          f"— 현재 매출은 그 {totals['매출'] / totals['BEP매출'] * 100:.0f}% 수준")

    # 미분류는 변동비에도 고정비에도 안 들어간다. 위 숫자의 신뢰구간이므로 항상 노출한다.
    unc = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE cost_behavior = '해당없음' AND tx_type <> '매출'"
    ).fetchone()
    print(f"  ▶ 주의: 미분류 {unc['n']}건 {won(unc['s'])} 은 변동비에도 고정비에도 "
          f"들어가 있지 않다.\n"
          f"    분류하면 진짜 영업이익이 그만큼 더 줄어든다.")

    # ---------------------------------------------------------
    rule("② 고정비 풀 구성")
    bd = finance.fixed_cost_breakdown(conn)
    print(f"  고정비 마스터 월액 {won(bd['월고정비월액'])} × {bd['개월수']}개월"
          f"      {won(bd['월고정비']):>18s}")
    print(f"  차입금 이자 (loans 자동계산)                     {won(bd['이자비용']):>18s}")
    print(f"  원장의 공통 고정비 (프로젝트 미귀속)             {won(bd['공통고정비']):>18s}")
    print(f"  {'-' * 66}")
    print(f"  배부 대상 고정비 풀                              {won(bd['합계']):>18s}")

    ledger_interest = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE account = '이자비용'"
    ).fetchone()["s"]
    print(f"\n  [이중계상 점검] 이자 출처 설정 = '{bd['이자출처']}'")
    print(f"    원장의 이자비용 거래 {won(ledger_interest)} 는 고정비 풀에서 제외됨")
    print(f"    대신 loans 로 계산한 {won(bd['이자비용'])} 를 반영 "
          f"(차이 {won(abs(ledger_interest - bd['이자비용']))})")

    # ---------------------------------------------------------
    rule("③ 프로젝트별 2단 손익")
    df = profit.summary_frame(conn)
    view = df.copy()
    for col in ("매출", "변동비", "공헌이익", "배부고정비", "맨데이", "진짜영업이익"):
        view[col] = view[col].map(lambda v: f"{v:,}")
    for col in ("공헌이익률", "진짜이익률"):
        view[col] = view[col].map(pct)
    view["프로젝트"] = view["프로젝트"].str.slice(0, 18)
    print(view.to_string(index=False))
    print(f"\n  합계  매출 {won(df['매출'].sum())} / 공헌이익 {won(df['공헌이익'].sum())} "
          f"/ 진짜영업이익 {won(df['진짜영업이익'].sum())}")
    print("  * '배부고정비' 열 = 공통 고정비 배부액 + 그 현장에 직접 귀속된 고정비.")
    print("    표 위에서 공헌이익 − 배부고정비 − 맨데이 = 진짜영업이익 이 그대로 맞는다.")

    # ---------------------------------------------------------
    rule("④ 배부기준별 비교")
    pool = finance.total_fixed_cost(conn)
    names = {r["id"]: r["name"][:18] for r in conn.execute("SELECT id, name FROM projects")}
    compare = pd.DataFrame(
        {
            finance.BASIS_LABELS[basis]: pd.Series(finance.allocate_fixed_costs(conn, basis=basis))
            for basis in (finance.BASIS_REVENUE, finance.BASIS_DURATION)
        }
    )
    compare.index = [names[i] for i in compare.index]
    print(compare.map(lambda v: f"{v:,}").to_string())
    for label in compare.columns:
        assert compare[label].sum() == pool
    print(f"\n  두 기준 모두 배부 합계 = 고정비 풀 {won(pool)} (잔차 없음)")

    # ---------------------------------------------------------
    top = max(profit.compute_all(conn), key=lambda p: p.revenue)
    rule(f"⑤ 수기 검산 — {top.project_name}")

    q = lambda sql, *a: conn.execute(sql, a).fetchone()["s"]  # noqa: E731
    raw_revenue = q(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
        "WHERE tx_type='매출' AND project_id=?", top.project_id)
    raw_variable = q(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
        "WHERE cost_behavior=? AND project_id=?", VARIABLE, top.project_id)
    raw_direct = q(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
        "WHERE cost_behavior=? AND project_id=? AND account<>'이자비용'", FIXED, top.project_id)
    raw_manday = q(
        "SELECT COALESCE(SUM(headcount*days*daily_rate),0) AS s FROM mandays "
        "WHERE project_id=?", top.project_id)
    my_revenue_share = raw_revenue / q(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
        "WHERE tx_type='매출' AND project_id IS NOT NULL")

    print("  원장 직접 집계 (엑셀에서 필터·합계 낸 것과 같은 값):")
    print(f"    매출 (tx_type=매출)            {won(raw_revenue):>20s}   ← 엔진 {won(top.revenue)}")
    print(f"    변동비 (cost_behavior=변동)    {won(raw_variable):>20s}   ← 엔진 {won(top.variable_cost)}")
    print(f"    직접귀속 고정비                {won(raw_direct):>20s}   ← 엔진 {won(top.direct_fixed)}")
    print(f"    맨데이 Σ(인원×일수×단가)       {won(raw_manday):>20s}   ← 엔진 {won(top.manday_cost)}")
    print(f"\n  고정비 배부 (매출 비례):")
    print(f"    고정비 풀 {won(pool)} × 매출비중 {pct(my_revenue_share)} "
          f"= {won(pool * my_revenue_share)}   ← 엔진 {won(top.allocated_fixed)}")
    print(f"\n  단계별 계산:")
    print(f"    공헌이익 = {raw_revenue:,} − {raw_variable:,} = {top.contribution_margin:,}원"
          f"  ({pct(top.contribution_margin_rate)})")
    print(f"    진짜영업이익 = {top.contribution_margin:,} − {top.allocated_fixed:,}"
          f" − {top.direct_fixed:,} − {top.manday_cost:,} = {top.operating_profit:,}원"
          f"  ({pct(top.operating_profit_rate)})")

    ok = (
        raw_revenue == top.revenue
        and raw_variable == top.variable_cost
        and raw_direct == top.direct_fixed
        and round(raw_manday) == top.manday_cost
        and top.contribution_margin - top.gap == top.operating_profit
    )
    print(f"\n  검산 결과: {'일치 ✓' if ok else '불일치 ✗'}"
          f"   (공헌이익률 {pct(top.contribution_margin_rate)}"
          f" → 진짜이익률 {pct(top.operating_profit_rate)},"
          f" {pct(top.contribution_margin_rate - top.operating_profit_rate)}p 하락)")

    # ---------------------------------------------------------
    rule("⑥ 향후 12개월 고정비 지출 예정표")
    last = conn.execute("SELECT MAX(date) AS d FROM transactions").fetchone()["d"]
    y, m = int(last[:4]), int(last[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    sched = finance.fixed_cost_schedule(conn, y, m, months=12)
    print(sched.map(lambda v: f"{v:,}" if isinstance(v, int) else v).to_string(index=False))
    print(f"\n  12개월 합계 {won(sched['합계'].sum())} "
          f"(고정비 {won(sched['고정비'].sum())} + 이자 {won(sched['이자'].sum())})")

    conn.close()


if __name__ == "__main__":
    main()
