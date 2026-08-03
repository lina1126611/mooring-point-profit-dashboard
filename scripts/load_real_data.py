"""실데이터 적재 + 품질 점검.

    python scripts/load_real_data.py
    python scripts/load_real_data.py --db db/시산.db   # 다른 파일로 시험 적재

data/raw 의 ERP export 를 rules.ERP_FORMS 정의대로 읽어 db/mooring.db 에 넣고,
"이 숫자를 믿어도 되는가"를 판정하는 점검을 전부 돌린다.

샘플 로더(load_sample_data.py)와 따로 두는 이유 — 파일 구성이 완전히 다르다.
실데이터에는 프로젝트 마스터도 맨데이도 없고, 매입이 3종(세금계산서/간이영수증/
국세·지방세)으로 쪼개져 있으며, 금액 칸이 부가세 포함 총액이다.

**주의: 적재 후 classify.reclassify_all() 을 돌리면 안 된다.**
reclassify_all 은 기존 account 를 무시하고 키워드 규칙으로 다시 판정한다.
실데이터는 ERP 의 '용도' 칸이 계정 역할을 하는데, 재분류하면 그 판단이
전부 날아가고 대부분 미분류가 된다. 규칙을 고친 뒤 다시 적용하려면
reclassify 대신 이 스크립트를 다시 돌린다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src import db as db_module  # noqa: E402
from src import erp_forms, finance, ingest, profit  # noqa: E402
from src.classify import classification_stats, classify_dataframe  # noqa: E402
from src.rules import COST_BEHAVIOR, ERP_FORMS, UNCLASSIFIED  # noqa: E402

RAW_DIR = PROJECT_ROOT / "data" / "raw"
DB_PATH = PROJECT_ROOT / "db" / "mooring.db"

# 미분류 비율이 이 선을 넘으면 규칙 보강이 필요하다는 신호 (CLAUDE.md)
UNCLASSIFIED_ALERT_PCT = 20.0


def won(n) -> str:
    return f"{int(n or 0):,}원"


def find_file(spec: dict) -> Path | None:
    pattern = re.compile(spec["match"])
    for path in sorted(RAW_DIR.glob("*.xls*")):
        if pattern.search(path.name):
            return path
    return None


# ===============================================================
# 적재
# ===============================================================


def load_one(conn, spec: dict, path: Path) -> dict:
    raw = ingest.load_excel(path, header=spec.get("header", 0))
    prepared = erp_forms.prepare(raw, spec)
    normalized = ingest.normalize(
        prepared, path.name, spec["tx_type"], aliases=spec["aliases"]
    )
    classified = classify_dataframe(normalized)
    inserted = ingest.load_transactions(conn, classified)
    return {
        "read": len(raw),
        "inserted": inserted,
        "bad_date": len(raw) - len(normalized),
        "dups": int(classified["is_duplicate_suspect"].sum()) if len(classified) else 0,
    }


# ===============================================================
# 점검
# ===============================================================


def check_unmapped_accounts(conn) -> list[dict]:
    """원가행태 매핑에 없는 계정을 찾는다. **가장 중요한 점검.**

    매핑에 없으면 cost_behavior 가 '해당없음' 이 되어 변동비·고정비 어디에도
    안 들어가는데, account 가 '미분류' 도 아니라서 미분류 집계에서도 빠진다.
    즉 금액이 어떤 화면에도 나타나지 않고 사라진다.
    """
    known = set(COST_BEHAVIOR) | {UNCLASSIFIED}
    rows = conn.execute(
        "SELECT account, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amt "
        "FROM transactions WHERE account IS NOT NULL "
        "GROUP BY account ORDER BY amt DESC"
    ).fetchall()
    return [
        {"계정": r["account"], "건수": r["n"], "금액": r["amt"]}
        for r in rows
        if r["account"] not in known
    ]


def check_vat(conn) -> dict:
    """부가세 분리가 깨지지 않았는지 확인한다.

    절대값으로 비교하는 이유 — 수정세금계산서는 금액이 음수다. -1,100원의
    공급가액은 -1,000원이고 숫자로는 -1000 > -1100 이라서, 단순 비교로는
    정상 거래가 오류로 잡힌다.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(SUM(amount_incl_vat - amount), 0) AS vat, "
        "       SUM(CASE WHEN ABS(amount) > ABS(amount_incl_vat) THEN 1 ELSE 0 END) AS broken, "
        "       SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END) AS negative "
        "FROM transactions WHERE amount_incl_vat IS NOT NULL"
    ).fetchone()
    return {
        "대상": row["n"],
        "부가세합": row["vat"],
        "역전": row["broken"] or 0,
        "음수": row["negative"] or 0,
    }


def unclassified_by_file(conn) -> list[dict]:
    """미분류가 어느 파일에서 나오는지. 규칙 보강 우선순위를 정하는 근거."""
    rows = conn.execute(
        "SELECT source_file, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amt "
        "FROM transactions WHERE account = ? "
        "GROUP BY source_file ORDER BY amt DESC",
        (UNCLASSIFIED,),
    ).fetchall()
    return [{"파일": r["source_file"], "건수": r["n"], "금액": r["amt"]} for r in rows]


def check_periods(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT source_file, COUNT(*) AS n, MIN(date) AS lo, MAX(date) AS hi "
        "FROM transactions GROUP BY source_file ORDER BY hi DESC"
    ).fetchall()
    return [
        {"파일": r["source_file"], "건수": r["n"], "시작": r["lo"], "끝": r["hi"]}
        for r in rows
    ]


def resolve_db_path(argv: list[str]) -> Path:
    """--db 로 다른 파일을 지정할 수 있게 한다.

    Streamlit 이 db/mooring.db 를 열어 두면 삭제가 막힌다. 앱을 끄지 않고
    적재를 시험해 보려면 다른 파일에 넣어 보는 편이 빠르다.
    """
    if "--db" in argv:
        idx = argv.index("--db")
        if idx + 1 >= len(argv):
            sys.exit("--db 다음에 경로를 지정하세요.")
        return (PROJECT_ROOT / argv[idx + 1]).resolve()
    return DB_PATH


def main() -> None:
    if not RAW_DIR.exists() or not any(RAW_DIR.glob("*.xls*")):
        sys.exit(f"{RAW_DIR} 에 실데이터가 없습니다.")

    db_path = resolve_db_path(sys.argv[1:])
    if db_path.exists():
        try:
            db_path.unlink()
        except PermissionError:
            sys.exit(
                f"{db_path.name} 을 다른 프로그램이 사용 중입니다.\n"
                "Streamlit 을 끄고 다시 실행하거나, --db 로 다른 경로를 지정하세요."
            )

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)

    # -----------------------------------------------------------
    print("=== 적재 ===")
    total_read = 0
    for spec in ERP_FORMS:
        path = find_file(spec)
        if path is None:
            print(f"  {spec['name']:20} 파일 없음 — 건너뜀")
            continue
        r = load_one(conn, spec, path)
        total_read += r["read"]
        note = []
        if r["bad_date"]:
            note.append(f"날짜불량 {r['bad_date']}")
        if r["dups"]:
            note.append(f"중복의심 {r['dups']}")
        print(f"  {spec['name']:20} 읽음 {r['read']:4d} → 적재 {r['inserted']:4d}"
              + (f"  ({', '.join(note)})" if note else ""))

    total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    print(f"  {'합계':20} 읽음 {total_read:4d} → 적재 {total:4d}")
    if total == 0:
        sys.exit("적재된 거래가 없습니다.")

    # -----------------------------------------------------------
    print("\n=== ① 원가행태 미등록 계정 (금액 소실 검사) ===")
    unmapped = check_unmapped_accounts(conn)
    if unmapped:
        print("  ★ 아래 계정은 변동비·고정비·미분류 어디에도 안 들어가 금액이 사라집니다.")
        print("     src/rules.py 의 COST_BEHAVIOR 에 등록해야 합니다.")
        for item in unmapped:
            print(f"     - {item['계정']:16} {item['건수']:4d}건 {won(item['금액']):>16}")
    else:
        print("  전 계정이 원가행태 매핑에 등록돼 있습니다.")

    # -----------------------------------------------------------
    print("\n=== ② 부가세 분리 ===")
    vat = check_vat(conn)
    print(f"  총액 보유 거래  : {vat['대상']}건 (음수 거래 {vat['음수']}건 — 수정세금계산서)")
    print(f"  부가세 합계     : {won(vat['부가세합'])}")
    print(f"  |공급가액|>|총액|: {vat['역전']}건 (0이어야 정상)")

    # -----------------------------------------------------------
    print("\n=== ③ 프로젝트 매핑 ===")
    unmapped_proj = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amt "
        "FROM transactions WHERE project_id IS NULL"
    ).fetchone()
    n_proj = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    print(f"  프로젝트 수     : {n_proj}개 (계약금액은 전부 0 — 마스터 미수령)")
    print(f"  현장 귀속       : {total - unmapped_proj['n']}건 "
          f"({(total - unmapped_proj['n']) / total * 100:.1f}%)")
    print(f"  미귀속(공통비)  : {unmapped_proj['n']}건 {won(unmapped_proj['amt'])}")

    # -----------------------------------------------------------
    print("\n=== ④ 분류 현황 ===")
    stats = classification_stats(conn)
    print(f"  미분류          : {stats['unclassified']}건 "
          f"({stats['unclassified_pct']:.1f}%) {won(stats['unclassified_amount'])}")
    if stats["unclassified_pct"] > UNCLASSIFIED_ALERT_PCT:
        print(f"  ★ {UNCLASSIFIED_ALERT_PCT:.0f}% 초과 — 규칙 보강이 필요합니다.")
    for item in unclassified_by_file(conn):
        print(f"     {item['파일'][:34]:34} {item['건수']:4d}건 {won(item['금액']):>16}")

    orphan = profit.orphan_variable_cost(conn)
    print(f"  현장 미귀속 변동비: {orphan['건수']}건 {won(orphan['금액'])}")
    if orphan["건수"]:
        print("     ★ 변동으로 분류됐지만 현장이 없어 공헌이익 계산에서 빠집니다.")

    # -----------------------------------------------------------
    print("\n=== ⑤ 파일별 기간 ===")
    periods = check_periods(conn)
    for p in periods:
        print(f"  {p['파일'][:34]:34} {p['시작']} ~ {p['끝']}  ({p['건수']}건)")
    ends = {p["끝"] for p in periods}
    if len(ends) > 1:
        print(f"  ★ 종료일이 다릅니다({min(ends)} ~ {max(ends)}). 원가가 덜 반영된 달은")
        print("     매출만 남아 고수익으로 보입니다. 화면에 기간 경고가 필요합니다.")

    # -----------------------------------------------------------
    print("\n=== ⑥ 2단 손익 시산 ===")
    totals = profit.company_totals(conn)
    breakdown = finance.fixed_cost_breakdown(conn)
    print(f"  분석 기간       : {totals['개월수']}개월")
    print(f"  매출            : {won(totals['매출'])}")
    print(f"  변동비          : {won(totals['변동비'])}")
    print(f"  [1단] 공헌이익  : {won(totals['공헌이익'])} "
          f"({totals['공헌이익률'] * 100:.1f}%)")
    print(f"    - 배부고정비  : {won(totals['배부고정비'])}")
    print(f"    - 직접고정비  : {won(totals['직접고정비'])}")
    print(f"    - 맨데이인건비: {won(totals['맨데이인건비'])}")
    print(f"  [2단] 세전이익  : {won(totals['진짜영업이익'])} "
          f"({totals['진짜이익률'] * 100:.1f}%)")
    print(f"  1단 - 2단 차이  : {won(totals['차이'])}")

    # breakdown 에는 '이자출처'(문자열)·'개월수'(금액 아님)가 섞여 있다
    money = {
        k: v for k, v in breakdown.items()
        if isinstance(v, (int, float)) and k not in ("개월수",)
    }
    print(f"\n  고정비 풀 구성  : {', '.join(f'{k} {won(v)}' for k, v in money.items())}")

    # 항등식 확인 — 화면 숫자를 믿을 수 있는지의 최소 조건
    lhs = totals["공헌이익"] - totals["배부고정비"] - totals["직접고정비"] - totals["맨데이인건비"]
    print(f"  항등식 검산      : 공헌이익 - 고정비 - 맨데이 = {won(lhs)} "
          f"{'일치' if lhs == totals['진짜영업이익'] else '★불일치★'}")

    # -----------------------------------------------------------
    # 미분류를 원가로 인정하면 이익이 얼마나 줄어드는가.
    # 미분류는 변동비도 고정비도 아니어서 지금 손익에서 아예 빠져 있다.
    # 즉 위의 세전이익은 **미분류 금액만큼 과대표시된 상태**다.
    # 이 시스템이 고치려는 문제가 바로 이익 과대표시이므로 반드시 같이 본다.
    # -----------------------------------------------------------
    unclassified_amt = stats["unclassified_amount"]
    if unclassified_amt:
        adjusted = totals["진짜영업이익"] - unclassified_amt
        rate = adjusted / totals["매출"] * 100 if totals["매출"] else 0.0
        print("\n=== ⑦ 미분류를 원가로 인정하면 ===")
        print(f"  현재 세전이익   : {won(totals['진짜영업이익'])} "
              f"({totals['진짜이익률'] * 100:.1f}%)")
        print(f"  미분류 원가     : {won(unclassified_amt)}")
        print(f"  보정 세전이익   : {won(adjusted)} ({rate:.1f}%)")
        print("  ★ 위쪽 세전이익은 미분류 금액만큼 과대표시된 상태입니다.")
        print("     분류가 끝날 때까지 두 숫자를 같이 보여줘야 합니다.")

    print("\n=== 입력 대기 (자료 미수령) ===")
    print("  맨데이 인건비   : 타임시트 미수령 — 급여는 고정비 풀로 배부 중")
    print("  차입금 이자     : 차입금 자료 미수령 (통장 자료 제공 불가)")
    print("  계약금액        : 프로젝트 마스터 미수령")
    print("  4대보험         : 회사부담분 분리 불가 + 1개월치만 수령 → 적재 보류")
    print(f"\nDB 생성 완료: {db_path}")


if __name__ == "__main__":
    main()
