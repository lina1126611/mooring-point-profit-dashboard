"""가명화 테스트.

금액 계산은 아니지만 여기가 새면 보안각서를 위반한다. 그래서 금액 계산과
같은 기준으로 (a) 정상, (b) 빈 데이터 경계 케이스를 고정한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import pseudonym


# ---------------------------------------------------------------
# build_mapping
# ---------------------------------------------------------------


def test_mapping_is_deterministic():
    """같은 입력이면 순서가 뒤바뀌어도 같은 가명이 나온다."""
    a = pseudonym.build_mapping(["나가", "가나", "다라"], "거래처")
    b = pseudonym.build_mapping(["다라", "가나", "나가"], "거래처")
    assert a == b
    # 정렬 순서대로 번호가 붙는다
    assert a == {"가나": "거래처_001", "나가": "거래처_002", "다라": "거래처_003"}


def test_existing_aliases_are_never_reassigned():
    """거래가 추가돼도 이미 검토한 가명의 뜻이 바뀌면 안 된다."""
    first = pseudonym.build_mapping(["을", "병"], "거래처")
    # 사전순으로 '갑' 이 맨 앞이지만, 기존 번호를 밀어내지 않고 뒤에 붙어야 한다
    second = pseudonym.build_mapping(["갑", "을", "병"], "거래처", existing=first)

    assert second["을"] == first["을"]
    assert second["병"] == first["병"]
    assert second["갑"] == "거래처_003"


def test_duplicates_and_whitespace_collapse():
    """같은 거래처가 공백 차이로 두 개의 가명을 받으면 안 된다."""
    mapping = pseudonym.build_mapping(["  한국해양  ", "한국해양", "한국해양"], "거래처")
    assert mapping == {"한국해양": "거래처_001"}


def test_empty_input():
    assert pseudonym.build_mapping([], "거래처") == {}


@pytest.mark.parametrize("blank", [None, "", "   ", float("nan"), np.nan, pd.NA])
def test_blank_values_are_not_pseudonymised(blank):
    """빈 값은 가명 대상이 아니다. 'nan' 이라는 거래처가 생기면 안 된다."""
    assert pseudonym.build_mapping([blank], "거래처") == {}


def test_nan_from_pandas_object_column():
    """pandas object 컬럼의 None 은 NaN 으로 바뀌어 온다 — 실제로 터졌던 자리."""
    series = pd.Series(["한국해양", None, "제주중공업"], dtype="object")
    mapping = pseudonym.build_mapping(series, "거래처")
    assert set(mapping) == {"한국해양", "제주중공업"}
    assert "nan" not in mapping


# ---------------------------------------------------------------
# apply_mapping
# ---------------------------------------------------------------


def test_apply_mapping_replaces_and_preserves_blanks():
    mapping = {"한국해양": "거래처_001"}
    got = pseudonym.apply_mapping(["한국해양", None, ""], mapping)
    assert got == ["거래처_001", None, None]


def test_unmapped_value_is_flagged_not_leaked():
    """미등록 값을 조용히 통과시키면 원본이 새는 것을 눈치채지 못한다."""
    got = pseudonym.apply_mapping(["미등록거래처"], {"한국해양": "거래처_001"})
    assert got == ["<미등록>"]
    assert "미등록거래처" not in got


# ---------------------------------------------------------------
# invert / 역치환
# ---------------------------------------------------------------


def test_invert_roundtrip():
    names = ["한국해양", "제주중공업", "부산철구"]
    mapping = pseudonym.build_mapping(names, "거래처")
    back = pseudonym.invert(mapping)

    aliased = pseudonym.apply_mapping(names, mapping)
    restored = [back[a] for a in aliased]
    assert restored == names


def test_invert_rejects_duplicate_alias():
    """가명이 겹치면 역치환이 불가능하므로 조용히 넘기지 않고 실패해야 한다."""
    with pytest.raises(ValueError, match="가명이 중복"):
        pseudonym.invert({"갑": "거래처_001", "을": "거래처_001"})


def test_invert_empty():
    assert pseudonym.invert({}) == {}


# ---------------------------------------------------------------
# coverage — 외부로 내보내도 되는지 판정
# ---------------------------------------------------------------


def test_coverage_all_mapped_is_safe():
    mapping = pseudonym.build_mapping(["갑", "을"], "거래처")
    report = pseudonym.coverage(["갑", "을", None], mapping)
    assert report["대상"] == 2
    assert report["미등록"] == 0
    assert report["안전"] is True


def test_coverage_detects_leak():
    report = pseudonym.coverage(["갑", "신규거래처"], {"갑": "거래처_001"})
    assert report["안전"] is False
    assert report["미등록값"] == ["신규거래처"]


def test_coverage_on_empty_input_is_safe():
    report = pseudonym.coverage([], {})
    assert report == {"대상": 0, "미등록": 0, "미등록값": [], "안전": True}


# ---------------------------------------------------------------
# 저장 / 불러오기
# ---------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    mappings = {
        "vendor": pseudonym.build_mapping(["한국해양", "제주중공업"], "거래처"),
        "project": pseudonym.build_mapping(["○○항 계류시설"], "현장"),
    }
    path = tmp_path / "nested" / "map.json"
    pseudonym.save_mapping(path, mappings)

    assert path.exists()
    assert pseudonym.load_mapping(path) == mappings


def test_load_missing_file_returns_empty():
    """첫 실행에는 매핑표가 없다. 그때 죽으면 안 된다."""
    assert pseudonym.load_mapping("존재하지_않는_경로.json") == {}
