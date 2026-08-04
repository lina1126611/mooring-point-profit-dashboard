"""미분류 거래를 사람이 판단할 수 있는 검토표로 뽑는다.

    python scripts/review_unclassified.py                    # 전 파일
    python scripts/review_unclassified.py --form 매입\\(세금
    python scripts/review_unclassified.py --top 20           # 요약만 상위 N종

산출물 (둘 다 data/local/ — 원본 상호가 들어 있어 외부 금지):
    미분류_거래처별.csv   거래처 단위 집계. **여기의 '계정' 칸을 채우면 된다.**
    미분류_전체행.csv     행 단위 상세. 거래처 하나로 못 정할 때 들여다본다.

채운 CSV 를 되먹이는 곳은 rules.py 가 아니라 data/local/거래처_계정.csv 다
(src/local_overrides.py). 이 저장소는 공개용이라 실제 거래처명을 커밋할 수
없기 때문이다 — CLAUDE.md 의 '실제 회계 데이터는 포함하지 않는다'.

화면에 찍히는 요약은 **식별자를 전부 가명으로 바꿔서** 보여준다. 원본은
CSV 에만 들어간다. 콘솔 출력은 캡처·붙여넣기·AI 세션으로 새어나가기 쉬운
경로다.

주의 — 가릴 대상은 거래처만이 아니다. 처음 만들 때 거래처만 가명으로 바꾸고
현장명을 그대로 찍었는데, 현장명에 공사명과 발주처 상호가 들어 있어서
거래처를 가린 의미가 없었다. 콘솔에 새 컬럼을 추가할 때마다 그 값이
식별자인지 먼저 따진다.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src import erp_forms, ingest, pseudonym  # noqa: E402
from src.classify import classify_dataframe  # noqa: E402
from src.rules import ERP_FORMS, UNCLASSIFIED  # noqa: E402

RAW_DIR = PROJECT_ROOT / "data" / "raw"
LOCAL_DIR = PROJECT_ROOT / "data" / "local"
MAPPING_PATH = LOCAL_DIR / "mappings.json"

# 양식별로 '거래처' 칸의 성격이 다르다. 세금계산서는 사업자(vendor), 간이영수증은
# 카드 가맹점(merchant) 이라 가명 접두어도 다르다.
VENDOR_KIND: dict[str, str] = {
    "매출(세금계산서)": "vendor",
    "매입(세금계산서)": "vendor",
    "매입(간이영수증)": "merchant",
}


def won(n) -> str:
    return f"{int(n or 0):,}"


def find_file(spec: dict) -> Path | None:
    pattern = re.compile(spec["match"])
    for path in sorted(RAW_DIR.glob("*.xls*")):
        if pattern.search(path.name):
            return path
    return None


# ===============================================================
# 미분류 추출 — 적재와 똑같은 경로를 태운다
# ===============================================================


def unclassified_rows(spec: dict, path: Path) -> pd.DataFrame:
    """load_real_data 와 동일한 전처리·분류를 거친 뒤 미분류만 남긴다.

    검토표를 원본에서 직접 뽑으면 안 된다. 실제로 미분류가 되는 집합은
    '용도가 계정이 아닌 행' 이 아니라 **거기서 키워드 규칙까지 빠져나간 행**
    이라서, 원본 기준으로 세면 대상이 부풀고 우선순위가 어긋난다.
    """
    raw = ingest.load_excel(path, header=spec.get("header", 0))
    prepared = erp_forms.prepare(raw, spec)
    normalized = ingest.normalize(
        prepared, path.name, spec["tx_type"], aliases=spec["aliases"]
    )
    classified = classify_dataframe(normalized)
    out = classified[classified["account"] == UNCLASSIFIED].copy()
    out["form_name"] = spec["name"]
    return out.reset_index(drop=True)


def mark_offsets(df: pd.DataFrame) -> pd.DataFrame:
    """수정세금계산서로 상계되는 쌍을 찾아 표시한다.

    같은 거래처·같은 적요·절대값이 같고 부호가 반대인 행은 발행 취소다.
    순액이 0 이라 계정을 정해도 손익이 안 바뀌므로, 검토 목록에서 빼 주면
    사람이 볼 건수가 줄어든다. **삭제하지는 않는다** — 같은 금액을 두 번
    청구하고 한 번만 취소한 경우가 섞여 있으면 자동 판단이 위험하다
    (CLAUDE.md 의 중복의심 처리와 같은 이유).
    """
    out = df.copy()
    out["상계"] = ""
    key = list(zip(
        out["vendor"].astype(str),
        out["description"].astype(str),
        out["amount"].abs(),
    ))
    for k in set(key):
        idx = [i for i, kk in enumerate(key) if kk == k]
        plus = [i for i in idx if out.iloc[i]["amount"] > 0]
        minus = [i for i in idx if out.iloc[i]["amount"] < 0]
        for i, j in zip(plus, minus):
            out.iat[i, out.columns.get_loc("상계")] = "상계쌍"
            out.iat[j, out.columns.get_loc("상계")] = "상계쌍"
    return out


# ===============================================================
# 집계
# ===============================================================


def by_vendor(df: pd.DataFrame) -> pd.DataFrame:
    """거래처별 집계. 판단 단위가 거래처라서 이쪽이 작업 목록이 된다."""
    live = df[df["상계"] == ""]
    if live.empty:
        return pd.DataFrame()

    def joined(s):
        return " / ".join(sorted({str(x) for x in s if str(x) not in ("nan", "None")}))

    g = (
        live.assign(_v=live["vendor"].fillna("(거래처없음)").astype(str))
        .groupby("_v")
        .agg(
            건수=("amount", "size"),
            공급가액=("amount", "sum"),
            적요=("description", joined),
            현장=("project", joined),
            파일=("form_name", lambda s: sorted(set(s))[0]),
        )
        .sort_values("공급가액", ascending=False)
    )
    total = g["공급가액"].sum()
    # 컬럼명에 '%' 를 넣지 않는다 — itertuples 에서 위치 이름(_5)으로 바뀌어
    # 컬럼 순서가 바뀌면 조용히 다른 값을 읽는다. CSV 헤더에서만 % 를 붙인다.
    g["누적비중"] = (g["공급가액"].cumsum() / total * 100).round(1) if total else 0.0
    return g.reset_index().rename(columns={"_v": "거래처"})


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig — 안 붙이면 엑셀에서 한글이 깨진다
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ===============================================================
def main() -> None:
    argv = sys.argv[1:]
    only = None
    if "--form" in argv:
        only = argv[argv.index("--form") + 1]
    top = 25
    if "--top" in argv:
        top = int(argv[argv.index("--top") + 1])

    if not RAW_DIR.exists() or not any(RAW_DIR.glob("*.xls*")):
        sys.exit(f"{RAW_DIR} 에 실데이터가 없습니다.")

    frames = []
    print("=== 미분류 추출 ===")
    for spec in ERP_FORMS:
        if only and only not in spec["name"]:
            continue
        path = find_file(spec)
        if path is None:
            continue
        got = unclassified_rows(spec, path)
        print(f"  {spec['name']:20} 미분류 {len(got):4d}건 {won(got['amount'].sum()):>16}원")
        if len(got):
            frames.append(got)

    if not frames:
        print("  미분류가 없습니다.")
        return

    df = mark_offsets(pd.concat(frames, ignore_index=True))
    offset_n = int((df["상계"] == "상계쌍").sum())
    live = df[df["상계"] == ""]

    print(f"\n  전체 미분류      {len(df):4d}건 {won(df['amount'].sum()):>16}원")
    print(f"  ├ 상계쌍(취소)   {offset_n:4d}건 — 순액 0, 판단 불필요")
    print(f"  └ 검토 대상      {len(live):4d}건 {won(live['amount'].sum()):>16}원")

    # -----------------------------------------------------------
    vendors = by_vendor(df)

    print("\n=== 거래처별 (가명 표기 — 원본 상호는 CSV 에만) ===")
    mappings = pseudonym.load_mapping(MAPPING_PATH)
    alias_of: dict[str, str] = {}
    for kind in ("vendor", "merchant"):
        alias_of.update(mappings.get(kind, {}))
    project_alias = mappings.get("project", {})
    if not alias_of:
        print("  (매핑표가 없어 가명을 못 붙입니다 — pseudonymize.py 를 먼저 돌리세요)")

    def hide_projects(joined: str) -> str:
        """현장명도 가명으로 바꾼다.

        거래처만 가리고 현장명을 그대로 찍었던 버그가 있었다. 현장명에는
        공사명과 발주처 상호가 들어 있어서 거래처를 가린 의미가 없어진다.
        매핑에 없으면 원본을 통과시키지 않고 <미등록> 으로 눕힌다.
        """
        parts = [p.strip() for p in joined.split(" / ") if p.strip()]
        return " / ".join(project_alias.get(p, "<미등록>") for p in parts) or "-"

    for r in vendors.head(top).itertuples(index=False):
        alias = alias_of.get(r.거래처, "<미등록>")
        print(f"  {alias:12} {r.건수:3d}건 {won(r.공급가액):>14}원  "
              f"누적{r.누적비중:5.1f}%  현장[{hide_projects(r.현장)[:34]}]")
    if len(vendors) > top:
        print(f"  ... 이하 {len(vendors) - top}종 생략 (--top 으로 조절)")

    print(f"\n  거래처 {len(vendors)}종")
    for cut in (50, 80, 90):
        n = int((vendors["누적비중"] <= cut).sum()) + 1
        print(f"    상위 {n:3d}종 = 금액의 약 {cut}%")

    # -----------------------------------------------------------
    v_out = LOCAL_DIR / "미분류_거래처별.csv"
    write_csv(v_out, [
        {
            "거래처": r.거래처, "건수": int(r.건수), "공급가액": int(r.공급가액),
            "누적비중%": r.누적비중, "파일": r.파일, "현장": r.현장, "적요": r.적요,
            "계정": "",        # ← 여기를 채운다
            "원가행태": "",    # 비워 두면 rules.COST_BEHAVIOR 가 정한다
            "근거": "",
        }
        for r in vendors.itertuples(index=False)
    ])

    r_out = LOCAL_DIR / "미분류_전체행.csv"
    cols = ["date", "vendor", "description", "project", "amount",
            "amount_incl_vat", "source_file", "상계"]
    write_csv(r_out, df.sort_values("amount", ascending=False)[cols]
              .assign(계정="", 근거="").to_dict("records"))

    print(f"\n=== 산출 ===")
    print(f"  {v_out.relative_to(PROJECT_ROOT)}   ({len(vendors)}행) ← '계정' 칸을 채우세요")
    print(f"  {r_out.relative_to(PROJECT_ROOT)}   ({len(df)}행)")
    print("\n  채운 뒤: data/local/거래처_계정.csv 로 저장 → load_real_data.py 재실행")
    print("  (공개 저장소라 실제 거래처명을 rules.py 에 넣지 않습니다)")


if __name__ == "__main__":
    main()
