"""로컬 거래처→계정 오버라이드 테스트.

이 층은 미분류 금액을 변동비·고정비로 옮긴다 — 즉 손익을 직접 바꾼다.
그래서 CLAUDE.md 규칙 1에 따라 (a) 정상 (b) 경계·빈 데이터 (c) 금액이
사라지는 입력을 막는지까지 고정한다.
"""

from __future__ import annotations

import pytest

from src import local_overrides
from src.local_overrides import OverrideError
from src.rules import FIXED, NOT_APPLICABLE, UNCLASSIFIED, VARIABLE


# ===============================================================
# 픽스처
# ===============================================================


def insert(conn, **kw):
    """transactions 한 행. 기본은 미분류 매입."""
    row = {
        "date": "2026-03-01",
        "project_id": None,
        "vendor": "○○철강",
        "description": "적요",
        "account": UNCLASSIFIED,
        "tx_type": "매입",
        "amount": 1_000_000,
        "amount_incl_vat": 1_100_000,
        "cost_behavior": NOT_APPLICABLE,
        "source_file": "test.xls",
        "is_manual_override": 0,
        "is_duplicate_suspect": 0,
    }
    row.update(kw)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT INTO transactions ({cols}) VALUES ({marks})", list(row.values())
    )
    conn.commit()
    return cur.lastrowid


def write_csv(tmp_path, text: str, bom: bool = False):
    path = tmp_path / "거래처_계정.csv"
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
    return path


HEADER = "거래처,계정,원가행태,근거\n"


# ===============================================================
# 파싱
# ===============================================================


def test_계정만_채우면_원가행태는_COST_BEHAVIOR가_정한다(tmp_path):
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,강재 구매\n")
    parsed, skipped = local_overrides.load(path)

    assert skipped == []
    assert len(parsed) == 1
    assert parsed[0]["계정"] == "자재비"
    assert parsed[0]["원가행태"] == VARIABLE   # COST_BEHAVIOR['자재비']
    assert parsed[0]["근거"] == "강재 구매"


def test_원가행태를_직접_적으면_그_값이_이긴다(tmp_path):
    """자재비는 변동이 기본이지만, 사무실 재고용이라 고정으로 두고 싶을 수 있다."""
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,고정,본사 상비 재고\n")
    parsed, _ = local_overrides.load(path)
    assert parsed[0]["원가행태"] == FIXED


def test_계정_빈칸은_건너뛰고_알린다(tmp_path):
    """아직 판단하지 않은 줄. 조용히 사라지면 '왜 안 바뀌지'로 시간을 버린다."""
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n△△기공,,,아직 모름\n")
    parsed, skipped = local_overrides.load(path)

    assert [p["거래처"] for p in parsed] == ["○○철강"]
    assert len(skipped) == 1
    assert skipped[0]["거래처"] == "△△기공"
    assert skipped[0]["줄"] == 3          # 헤더가 1행


def test_엑셀이_붙인_BOM을_벗긴다(tmp_path):
    """엑셀로 저장하면 BOM 이 붙어 첫 컬럼명이 '﻿거래처' 가 된다."""
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n", bom=True)
    parsed, _ = local_overrides.load(path)
    assert parsed[0]["거래처"] == "○○철강"


def test_파일이_없으면_빈_결과(tmp_path):
    """아직 아무도 안 채운 상태. 예외가 아니라 '적용할 것 없음' 이다."""
    assert local_overrides.load(tmp_path / "없는파일.csv") == ([], [])


def test_공백만_있는_거래처_줄은_무시한다(tmp_path):
    path = write_csv(tmp_path, HEADER + "  ,자재비,,\n○○철강,자재비,,\n")
    parsed, skipped = local_overrides.load(path)
    assert len(parsed) == 1
    assert skipped == []


# ===============================================================
# 금액이 사라지는 입력은 막는다
# ===============================================================


def test_COST_BEHAVIOR에_없는_계정은_거부한다(tmp_path):
    """매핑에 없으면 '해당없음' 이 되고 미분류 집계에서도 빠져 금액이 사라진다.

    load_real_data 의 ①번 점검이 사후에 잡아 주지만, 입력 시점에 막는 것이
    맞다 — 그 사이에 나온 화면 숫자를 사람이 이미 봤을 수 있다.
    """
    path = write_csv(tmp_path, HEADER + "○○철강,없는계정과목,,\n")
    with pytest.raises(OverrideError, match="COST_BEHAVIOR"):
        local_overrides.load(path)


def test_원가행태_오타는_거부한다(tmp_path):
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,변동비,\n")
    with pytest.raises(OverrideError, match="원가행태"):
        local_overrides.load(path)


def test_같은_거래처가_두_번_나오면_거부한다(tmp_path):
    """어느 판단이 적용됐는지 알 수 없는 상태를 만들지 않는다."""
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n○○철강,외주비,,\n")
    with pytest.raises(OverrideError, match="둘이면"):
        local_overrides.load(path)


def test_필수_컬럼이_없으면_거부한다(tmp_path):
    path = write_csv(tmp_path, "상호,계정\n○○철강,자재비\n")
    with pytest.raises(OverrideError, match="필수 컬럼"):
        local_overrides.load(path)


# ===============================================================
# 적용
# ===============================================================


def test_미분류_행에_계정과_원가행태가_박힌다(conn, tmp_path):
    tx = insert(conn, vendor="○○철강", amount=5_000_000)
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,강재\n")

    result = local_overrides.apply(conn, path)

    assert result["적용건수"] == 1
    assert result["적용금액"] == 5_000_000
    row = conn.execute(
        "SELECT account, cost_behavior, is_manual_override FROM transactions WHERE id = ?",
        (tx,),
    ).fetchone()
    assert row["account"] == "자재비"
    assert row["cost_behavior"] == VARIABLE
    assert row["is_manual_override"] == 1   # 재분류가 덮어쓰지 못하게


def test_같은_거래처의_여러_행이_한_번에_바뀐다(conn, tmp_path):
    """한 건씩 고치면 빠뜨린 행이 '해당없음' 으로 남아 손익이 조용히 틀어진다."""
    for amt in (1_000_000, 2_000_000, 3_000_000):
        insert(conn, vendor="○○철강", amount=amt)
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n")

    result = local_overrides.apply(conn, path)

    assert result["적용건수"] == 3
    assert result["적용금액"] == 6_000_000
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE account = ?", (UNCLASSIFIED,)
    ).fetchone()["n"]
    assert remaining == 0


def test_이미_계정이_있는_행은_건드리지_않는다(conn, tmp_path):
    """ERP '용도' 칸이 정해 준 계정 = 경리 담당자의 판단. 우선순위 1번이다."""
    keep = insert(conn, vendor="○○철강", account="외주비", cost_behavior=VARIABLE)
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n")

    result = local_overrides.apply(conn, path)

    assert result["적용건수"] == 0
    assert conn.execute(
        "SELECT account FROM transactions WHERE id = ?", (keep,)
    ).fetchone()["account"] == "외주비"


def test_only_unclassified_False면_거래처_전체를_덮어쓴다(conn, tmp_path):
    """계정 체계를 통째로 갈아야 할 때의 탈출구. 기본값은 아니다."""
    tx = insert(conn, vendor="○○철강", account="외주비", cost_behavior=VARIABLE)
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n")

    local_overrides.apply(conn, path, only_unclassified=False)

    assert conn.execute(
        "SELECT account FROM transactions WHERE id = ?", (tx,)
    ).fetchone()["account"] == "자재비"


def test_대상이_없는_판단은_적용목록에_안_들어간다(conn, tmp_path):
    """거래처명이 원장과 안 맞는 오타를 '0건 적용' 으로 드러낸다."""
    insert(conn, vendor="○○철강")
    path = write_csv(tmp_path, HEADER + "없는거래처,자재비,,\n")

    result = local_overrides.apply(conn, path)

    assert result["판단수"] == 1
    assert result["적용"] == []
    assert result["적용건수"] == 0


def test_두_번_돌려도_결과가_같다(conn, tmp_path):
    """재적재 후 자동 호출되므로 멱등이어야 한다."""
    insert(conn, vendor="○○철강", amount=1_000_000)
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n")

    first = local_overrides.apply(conn, path)
    second = local_overrides.apply(conn, path)

    assert first["적용건수"] == 1
    assert second["적용건수"] == 0      # 이미 미분류가 아니다
    assert conn.execute(
        "SELECT account FROM transactions"
    ).fetchone()["account"] == "자재비"


def test_빈_DB에_적용해도_죽지_않는다(conn, tmp_path):
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n")
    result = local_overrides.apply(conn, path)
    assert result["적용건수"] == 0
    assert result["적용금액"] == 0


# ===============================================================
# 금액 보존 — 배부와 같은 기준으로 검산한다
# ===============================================================


def test_미분류_총액이_적용액과_잔액으로_정확히_쪼개진다(conn, tmp_path):
    """옮기는 과정에서 금액이 새지 않는지. 손익 신뢰의 최소 조건이다."""
    insert(conn, vendor="○○철강", amount=1_640_000)
    insert(conn, vendor="△△기공", amount=350_000)
    insert(conn, vendor="판단보류", amount=24_000)
    before = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE account = ?",
        (UNCLASSIFIED,),
    ).fetchone()["s"]

    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n△△기공,외주가공비,,\n")
    result = local_overrides.apply(conn, path)

    after = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE account = ?",
        (UNCLASSIFIED,),
    ).fetchone()["s"]

    assert before == 2_014_000
    assert result["적용금액"] == 1_990_000
    assert after == 24_000
    assert result["적용금액"] + after == before       # 한 원도 새지 않는다


def test_음수_거래도_금액이_보존된다(conn, tmp_path):
    """수정세금계산서는 금액이 음수다. abs 로 집계하면 합계가 틀어진다."""
    insert(conn, vendor="○○철강", amount=1_000_000)
    insert(conn, vendor="○○철강", amount=-1_000_000)
    path = write_csv(tmp_path, HEADER + "○○철강,자재비,,\n")

    result = local_overrides.apply(conn, path)

    assert result["적용건수"] == 2
    assert result["적용금액"] == 0       # 상계쌍 — 순액 0
