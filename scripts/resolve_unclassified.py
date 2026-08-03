"""사람이 확정한 미분류 판단을 DB에 다시 적용한다.

    python scripts/resolve_unclassified.py            # 적용
    python scripts/resolve_unclassified.py --dry-run  # 영향만 보고 안 고침

왜 스크립트인가 — load_sample_data.py 는 매번 DB를 지우고 새로 만든다.
분류 수정은 UI에서 손으로 하지만, 재적재할 때마다 같은 판단을 18건씩 다시
찍을 수는 없다. 그래서 '합의된 판단'만 여기에 적어 두고 재적재 후 한 번 돌린다.

여기 있는 항목은 전부 **규칙으로 풀면 안 되는** 건들이다. 적요만 봐서는
무엇의 차액·비용인지 알 수 없어서 rules.py 가 일부러 미분류로 남긴 것이고,
tests/test_ingest.py 가 그 동작을 고정하고 있다. 사람이 판단한 결과를
is_manual_override 로 못 박는 것이 설계된 경로다.

새 판단이 합의되면 RESOLUTIONS 에 한 줄 추가한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 기본 콘솔 코드페이지(cp949)로는 '—' 같은 문자를 못 찍고 죽는다
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src import db as db_module  # noqa: E402
from src import profit  # noqa: E402
from src.classify import set_override_bulk  # noqa: E402
from src.rules import UNCLASSIFIED  # noqa: E402

DB_PATH = PROJECT_ROOT / "db" / "mooring.db"

# ---------------------------------------------------------------
# 합의된 판단 — (적요, 계정과목, 원가행태, 근거)
#
# '정산 차액': 18건 전부 project_id 가 비어 있는 본사 공통 지출이고 매달
# 반복된다. 수주가 없어도 나가므로 고정. rules.py 의 '기타 운영비 →
# 사무관리비(고정)' 와 같은 논리다. 변동으로 두면 현장에 안 붙어 있어
# 변동비 합산(project_id 로 묶음)에서 빠지고, 미분류와 똑같이 원가에서
# 사라진다. — 2026-08-03 확정
# ---------------------------------------------------------------
RESOLUTIONS = [
    ("정산 차액", "사무관리비", "고정", "본사 공통·매월 반복 → 고정"),
]


def apply_resolutions(conn, dry_run: bool = False) -> list[dict]:
    """RESOLUTIONS 를 미분류 행에 적용한다. 적용 내역을 리스트로 돌려준다.

    load_sample_data.py 가 재적재 직후 그대로 호출한다 (사람이 순서를 기억하지
    않아도 되도록). 이미 처리된 항목은 대상이 0건이라 그냥 건너뛰므로,
    몇 번을 돌려도 결과가 같다.
    """
    applied = []
    for description, account, behavior, reason in RESOLUTIONS:
        rows = conn.execute(
            "SELECT id, amount FROM transactions "
            "WHERE account = ? AND description = ?",
            (UNCLASSIFIED, description),
        ).fetchall()

        if not rows:
            continue

        if not dry_run:
            set_override_bulk(
                conn,
                [int(r["id"]) for r in rows],
                account=account,
                cost_behavior=behavior,
            )

        applied.append({
            "적요": description,
            "건수": len(rows),
            "금액": sum(int(r["amount"]) for r in rows),
            "계정과목": account,
            "원가행태": behavior,
            "근거": reason,
        })
    return applied


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not DB_PATH.exists():
        print(f"DB가 없습니다: {DB_PATH}\n먼저 scripts/load_sample_data.py 를 돌리세요.")
        raise SystemExit(1)

    conn = db_module.open_app_db(DB_PATH)

    before = profit.company_totals(conn)
    print(f"적용 전 진짜 영업이익: {before['진짜영업이익']:>18,} 원")
    print()

    applied = apply_resolutions(conn, dry_run=dry_run)
    if not applied:
        print("  적용할 대상이 없습니다 (이미 처리됨).")
    for item in applied:
        print(f"  '{item['적요']}' — {item['건수']}건 · {item['금액']:,}원 "
              f"→ {item['계정과목']}/{item['원가행태']}")
        print(f"      근거: {item['근거']}")

    total_changed = sum(item["건수"] for item in applied)

    print()
    if dry_run:
        print("--dry-run 이므로 아무것도 고치지 않았습니다.")
        conn.close()
        return

    after = profit.company_totals(conn)
    print(f"{total_changed}건 확정했습니다. (is_manual_override=1 → 재분류에도 보존)")
    print()
    print(f"적용 후 진짜 영업이익: {after['진짜영업이익']:>18,} 원 "
          f"({after['진짜영업이익'] - before['진짜영업이익']:+,})")

    # 남은 구멍도 같이 보여 준다 — 둘 다 '원가에서 새는 금액'이라 성격이 같다
    remaining = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s "
        "FROM transactions WHERE account = ?",
        (UNCLASSIFIED,),
    ).fetchone()
    orphan = profit.orphan_variable_cost(conn)
    print()
    print(f"남은 미분류        : {remaining['n']:>3}건 · {int(remaining['s']):>13,}원")
    print(f"현장 미귀속 변동비 : {orphan['건수']:>3}건 · {orphan['금액']:>13,}원")

    conn.close()


if __name__ == "__main__":
    main()
