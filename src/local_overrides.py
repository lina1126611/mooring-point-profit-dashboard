"""거래처 → 계정과목 확정 판단을 로컬 파일에서 읽어 적용한다.

왜 rules.py 가 아니라 여기인가:
    거래처명 기반 판단("○○철강 → 자재비")을 rules.ACCOUNT_RULES 나
    resolve_unclassified.RESOLUTIONS 에 적으면 **실제 거래처명이 공개 저장소에
    커밋된다.** CLAUDE.md 가 "특정 기업의 실제 회계 데이터는 포함하지 않는다"
    고 못 박은 것과 정면으로 충돌한다.
    그래서 판단 내용은 gitignore 되는 data/local/ 에 두고, 코드(여기)는
    '그 파일을 읽어 적용하는 방법' 만 갖는다. 로직은 공개, 데이터는 로컬.

CSV 양식 (data/local/거래처_계정.csv — scripts/review_unclassified.py 산출물의
'계정' 칸을 채운 것):

    거래처,계정,원가행태,근거
    ○○철강,자재비,,강재 구매 — 현장 물량 비례
    △△기공,외주가공비,변동,절단·용접 외주

    계정      필수. rules.COST_BEHAVIOR 에 있는 이름이어야 한다.
    원가행태  비워 두면 COST_BEHAVIOR 가 정한다. 채우면 그 값이 이긴다.
    근거      감사용. 로직에는 영향 없음.

**빈 계정은 조용히 건너뛰지 않고 건너뛴 사실을 알린다.** 채운 줄만 적용되고
안 채운 줄은 미분류로 남는데, 그걸 모르면 "적용했는데 왜 안 바뀌지" 로
시간을 버린다.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from src.classify import set_override_bulk
from src.rules import COST_BEHAVIOR, UNCLASSIFIED, VALID_BEHAVIORS

# 기본 위치. data/local/ 은 .gitignore 대상이다.
DEFAULT_PATH = Path("data/local/거래처_계정.csv")

REQUIRED_COLUMNS = ("거래처", "계정")


class OverrideError(ValueError):
    """CSV 내용이 손익을 조용히 틀어뜨릴 수 있을 때 올린다."""


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


# ===============================================================
# 읽기 + 검증
# ===============================================================


def parse_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(적용할 판단, 건너뛴 줄) 을 돌려준다.

    검증에서 막는 것은 **금액이 사라지는 입력** 뿐이다:
      - 계정이 COST_BEHAVIOR 에 없으면 cost_behavior 가 '해당없음' 이 되고
        account 는 '미분류' 도 아니라서 미분류 집계에서도 빠진다. 즉 그 금액이
        어떤 화면에도 안 나온다 (load_real_data 의 ①번 점검이 잡는 그 함정).
      - 원가행태에 오타가 있으면 같은 결과가 된다.
    이 둘은 경고로 넘기지 않고 예외로 세운다. 조용히 틀리는 쪽이 더 위험하다.
    """
    parsed: list[dict] = []
    skipped: list[dict] = []
    seen: dict[str, int] = {}

    for lineno, row in enumerate(rows, start=2):  # 1행은 헤더
        vendor = _clean(row.get("거래처"))
        account = _clean(row.get("계정"))
        behavior = _clean(row.get("원가행태"))
        reason = _clean(row.get("근거"))

        if not vendor:
            continue
        if not account:
            skipped.append({"줄": lineno, "거래처": vendor, "이유": "계정 미기입"})
            continue

        if account not in COST_BEHAVIOR:
            raise OverrideError(
                f"{lineno}행 '{vendor}' — 계정 {account!r} 이 "
                f"rules.COST_BEHAVIOR 에 없습니다. 그대로 적용하면 변동비·고정비 "
                f"어디에도 안 들어가 금액이 사라집니다. "
                f"COST_BEHAVIOR 에 먼저 등록하세요."
            )
        if behavior and behavior not in VALID_BEHAVIORS:
            raise OverrideError(
                f"{lineno}행 '{vendor}' — 원가행태 {behavior!r} 은 "
                f"{VALID_BEHAVIORS} 중 하나여야 합니다."
            )

        if vendor in seen:
            raise OverrideError(
                f"{lineno}행 — 거래처 {vendor!r} 가 {seen[vendor]}행에도 있습니다. "
                f"같은 거래처에 판단이 둘이면 어느 쪽이 적용됐는지 알 수 없습니다."
            )
        seen[vendor] = lineno

        parsed.append({
            "거래처": vendor,
            "계정": account,
            "원가행태": behavior or COST_BEHAVIOR[account],
            "근거": reason,
        })

    return parsed, skipped


def load(path: str | Path = DEFAULT_PATH) -> tuple[list[dict], list[dict]]:
    """CSV 를 읽어 파싱한다. 파일이 없으면 빈 결과 (아직 안 채운 상태)."""
    p = Path(path)
    if not p.exists():
        return [], []

    # utf-8-sig — 엑셀이 저장한 CSV 에는 BOM 이 붙는다. 안 벗기면 첫 컬럼명이
    # '﻿거래처' 가 되어 '거래처' 를 못 찾는다.
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise OverrideError(
                f"{p} 에 필수 컬럼이 없습니다: {missing}. 발견한 컬럼={header}"
            )
        return parse_rows(list(reader))


# ===============================================================
# 적용
# ===============================================================


def apply(
    conn: sqlite3.Connection,
    path: str | Path = DEFAULT_PATH,
    only_unclassified: bool = True,
) -> dict:
    """판단을 transactions 에 적용한다. 적용 내역 요약을 돌려준다.

    only_unclassified=True (기본) — 미분류 행만 건드린다. ERP '용도' 칸이
    이미 계정을 정해 준 행까지 거래처 단위로 덮어쓰면 경리 담당자의 판단을
    지우게 된다 (CLAUDE.md 의 분류 우선순위 1번).

    is_manual_override = 1 을 세우므로 reclassify_all 이 덮어쓰지 않는다.
    몇 번을 돌려도 결과가 같다(멱등).
    """
    parsed, skipped = load(path)

    applied: list[dict] = []
    for item in parsed:
        where = "vendor = ?"
        params: list = [item["거래처"]]
        if only_unclassified:
            where += " AND account = ?"
            params.append(UNCLASSIFIED)

        rows = conn.execute(
            f"SELECT id, amount FROM transactions WHERE {where}", params
        ).fetchall()
        if not rows:
            continue

        set_override_bulk(
            conn,
            [int(r["id"]) for r in rows],
            account=item["계정"],
            cost_behavior=item["원가행태"],
        )
        applied.append({
            **item,
            "건수": len(rows),
            "금액": sum(int(r["amount"]) for r in rows),
        })

    return {
        "적용": applied,
        "건너뜀": skipped,
        "판단수": len(parsed),
        "적용건수": sum(a["건수"] for a in applied),
        "적용금액": sum(a["금액"] for a in applied),
    }
