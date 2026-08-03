"""데이터 통합 — 엑셀 원본을 읽어 정규화하고 SQLite에 적재한다.

    엑셀 → load_excel → normalize → classify → load_transactions

파일마다 다른 컬럼명은 rules.COLUMN_ALIASES 로 흡수한다.
모든 행에 source_file 을 남겨 원본 추적성을 보장한다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd

from src.rules import (
    COLUMN_ALIASES,
    MANDAY_ALIASES,
    PROJECT_ALIASES,
    TX_TYPE_HINTS,
)

# transactions 적재에 사용하는 표준 컬럼
TRANSACTION_COLUMNS = [
    "date",
    "project",
    "vendor",
    "description",
    "account",
    "tx_type",
    "amount",
    "amount_incl_vat",
    "cost_behavior",
    "source_file",
    "is_manual_override",
    "is_duplicate_suspect",
]

VALID_TX_TYPES = ("매입", "경비", "매출")


# ===============================================================
# 파싱 헬퍼
# ===============================================================

_DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%y-%m-%d")


def parse_date(value) -> str | None:
    """제각각인 날짜 표기를 'YYYY-MM-DD' 문자열로 통일한다.

    datetime / Timestamp / '2026-05-03' / '2026/05/03' / '2026.05.03' / 20260503 지원.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "strftime"):  # datetime.date / datetime.datetime
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none"):
        return None

    for fmt in _DATE_PATTERNS:
        try:
            return pd.to_datetime(text, format=fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue

    # 마지막 수단: pandas 추론에 맡긴다
    try:
        parsed = pd.to_datetime(text)
        return None if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


_AMOUNT_STRIP = re.compile(r"[₩,\s원]")


def parse_amount(value) -> int:
    """금액 표기를 정수(원)로 통일한다.

    1234000 / '1,234,000' / '₩1,234,000' / '(1,000)'(음수) / '' → 0
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return int(round(value))

    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "-"):
        return 0

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]

    text = _AMOUNT_STRIP.sub("", text)
    if not text:
        return 0
    try:
        amount = int(round(float(text)))
    except ValueError:
        return 0
    return -amount if negative else amount


def clean_text(value) -> str | None:
    """빈 문자열·NaN 을 None 으로 통일한다."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text if text and text.lower() not in ("nan", "none") else None


# ===============================================================
# 컬럼 매핑
# ===============================================================


def resolve_columns(
    df: pd.DataFrame,
    aliases: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """엑셀 컬럼명 → 표준 컬럼명 매핑을 만든다. {표준명: 실제 엑셀 컬럼명}

    별칭 테이블에 없으면 그 표준 컬럼은 매핑에서 빠진다(추측하지 않는다).
    """
    aliases = aliases or COLUMN_ALIASES
    normalized = {str(c).strip(): c for c in df.columns}

    mapping: dict[str, str] = {}
    for std, candidates in aliases.items():
        for cand in candidates:
            if cand in normalized:
                mapping[std] = normalized[cand]
                break
    return mapping


def guess_tx_type(filename: str) -> str | None:
    """파일명으로 거래유형을 추정한다. 확신 없으면 None (사용자가 고르게 한다)."""
    name = Path(filename).stem
    for pattern, tx_type in TX_TYPE_HINTS:
        if re.search(pattern, name):
            return tx_type
    return None


def load_excel(
    path: str | Path,
    sheet_name: str | int = 0,
    header: int = 0,
) -> pd.DataFrame:
    """엑셀 파일을 DataFrame으로 읽는다.

    header 는 컬럼명이 있는 줄(0-base). ERP export 는 0행에 '매입(세금계산서)'
    같은 제목만 있고 실제 컬럼명이 1행에 오는 경우가 있어서 열어 두었다.
    지정하지 않으면 기존 동작(0행이 헤더)과 같다.
    """
    df = pd.read_excel(path, sheet_name=sheet_name, header=header)
    df.columns = [str(c).strip() for c in df.columns]
    return df.dropna(how="all").reset_index(drop=True)


# ===============================================================
# 정규화
# ===============================================================


def mark_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """같은 (날짜 + 거래처 + 금액) 조합의 2번째 행부터 중복 의심으로 표시한다.

    삭제하지 않는다 — 같은 날 같은 거래처에 같은 금액을 두 번 지급하는
    정상 거래가 실제로 존재하기 때문이다. 판단은 사람이 한다.
    """
    out = df.copy()
    if out.empty:
        out["is_duplicate_suspect"] = pd.Series(dtype=int)
        return out
    key = ["date", "vendor", "amount"]
    out["is_duplicate_suspect"] = out.duplicated(subset=key, keep="first").astype(int)
    return out


def normalize(
    df: pd.DataFrame,
    source_file: str,
    tx_type: str,
    aliases: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """원본 엑셀을 표준 transactions 스키마로 정규화한다.

    account / cost_behavior 는 여기서 채우지 않는다 (classify 의 일).
    원본에 계정 컬럼이 있으면 그 값만 그대로 실어 보낸다.
    """
    if tx_type not in VALID_TX_TYPES:
        raise ValueError(f"tx_type 은 {VALID_TX_TYPES} 중 하나여야 합니다: {tx_type!r}")

    mapping = resolve_columns(df, aliases)

    missing = [c for c in ("date", "amount") if c not in mapping]
    if missing:
        raise ValueError(
            f"필수 컬럼을 찾지 못했습니다: {missing}. "
            f"엑셀 컬럼={list(df.columns)}. rules.COLUMN_ALIASES 에 별칭을 추가하세요."
        )

    def col(std: str):
        """표준 컬럼에 해당하는 원본 시리즈. 없으면 전부 None."""
        if std in mapping:
            return df[mapping[std]]
        return pd.Series([None] * len(df), index=df.index)

    out = pd.DataFrame(
        {
            "date": [parse_date(v) for v in col("date")],
            "project": [clean_text(v) for v in col("project")],
            "vendor": [clean_text(v) for v in col("vendor")],
            "description": [clean_text(v) for v in col("description")],
            "account": [clean_text(v) for v in col("account")],
            "tx_type": tx_type,
            "amount": [parse_amount(v) for v in col("amount")],
            "amount_incl_vat": [
                parse_amount(v) if clean_text(v) is not None else None
                for v in col("amount_incl_vat")
            ],
            "cost_behavior": None,
            "source_file": source_file,
            "is_manual_override": 0,
        }
    )

    # 날짜를 못 읽은 행은 손익 기간 귀속이 불가능하므로 버리고 알린다
    bad = out["date"].isna()
    if bad.any():
        out = out[~bad].reset_index(drop=True)

    return mark_duplicates(out)


# ===============================================================
# DB 적재
# ===============================================================


def _na_to_none(value):
    """pandas 가 object 컬럼의 None 을 NaN 으로 바꿔 놓는 것을 되돌린다.

    이 변환을 빠뜨리면 빈 현장명이 공통비(NULL)가 아니라 'nan' 이라는
    이름의 프로젝트로 들어간다. 실제로 그렇게 터졌던 자리다.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def get_or_create_project(conn: sqlite3.Connection, name: str | None) -> int | None:
    """프로젝트명으로 id를 찾고, 없으면 만든다. 이름이 비면 None(공통비)."""
    name = _na_to_none(name)
    if not name:
        return None
    row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
    return cur.lastrowid


def load_transactions(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """정규화·분류된 DataFrame을 transactions 에 적재하고 건수를 반환한다."""
    if df.empty:
        return 0

    project_ids: dict[str | None, int | None] = {}
    rows = []
    for r in df.to_dict("records"):
        pname = _na_to_none(r.get("project"))
        if pname not in project_ids:
            project_ids[pname] = get_or_create_project(conn, pname)

        incl_vat = _na_to_none(r.get("amount_incl_vat"))
        rows.append(
            (
                r["date"],
                project_ids[pname],
                _na_to_none(r.get("vendor")),
                _na_to_none(r.get("description")),
                _na_to_none(r.get("account")),
                r["tx_type"],
                int(r["amount"]),
                None if incl_vat is None else int(incl_vat),
                _na_to_none(r.get("cost_behavior")) or "해당없음",
                _na_to_none(r.get("source_file")),
                int(r.get("is_manual_override", 0)),
                int(r.get("is_duplicate_suspect", 0)),
            )
        )

    conn.executemany(
        "INSERT INTO transactions "
        "(date, project_id, vendor, description, account, tx_type, amount, "
        " amount_incl_vat, cost_behavior, source_file, is_manual_override, "
        " is_duplicate_suspect) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_projects(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """프로젝트 마스터 엑셀을 projects 에 반영한다(이름 기준 upsert)."""
    mapping = resolve_columns(df, PROJECT_ALIASES)
    if "name" not in mapping:
        raise ValueError(f"프로젝트명 컬럼을 찾지 못했습니다: {list(df.columns)}")

    n = 0
    for _, row in df.iterrows():
        name = clean_text(row[mapping["name"]])
        if not name:
            continue
        pid = get_or_create_project(conn, name)
        conn.execute(
            "UPDATE projects SET client = ?, start_date = ?, end_date = ?, "
            "contract_amount = ? WHERE id = ?",
            (
                clean_text(row[mapping["client"]]) if "client" in mapping else None,
                parse_date(row[mapping["start_date"]]) if "start_date" in mapping else None,
                parse_date(row[mapping["end_date"]]) if "end_date" in mapping else None,
                parse_amount(row[mapping["contract_amount"]]) if "contract_amount" in mapping else 0,
                pid,
            ),
        )
        n += 1
    conn.commit()
    return n


def load_mandays(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """맨데이 투입 내역을 mandays 에 적재한다."""
    mapping = resolve_columns(df, MANDAY_ALIASES)
    missing = [c for c in ("project", "days", "daily_rate") if c not in mapping]
    if missing:
        raise ValueError(
            f"맨데이 필수 컬럼을 찾지 못했습니다: {missing}. 엑셀 컬럼={list(df.columns)}"
        )

    rows = []
    for _, row in df.iterrows():
        pid = get_or_create_project(conn, clean_text(row[mapping["project"]]))
        if pid is None:
            continue
        rows.append(
            (
                pid,
                clean_text(row[mapping["role"]]) if "role" in mapping else None,
                int(parse_amount(row[mapping["headcount"]])) if "headcount" in mapping else 1,
                float(row[mapping["days"]]),
                int(parse_amount(row[mapping["daily_rate"]])),
            )
        )

    conn.executemany(
        "INSERT INTO mandays (project_id, role, headcount, days, daily_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


# ===============================================================
# 파이프라인
# ===============================================================


def ingest_file(
    conn: sqlite3.Connection,
    path: str | Path,
    tx_type: str | None = None,
    source_file: str | None = None,
) -> dict:
    """엑셀 1개를 읽어 정규화 → 분류 → 적재까지 수행하고 요약을 돌려준다."""
    from src.classify import classify_dataframe  # 순환 import 방지

    path = Path(path)
    source_file = source_file or path.name
    tx_type = tx_type or guess_tx_type(source_file)
    if tx_type is None:
        raise ValueError(
            f"파일명으로 거래유형을 판단할 수 없습니다: {source_file}. "
            "tx_type 을 직접 지정하세요."
        )

    raw = load_excel(path)
    normalized = normalize(raw, source_file, tx_type)
    classified = classify_dataframe(normalized)
    inserted = load_transactions(conn, classified)

    return {
        "source_file": source_file,
        "tx_type": tx_type,
        "read": len(raw),
        "inserted": inserted,
        "skipped_bad_date": len(raw) - len(normalized),
        "duplicate_suspects": int(classified["is_duplicate_suspect"].sum()),
    }
