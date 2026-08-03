"""가명화 — 민감한 식별자를 되돌릴 수 있는 가명으로 치환한다.

왜 LLM이 아니라 사전(dict)인가:
  가명화는 판단이 아니라 치환이다. LLM에 맡기면 같은 거래처가 실행마다
  다른 가명을 받고(재현 불가), 왜 그렇게 바뀌었는지 감사할 수 없고, 265종을
  훑는 데 몇 분이 걸린다. 사전 치환은 결정적이고 즉시이며 테스트 가능하다.
  LLM은 '자유 텍스트에서 고유명사를 찾아내는' 판단이 필요한 곳에만 쓴다.

무엇을 치환하고 무엇을 남기는가:
  치환 = 거래처명·사용처·프로젝트명·사용자 (회사·현장·개인을 특정하는 값)
  보존 = 금액·날짜 (손익 숫자의 정확성이 곧 이 시스템의 목적)
  보존 = 계정과목·용도 (자재비/외주비 같은 일반 회계용어. 바꾸면 분류가 죽는다)

주의 — 가명화 데이터로는 '분류'를 할 수 없다:
  '사용처'를 가맹점_017 로 바꾸면 "주유소니까 차량유지비" 라는 판정 근거가
  사라진다. 분류는 원본을 쥔 로컬에서 해야 한다(src/local_llm.py).
  가명화는 구조를 외부와 공유하거나, 의미가 필요 없는 작업
  (프로젝트명 표기 유사도 그룹핑)에 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd

# 결측을 나타내는 문자열 표현. pandas 는 결측을 dtype 에 따라 NaN / NaT / <NA> /
# None 으로 다르게 돌려주는데, 어느 하나라도 놓치면 그 표현 자체가 거래처명이
# 되어 가명을 받는다. (pd.NA 가 '<NA>' 라는 거래처로 들어가던 버그가 있었다)
_BLANKS = {"", "nan", "none", "nat", "<na>", "null"}

# 컬럼 성격별 가명 접두어. 사람이 가명만 보고도 무엇이었는지 알 수 있어야
# 검토가 가능하므로 의미 있는 접두어를 쓴다.
DEFAULT_PREFIXES: dict[str, str] = {
    "vendor": "거래처",
    "project": "현장",
    "merchant": "가맹점",
    "user": "사용자",
    "description": "적요",
}


def _clean(value) -> str | None:
    """가명화 대상 문자열만 남긴다. 결측·빈 문자열은 대상이 아니다.

    pandas 를 거친 값은 None 이 NaN / NaT / pd.NA 로 바뀌어 오는데 이들은
    truthy 라서, 이 처리를 빠뜨리면 'nan' 이나 '<NA>' 라는 거래처가 가명을
    받는다. 실제로 그렇게 터졌던 자리다.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # 배열·리스트가 들어오면 pd.isna 가 스칼라를 안 돌려준다. 값 판정은
        # 아래 문자열 변환에 맡긴다.
        pass

    if isinstance(value, float) and value.is_integer():
        # 엑셀이 숫자로 읽은 식별자(상호가 '7788' 같은 경우)를 '7788.0' 이
        # 아니라 '7788' 로 살린다. 식별자는 버리지 않고 반드시 가명화한다.
        text = str(int(value))
    else:
        text = str(value).strip()

    return text if text and text.lower() not in _BLANKS else None


def build_mapping(
    values: Iterable,
    prefix: str,
    existing: dict[str, str] | None = None,
) -> dict[str, str]:
    """{원본값: 가명} 매핑을 만든다.

    - 같은 입력 집합에는 항상 같은 결과가 나온다(정렬 후 번호 부여).
      재현 가능해야 어제 만든 가명 데이터와 오늘 만든 것을 비교할 수 있다.
    - existing 을 주면 기존 가명은 절대 바꾸지 않고 새 값만 뒤에 붙인다.
      거래가 추가돼도 이미 검토한 가명의 뜻이 달라지면 안 되기 때문이다.
    """
    mapping = dict(existing or {})
    known = set(mapping)
    fresh = sorted({c for c in (_clean(v) for v in values) if c} - known)

    next_no = len(mapping) + 1
    for offset, original in enumerate(fresh):
        mapping[original] = f"{prefix}_{next_no + offset:03d}"
    return mapping


def apply_mapping(values: Iterable, mapping: dict[str, str]) -> list[str | None]:
    """값 목록을 가명으로 바꾼다. 매핑에 없는 값은 그대로 두지 않고 표시한다.

    조용히 원본을 통과시키면 가명화가 새는 것을 눈치채지 못한다.
    그래서 미등록 값은 '<미등록>' 으로 눕혀 즉시 눈에 띄게 한다.
    """
    out: list[str | None] = []
    for value in values:
        cleaned = _clean(value)
        if cleaned is None:
            out.append(None)
        else:
            out.append(mapping.get(cleaned, "<미등록>"))
    return out


def invert(mapping: dict[str, str]) -> dict[str, str]:
    """{가명: 원본}. 클라우드에서 받은 결과를 실데이터에 되돌릴 때 쓴다."""
    flipped: dict[str, str] = {}
    for original, alias in mapping.items():
        if alias in flipped:
            raise ValueError(
                f"가명이 중복됐습니다: {alias!r} 가 "
                f"{flipped[alias]!r} 와 {original!r} 에 모두 붙어 있습니다. "
                "역치환이 불가능하므로 매핑을 다시 만들어야 합니다."
            )
        flipped[alias] = original
    return flipped


def save_mapping(path: str | Path, mappings: dict[str, dict[str, str]]) -> None:
    """매핑표를 JSON 으로 저장한다. **이 파일이 유출되면 가명화가 무의미하다.**

    저장 위치는 .gitignore 로 제외돼 있어야 한다.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(mappings, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_mapping(path: str | Path) -> dict[str, dict[str, str]]:
    """저장된 매핑표를 읽는다. 없으면 빈 dict (첫 실행)."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def coverage(values: Iterable, mapping: dict[str, str]) -> dict:
    """가명화가 새고 있지 않은지 점검한다.

    치환 대상인데 매핑에 없는 값이 하나라도 있으면 그 파일은 외부로
    내보낼 수 없다. 적재 품질 점검과 같은 역할이다.
    """
    total = 0
    unmapped: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        if cleaned is None:
            continue
        total += 1
        if cleaned not in mapping:
            unmapped.add(cleaned)
    return {
        "대상": total,
        "미등록": len(unmapped),
        "미등록값": sorted(unmapped),
        "안전": not unmapped,
    }
