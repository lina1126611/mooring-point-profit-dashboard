"""data/sample/ 의 엑셀을 db/mooring.db 에 적재하고 통합 결과를 점검한다.

    python scripts/load_sample_data.py

점검 항목:
  ① 전 거래가 프로젝트에 매핑되는가 (공통비는 정상적인 미매핑)
  ② 미분류 비율이 몇 %인가 (20% 초과면 규칙 보강 필요)
  ③ 합의된 미분류 판단 적용 (resolve_unclassified.RESOLUTIONS)

①②는 '규칙이 얼마나 잡았나'를 보는 진단이라 사람 판단을 얹기 전 숫자다.
그래서 ③은 진단이 끝난 뒤에 적용한다. 이 스크립트는 DB를 매번 새로 만들므로,
③을 여기서 자동으로 돌려야 재적재 때마다 같은 판단을 손으로 다시 찍지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.resolve_unclassified import apply_resolutions  # noqa: E402
from src import db as db_module  # noqa: E402
from src import ingest, profit  # noqa: E402
from src.classify import classification_stats  # noqa: E402
from src.rules import UNCLASSIFIED  # noqa: E402

SAMPLE_DIR = PROJECT_ROOT / "data" / "sample"
DB_PATH = PROJECT_ROOT / "db" / "mooring.db"

TX_FILES = [
    ("매입_세금계산서_2026.xlsx", "매입"),
    ("경비지출대장_2026.xlsx", "경비"),
    ("매출_세금계산서_2026.xlsx", "매출"),
]

# ---------------------------------------------------------------
# 고정비 마스터 / 차입금 — ERP 엑셀에는 없고 대표·경리 인터뷰로 채우는 값.
#
# 주의: 경비 원장에 이미 들어 있는 항목(임차료·보험료·통신비·리스료·
# 감가상각비·지급수수료)은 여기 넣으면 이중계상된다. 원장에 안 잡히는
# 관리직 인건비만 마스터로 둔다.
# ---------------------------------------------------------------
FIXED_COSTS = [
    ("관리직 급여 (대표·경리·총무)", 28_000_000, "관리인건비"),
    ("4대보험 사업자부담분",          2_800_000, "관리인건비"),
]

# 원장에도 '이자비용' 거래가 있으므로 settings.interest_source='loans' 로
# 원장 쪽을 제외한다. 둘 다 더하면 이자가 두 번 잡힌다.
LOANS = [
    ("기업은행 운전자금", 1_500_000_000, 0.058, "2025-03-02", "2027-03-01"),
    ("산업은행 시설자금",   700_000_000, 0.045, "2024-07-15", "2029-07-14"),
]


def seed_master_data(conn) -> None:
    conn.executemany(
        "INSERT INTO fixed_costs (name, monthly_amount, category) VALUES (?, ?, ?)",
        FIXED_COSTS,
    )
    conn.executemany(
        "INSERT INTO loans (name, principal, annual_rate, start_date, end_date) "
        "VALUES (?, ?, ?, ?, ?)",
        LOANS,
    )
    conn.commit()


def won(n) -> str:
    return f"{int(n or 0):,}원"


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()  # 매번 새로 만든다 (샘플 적재 스크립트이므로)

    conn = db_module.connect(DB_PATH)
    db_module.init_schema(conn)

    # 프로젝트 마스터 먼저 (계약금액·발주처를 채워 두기 위해)
    projects_df = ingest.load_excel(SAMPLE_DIR / "프로젝트_계약현황.xlsx")
    n_proj = ingest.load_projects(conn, projects_df)
    print(f"프로젝트 마스터: {n_proj}건")

    # 거래 3종
    print("\n--- 거래 적재 ---")
    for fname, tx_type in TX_FILES:
        r = ingest.ingest_file(conn, SAMPLE_DIR / fname, tx_type=tx_type)
        print(
            f"{fname:30s} 읽음 {r['read']:4d} → 적재 {r['inserted']:4d}"
            f"  (날짜불량 {r['skipped_bad_date']}, 중복의심 {r['duplicate_suspects']})"
        )

    # 맨데이
    mandays_df = ingest.load_excel(SAMPLE_DIR / "설계맨데이_투입내역.xlsx")
    n_md = ingest.load_mandays(conn, mandays_df)
    print(f"{'설계맨데이_투입내역.xlsx':30s} 적재 {n_md:4d}")

    # 고정비 마스터 / 차입금 (엑셀 아님 — 인터뷰로 채우는 값)
    seed_master_data(conn)
    monthly = sum(f[1] for f in FIXED_COSTS)
    print(f"{'고정비 마스터':30s} 적재 {len(FIXED_COSTS):4d}  월 {won(monthly)}")
    print(f"{'차입금':30s} 적재 {len(LOANS):4d}  원금 {won(sum(l[1] for l in LOANS))}")

    # ---------------------------------------------------------
    # ① 프로젝트 매핑 점검
    # ---------------------------------------------------------
    print("\n=== ① 프로젝트 매핑 ===")
    total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    unmapped = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE project_id IS NULL"
    ).fetchone()["n"]
    print(f"전체 거래         : {total}건")
    print(f"프로젝트 매핑     : {total - unmapped}건 ({(total - unmapped) / total * 100:.1f}%)")
    print(f"미매핑(공통비)    : {unmapped}건 ({unmapped / total * 100:.1f}%)")

    by_type = conn.execute(
        "SELECT tx_type, COUNT(*) AS n, "
        "       SUM(CASE WHEN project_id IS NULL THEN 1 ELSE 0 END) AS unmapped "
        "FROM transactions GROUP BY tx_type"
    ).fetchall()
    for r in by_type:
        print(f"  {r['tx_type']:4s} {r['n']:4d}건 중 미매핑 {r['unmapped']:3d}건")

    print("\n  미매핑 행의 계정 분포 (공통비로 배부 대상):")
    for r in conn.execute(
        "SELECT account, COUNT(*) AS n, SUM(amount) AS amt FROM transactions "
        "WHERE project_id IS NULL GROUP BY account ORDER BY amt DESC"
    ):
        print(f"    {r['account']:12s} {r['n']:4d}건  {won(r['amt']):>18s}")

    # ---------------------------------------------------------
    # ② 미분류 비율 점검
    # ---------------------------------------------------------
    print("\n=== ② 분류 현황 ===")
    stats = classification_stats(conn)
    print(f"전체 {stats['total']}건 중 미분류 {stats['unclassified']}건 "
          f"({stats['unclassified_pct']:.1f}%), 금액 {won(stats['unclassified_amount'])}")
    verdict = "OK (20% 이하)" if stats["unclassified_pct"] <= 20 else "규칙 보강 필요 (20% 초과)"
    print(f"판정: {verdict}")

    print("\n  계정과목별 분포:")
    for r in conn.execute(
        "SELECT account, cost_behavior, COUNT(*) AS n, SUM(amount) AS amt "
        "FROM transactions GROUP BY account, cost_behavior ORDER BY amt DESC"
    ):
        print(f"    {r['account']:12s} [{r['cost_behavior']:4s}] {r['n']:4d}건  {won(r['amt']):>20s}")

    print("\n  미분류 건의 적요 샘플:")
    for r in conn.execute(
        "SELECT description, COUNT(*) AS n, SUM(amount) AS amt FROM transactions "
        "WHERE account = ? GROUP BY description ORDER BY n DESC LIMIT 10",
        (UNCLASSIFIED,),
    ):
        print(f"    {str(r['description']):20s} {r['n']:4d}건  {won(r['amt']):>18s}")

    # ---------------------------------------------------------
    # 원가행태 요약 (2단 손익의 재료가 갖춰졌는지)
    # ---------------------------------------------------------
    print("\n=== 원가행태 요약 ===")
    for r in conn.execute(
        "SELECT cost_behavior, tx_type, COUNT(*) AS n, SUM(amount) AS amt "
        "FROM transactions GROUP BY cost_behavior, tx_type ORDER BY cost_behavior, tx_type"
    ):
        print(f"  {r['cost_behavior']:6s} / {r['tx_type']:4s}  {r['n']:4d}건  {won(r['amt']):>20s}")

    dup = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE is_duplicate_suspect = 1"
    ).fetchone()["n"]
    print(f"\n중복 의심: {dup}건 (삭제하지 않음 — UI에서 확인)")

    # ---------------------------------------------------------
    # ③ 사람이 확정한 미분류 판단 적용
    #
    # ②까지는 '규칙이 얼마나 잡았나'를 보는 진단이라 손대기 전 숫자여야 한다.
    # 그래서 확정 판단은 진단이 끝난 뒤에 얹는다.
    # ---------------------------------------------------------
    print("\n=== ③ 확정 판단 적용 (scripts/resolve_unclassified.py) ===")
    applied = apply_resolutions(conn)
    if not applied:
        print("  적용할 대상 없음")
    for item in applied:
        print(f"  '{item['적요']}' {item['건수']}건 {won(item['금액']):>18s}"
              f"  → {item['계정과목']}/{item['원가행태']}")

    after = classification_stats(conn)
    orphan = profit.orphan_variable_cost(conn)
    print(f"\n  남은 미분류        : {after['unclassified']:3d}건 "
          f"{won(after['unclassified_amount']):>18s}")
    # 분류는 됐는데 현장이 비어 원가에서 빠지는 금액. 미분류와 결과가 같다.
    print(f"  현장 미귀속 변동비 : {orphan['건수']:3d}건 {won(orphan['금액']):>18s}")

    print(f"\nDB 생성 완료: {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
