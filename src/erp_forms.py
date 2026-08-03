"""ERP export 양식별 전처리 — 원본 엑셀을 표준 스키마에 넣기 전 단계.

여기서 하는 일은 두 가지다.

1) **부가세 분리.** 실데이터의 금액 칸('합계금액')은 부가세 포함 총액이다
   (수령분 200건이 100% 1.1로 나눠떨어져 확인됐다). 손익 계산은 공급가액
   기준이어야 하므로 나눠 준다. 면세(계산서)는 나누면 안 된다.

2) **계정과목 확정.** ERP 의 '용도' 칸이 계정 역할을 하는데, 계정이 아닌
   값('매입', '기타비용')이 섞여 있다. 그걸 계정으로 존중하면 원가행태
   매핑에 없어서 '해당없음' 이 되고, account 가 '미분류' 도 아니라서
   미분류 집계에서도 빠진다 — 금액이 조용히 사라진다. 그래서 비워서
   키워드 규칙이 판정하게 한다.

계산 로직을 UI·스크립트가 아니라 여기 두는 이유는 테스트가 가능해야
하기 때문이다. (CLAUDE.md 규칙 1)
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pandas as pd

from src.rules import (
    ACCOUNT_COL,
    NON_ACCOUNT_LABELS,
    SUPPLY_COL,
    TAXABLE_EVIDENCE,
)

# 부가세율 10% → 공급가액 = 총액 / 1.1
VAT_DIVISOR = Decimal("1.1")

# 원천세 납부액에 붙일 계정. 비용이 아니라 예수금 정산이다.
WITHHOLDING_ACCOUNT = "원천세예수금"

# 급여대장 '구분' → 계정과목
PAYROLL_ACCOUNTS: dict[str, str] = {"급여": "급여", "일용직": "일용직급여"}


# ===============================================================
# 부가세 — 순수 함수. 여기가 틀리면 전 손익이 틀어진다.
# ===============================================================


def supply_amount(total_incl_vat, taxable: bool) -> int:
    """부가세 포함 총액에서 공급가액을 뽑는다.

    과세거래: 총액 = 공급가액 × 1.1 이므로 1.1 로 나눈다.
    면세거래: 부가세가 없으므로 총액이 곧 공급가액이다.

    반올림은 ROUND_HALF_UP(원 단위). 실데이터는 전부 정확히 나눠떨어지지만
    규칙을 정해 두지 않으면 파일이 바뀔 때 결과가 흔들린다.
    """
    total = int(total_incl_vat or 0)
    if not taxable or total == 0:
        return total
    quotient = Decimal(total) / VAT_DIVISOR
    return int(quotient.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def vat_amount(total_incl_vat, taxable: bool) -> int:
    """부가세액 = 총액 - 공급가액.

    공급가액을 반올림했더라도 이렇게 빼면 **공급가액 + 부가세 = 총액** 이
    항상 정확히 성립한다. 각각 따로 반올림하면 1원이 새어 나간다.
    """
    total = int(total_incl_vat or 0)
    return total - supply_amount(total, taxable)


def is_taxable(evidence) -> bool:
    """증빙 구분으로 과세 여부를 판정한다. 모르면 과세로 보지 않는다.

    왜 모를 때 '면세'(나누지 않음)인가 — 잘못 나누면 원가가 10% 작아져
    이익이 과대표시된다. 이 시스템이 고치려는 문제가 바로 이익 과대표시라서,
    확신이 없을 때는 원가를 크게 잡는 쪽으로 둔다.
    """
    if evidence is None:
        return False
    try:
        if pd.isna(evidence):
            return False
    except (TypeError, ValueError):
        pass
    return TAXABLE_EVIDENCE.get(str(evidence).strip(), False)


# ===============================================================
# 계정과목
# ===============================================================


def clean_account(value) -> str | None:
    """'용도' 값을 계정과목으로 쓸 수 있으면 돌려주고, 아니면 None.

    None 을 돌려주면 classify_account 가 적요·거래처 키워드로 판정하고,
    그래도 안 걸리면 '미분류' 로 남는다 — 이것이 설계된 경로다.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat", "<na>"):
        return None
    return None if text in NON_ACCOUNT_LABELS else text


def payroll_account(구분) -> str | None:
    """급여대장 '구분' → 계정. 모르는 구분은 미분류로 보낸다."""
    if 구분 is None:
        return None
    try:
        if pd.isna(구분):
            return None
    except (TypeError, ValueError):
        pass
    return PAYROLL_ACCOUNTS.get(str(구분).strip())


# ===============================================================
# 양식별 전처리 — 표준 컬럼(공급가액 / 계정)을 붙여 돌려준다
# ===============================================================


def _obj_series(values, index) -> pd.Series:
    """None 을 NaN 으로 바꾸지 않고 그대로 담는다.

    `df[col] = [..., None]` 처럼 리스트를 대입하면 pandas 가 dtype 을 추론해
    None 을 NaN 으로 바꿔 놓는다. NaN 은 truthy 라서 '계정 없음' 판정이
    무너진다. dtype=object 로 명시해 None 을 보존한다. (CLAUDE.md 의 NaN 함정)
    """
    return pd.Series(list(values), index=index, dtype=object)


def _accounts_from_용도(df: pd.DataFrame) -> pd.Series:
    source = df["용도"] if "용도" in df.columns else pd.Series([None] * len(df), index=df.index)
    return _obj_series((clean_account(v) for v in source), df.index)


def _tax_invoice(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    out = df.copy()
    totals = pd.to_numeric(out[spec["total_col"]], errors="coerce").fillna(0)
    evidence_col = spec.get("evidence_col")
    taxable = (
        [is_taxable(v) for v in out[evidence_col]]
        if evidence_col and evidence_col in out.columns
        else [False] * len(out)
    )
    out[SUPPLY_COL] = [supply_amount(t, tx) for t, tx in zip(totals, taxable)]
    out[ACCOUNT_COL] = _accounts_from_용도(out)
    return out


def _simple_receipt(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """간이영수증 — 부가세를 분리하지 않는다(매입세액공제 대상 아님)."""
    out = df.copy()
    out[ACCOUNT_COL] = _accounts_from_용도(out)
    return out


def _withholding(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """원천세 납부액 — 계정을 예수금으로 못 박아 손익에서 빠지게 한다."""
    out = df.copy()
    out[ACCOUNT_COL] = WITHHOLDING_ACCOUNT
    return out


def _payroll(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    out = df.copy()
    source = out["구분"] if "구분" in out.columns else pd.Series([None] * len(out), index=out.index)
    out[ACCOUNT_COL] = _obj_series((payroll_account(v) for v in source), out.index)
    return out


_PREPARERS = {
    "tax_invoice": _tax_invoice,
    "simple_receipt": _simple_receipt,
    "withholding": _withholding,
    "payroll": _payroll,
}


def prepare(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """양식 정의(rules.ERP_FORMS 의 항목)에 따라 전처리한다."""
    kind = spec.get("form")
    if kind not in _PREPARERS:
        raise ValueError(
            f"알 수 없는 양식 종류: {kind!r} "
            f"(가능: {', '.join(sorted(_PREPARERS))})"
        )
    return _PREPARERS[kind](df, spec)
