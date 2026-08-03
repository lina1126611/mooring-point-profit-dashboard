"""데이터 통합 + 자동분류 테스트.

핵심 검증:
- 컬럼명이 서로 다른 엑셀 2개가 동일 스키마로 통합되는가
- 키워드 분류가 규칙대로 동작하고, 미분류가 보존되는가
- 사람이 고친 분류가 재분류에 살아남는가
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import ingest
from src.classify import (
    classification_stats,
    classify_account,
    classify_behavior,
    classify_dataframe,
    reclassify_all,
    set_override,
    set_override_bulk,
)
from src.rules import (
    AMBIGUOUS_ACCOUNTS,
    FIXED,
    NOT_APPLICABLE,
    SALES_ACCOUNT,
    UNCLASSIFIED,
    VARIABLE,
)


# ===============================================================
# 파싱 헬퍼
# ===============================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        (pd.Timestamp("2026-05-03"), "2026-05-03"),
        ("2026-05-03", "2026-05-03"),
        ("2026/05/03", "2026-05-03"),
        ("2026.05.03", "2026-05-03"),
        ("", None),
        (None, None),
        (float("nan"), None),
    ],
)
def test_parse_date_formats(raw, expected):
    assert ingest.parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1_234_000, 1_234_000),
        ("1,234,000", 1_234_000),
        ("₩1,234,000", 1_234_000),
        ("1234000원", 1_234_000),
        ("(1,000)", -1_000),
        ("-5,000", -5_000),
        ("", 0),
        (None, 0),
        (float("nan"), 0),
    ],
)
def test_parse_amount_formats(raw, expected):
    assert ingest.parse_amount(raw) == expected


# ===============================================================
# 서로 다른 컬럼명의 엑셀 2개 → 동일 스키마
# ===============================================================


def _purchase_style_df() -> pd.DataFrame:
    """매입 세금계산서 양식: datetime 날짜, 정수 금액."""
    return pd.DataFrame(
        {
            "작성일자": [pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-05")],
            "거래처명": ["대한제강(주)", "성원기계공업(주)"],
            "품목": ["일반구조용 강재 SS275 납품", "철구조물 외주가공"],
            "공급가액": [12_000_000, 8_500_000],
            "합계금액": [13_200_000, 9_350_000],
            "프로젝트": ["부산항 신항 부잔교 설치공사", "부산항 신항 부잔교 설치공사"],
        }
    )


def _expense_style_df() -> pd.DataFrame:
    """경비지출 대장 양식: 문자열 날짜, 콤마 금액, 컬럼명 전부 다름."""
    return pd.DataFrame(
        {
            "지출일": ["2026/03/02", "2026/03/06"],
            "지급처": ["해운대빌딩관리단", "기업은행"],
            "적요": ["본사 사무실 임차료", "운전자금 대출이자"],
            "계정": ["임차료", ""],
            "금액": ["4,500,000", "3,200,000"],
            "현장명": ["", ""],
        }
    )


def test_two_layouts_normalize_to_same_schema():
    """컬럼명·형식이 전혀 다른 두 파일이 같은 표준 스키마로 합쳐진다."""
    a = ingest.normalize(_purchase_style_df(), "매입.xlsx", "매입")
    b = ingest.normalize(_expense_style_df(), "경비.xlsx", "경비")

    assert list(a.columns) == list(b.columns)

    merged = pd.concat([a, b], ignore_index=True)
    assert len(merged) == 4

    # 날짜는 세 가지 원본 형식이 모두 'YYYY-MM-DD' 로 통일된다
    assert set(merged["date"]) == {"2026-03-02", "2026-03-05", "2026-03-06"}
    # 콤마 문자열 금액이 정수로 변환된다
    assert merged["amount"].tolist() == [12_000_000, 8_500_000, 4_500_000, 3_200_000]
    assert all(isinstance(v, int) for v in merged["amount"])
    # 출처가 행마다 보존된다
    assert merged["source_file"].tolist() == ["매입.xlsx"] * 2 + ["경비.xlsx"] * 2


def test_normalize_maps_project_column_variants():
    """'프로젝트' / '현장명' / '현장' 이 모두 project 로 매핑된다."""
    for colname in ("프로젝트", "현장명", "현장", "공사명"):
        df = pd.DataFrame(
            {"일자": ["2026-01-01"], "금액": [1000], colname: ["부산항 신항 부잔교 설치공사"]}
        )
        out = ingest.normalize(df, "x.xlsx", "매입")
        assert out["project"].iloc[0] == "부산항 신항 부잔교 설치공사"


def test_normalize_rejects_unknown_required_column():
    """필수 컬럼을 못 찾으면 조용히 넘어가지 않고 에러를 낸다."""
    df = pd.DataFrame({"알수없는컬럼": [1], "또다른컬럼": [2]})
    with pytest.raises(ValueError, match="필수 컬럼"):
        ingest.normalize(df, "x.xlsx", "매입")


def test_normalize_rejects_bad_tx_type():
    df = _purchase_style_df()
    with pytest.raises(ValueError, match="tx_type"):
        ingest.normalize(df, "x.xlsx", "이상한값")


def test_empty_project_becomes_none():
    """현장명이 비면 공통비(project=None)로 남는다."""
    out = ingest.normalize(_expense_style_df(), "경비.xlsx", "경비")
    assert out["project"].isna().all()


# ===============================================================
# 중복 감지
# ===============================================================


def test_duplicate_detection_flags_second_occurrence_only():
    df = pd.DataFrame(
        {
            "일자": ["2026-01-05", "2026-01-05", "2026-01-06"],
            "거래처": ["대한제강(주)", "대한제강(주)", "대한제강(주)"],
            "금액": [1_000_000, 1_000_000, 1_000_000],
        }
    )
    out = ingest.normalize(df, "x.xlsx", "매입")
    # 첫 행은 남기고 두 번째만 의심 표시. 날짜가 다른 세 번째는 정상.
    assert out["is_duplicate_suspect"].tolist() == [0, 1, 0]


def test_duplicates_are_not_deleted(conn):
    """중복 의심 행도 DB에는 저장된다 (자동 삭제 금지)."""
    df = pd.DataFrame(
        {
            "일자": ["2026-01-05", "2026-01-05"],
            "거래처": ["대한제강(주)", "대한제강(주)"],
            "금액": [1_000_000, 1_000_000],
        }
    )
    out = classify_dataframe(ingest.normalize(df, "x.xlsx", "매입"))
    assert ingest.load_transactions(conn, out) == 2
    n = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    assert n == 2


# ===============================================================
# 키워드 분류
# ===============================================================


@pytest.mark.parametrize(
    "description,expected_account",
    [
        ("일반구조용 강재 SS275 납품", "원재료비"),
        ("후판 철판 12T 절단납품", "원재료비"),
        ("앵커체인 76mm 납품", "원재료비"),
        ("철구조물 외주가공", "외주가공비"),
        ("용접 외주용역", "외주가공비"),
        ("자재 운반비 (현장반입)", "운반비"),
        ("용접봉 및 소모품", "소모품비"),
        ("방식도장 시공", "도장비"),
        ("운전자금 대출이자", "이자비용"),
        ("본사 사무실 임차료", "임차료"),
        ("산재보험료 납부", "보험료"),
        ("차량 리스료", "리스료"),
        ("설비 감가상각비 계상", "감가상각비"),
    ],
)
def test_keyword_rules(description, expected_account):
    assert classify_account(None, description) == expected_account


@pytest.mark.parametrize(
    "description,expected_account",
    [
        # '임차'가 들어가지만 현장 장비비지 사무실 임차료가 아니다
        ("예인선 임차", "장비임차료"),
        ("바지선 용선료", "장비임차료"),
        ("해상크레인 장비임차", "장비임차료"),
        # '용역'이 들어가지만 외주가공비가 아니다
        ("측량 용역", "검사수수료"),
        ("비파괴검사 용역", "검사수수료"),
        # '소모품'이 들어가지만 현장 소모품비가 아니라 관리비다
        ("사무용품 구입", "사무용품비"),
    ],
)
def test_rule_precedence_traps(description, expected_account):
    """순서에 의존하는 함정 규칙들이 의도대로 동작하는지 고정한다."""
    assert classify_account(None, description) == expected_account


def test_unclassified_is_preserved():
    """규칙에 안 걸리면 억지로 분류하지 않고 '미분류'로 남긴다.

    '정산 차액'은 무엇의 차액인지 원장만 봐서는 알 수 없다. 금액이 크다고
    아무 계정에나 밀어 넣으면 손익이 조용히 틀어지므로 사람이 판단한다.
    '기타 정산분'도 적요만으로는 못 잡는다 — 거래처가 있어야 판정된다
    (test_vendor_industry_rules 참고).
    """
    for desc in ("기타 정산분", "정산 차액", "차액 정산", ""):
        assert classify_account(None, desc) == UNCLASSIFIED


# ===============================================================
# 모호한 적요 보강 규칙
#   원장에 '잡비', '기타 운영비' 처럼만 적힌 행들을 잡아낸다.
#   전부 AMBIGUOUS_ACCOUNTS 라 UI에서 '검토 권장'으로 뜬다.
# ===============================================================


@pytest.mark.parametrize(
    "description,expected_account,expected_behavior",
    [
        ("잡비", "잡비", VARIABLE),
        ("현장 잡비", "잡비", VARIABLE),
        ("제잡비 정산", "잡비", VARIABLE),
        ("기타 운영비", "사무관리비", FIXED),
    ],
)
def test_vague_description_rules(description, expected_account, expected_behavior):
    account = classify_account(None, description)
    assert account == expected_account
    assert classify_behavior(account) == expected_behavior


def test_vague_rules_are_marked_ambiguous():
    """근거가 약한 판정이므로 '검토 권장' 목록에 올려 둔다."""
    assert {"잡비", "사무관리비"} <= AMBIGUOUS_ACCOUNTS


@pytest.mark.parametrize(
    "vendor,expected_account",
    [
        ("대명중공업", "외주가공비"),
        ("성원기계공업(주)", "외주가공비"),
        ("우진철구", "외주가공비"),
        ("동해플랜트외주", "외주가공비"),
        ("한국산업가스", "소모품비"),
    ],
)
def test_vendor_industry_rules(vendor, expected_account):
    """적요가 '기타 정산분' 처럼 비어 있어도 거래처 업종으로 판정한다."""
    assert classify_account(vendor, "기타 정산분") == expected_account


def test_description_still_beats_vendor():
    """거래처 규칙을 추가해도 적요 우선 원칙은 그대로다."""
    # 가스 공급업체지만 적요가 '현장 잡비'면 잡비로 본다
    assert classify_account("한국산업가스", "현장 잡비") == "잡비"
    # 외주 가공업체지만 적요가 '측량 용역'이면 검사수수료다
    assert classify_account("대명중공업", "측량 용역") == "검사수수료"


def test_vendor_rules_do_not_fire_on_unknown_vendor():
    """모르는 거래처는 여전히 미분류. 업종 규칙이 아무 데나 걸리면 안 된다."""
    assert classify_account("기타거래처", "기타 정산분") == UNCLASSIFIED
    assert classify_account("㈜대한종합", "정산 차액") == UNCLASSIFIED


def test_existing_account_is_respected():
    """원본 엑셀에 계정이 이미 있으면 키워드 추론보다 우선한다."""
    # 적요만 보면 '임차료'로 갈 상황이지만 경리가 넣은 계정을 존중한다
    assert classify_account(None, "본사 사무실 임차료", existing_account="지급수수료") == "지급수수료"
    # 공란이면 추론으로 넘어간다
    assert classify_account(None, "본사 사무실 임차료", existing_account="  ") == "임차료"


def test_sales_rows_get_sales_account():
    assert classify_account("부산항만공사", "3차 기성금 청구", tx_type="매출") == SALES_ACCOUNT


def test_vendor_name_used_when_description_missing():
    """적요가 없으면 거래처명으로 판정한다."""
    assert classify_account("삼호앵커체인", None) == "원재료비"


# ===============================================================
# 원가행태
# ===============================================================


@pytest.mark.parametrize(
    "account,expected",
    [
        ("원재료비", VARIABLE),
        ("외주가공비", VARIABLE),
        ("운반비", VARIABLE),
        ("소모품비", VARIABLE),
        ("장비임차료", VARIABLE),
        ("이자비용", FIXED),
        ("임차료", FIXED),
        ("보험료", FIXED),
        ("감가상각비", FIXED),
        ("리스료", FIXED),
    ],
)
def test_cost_behavior_mapping(account, expected):
    assert classify_behavior(account) == expected


def test_sales_behavior_is_not_applicable():
    assert classify_behavior(SALES_ACCOUNT) == NOT_APPLICABLE
    assert classify_behavior("원재료비", tx_type="매출") == NOT_APPLICABLE


def test_unclassified_behavior_is_not_applicable():
    """미분류는 변동/고정 어느 쪽으로도 조용히 섞이지 않는다."""
    assert classify_behavior(UNCLASSIFIED) == NOT_APPLICABLE
    assert classify_behavior("듣도보도못한계정") == NOT_APPLICABLE


# ===============================================================
# 수동 수정 보존
# ===============================================================


def test_manual_override_survives_reclassify(conn):
    df = pd.DataFrame(
        {
            "일자": ["2026-02-01"],
            "거래처": ["㈜대한종합"],
            "적요": ["정산 차액"],
            "금액": [500_000],
        }
    )
    out = classify_dataframe(ingest.normalize(df, "경비.xlsx", "경비"))
    ingest.load_transactions(conn, out)

    tx_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    assert conn.execute("SELECT account FROM transactions").fetchone()["account"] == UNCLASSIFIED

    # 사람이 직접 지정
    set_override(conn, tx_id, account="소모품비", cost_behavior=VARIABLE)

    # 재분류를 돌려도 사람이 고친 값이 살아남아야 한다
    reclassify_all(conn)
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    assert row["account"] == "소모품비"
    assert row["cost_behavior"] == VARIABLE
    assert row["is_manual_override"] == 1


def _load_settlement_rows(conn, amounts) -> list[int]:
    """'정산 차액' 처럼 규칙에 안 걸리는 행을 여러 건 적재한다."""
    df = pd.DataFrame(
        {
            "일자": ["2026-02-01"] * len(amounts),
            "거래처": ["㈜대한종합"] * len(amounts),
            "적요": ["정산 차액"] * len(amounts),
            "금액": list(amounts),
        }
    )
    out = classify_dataframe(ingest.normalize(df, "경비.xlsx", "경비"))
    ingest.load_transactions(conn, out)
    return [r["id"] for r in conn.execute("SELECT id FROM transactions ORDER BY id")]


def test_set_override_bulk_applies_to_every_row(conn):
    """반복되는 한 가지 적요를 한 번에 확정한다.

    한 건씩 고치면 빠뜨린 행이 '해당없음' 으로 남아 원가에서 조용히 빠진다.
    """
    ids = _load_settlement_rows(conn, [500_000, 700_000, 900_000])
    assert len(ids) == 3

    n = set_override_bulk(conn, ids, account="사무관리비", cost_behavior=FIXED)
    assert n == 3

    rows = conn.execute("SELECT * FROM transactions").fetchall()
    assert all(r["account"] == "사무관리비" for r in rows)
    assert all(r["cost_behavior"] == FIXED for r in rows)
    assert all(r["is_manual_override"] == 1 for r in rows)


def test_set_override_bulk_survives_reclassify(conn):
    """일괄 지정도 set_override 와 똑같이 재분류에 보존된다."""
    ids = _load_settlement_rows(conn, [500_000, 700_000])
    set_override_bulk(conn, ids, account="사무관리비", cost_behavior=FIXED)

    reclassify_all(conn)

    rows = conn.execute("SELECT * FROM transactions").fetchall()
    assert all(r["account"] == "사무관리비" for r in rows)
    assert all(r["cost_behavior"] == FIXED for r in rows)


def test_set_override_bulk_touches_only_given_ids(conn):
    """지정하지 않은 행은 건드리지 않는다."""
    ids = _load_settlement_rows(conn, [500_000, 700_000, 900_000])

    assert set_override_bulk(conn, ids[:1], account="사무관리비", cost_behavior=FIXED) == 1

    untouched = conn.execute(
        "SELECT * FROM transactions WHERE id IN (?, ?)", (ids[1], ids[2])
    ).fetchall()
    assert all(r["account"] == UNCLASSIFIED for r in untouched)
    assert all(r["is_manual_override"] == 0 for r in untouched)


@pytest.mark.parametrize(
    "ids, account, behavior",
    [
        ([], "사무관리비", FIXED),   # 빈 목록
        ([1], None, None),          # 바꿀 값이 없음
    ],
)
def test_set_override_bulk_no_op_cases(conn, ids, account, behavior):
    """경계 케이스 — 아무것도 안 바꾸고 0을 돌려준다."""
    _load_settlement_rows(conn, [500_000])
    assert set_override_bulk(conn, ids, account=account, cost_behavior=behavior) == 0
    row = conn.execute("SELECT * FROM transactions").fetchone()
    assert row["account"] == UNCLASSIFIED
    assert row["is_manual_override"] == 0


def test_reclassify_updates_non_overridden_rows(conn):
    """override 가 아닌 행은 재분류로 갱신된다."""
    df = pd.DataFrame(
        {"일자": ["2026-02-01"], "거래처": ["대한제강(주)"], "적요": ["강재 납품"], "금액": [1_000_000]}
    )
    out = classify_dataframe(ingest.normalize(df, "매입.xlsx", "매입"))
    ingest.load_transactions(conn, out)

    # 일부러 틀린 값으로 오염시킨 뒤 재분류가 고치는지 본다
    conn.execute("UPDATE transactions SET account = '엉뚱한계정', cost_behavior = '고정'")
    conn.commit()

    assert reclassify_all(conn) == 1
    row = conn.execute("SELECT * FROM transactions").fetchone()
    assert row["account"] == "원재료비"
    assert row["cost_behavior"] == VARIABLE


# ===============================================================
# 적재 + 통계
# ===============================================================


def test_load_transactions_creates_projects(conn):
    df = pd.DataFrame(
        {
            "일자": ["2026-01-10", "2026-01-11"],
            "거래처": ["대한제강(주)", "대한제강(주)"],
            "적요": ["강재 납품", "강재 납품"],
            "금액": [1_000_000, 2_000_000],
            "현장명": ["부산항 신항 부잔교 설치공사", ""],
        }
    )
    out = classify_dataframe(ingest.normalize(df, "매입.xlsx", "매입"))
    ingest.load_transactions(conn, out)

    rows = conn.execute(
        "SELECT project_id, amount FROM transactions ORDER BY amount"
    ).fetchall()
    assert rows[0]["project_id"] is not None   # 현장명 있는 행
    assert rows[1]["project_id"] is None       # 공통비
    assert conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 1


def test_amount_incl_vat_is_stored(conn):
    out = classify_dataframe(ingest.normalize(_purchase_style_df(), "매입.xlsx", "매입"))
    ingest.load_transactions(conn, out)
    row = conn.execute(
        "SELECT amount, amount_incl_vat FROM transactions ORDER BY amount DESC"
    ).fetchone()
    assert row["amount"] == 12_000_000
    assert row["amount_incl_vat"] == 13_200_000


def test_classification_stats_on_empty_db(conn):
    stats = classification_stats(conn)
    assert stats["total"] == 0
    assert stats["unclassified_pct"] == 0.0


def test_classification_stats_counts_unclassified(conn):
    df = pd.DataFrame(
        {
            "일자": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
            "거래처": ["대한제강(주)", "㈜대한종합", "㈜대한종합", "㈜대한종합"],
            "적요": ["강재 납품", "차액 정산", "기타 정산분", "정산 차액"],
            "금액": [1_000_000, 100_000, 200_000, 300_000],
        }
    )
    out = classify_dataframe(ingest.normalize(df, "x.xlsx", "매입"))
    ingest.load_transactions(conn, out)

    stats = classification_stats(conn)
    assert stats["total"] == 4
    assert stats["unclassified"] == 3
    assert stats["unclassified_pct"] == 75.0
    assert stats["unclassified_amount"] == 600_000


def test_guess_tx_type_from_filename():
    assert ingest.guess_tx_type("매입_세금계산서_2026.xlsx") == "매입"
    assert ingest.guess_tx_type("경비지출대장_2026.xlsx") == "경비"
    assert ingest.guess_tx_type("매출_세금계산서_2026.xlsx") == "매출"
    # 판단 못 하면 추측하지 않고 None
    assert ingest.guess_tx_type("2026년_자료.xlsx") is None
