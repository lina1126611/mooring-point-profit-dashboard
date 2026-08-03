"""계정과목 자동분류 + 원가행태 분류.

ERP가 못 하는 일이 이것이고, 2단 손익 구조 전체가 이 분류에 의존한다.

두 가지 규칙을 지킨다:
1. 규칙에 안 걸리면 '미분류'로 남긴다. 억지로 분류하지 않는다.
2. 사람이 고친 행(is_manual_override = 1)은 재분류가 절대 덮어쓰지 않는다.
"""

from __future__ import annotations

import re
import sqlite3

import pandas as pd

from src.rules import (
    ACCOUNT_RULES,
    COST_BEHAVIOR,
    NOT_APPLICABLE,
    SALES_ACCOUNT,
    UNCLASSIFIED,
)

# 정규식은 한 번만 컴파일해 둔다 (수백~수천 행을 훑으므로)
_COMPILED_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern), account) for pattern, account in ACCOUNT_RULES
]


def _text(value) -> str | None:
    """NaN / 빈 문자열 / 'nan' 을 전부 None 으로 눕힌다.

    pandas 를 거친 값은 None 이 NaN 으로 바뀌어 오는데, NaN 은 truthy 라서
    이 처리를 빠뜨리면 계정과목이 문자열 'nan' 으로 저장된다.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text if text and text.lower() not in ("nan", "none", "nat") else None


def classify_account(
    vendor: str | None,
    description: str | None,
    existing_account: str | None = None,
    tx_type: str | None = None,
) -> str:
    """거래처명·적요 키워드로 계정과목을 판정한다.

    우선순위:
      1) 원본 엑셀에 이미 계정이 적혀 있으면 그대로 존중한다 (경리 담당자의 판단)
      2) 매출 거래는 무조건 매출 계정
      3) 적요 → 거래처명 순으로 키워드 규칙 적용, 먼저 걸리는 규칙이 이김
      4) 아무것도 안 걸리면 '미분류'
    """
    # 1) 사람이 이미 넣어 둔 계정 우선
    existing = _text(existing_account)
    if existing:
        return existing

    # 2) 매출은 계정 판정이 필요 없다
    if tx_type == "매출":
        return SALES_ACCOUNT

    # 3) 적요를 먼저 본다. 적요가 거래처명보다 거래 내용을 정확히 담고 있다.
    for raw in (description, vendor):
        haystack = _text(raw)
        if not haystack:
            continue
        for pattern, account in _COMPILED_RULES:
            if pattern.search(haystack):
                return account

    # 4) 모르면 모른다고 남긴다
    return UNCLASSIFIED


def classify_behavior(account: str | None, tx_type: str | None = None) -> str:
    """계정과목 → 원가행태(변동/고정/해당없음).

    매출 행과 미분류 행은 '해당없음'. 매핑에 없는 계정도 '해당없음'으로 두어
    변동비/고정비 어느 쪽으로도 조용히 섞여 들어가지 않게 한다.
    """
    if tx_type == "매출":
        return NOT_APPLICABLE
    name = _text(account)
    if not name:
        return NOT_APPLICABLE
    return COST_BEHAVIOR.get(name, NOT_APPLICABLE)


def classify_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """정규화된 DataFrame에 account / cost_behavior 를 채워 반환한다.

    ingest 파이프라인에서 DB 적재 직전에 호출한다. 원본은 건드리지 않는다.
    """
    out = df.copy()
    existing = out["account"] if "account" in out.columns else pd.Series([None] * len(out))

    out["account"] = [
        classify_account(v, d, a, t)
        for v, d, a, t in zip(
            out.get("vendor", pd.Series([None] * len(out))),
            out.get("description", pd.Series([None] * len(out))),
            existing,
            out.get("tx_type", pd.Series([None] * len(out))),
        )
    ]
    out["cost_behavior"] = [
        classify_behavior(a, t)
        for a, t in zip(out["account"], out.get("tx_type", pd.Series([None] * len(out))))
    ]
    return out


def reclassify_all(conn: sqlite3.Connection) -> int:
    """DB의 transactions 를 재분류한다. 변경된 행 수를 반환.

    is_manual_override = 1 인 행은 건너뛴다. 규칙을 고친 뒤 다시 돌려도
    사람이 손본 결과가 살아남아야 하기 때문이다.
    """
    rows = conn.execute(
        "SELECT id, vendor, description, account, tx_type, cost_behavior "
        "FROM transactions WHERE is_manual_override = 0"
    ).fetchall()

    changed = 0
    for row in rows:
        # 재분류이므로 기존 account 는 힌트로 쓰지 않는다.
        # (규칙 변경을 반영하는 것이 목적)
        account = classify_account(row["vendor"], row["description"], None, row["tx_type"])
        behavior = classify_behavior(account, row["tx_type"])
        if account != row["account"] or behavior != row["cost_behavior"]:
            conn.execute(
                "UPDATE transactions SET account = ?, cost_behavior = ? WHERE id = ?",
                (account, behavior, row["id"]),
            )
            changed += 1
    conn.commit()
    return changed


def set_override(
    conn: sqlite3.Connection,
    tx_id: int,
    account: str | None = None,
    cost_behavior: str | None = None,
) -> None:
    """사람이 분류를 직접 지정하고 override 플래그를 세운다.

    이후 재업로드·재분류에도 이 값은 보존된다.
    """
    sets, params = [], []
    if account is not None:
        sets.append("account = ?")
        params.append(account)
    if cost_behavior is not None:
        sets.append("cost_behavior = ?")
        params.append(cost_behavior)
    if not sets:
        return

    sets.append("is_manual_override = 1")
    params.append(tx_id)
    conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def set_override_bulk(
    conn: sqlite3.Connection,
    tx_ids: list[int],
    account: str | None = None,
    cost_behavior: str | None = None,
) -> int:
    """같은 판단을 여러 행에 한 번에 적용한다. 수정한 행 수를 돌려준다.

    '정산 차액' 처럼 한 가지 적요가 수십 건씩 미분류로 쌓이는 경우, 한 건씩
    고치면 중간에 빠뜨린 행이 생긴다. 빠뜨린 행은 '해당없음' 으로 남아
    변동비에도 고정비에도 안 들어가므로 손익이 조용히 틀어진다.

    set_override 와 동일하게 is_manual_override = 1 을 세우므로 재분류가
    덮어쓰지 않는다.
    """
    ids = [int(tx_id) for tx_id in tx_ids]
    if not ids:
        return 0

    sets, params = [], []
    if account is not None:
        sets.append("account = ?")
        params.append(account)
    if cost_behavior is not None:
        sets.append("cost_behavior = ?")
        params.append(cost_behavior)
    if not sets:
        return 0

    sets.append("is_manual_override = 1")
    placeholders = ", ".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE transactions SET {', '.join(sets)} WHERE id IN ({placeholders})",
        params + ids,
    )
    conn.commit()
    return cur.rowcount


def classification_stats(conn: sqlite3.Connection) -> dict:
    """분류 현황 요약 — 미분류 비율 점검용."""
    total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    if total == 0:
        return {"total": 0, "unclassified": 0, "unclassified_pct": 0.0, "unclassified_amount": 0}

    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amt "
        "FROM transactions WHERE account = ?",
        (UNCLASSIFIED,),
    ).fetchone()
    return {
        "total": total,
        "unclassified": row["n"],
        "unclassified_pct": row["n"] / total * 100,
        "unclassified_amount": row["amt"],
    }
