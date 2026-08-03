"""ERP 양식 전처리 테스트 — 부가세 분리와 계정 확정.

여기가 틀리면 전 프로젝트의 손익이 10% 틀어진다. CLAUDE.md 규칙 1에 따라
(a) 정상, (b) 0/빈 데이터 경계를 모두 고정한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import erp_forms
from src.rules import ACCOUNT_COL, COST_BEHAVIOR, SUPPLY_COL


# ---------------------------------------------------------------
# supply_amount / vat_amount
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "total,expected",
    [
        (1_100, 1_000),
        (11_000, 10_000),
        (5_500_000, 5_000_000),
        (1_100_000_000, 1_000_000_000),
    ],
)
def test_supply_amount_taxable_exact(total, expected):
    """실데이터는 전부 총액 = 공급가액 x 1.1 이라 정확히 복원돼야 한다."""
    assert erp_forms.supply_amount(total, taxable=True) == expected


def test_supply_amount_exempt_is_unchanged():
    """면세(계산서)는 부가세가 없으므로 총액이 곧 공급가액이다."""
    assert erp_forms.supply_amount(1_100, taxable=False) == 1_100


@pytest.mark.parametrize("total", [0, None])
def test_supply_amount_zero_and_none(total):
    assert erp_forms.supply_amount(total, taxable=True) == 0
    assert erp_forms.vat_amount(total, taxable=True) == 0


def test_supply_amount_negative_credit_note():
    """수정세금계산서(마이너스)도 같은 규칙으로 나뉜다."""
    assert erp_forms.supply_amount(-1_100, taxable=True) == -1_000
    assert erp_forms.vat_amount(-1_100, taxable=True) == -100


@pytest.mark.parametrize("total", [1, 7, 999, 1_234_567, 55_555, -3_333])
def test_supply_plus_vat_always_equals_total(total):
    """공급가액 + 부가세 = 총액. 반올림해도 1원도 새지 않아야 한다."""
    for taxable in (True, False):
        s = erp_forms.supply_amount(total, taxable)
        v = erp_forms.vat_amount(total, taxable)
        assert s + v == total, f"{total} / taxable={taxable}"


def test_supply_amount_rounding_is_half_up():
    """505 = 555/1.1 = 504.545... → ROUND_HALF_UP 로 505."""
    assert erp_forms.supply_amount(555, taxable=True) == 505
    assert erp_forms.vat_amount(555, taxable=True) == 50


def test_supply_amount_returns_int():
    got = erp_forms.supply_amount(1_100, taxable=True)
    assert isinstance(got, int) and not isinstance(got, bool)


# ---------------------------------------------------------------
# is_taxable
# ---------------------------------------------------------------


def test_is_taxable_known_evidence():
    assert erp_forms.is_taxable("세계") is True     # 세금계산서 = 과세
    assert erp_forms.is_taxable("계") is False      # 계산서 = 면세
    assert erp_forms.is_taxable(" 세계 ") is True   # 공백 허용


@pytest.mark.parametrize("value", [None, "", "  ", "알수없는증빙", np.nan, pd.NA])
def test_unknown_evidence_is_not_taxable(value):
    """모를 때 나누지 않는다 — 잘못 나누면 원가가 작아져 이익이 과대표시된다."""
    assert erp_forms.is_taxable(value) is False


# ---------------------------------------------------------------
# clean_account
# ---------------------------------------------------------------


def test_clean_account_keeps_real_account():
    assert erp_forms.clean_account("자재비") == "자재비"
    assert erp_forms.clean_account("  운반비 ") == "운반비"


@pytest.mark.parametrize("label", ["매입", "기타비용", "기타"])
def test_non_account_labels_are_blanked(label):
    """'매입'을 계정으로 존중하면 원가행태가 없어 금액이 조용히 사라진다."""
    assert erp_forms.clean_account(label) is None


@pytest.mark.parametrize("value", [None, "", "   ", np.nan, pd.NA, "nan", "<NA>"])
def test_clean_account_blank_values(value):
    assert erp_forms.clean_account(value) is None


def test_every_kept_account_has_cost_behavior():
    """실데이터의 '용도' 값이 전부 원가행태 매핑에 등록돼 있어야 한다."""
    실제_용도값 = [
        "경상연구개발비", "복리후생비", "소모품비", "외주/용역 인건비",
        "외주가공비", "외주비", "운반비", "임차료", "자재비", "장비임차료",
        "전기료", "지급수수료", "주유비",
    ]
    for 용도 in 실제_용도값:
        account = erp_forms.clean_account(용도)
        assert account in COST_BEHAVIOR, f"{account} 가 COST_BEHAVIOR 에 없다 → 금액 소실"


# ---------------------------------------------------------------
# payroll_account
# ---------------------------------------------------------------


def test_payroll_account_mapping():
    assert erp_forms.payroll_account("급여") == "급여"
    assert erp_forms.payroll_account("일용직") == "일용직급여"


@pytest.mark.parametrize("value", [None, "", "상여금", np.nan, pd.NA])
def test_unknown_payroll_division_is_none(value):
    """모르는 구분은 미분류로 보낸다. 급여로 밀어붙이지 않는다."""
    assert erp_forms.payroll_account(value) is None


def test_payroll_accounts_are_fixed_cost():
    """급여·일용직급여는 둘 다 고정비 풀로 간다 (2026-08-03 확정)."""
    for account in ("급여", "일용직급여"):
        assert COST_BEHAVIOR[account] == "고정"


# ---------------------------------------------------------------
# prepare — 양식별 전처리
# ---------------------------------------------------------------


def test_prepare_tax_invoice_adds_supply_and_account():
    df = pd.DataFrame({
        "작성일자": ["2026-01-15", "2026-01-16"],
        "합계금액": [1_100, 2_200],
        "증빙": ["세계", "계"],
        "용도": ["자재비", "매입"],
    })
    spec = {"form": "tax_invoice", "total_col": "합계금액", "evidence_col": "증빙"}
    got = erp_forms.prepare(df, spec)

    assert list(got[SUPPLY_COL]) == [1_000, 2_200]      # 과세는 나누고 면세는 그대로
    assert list(got[ACCOUNT_COL]) == ["자재비", None]   # '매입'은 비운다
    assert list(got["합계금액"]) == [1_100, 2_200]      # 원본은 보존


def test_prepare_simple_receipt_does_not_split_vat():
    """간이영수증은 매입세액공제 대상이 아니라 지급총액 전체가 원가다."""
    df = pd.DataFrame({"사용일시": ["2026-02-01"], "사용금액": [5_500], "용도": [None]})
    got = erp_forms.prepare(df, {"form": "simple_receipt", "total_col": "사용금액"})

    assert SUPPLY_COL not in got.columns    # 공급가액 컬럼을 만들지 않는다
    assert list(got["사용금액"]) == [5_500]
    assert list(got[ACCOUNT_COL]) == [None]


def test_prepare_withholding_forces_account():
    df = pd.DataFrame({"과세월일": ["2026-01-31"], "총납부세액": [52_010],
                       "용도": ["세금(공과금)"]})
    got = erp_forms.prepare(df, {"form": "withholding", "total_col": "총납부세액"})

    assert list(got[ACCOUNT_COL]) == [erp_forms.WITHHOLDING_ACCOUNT]
    # 손익에 안 들어가야 한다
    assert COST_BEHAVIOR[erp_forms.WITHHOLDING_ACCOUNT] == "해당없음"


def test_prepare_payroll_maps_division():
    df = pd.DataFrame({"귀속년월": ["2026-01", "2026-03"],
                       "구분": ["급여", "일용직"], "지급액": [10_000_000, 3_000_000]})
    got = erp_forms.prepare(df, {"form": "payroll", "total_col": "지급액"})
    assert list(got[ACCOUNT_COL]) == ["급여", "일용직급여"]


def test_prepare_on_empty_dataframe():
    """경계: 빈 파일이 와도 죽지 않고 컬럼만 붙여 돌려준다."""
    df = pd.DataFrame({"합계금액": [], "증빙": [], "용도": []})
    got = erp_forms.prepare(
        df, {"form": "tax_invoice", "total_col": "합계금액", "evidence_col": "증빙"}
    )
    assert len(got) == 0
    assert SUPPLY_COL in got.columns and ACCOUNT_COL in got.columns


def test_prepare_missing_용도_column():
    """'용도' 칸이 없는 양식도 있다. 계정은 비워 두고 규칙에 맡긴다."""
    df = pd.DataFrame({"합계금액": [1_100], "증빙": ["세계"]})
    got = erp_forms.prepare(
        df, {"form": "tax_invoice", "total_col": "합계금액", "evidence_col": "증빙"}
    )
    assert list(got[ACCOUNT_COL]) == [None]


def test_prepare_rejects_unknown_form():
    with pytest.raises(ValueError, match="알 수 없는 양식"):
        erp_forms.prepare(pd.DataFrame(), {"form": "존재하지않는양식"})


def test_prepare_without_evidence_column_treats_as_exempt():
    """증빙 컬럼이 없으면 과세로 단정하지 않는다(원가를 크게 잡는 쪽)."""
    df = pd.DataFrame({"합계금액": [1_100], "용도": ["자재비"]})
    got = erp_forms.prepare(
        df, {"form": "tax_invoice", "total_col": "합계금액", "evidence_col": None}
    )
    assert list(got[SUPPLY_COL]) == [1_100]
